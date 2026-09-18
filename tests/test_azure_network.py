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

from azure.core.exceptions import ResourceNotFoundError  # noqa: E402
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
    # _find_existing() cae a un GET en vivo cuando el estado no tiene el
    # recurso (ver azure_network.py::_lookup_live, fix de idempotencia con BD
    # perdida). Un MagicMock().get(...) "encuentra" cualquier cosa por
    # defecto -para que el fixture simule el caso realista ("todavía no
    # existe nada en Azure"), los .get() de los sub-clientes consultados por
    # _lookup_live lanzan ResourceNotFoundError salvo que un test los
    # sobreescriba explícitamente.
    not_found = ResourceNotFoundError("not found")
    mgr.resource_client.resource_groups.get.side_effect = not_found
    mgr.network_client.virtual_networks.get.side_effect = not_found
    mgr.network_client.network_security_groups.get.side_effect = not_found
    mgr.network_client.public_ip_addresses.get.side_effect = not_found
    mgr.network_client.nat_gateways.get.side_effect = not_found
    mgr.network_client.subnets.get.side_effect = not_found
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
    # El fixture pre-configura .get.side_effect = ResourceNotFoundError (para
    # que _find_existing() no "adopte" un recurso falso en los tests que no
    # lo necesitan, ver el fixture 'manager'); limpiarlo aquí para que
    # .return_value sea lo que realmente responde esta llamada.
    manager.network_client.virtual_networks.get.side_effect = None
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


# -- _find_existing / _lookup_live: recuperación tras perder el estado ------
# (fix del hueco A6 de la auditoría: antes _find_existing SOLO miraba
# Postgres, nunca Azure en vivo -a diferencia de aws_network.py::
# _find_by_component, que sí consulta AWS porque create_vpc() no es
# idempotente por nombre. En Azure el PUT de ARM SÍ lo es, pero sin este
# fallback una BD perdida dejaba filas de estado huérfanas y reintentos
# evitables de create_or_update en cada ensure_*.)
def test_find_existing_returns_none_sin_estado_ni_azure(manager):
    """Comportamiento por defecto del fixture: nada en el estado, nada en
    Azure (.get() lanza ResourceNotFoundError) -> None."""
    assert manager._find_existing("resource-group") is None
    assert manager._find_existing("vnet", rg_name="rg-x") is None


def test_find_existing_cae_a_azure_en_vivo_para_resource_group(manager):
    manager.resource_client.resource_groups.get.side_effect = None
    manager.resource_client.resource_groups.get.return_value = MagicMock()

    existing = manager._find_existing("resource-group")

    assert existing is not None
    # Convención del módulo: el 'aws_id' de resource-group es el nombre
    # desnudo, no el resourceId completo que devolvería el SDK -ver
    # ensure_resource_group()/_lookup_live().
    assert existing["aws_id"] == manager.resource_group_name


def test_find_existing_autoregistra_en_el_estado_tras_encontrarlo_en_azure(manager):
    """El hallazgo en vivo se adopta bajo el deployment_id ACTUAL, para que
    una segunda llamada ya lo encuentre en el estado sin volver a golpear la
    API de Azure."""
    manager.network_client.virtual_networks.get.side_effect = None
    manager.network_client.virtual_networks.get.return_value = MagicMock(id="vnet-real-id")

    manager._find_existing("vnet", rg_name="rg-x")
    assert manager.network_client.virtual_networks.get.call_count == 1

    registrados = manager.state.list_resources(manager.deployment_id)
    assert any(r["component"] == "vnet" and r["aws_id"] == "vnet-real-id" for r in registrados)

    # Segunda llamada: ya está en el estado, no debe volver a consultar Azure.
    manager._find_existing("vnet", rg_name="rg-x")
    assert manager.network_client.virtual_networks.get.call_count == 1


def test_ensure_resource_group_no_recrea_si_ya_existe_en_azure_sin_estado(manager):
    """El escenario real de A6: la BD se perdió/reinició pero el Resource
    Group ya existía en Azure de un despliegue anterior -no debe intentar
    'crearlo' de nuevo (aunque create_or_update sería idempotente igual, el
    punto es no depender de eso y no dejar una fila de estado huérfana)."""
    manager.resource_client.resource_groups.get.side_effect = None
    manager.resource_client.resource_groups.get.return_value = MagicMock()

    manager.ensure_resource_group()

    manager.resource_client.resource_groups.create_or_update.assert_not_called()


