"""
Pruebas de los 4 chequeos de red que la rama feature/azure dejaba en "N/A"
para cualquier despliegue Azure (ver docstrings originales en
verify_deployment.py, ya reemplazados por implementaciones reales):
check_private_route_to_nat, check_workers_no_public_ip,
check_workers_sg_no_open_cidr. check_public_route_to_igw sigue siendo N/A a
propósito (Azure no tiene un recurso IGW explícito, es una ruta de sistema
implícita).

No requiere una suscripción Azure real: verify_deployment._azure_clients()
se mockea vía monkeypatch, mismo patrón que test_azure_network.py.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import verify_deployment as vd  # noqa: E402


def _res(component, aws_id, attributes=None):
    return {"component": component, "aws_id": aws_id, "attributes": attributes or {}}


def _base_config(**workload_overrides):
    wl = {"id": "qwen3-5-llm", "puerto": 8007}
    wl.update(workload_overrides)
    return {
        "red_y_aislamiento": {"cloud": "azure", "region": "westus3"},
        "workloads": [wl],
    }


def _patch_clients(monkeypatch, network_client=None, compute_client=None):
    network_client = network_client or MagicMock()
    compute_client = compute_client or MagicMock()
    monkeypatch.setattr(vd, "_azure_clients", lambda ctx: (network_client, compute_client))
    return network_client, compute_client


# -- check_private_route_to_nat ----------------------------------------------
def test_private_route_to_nat_ok_cuando_subred_tiene_nat_gateway(monkeypatch):
    ctx = vd.VerificationContext(
        config=_base_config(),
        resources=[
            _res("resource-group", "rg-1", {"name": "sooniverse-acme-prod-rg"}),
            _res("vnet", "vnet-1", {"name": "sooniverse-acme-prod-vnet"}),
            _res("subnet-private", "subnet-priv-1", {"name": "subred-private"}),
        ],
    )
    network_client, _ = _patch_clients(monkeypatch)
    network_client.subnets.get.return_value = MagicMock(nat_gateway=MagicMock(id="natgw-real-id"))

    result = vd.check_private_route_to_nat(ctx)

    assert result.status == "OK"


def test_private_route_to_nat_na_sin_nat_ni_vms_worker(monkeypatch):
    """Sin NAT Gateway asociado NI ninguna VM worker todavía (despliegue a
    medio camino): N/A, no FAIL -no hay suficiente información para saber si
    el workaround de IP de salida por worker (ver azure_worker_egress_ip.py)
    va a aplicar o no."""
    ctx = vd.VerificationContext(
        config=_base_config(),
        resources=[
            _res("resource-group", "rg-1", {"name": "sooniverse-acme-prod-rg"}),
            _res("vnet", "vnet-1", {"name": "sooniverse-acme-prod-vnet"}),
            _res("subnet-private", "subnet-priv-1", {"name": "subred-private"}),
        ],
    )
    network_client, compute_client = _patch_clients(monkeypatch)
    network_client.subnets.get.return_value = MagicMock(nat_gateway=None)
    compute_client.virtual_machines.list.return_value = []

    result = vd.check_private_route_to_nat(ctx)

    assert result.status == "N/A"
    assert result.critical is False


def test_private_route_to_nat_fail_sin_nat_y_workers_sin_ip_salida(monkeypatch):
    """Sin NAT Gateway asociado Y las VMs worker existentes tampoco tienen
    Public IP propia: ningún mecanismo de salida a internet -FAIL real."""
    ctx = vd.VerificationContext(
        config=_base_config(),
        resources=[
            _res("resource-group", "rg-1", {"name": "sooniverse-acme-prod-rg"}),
            _res("vnet", "vnet-1", {"name": "sooniverse-acme-prod-vnet"}),
            _res("subnet-private", "subnet-priv-1", {"name": "subred-private"}),
        ],
    )
    network_client, compute_client = _patch_clients(monkeypatch)
    network_client.subnets.get.return_value = MagicMock(nat_gateway=None)
    nic_id = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Network/networkInterfaces/nic-1"
    vm = MagicMock(network_profile=MagicMock(network_interfaces=[MagicMock(id=nic_id)]))
    vm.tags = {"rol": "worker"}
    compute_client.virtual_machines.list.return_value = [vm]
    network_client.network_interfaces.get.return_value = MagicMock(
        ip_configurations=[MagicMock(public_ip_address=None)]
    )

    result = vd.check_private_route_to_nat(ctx)

    assert result.status == "FAIL"


def test_private_route_to_nat_ok_via_egress_ip_por_worker(monkeypatch):
    """Sin NAT Gateway asociado, pero CADA VM worker tiene su propia Public
    IP de salida (el workaround real, ver azure_worker_egress_ip.py): OK."""
    ctx = vd.VerificationContext(
        config=_base_config(),
        resources=[
            _res("resource-group", "rg-1", {"name": "sooniverse-acme-prod-rg"}),
            _res("vnet", "vnet-1", {"name": "sooniverse-acme-prod-vnet"}),
            _res("subnet-private", "subnet-priv-1", {"name": "subred-private"}),
        ],
    )
    network_client, compute_client = _patch_clients(monkeypatch)
    network_client.subnets.get.return_value = MagicMock(nat_gateway=None)
    nic_id = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Network/networkInterfaces/nic-1"
    vm = MagicMock(network_profile=MagicMock(network_interfaces=[MagicMock(id=nic_id)]))
    vm.tags = {"rol": "worker"}
    compute_client.virtual_machines.list.return_value = [vm]
    network_client.network_interfaces.get.return_value = MagicMock(
        ip_configurations=[MagicMock(public_ip_address=MagicMock(id="pip-egress"))]
    )

    result = vd.check_private_route_to_nat(ctx)

    assert result.status == "OK"


def test_private_route_to_nat_na_sin_subred_registrada(monkeypatch):
    ctx = vd.VerificationContext(config=_base_config(), resources=[])
    _patch_clients(monkeypatch)

    result = vd.check_private_route_to_nat(ctx)

    assert result.status == "N/A"
    assert result.critical is False


# -- check_public_route_to_igw: sigue N/A a propósito en Azure --------------
def test_public_route_to_igw_sigue_siendo_na_en_azure():
    ctx = vd.VerificationContext(config=_base_config(), resources=[])
    result = vd.check_public_route_to_igw(ctx)
    assert result.status == "N/A"
    assert result.critical is False


# -- check_workers_no_public_ip ----------------------------------------------
def _vm_with_nic(nic_id, vm_name="vm-1"):
    nic_ref = MagicMock(id=nic_id)
    vm = MagicMock(network_profile=MagicMock(network_interfaces=[nic_ref]))
    vm.name = vm_name
    return vm


def test_workers_no_public_ip_ok_sin_ip_publica(monkeypatch):
    private_subnet_id = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Network/virtualNetworks/vnet/subnets/subred-private"
    ctx = vd.VerificationContext(
        config=_base_config(),
        resources=[
            _res("resource-group", "rg-1", {"name": "rg"}),
            _res("subnet-private", private_subnet_id),
        ],
    )
    nic_id = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Network/networkInterfaces/nic-1"
    network_client, compute_client = _patch_clients(monkeypatch)
    compute_client.virtual_machines.list.return_value = [_vm_with_nic(nic_id)]
    ip_cfg = MagicMock(subnet=MagicMock(id=private_subnet_id), public_ip_address=None)
    network_client.network_interfaces.get.return_value = MagicMock(ip_configurations=[ip_cfg])

    result = vd.check_workers_no_public_ip(ctx)

    assert result.status == "OK"


def test_workers_no_public_ip_fail_con_ip_publica(monkeypatch):
    private_subnet_id = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Network/virtualNetworks/vnet/subnets/subred-private"
    ctx = vd.VerificationContext(
        config=_base_config(),
        resources=[
            _res("resource-group", "rg-1", {"name": "rg"}),
            _res("subnet-private", private_subnet_id),
        ],
    )
    nic_id = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Network/networkInterfaces/nic-1"
    network_client, compute_client = _patch_clients(monkeypatch)
    vm = _vm_with_nic(nic_id, vm_name="sooniverse-acme-prod-worker-1")
    compute_client.virtual_machines.list.return_value = [vm]
    ip_cfg = MagicMock(subnet=MagicMock(id=private_subnet_id), public_ip_address=MagicMock(id="pip-oops"))
    network_client.network_interfaces.get.return_value = MagicMock(ip_configurations=[ip_cfg])

    result = vd.check_workers_no_public_ip(ctx)

    assert result.status == "FAIL"
    assert "sooniverse-acme-prod-worker-1" in result.detail


def test_workers_no_public_ip_na_sin_vms(monkeypatch):
    ctx = vd.VerificationContext(
        config=_base_config(),
        resources=[
            _res("resource-group", "rg-1", {"name": "rg"}),
            _res("subnet-private", "subnet-priv-id"),
        ],
    )
    _, compute_client = _patch_clients(monkeypatch)
    compute_client.virtual_machines.list.return_value = []

    result = vd.check_workers_no_public_ip(ctx)

    assert result.status == "N/A"


# -- check_workers_sg_no_open_cidr -------------------------------------------
def _rule(rule_name, source, dest_port_range, direction="Inbound", access="Allow"):
    rule = MagicMock(
        direction=direction, access=access,
        source_address_prefix=source, source_address_prefixes=[],
        destination_port_range=dest_port_range, destination_port_ranges=[],
    )
    # 'name' es un kwarg especial de MagicMock (usado en su repr interno, no
    # se puede fijar en el constructor); se asigna aparte, igual que 'vm.name'
    # arriba.
    rule.name = rule_name
    return rule


def test_workers_sg_fail_puerto_vllm_abierto_a_internet(monkeypatch):
    ctx = vd.VerificationContext(
        config=_base_config(puerto=8007),
        resources=[
            _res("resource-group", "rg-1", {"name": "rg"}),
            _res("nsg-workers", "nsg-1", {"name": "sooniverse-acme-prod-workers"}),
        ],
    )
    network_client, _ = _patch_clients(monkeypatch)
    network_client.security_rules.list.return_value = [
        _rule("allow-oops", "0.0.0.0/0", "8007"),
    ]

    result = vd.check_workers_sg_no_open_cidr(ctx)

    assert result.status == "FAIL"


def test_workers_sg_ok_solo_acotado_a_subred(monkeypatch):
    ctx = vd.VerificationContext(
        config=_base_config(puerto=8007),
        resources=[
            _res("resource-group", "rg-1", {"name": "rg"}),
            _res("nsg-workers", "nsg-1", {"name": "sooniverse-acme-prod-workers"}),
        ],
    )
    network_client, _ = _patch_clients(monkeypatch)
    network_client.security_rules.list.return_value = [
        _rule("allow-gateway", "10.0.0.0/20", "8007"),
    ]

    result = vd.check_workers_sg_no_open_cidr(ctx)

    assert result.status == "OK"


def test_workers_sg_fail_con_rango_de_puertos_que_incluye_vllm(monkeypatch):
    ctx = vd.VerificationContext(
        config=_base_config(puerto=8007),
        resources=[
            _res("resource-group", "rg-1", {"name": "rg"}),
            _res("nsg-workers", "nsg-1", {"name": "sooniverse-acme-prod-workers"}),
        ],
    )
    network_client, _ = _patch_clients(monkeypatch)
    network_client.security_rules.list.return_value = [
        _rule("allow-range", "0.0.0.0/0", "8000-9000"),
    ]

    result = vd.check_workers_sg_no_open_cidr(ctx)

    assert result.status == "FAIL"


def test_workers_sg_ignora_reglas_de_denegacion(monkeypatch):
    ctx = vd.VerificationContext(
        config=_base_config(puerto=8007),
        resources=[
            _res("resource-group", "rg-1", {"name": "rg"}),
            _res("nsg-workers", "nsg-1", {"name": "sooniverse-acme-prod-workers"}),
        ],
    )
    network_client, _ = _patch_clients(monkeypatch)
    network_client.security_rules.list.return_value = [
        _rule("deny-all", "0.0.0.0/0", "8007", access="Deny"),
    ]

    result = vd.check_workers_sg_no_open_cidr(ctx)

    assert result.status == "OK"
