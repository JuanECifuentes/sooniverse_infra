"""
Pruebas del dispatch 'cloud: gcp' en generate_infra.py (Fase 7, implementación
teórica -ver scripts/gcp_network.py). Cubre build_network_spec_from_config()
y load_network_outputs_from_state() para GCP, análogas a las de AWS/Azure en
tests/test_build_network_spec_dominio.py. No requiere GCP ni PostgreSQL -usa
InMemoryInfraStateStore.
"""

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from generate_infra import (  # noqa: E402
    ConfigValidator,
    build_network_spec_from_config,
    load_network_outputs_from_state,
)
from gcp_network import GcpNetworkSpec  # noqa: E402
from infra_state import InMemoryInfraStateStore  # noqa: E402


def load_gcp_config():
    with (REPO_ROOT / "clients" / "_ejemplo_gcp" / "config_global.yaml").open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    ConfigValidator.validate(cfg)
    return cfg


def clone(config):
    return yaml.safe_load(yaml.dump(config))


def test_build_network_spec_gcp_devuelve_gcp_network_spec():
    cfg = load_gcp_config()
    spec = build_network_spec_from_config(cfg)
    assert isinstance(spec, GcpNetworkSpec)
    assert spec.project_id == cfg["red_y_aislamiento"]["gcp_project"]
    assert spec.region == cfg["red_y_aislamiento"]["region"]
    assert spec.az_count == 1


def test_build_network_spec_gcp_sin_gateway_eip():
    """A diferencia de AWS/Azure, GcpNetworkSpec no tiene campos gateway_eip*
    -dominio propio NO implementado para GCP en esta versión."""
    cfg = load_gcp_config()
    spec = build_network_spec_from_config(cfg)
    assert not hasattr(spec, "gateway_eip")
    assert not hasattr(spec, "gateway_domain")


def test_load_network_outputs_from_state_gcp_reconstruye_red():
    cfg = load_gcp_config()
    state = InMemoryInfraStateStore()
    deployment_id = state.open_deployment(
        cfg["cliente"]["id"], cfg["cliente"]["entorno"], cfg["red_y_aislamiento"]["region"], cloud="gcp",
    )
    for component, gcp_id, attrs in [
        ("vpc", "vpc-1", {"name": "sooniverse-mi-cliente-gcp-dev-vpc"}),
        ("subnet-public", "subnet-pub-1", {"name": "sooniverse-mi-cliente-gcp-dev-subnet-public", "cidr": "10.30.0.0/20"}),
        ("subnet-private", "subnet-priv-1", {"name": "sooniverse-mi-cliente-gcp-dev-subnet-private", "cidr": "10.30.8.0/20"}),
        ("router", "router-1", {"name": "sooniverse-mi-cliente-gcp-dev-router"}),
        ("nat", "nat-1", {"name": "sooniverse-mi-cliente-gcp-dev-nat"}),
        ("fw-gateway-ssh", "fw-1", {"name": "sooniverse-mi-cliente-gcp-dev-fw-gateway-ssh"}),
        ("fw-workers-internal", "fw-2", {"name": "sooniverse-mi-cliente-gcp-dev-fw-workers-internal"}),
    ]:
        state.record_resource(
            deployment_id, resource_type=component, component=component, aws_id=gcp_id,
            delete_order=1, managed_by_us=True, state="active", attributes=attrs,
        )

    outputs = load_network_outputs_from_state(cfg, state, deployment_id)

    assert outputs is not None
    assert outputs.vpc_name == "sooniverse-mi-cliente-gcp-dev-vpc"
    assert outputs.public_subnet_cidr == "10.30.0.0/20"
    assert outputs.private_subnet_cidr == "10.30.8.0/20"
    assert outputs.router_name == "sooniverse-mi-cliente-gcp-dev-router"
    assert outputs.nat_name == "sooniverse-mi-cliente-gcp-dev-nat"
    assert set(outputs.firewall_ids) == {"fw-1", "fw-2"}


def test_load_network_outputs_from_state_gcp_sin_subredes_da_none():
    cfg = load_gcp_config()
    state = InMemoryInfraStateStore()
    deployment_id = state.open_deployment(
        cfg["cliente"]["id"], cfg["cliente"]["entorno"], cfg["red_y_aislamiento"]["region"], cloud="gcp",
    )
    state.record_resource(
        deployment_id, resource_type="vpc", component="vpc", aws_id="vpc-1",
        delete_order=1, managed_by_us=True, state="active", attributes={"name": "vpc-1"},
    )

    outputs = load_network_outputs_from_state(cfg, state, deployment_id)
    assert outputs is None