def test_lookup_live_subred_requiere_vnet_name(manager):
    """Sin vnet_name no hay forma de hacer el GET (subnets.get necesita
    resource_group + vnet + nombre de subred) -debe devolver None sin
    intentar la llamada, no lanzar una excepción."""
    manager.network_client.subnets.get.side_effect = None
    manager.network_client.subnets.get.return_value = MagicMock(id="subnet-real")

    existing = manager._find_existing("subnet-public", rg_name="rg-x")

    assert existing is None
    manager.network_client.subnets.get.assert_not_called()


# -- _sync_nsg_rules: prioridad NSG única incluso con muchos CIDR -----------
# (fix del hueco A5: la fórmula 100 + idx*10 + cidr_idx colisionaba en cuanto
# una regla traía 10 o más CIDR.)
def test_sync_nsg_rules_prioridad_unica_con_mas_de_diez_cidrs(manager):
    manager.network_client.security_rules.list.return_value = []

    muchos_cidrs = [f"10.0.{i}.0/24" for i in range(15)]
    rules = [{"port": 22, "cidrs": muchos_cidrs}]
    manager._sync_nsg_rules("rg-x", "nsg-x", rules)

    prioridades = [
        call.args[3]["priority"]
        for call in manager.network_client.security_rules.begin_create_or_update.call_args_list
    ]
    assert len(prioridades) == 15
    assert len(set(prioridades)) == 15, "hay prioridades NSG duplicadas"


def test_sync_nsg_rules_prioridad_estable_entre_corridas_identicas(manager):
    """Idempotencia: la MISMA lista de reglas debe producir las MISMAS
    prioridades en corridas repetidas (para que el diff por nombre de regla
    siga siendo válido)."""
    manager.network_client.security_rules.list.return_value = []
    rules = [{"port": 22, "cidrs": ["1.2.3.4/32"]}, {"port": 80, "cidrs": ["0.0.0.0/0"]}]

    manager._sync_nsg_rules("rg-x", "nsg-x", rules)
    primera = {
        call.args[2]: call.args[3]["priority"]
        for call in manager.network_client.security_rules.begin_create_or_update.call_args_list
    }

    manager.network_client.security_rules.begin_create_or_update.reset_mock()
    manager._sync_nsg_rules("rg-x", "nsg-x", rules)
    segunda = {
        call.args[2]: call.args[3]["priority"]
        for call in manager.network_client.security_rules.begin_create_or_update.call_args_list
    }

    assert primera == segunda


# -- ensure_security_groups: honra los CIDR explícitos (fix A4) -------------
def test_ensure_security_groups_usa_cidrs_explicitos_de_subredes(manager):
    """CORREGIDO: antes ensure_security_groups() SIEMPRE recalculaba con
    compute_subnet_cidrs(vnet_cidr), ignorando los CIDR explícitos de
    'red_y_aislamiento.subredes.publicas/privadas' -si el operador los
    fijaba a mano, las reglas NSG de los workers permitían el CIDR
    calculado, no el real, bloqueando el tráfico gateway -> worker."""
    manager.spec = AzureNetworkSpec(
        client_id=manager.spec.client_id,
        environment=manager.spec.environment,
        region=manager.spec.region,
        vnet_cidr="10.0.0.0/16",
        az_count=1,
        nat_mode="single",
        admin_cidrs=["1.2.3.4/32"],
        public_cidrs=["0.0.0.0/0"],
        gateway_public_ports=[4000],
        worker_ports=[8007],
        expose_direct_ports=False,
        # CIDR explícitos, deliberadamente DISTINTOS de lo que
        # compute_subnet_cidrs("10.0.0.0/16") calcularía por defecto.
        public_subnet_cidrs=["10.0.99.0/24"],
        private_subnet_cidrs=["10.0.199.0/24"],
    )
    manager.network_client.security_rules.list.return_value = []

    manager.ensure_security_groups("rg-x")

    origenes = {
        call.args[3]["source_address_prefix"]
        for call in manager.network_client.security_rules.begin_create_or_update.call_args_list
    }
    assert "10.0.99.0/24" in origenes    # regla del worker hacia el gateway
    assert "10.0.199.0/24" in origenes   # regla inter-nodo (worker -> worker)
    # El CIDR que compute_subnet_cidrs habría calculado NO debe aparecer.
    calculado_publico, calculado_privado = compute_subnet_cidrs("10.0.0.0/16")
    assert calculado_publico not in origenes
    assert calculado_privado not in origenes


def test_ensure_security_groups_y_ensure_subnets_usan_el_mismo_cidr(manager):
    """Ambos métodos deben coincidir SIEMPRE -antes podían divergir porque
    cada uno calculaba el CIDR por su cuenta."""
    assert manager._resolve_subnet_cidrs() == compute_subnet_cidrs(manager.spec.vnet_cidr)
