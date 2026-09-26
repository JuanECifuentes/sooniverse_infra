"""
Pruebas unitarias de scripts/gcp_network.py.

⚠️ Igual que test_azure_network.py, no existe un equivalente a moto para GCP:
aquí se reemplaza `self.compute` (el objeto que devolvería
`googleapiclient.discovery.build("compute", "v1", ...)`) por un
`unittest.mock.MagicMock()`, pasado directamente al constructor de
`GcpNetworkManager` (que ya acepta un `compute=` explícito para esto). Cubre:
cálculo de subredes, blob de propiedad en 'description' (GCP no soporta
labels en estos recursos), orden de destroy, mecanismo de propiedad (doble
condición: estado + description real), recuperación vía `_lookup_live` con
estado perdido, reglas de firewall (rango completo de puertos para tráfico
inter-nodo) y `scan_orphans`. Esta implementación es teórica -no probada
contra un proyecto GCP real- por eso el peso está en verificar la LÓGICA
contra el esquema real de la API (nombres de campo, forma de las llamadas),
no un comportamiento end-to-end.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from googleapiclient.errors import HttpError  # noqa: E402

from gcp_network import (  # noqa: E402
    GcpNetworkError,
    GcpNetworkManager,
    GcpNetworkSpec,
    compute_subnet_cidrs,
)
from infra_state import InMemoryInfraStateStore  # noqa: E402


def _http_error(status: int) -> HttpError:
    resp = MagicMock(status=status, reason="error")
    return HttpError(resp, b"{}")


def make_spec(**overrides):
    defaults = dict(
        client_id="acme",
        environment="prod",
        region="us-central1",
        project_id="proyecto-fake",
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
    return GcpNetworkSpec(**defaults)


def _op_done(name="op-1"):
    return {"name": name, "status": "DONE"}


@pytest.fixture
def manager():
    state = InMemoryInfraStateStore()
    spec = make_spec()
    compute = MagicMock()
    mgr = GcpNetworkManager(spec, state=state, compute=compute)

    # _find_existing() cae a un GET en vivo cuando el estado no tiene el
    # recurso (_lookup_live, mismo patrón que azure_network.py::_find_existing).
    # Por defecto simula "todavía no existe nada en GCP": todos los .get()
    # relevantes lanzan 404, salvo que un test los sobreescriba.
    not_found = _http_error(404)
    compute.networks.return_value.get.side_effect = not_found
    compute.subnetworks.return_value.get.side_effect = not_found
    compute.routers.return_value.get.side_effect = not_found
    compute.firewalls.return_value.get.side_effect = not_found
    return mgr


def test_compute_subnet_cidrs_deterministic_and_non_overlapping():
    public, private = compute_subnet_cidrs("10.0.0.0/16")
    public2, private2 = compute_subnet_cidrs("10.0.0.0/16")
    assert public == public2 and private == private2

    import ipaddress

    assert not ipaddress.ip_network(public).overlaps(ipaddress.ip_network(private))


def test_nat_mode_per_az_rejected():
    """'per-az' no tiene mapeo en GCP: Cloud NAT se asocia a un Cloud Router
    regional, sin concepto de zona (a diferencia de AWS)."""
    with pytest.raises(GcpNetworkError):
        make_spec(nat_mode="per-az")


def test_az_count_other_than_one_rejected():
    """Las subredes de GCP son REGIONALES, no zonales -az_count solo admite 1."""
    with pytest.raises(GcpNetworkError):
        make_spec(az_count=2)


def test_client_id_normalization_enforced():
    with pytest.raises(GcpNetworkError):
        make_spec(client_id="ACME")


def test_tags_blob_includes_required_keys(manager):
    """GCP no admite labels en Network/Subnetwork/Router/Firewall -la prueba
    de propiedad va en un blob JSON dentro de 'description'."""
    description = manager._tags("vpc")
    payload = json.loads(description)
    assert payload["managed"] == "true"
    assert payload["deployment_id"] == manager.deployment_id
    assert payload["component"] == "vpc"
    assert payload["client_id"] == "acme"


def test_naming_convention(manager):
    assert manager._name("vpc") == "sooniverse-acme-prod-vpc"
    assert manager._name("subnet-public") == "sooniverse-acme-prod-subnet-public"


def test_ensure_network_idempotent(manager):
    """La primera llamada crea la VPC (get() del pre-chequeo 404, luego insert,
    luego un get() final para leer el selfLink real); la segunda ya la
    encuentra en el ESTADO (Postgres) y no vuelve a tocar la API de GCP."""
    manager.compute.networks.return_value.insert.return_value.execute.return_value = _op_done()

    call_count = {"n": 0}

    def fake_get(project, network):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise _http_error(404)
        result = MagicMock()
        result.execute.return_value = {"selfLink": "projects/p/global/networks/sooniverse-acme-prod-vpc"}
        return result

    manager.compute.networks.return_value.get.side_effect = fake_get

    self_link, name = manager.ensure_network()
    assert name == "sooniverse-acme-prod-vpc"
    assert manager.compute.networks.return_value.insert.call_count == 1

    manager.ensure_network()
    assert manager.compute.networks.return_value.insert.call_count == 1
    assert call_count["n"] == 2  # el segundo ensure_network() no llamó a get() de nuevo


def test_ensure_subnets_uses_resolved_cidrs(manager, monkeypatch):
    """Cada subred registra el CIDR que realmente se le pidió a GCP -no el
    que devuelva la API en la respuesta simulada- para que
    `_resolve_subnet_cidrs()` sea la única fuente de verdad (mismo principio
    que azure_network.py::_resolve_subnet_cidrs, que evita que dos métodos
    calculen el CIDR por separado y diverjan)."""
    monkeypatch.setattr(manager, "_find_existing", lambda component: None)
    manager.compute.subnetworks.return_value.insert.return_value.execute.return_value = _op_done()

    def fake_get(project, region, subnetwork):
        result = MagicMock()
        result.execute.return_value = {
            "selfLink": f"projects/p/regions/{region}/subnetworks/{subnetwork}",
        }
        return result

    manager.compute.subnetworks.return_value.get.side_effect = fake_get

    pub_id, pub_name, priv_id, priv_name = manager.ensure_subnets("sooniverse-acme-prod-vpc")

    assert pub_name == "sooniverse-acme-prod-subnet-public"
    assert priv_name == "sooniverse-acme-prod-subnet-private"

    registrados = {r["component"]: r for r in manager.state.list_resources(manager.deployment_id)}
    assert registrados["subnet-public"]["attributes"]["cidr"] != registrados["subnet-private"]["attributes"]["cidr"]


def test_ensure_firewall_workers_internal_allows_all_ports(manager):
    """La regla inter-nodo (worker<->worker) no puede enumerar puertos de
    antemano (tensor/pipeline parallel, coordinación de Ray) -debe cubrir
    TODO el rango TCP/UDP, confinada a la subred privada."""
    created_bodies = []
    seen_names = set()

    def fake_insert(project, body):
        created_bodies.append(body)
        result = MagicMock()
        result.execute.return_value = _op_done()
        return result

    def fake_get(project, firewall):
        # Pre-chequeo (404, fuerza la creación) la primera vez que se
        # consulta cada nombre; get() final tras crear, la segunda vez.
        if firewall not in seen_names:
            seen_names.add(firewall)
            raise _http_error(404)
        body = next((b for b in created_bodies if b["name"] == firewall), {})
        result = MagicMock()
        result.execute.return_value = {"selfLink": f"projects/p/global/firewalls/{firewall}", **body}
        return result

    manager.compute.firewalls.return_value.insert.side_effect = fake_insert
    manager.compute.firewalls.return_value.get.side_effect = fake_get

    manager.ensure_firewall_rules("sooniverse-acme-prod-vpc")

    internal_body = next(b for b in created_bodies if b["name"] == manager._name("fw-workers-internal"))
    protocols = {rule["IPProtocol"] for rule in internal_body["allowed"]}
    assert protocols == {"tcp", "udp"}
    assert all("ports" not in rule for rule in internal_body["allowed"])


def test_plan_destroy_orders_by_delete_order(manager):
    manager.state.record_resource(
        manager.deployment_id, resource_type="vpc", component="vpc", aws_id="vpc-1",
        delete_order=80, managed_by_us=True, state="active",
    )
    manager.state.record_resource(
        manager.deployment_id, resource_type="router", component="router", aws_id="router-1",
        delete_order=21, managed_by_us=True, state="active",
    )
    manager.state.record_resource(
        manager.deployment_id, resource_type="fw-gateway-ssh", component="fw-gateway-ssh", aws_id="fw-1",
        delete_order=13, managed_by_us=True, state="active",
    )

    plan = manager.plan_destroy()
    assert [item.component for item in plan] == ["fw-gateway-ssh", "router", "vpc"]


def test_destroy_skips_not_managed_by_us(manager):
    manager.state.record_resource(
        manager.deployment_id, resource_type="vpc", component="vpc", aws_id="vpc-ajeno",
        delete_order=80, managed_by_us=False, state="active",
    )

    report = manager.destroy()

    assert len(report.skipped_not_ours) == 1
    manager.compute.networks.return_value.delete.assert_not_called()


def test_destroy_skips_when_description_doesnt_match(manager):
    otro_deployment = "otro-deployment-id"
    manager.state.record_resource(
        manager.deployment_id, resource_type="vpc", component="vpc", aws_id="vpc-1",
        delete_order=80, managed_by_us=True, state="active", attributes={"name": "otra-vpc"},
    )
    manager.compute.networks.return_value.get.side_effect = None
    manager.compute.networks.return_value.get.return_value.execute.return_value = {
        "selfLink": "vpc-1",
        "description": json.dumps({"managed": "true", "deployment_id": otro_deployment}),
    }

    report = manager.destroy()

    assert len(report.skipped_not_ours) == 1
    manager.compute.networks.return_value.delete.assert_not_called()


def test_destroy_treats_already_gone_resource_as_success(manager):
    manager.state.record_resource(
        manager.deployment_id, resource_type="vpc", component="vpc", aws_id="vpc-1",
        delete_order=80, managed_by_us=True, state="active", attributes={"name": "vpc-1"},
    )
    manager.compute.networks.return_value.get.side_effect = _http_error(404)

    report = manager.destroy()

    assert len(report.succeeded) == 1
    assert not report.failed


def test_find_existing_falls_back_to_live_lookup_and_registers(manager):
    """Recuperación tras perder el estado (mismo patrón que
    azure_network.py::_find_existing/_lookup_live): si Postgres no tiene el
    recurso pero SÍ existe en GCP (nombre determinista), se adopta bajo el
    deployment_id actual en vez de intentar recrearlo."""
    manager.compute.networks.return_value.get.side_effect = None
    manager.compute.networks.return_value.get.return_value.execute.return_value = {
        "selfLink": "vpc-real-id"
    }

    existing = manager._find_existing("vpc")
    assert existing is not None
    assert existing["aws_id"] == "vpc-real-id"

    registrados = manager.state.list_resources(manager.deployment_id)
    assert any(r["component"] == "vpc" and r["aws_id"] == "vpc-real-id" for r in registrados)

    # Segunda llamada: ya está en el estado, no debe repetir la consulta a GCP.
    call_count_before = manager.compute.networks.return_value.get.call_count
    manager._find_existing("vpc")
    assert manager.compute.networks.return_value.get.call_count == call_count_before


def test_scan_orphans_filters_by_tags_and_known_ids(manager):
    ours_but_known = json.dumps({
        "managed": "true", "prefix": "sooniverse", "client_id": "acme", "environment": "prod",
    })
    ours_orphan = json.dumps({
        "managed": "true", "prefix": "sooniverse", "client_id": "acme", "environment": "prod",
    })
    otro_cliente = json.dumps({
        "managed": "true", "prefix": "sooniverse", "client_id": "otro-cliente", "environment": "prod",
    })

    manager.state.record_resource(
        manager.deployment_id, resource_type="vpc", component="vpc", aws_id="vpc-known",
        delete_order=80, managed_by_us=True, state="active",
    )

    manager.compute.networks.return_value.list.return_value.get.return_value = [
        {"selfLink": "vpc-known", "description": ours_but_known},
        {"selfLink": "vpc-orphan", "description": ours_orphan},
        {"selfLink": "vpc-otro-cliente", "description": otro_cliente},
    ]
    manager.compute.subnetworks.return_value.list.return_value.get.return_value = []
    manager.compute.routers.return_value.list.return_value.get.return_value = []
    manager.compute.firewalls.return_value.list.return_value.get.return_value = []

    orphans = manager.scan_orphans()

    ids = {o["gcp_id"] for o in orphans}
    assert "vpc-orphan" in ids
    assert "vpc-known" not in ids
    assert "vpc-otro-cliente" not in ids
