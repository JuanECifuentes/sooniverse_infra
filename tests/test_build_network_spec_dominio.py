"""
Pruebas de generate_infra.py::build_network_spec_from_config() /
load_network_outputs_from_state() para gateway.dominio (IP persistente del
Gateway), en AWS y Azure. No requiere ninguna nube ni PostgreSQL -usa
InMemoryInfraStateStore.

Cubre la Fase 2.2 del plan: antes, 'gateway.dominio.habilitado: true' en Azure
no tenía forma de propagar gateway_eip/gateway_eip_persistent/gateway_domain
a AzureNetworkSpec (los campos ni existían), y load_network_outputs_from_state()
no reconstruía gateway_eip_public_ip desde el estado -_associate_gateway_eip()
nunca se habría disparado para Azure en un '--only gateway' separado.
"""

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from generate_infra import (  # noqa: E402
    build_network_spec_from_config,
    load_network_outputs_from_state,
)
from infra_state import InMemoryInfraStateStore  # noqa: E402


def load_base_config():
    with (REPO_ROOT / "config_global.yaml").open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def clone(config):
    return yaml.safe_load(yaml.dump(config))


def test_build_network_spec_aws_propaga_gateway_eip():
    cfg = load_base_config()
    assert cfg["gateway"]["dominio"]["habilitado"] is True
    spec = build_network_spec_from_config(cfg)
    assert spec.gateway_eip is True
    assert spec.gateway_eip_persistent is True
    assert spec.gateway_domain == "ia.sooniverse.co"


def test_build_network_spec_azure_propaga_gateway_eip():
    cfg = clone(load_base_config())
    cfg["red_y_aislamiento"]["cloud"] = "azure"
    assert cfg["gateway"]["dominio"]["habilitado"] is True
    spec = build_network_spec_from_config(cfg)
    assert spec.gateway_eip is True
    assert spec.gateway_eip_persistent is True
    assert spec.gateway_domain == "ia.sooniverse.co"


def test_build_network_spec_azure_sin_dominio_no_pide_ip():
    cfg = clone(load_base_config())
    cfg["red_y_aislamiento"]["cloud"] = "azure"
    cfg["gateway"]["dominio"]["habilitado"] = False
    spec = build_network_spec_from_config(cfg)
    assert spec.gateway_eip is False
    assert spec.gateway_domain is None


def test_load_network_outputs_from_state_azure_reconstruye_gateway_ip():
    cfg = clone(load_base_config())
    cfg["red_y_aislamiento"]["cloud"] = "azure"
    state = InMemoryInfraStateStore()
    deployment_id = state.open_deployment(
        cfg["cliente"]["id"], cfg["cliente"]["entorno"], cfg["red_y_aislamiento"]["region"],
        cloud="azure",
    )
    for component, aws_id, attrs in [
        ("resource-group", "rg-1", {"name": "sooniverse-acme-prod-rg"}),
        ("vnet", "vnet-1", {"name": "sooniverse-acme-prod-vnet"}),
        ("nsg-gateway", "nsg-gw-1", {"name": "sooniverse-acme-prod-gateway"}),
        ("nsg-workers", "nsg-wk-1", {"name": "sooniverse-acme-prod-workers"}),
        ("pip-gateway", "pip-gw-1", {"name": "sooniverse-acme-prod-pip-gateway", "public_ip": "20.1.2.3"}),
    ]:
        state.record_resource(
            deployment_id, resource_type=component, component=component, aws_id=aws_id,
            delete_order=1, managed_by_us=True, state="active", attributes=attrs,
        )

    outputs = load_network_outputs_from_state(cfg, state, deployment_id)

    assert outputs is not None
    assert outputs.gateway_eip_allocation_id == "pip-gw-1"
    assert outputs.gateway_eip_public_ip == "20.1.2.3"


def test_load_network_outputs_from_state_azure_sin_pip_gateway_da_none():
    cfg = clone(load_base_config())
    cfg["red_y_aislamiento"]["cloud"] = "azure"
    state = InMemoryInfraStateStore()
    deployment_id = state.open_deployment(
        cfg["cliente"]["id"], cfg["cliente"]["entorno"], cfg["red_y_aislamiento"]["region"],
        cloud="azure",
    )
    for component, aws_id, attrs in [
        ("resource-group", "rg-1", {"name": "sooniverse-acme-prod-rg"}),
        ("vnet", "vnet-1", {"name": "sooniverse-acme-prod-vnet"}),
        ("nsg-gateway", "nsg-gw-1", {"name": "sooniverse-acme-prod-gateway"}),
        ("nsg-workers", "nsg-wk-1", {"name": "sooniverse-acme-prod-workers"}),
    ]:
        state.record_resource(
            deployment_id, resource_type=component, component=component, aws_id=aws_id,
            delete_order=1, managed_by_us=True, state="active", attributes=attrs,
        )

    outputs = load_network_outputs_from_state(cfg, state, deployment_id)

    assert outputs is not None
    assert outputs.gateway_eip_allocation_id is None
    assert outputs.gateway_eip_public_ip is None
