"""
Pruebas de scripts/destroy_infra.py::_is_orphan -la regla pura que decide si un
recurso AWS con tag sooniverse:managed=true debe reportarse como huérfano.
No requiere AWS ni PostgreSQL: opera sobre el dict {aws_id: (status, state)} que
_region_known_aws_ids() construiría.
"""

import sys
from pathlib import Path

import boto3
from moto import mock_aws

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import destroy_infra  # noqa: E402
from destroy_infra import _is_orphan, scan_orphans  # noqa: E402


def test_untracked_resource_is_orphan():
    is_orphan, status = _is_orphan({}, "eipalloc-desconocido")
    assert is_orphan is True
    assert status is None


def test_resource_of_active_deployment_is_not_orphan():
    known = {"vpc-123": ("active", "active")}
    is_orphan, status = _is_orphan(known, "vpc-123")
    assert is_orphan is False
    assert status == "active"


def test_resource_of_destroyed_deployment_is_orphan():
    known = {"nat-123": ("destroyed", "active")}
    is_orphan, status = _is_orphan(known, "nat-123")
    assert is_orphan is True
    assert status == "destroyed"


def test_resource_of_error_deployment_is_orphan():
    known = {"sg-123": ("error", "active")}
    is_orphan, status = _is_orphan(known, "sg-123")
    assert is_orphan is True
    assert status == "error"


def test_adopted_resource_is_never_orphan_even_if_deployment_destroyed():
    """La Elastic IP del Gateway con 'gateway.dominio.eip_persistente: true' queda
    con state='adopted' tras un destroy -sigue viva en AWS a propósito, no porque
    el destroy se la haya saltado."""
    known = {"eipalloc-gw": ("destroyed", "adopted")}
    is_orphan, status = _is_orphan(known, "eipalloc-gw")
    assert is_orphan is False
    assert status == "destroyed"


def test_adopted_resource_of_active_deployment_is_not_orphan():
    known = {"eipalloc-gw": ("active", "adopted")}
    is_orphan, status = _is_orphan(known, "eipalloc-gw")
    assert is_orphan is False


def test_scan_orphans_ignores_nat_gateway_already_deleted(monkeypatch):
    """Bug real confirmado en una destrucción real: 'describe_nat_gateways'
    sigue devolviendo un NAT ya destruido con state='deleted' durante un buen
    rato -a diferencia de una VPC/subred ya borrada, que simplemente deja de
    aparecer-. Sin filtrar por estado, un NAT que AwsNetworkManager.destroy()
    SÍ eliminó correctamente se reportaba como huérfano para siempre."""
    region = "us-east-1"
    monkeypatch.setattr(destroy_infra, "_region_known_aws_ids", lambda r: {})

    with mock_aws():
        ec2 = boto3.client("ec2", region_name=region)
        vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]
        subnet = ec2.create_subnet(VpcId=vpc["VpcId"], CidrBlock="10.0.0.0/24")["Subnet"]
        eip = ec2.allocate_address(Domain="vpc")
        nat = ec2.create_nat_gateway(SubnetId=subnet["SubnetId"], AllocationId=eip["AllocationId"])["NatGateway"]
        ec2.create_tags(Resources=[nat["NatGatewayId"]], Tags=[{"Key": "sooniverse:managed", "Value": "true"}])

        ec2.delete_nat_gateway(NatGatewayId=nat["NatGatewayId"])

        orphans = scan_orphans(region)

    orphan_ids = {o["aws_id"] for o in orphans}
    assert nat["NatGatewayId"] not in orphan_ids


# -- purge_orphans_azure / --purge-orphans para Azure (antes no implementado) -
import types
from unittest.mock import MagicMock


def test_azure_delete_order_leaf_resources_first():
    from destroy_infra import _azure_delete_order

    vnet = {"type": "Microsoft.Network/virtualNetworks"}
    nsg = {"type": "Microsoft.Network/networkSecurityGroups"}
    natgw = {"type": "Microsoft.Network/natGateways"}
    assert _azure_delete_order(natgw) < _azure_delete_order(nsg) < _azure_delete_order(vnet)


def test_azure_delete_order_unknown_type_goes_last():
    from destroy_infra import _azure_delete_order

    assert _azure_delete_order({"type": "Microsoft.Weird/thing"}) == 999


def test_purge_orphans_azure_borra_por_resource_id_con_api_version_resuelto(monkeypatch):
    from destroy_infra import purge_orphans_azure

    fake_resource_client = MagicMock()
    fake_provider = MagicMock()
    fake_resource_type = MagicMock(resource_type="virtualNetworks", api_versions=["2024-01-01", "2023-01-01"])
    fake_provider.resource_types = [fake_resource_type]
    fake_resource_client.providers.get.return_value = fake_provider

    fake_credential_module = types.SimpleNamespace(
        _default_credential=lambda *a, **k: (MagicMock(), "sub-fake")
    )
    monkeypatch.setitem(sys.modules, "azure_network", fake_credential_module)
    monkeypatch.setattr(
        "azure.mgmt.resource.resources.ResourceManagementClient",
        lambda *a, **k: fake_resource_client,
    )

    orphans = [
        {"azure_id": "/subscriptions/x/resourceGroups/rg/providers/Microsoft.Network/virtualNetworks/vnet-1",
         "type": "Microsoft.Network/virtualNetworks"},
    ]
    purge_orphans_azure(orphans)

    fake_resource_client.resources.begin_delete_by_id.assert_called_once_with(
        orphans[0]["azure_id"], "2024-01-01"
    )


def test_purge_orphans_azure_reporta_error_sin_bloquear_el_resto(monkeypatch):
    from destroy_infra import purge_orphans_azure

    fake_resource_client = MagicMock()
    fake_resource_client.providers.get.side_effect = Exception("provider no encontrado")

    fake_credential_module = types.SimpleNamespace(
        _default_credential=lambda *a, **k: (MagicMock(), "sub-fake")
    )
    monkeypatch.setitem(sys.modules, "azure_network", fake_credential_module)
    monkeypatch.setattr(
        "azure.mgmt.resource.resources.ResourceManagementClient",
        lambda *a, **k: fake_resource_client,
    )

    orphans = [
        {"azure_id": "/subscriptions/x/.../publicIPAddresses/pip-huerfana",
         "type": "Microsoft.Network/publicIPAddresses"},
    ]
    # No debe lanzar -el error se reporta por recurso (ver el print interno).
    purge_orphans_azure(orphans)
    fake_resource_client.resources.begin_delete_by_id.assert_not_called()
