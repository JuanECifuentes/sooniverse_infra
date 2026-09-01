"""
Pruebas unitarias de scripts/azure_network.py.

A diferencia de test_aws_network.py (que usa moto para simular AWS de verdad),
no existe un equivalente a moto para Azure -así que aquí se reemplazan
`resource_client`/`network_client` por `unittest.mock.MagicMock()` después de
construir `AzureNetworkManager` (su constructor no hace ninguna llamada de red
por sí mismo). Cubre: cálculo de subredes, tags/prefijo reservado, mecanismo de
propiedad (doble condición: estado + tags reales), orden de destroy y
`nat_mode` inválido para Azure ('per-az' no existe).
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from azure_network import (  # noqa: E402
    TAG_DEPLOYMENT,
    TAG_MANAGED,
    AzureNetworkError,
    AzureNetworkManager,
    AzureNetworkSpec,
    compute_subnet_cidrs,
)
from infra_state import InMemoryInfraStateStore  # noqa: E402


def make_spec(**overrides):
    defaults = dict(
        client_id="acme",
        environment="prod",
        region="eastus",
        vnet_cidr="10.0.0.0/16",
        az_count=1,
        nat_mode="single",
        admin_cidrs=["1.2.3.4/32"],
        public_cidrs=["0.0.0.0/0"],
        gateway_public_ports=[4000, 8000, 8080],
        worker_ports=[8007],
        expose_direct_ports=False,
    )
    defaults.update(overrides)
    return AzureNetworkSpec(**defaults)


@pytest.fixture
def manager():
    state = InMemoryInfraStateStore()
    spec = make_spec()
    mgr = AzureNetworkManager(spec, state=state, credential=MagicMock(), subscription_id="sub-fake")
    # Sustituye los clientes reales del SDK por mocks: el constructor de
    # ResourceManagementClient/NetworkManagementClient no llama a la red, pero
    # cualquier método sí lo haría si no se reemplaza aquí.
    mgr.resource_client = MagicMock()
    mgr.network_client = MagicMock()
    return mgr


def test_compute_subnet_cidrs_deterministic_and_non_overlapping():
    public, private = compute_subnet_cidrs("10.0.0.0/16")
    public2, private2 = compute_subnet_cidrs("10.0.0.0/16")
    assert public == public2 and private == private2

    import ipaddress

    assert not ipaddress.ip_network(public).overlaps(ipaddress.ip_network(private))


def test_nat_mode_per_az_rejected():
    """'per-az' no tiene mapeo a NAT Gateway de Azure (no es zonal como en AWS)."""
    with pytest.raises(AzureNetworkError):
        make_spec(nat_mode="per-az")


def test_client_id_normalization_enforced():
    with pytest.raises(AzureNetworkError):
        make_spec(client_id="ACME")


def test_tags_include_required_keys(manager):
    tags = manager._tags("vnet")
    assert tags[TAG_MANAGED] == "true"
    assert tags[TAG_DEPLOYMENT] == manager.deployment_id
    assert tags["sooniverse:component"] == "vnet"


def test_tags_reject_reserved_prefix():
    state = InMemoryInfraStateStore()
    spec = make_spec(extra_tags={"sooniverse:custom": "x"})
    mgr = AzureNetworkManager(spec, state=state, credential=MagicMock(), subscription_id="sub-fake")
    with pytest.raises(AzureNetworkError):
        mgr._tags("vnet")


def test_resource_group_name_convention(manager):
    assert manager.resource_group_name == "sooniverse-acme-prod-rg"


def test_ensure_resource_group_idempotent(manager):
    manager.ensure_resource_group()
    assert manager.resource_client.resource_groups.create_or_update.call_count == 1

    # Segunda llamada: ya está en el estado (mismo deployment_id), no debe
    # volver a llamar a la API de Azure.
    manager.ensure_resource_group()
    assert manager.resource_client.resource_groups.create_or_update.call_count == 1


def test_plan_destroy_orders_by_delete_order(manager):
    manager.state.record_resource(
        manager.deployment_id, resource_type="vnet", component="vnet", aws_id="vnet-1",
        delete_order=40, managed_by_us=True, state="active",
    )
    manager.state.record_resource(
        manager.deployment_id, resource_type="natgw", component="natgw", aws_id="nat-1",
        delete_order=20, managed_by_us=True, state="active",
    )
    manager.state.record_resource(
        manager.deployment_id, resource_type="resource-group", component="resource-group", aws_id="rg-1",
        delete_order=90, managed_by_us=True, state="active",
    )

    plan = manager.plan_destroy()
    assert [item.component for item in plan] == ["natgw", "vnet", "resource-group"]


def test_destroy_skips_not_managed_by_us(manager):
    manager.state.record_resource(
        manager.deployment_id, resource_type="vnet", component="vnet", aws_id="vnet-ajeno",
        delete_order=40, managed_by_us=False, state="active",
    )

    report = manager.destroy()

    assert len(report.skipped_not_ours) == 1
    manager.network_client.virtual_networks.begin_delete.assert_not_called()


def test_destroy_skips_when_tags_dont_match(manager):
    manager.state.record_resource(
        manager.deployment_id, resource_type="vnet", component="vnet", aws_id="vnet-1",
        delete_order=40, managed_by_us=True, state="active", attributes={"name": "otro-vnet"},
    )
    otro_deployment = "otro-deployment-id"
    manager.network_client.virtual_networks.get.return_value = MagicMock(
        tags={TAG_MANAGED: "true", TAG_DEPLOYMENT: otro_deployment}
    )

    report = manager.destroy()

    assert len(report.skipped_not_ours) == 1
    manager.network_client.virtual_networks.begin_delete.assert_not_called()


def test_destroy_treats_already_gone_resource_as_success(manager):
    from azure.core.exceptions import ResourceNotFoundError

    manager.state.record_resource(
        manager.deployment_id, resource_type="vnet", component="vnet", aws_id="vnet-1",
        delete_order=40, managed_by_us=True, state="active", attributes={"name": "vnet-1"},
    )
    manager.network_client.virtual_networks.get.side_effect = ResourceNotFoundError()

    report = manager.destroy()

    assert len(report.succeeded) == 1
    assert not report.failed
