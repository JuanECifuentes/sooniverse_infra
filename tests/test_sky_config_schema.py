"""
Valida los manifiestos de cliente de SkyPilot generados por
TopologyBuilder.build_sky_gateway_config()/build_sky_workers_config() contra
el JSON Schema REAL de la versión de SkyPilot instalada
(sky.utils.schemas.get_config_schema()).

Motivación: la rama feature/azure emitía 'resource_group'/'security_group_name'
bajo la clave 'azure' del manifiesto -claves que NO existen en el esquema real
(additionalProperties: False; las correctas son 'resource_group_vm' y no hay
equivalente de 'security_group_name' para Azure). El propio autor de esa rama
dejó una nota admitiendo que nunca se había verificado contra una instalación
real de SkyPilot. Este test hace justamente eso, para que un futuro cambio de
esquema (nueva versión de SkyPilot) o un typo de clave rompan el CI en vez de
descubrirse 15 minutos dentro de un 'sky launch' real.

Se salta (no falla) si el paquete 'sky' no es importable en este intérprete
-en Windows, SkyPilot no importa (sky/utils/subprocess_utils.py depende de
'resource', solo POSIX). Corre de verdad en WSL/Linux, que es donde vive el
resto del pipeline de despliegue.
"""

import copy
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

jsonschema = pytest.importorskip("jsonschema")

try:
    from sky.utils import schemas as sky_schemas  # noqa: E402
except ImportError:
    sky_schemas = None

pytestmark = pytest.mark.skipif(
    sky_schemas is None,
    reason="paquete 'sky' no importable en este intérprete (ver docstring del módulo)",
)

from generate_infra import TopologyBuilder  # noqa: E402


def load_base_config():
    with (REPO_ROOT / "config_global.yaml").open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def clone(config):
    return yaml.safe_load(yaml.dump(config))


class _FakeAzureOutputs:
    """Stand-in mínimo de azure_network.AzureNetworkOutputs -solo los campos
    que build_sky_gateway_config()/build_sky_workers_config() leen."""

    resource_group_name = "sooniverse-acme-prod-rg"
    vnet_name = "sooniverse-acme-prod-vnet"
    nsg_gateway_name = "sooniverse-acme-prod-gateway"
    nsg_workers_name = "sooniverse-acme-prod-workers"
    remote_identity_name = "sooniverse-acme-prod-msi"


def _validate_provider_block(cloud_key: str, block: dict) -> None:
    """Valida `{cloud_key: block}` contra el mismo esquema que SkyPilot usa
    para el archivo apuntado por la env var SKYPILOT_CONFIG -mismo mecanismo
    que carga .sky_config_gateway.yaml/.sky_config_workers.yaml."""
    full_schema = sky_schemas.get_config_schema()
    instance = {cloud_key: block}
    jsonschema.validate(instance=instance, schema=full_schema)


def test_build_sky_gateway_config_azure_valida_contra_el_esquema_real():
    cfg = clone(load_base_config())
    cfg["red_y_aislamiento"]["cloud"] = "azure"
    builder = TopologyBuilder(cfg)
    builder.apply_network_outputs(_FakeAzureOutputs())

    generated = builder.build_sky_gateway_config()
    assert "azure" in generated
    _validate_provider_block("azure", generated["azure"])


def test_build_sky_workers_config_azure_valida_contra_el_esquema_real():
    cfg = clone(load_base_config())
    cfg["red_y_aislamiento"]["cloud"] = "azure"
    builder = TopologyBuilder(cfg)
    builder.apply_network_outputs(_FakeAzureOutputs())

    generated = builder.build_sky_workers_config(gateway_ip="20.1.2.3")
    assert "azure" in generated
    _validate_provider_block("azure", generated["azure"])


def test_build_sky_gateway_config_azure_no_usa_claves_inexistentes():
    """Aserción explícita y legible sin depender del mensaje de error de
    jsonschema: ninguna de las claves que la rama original emitía
    ('resource_group', 'vnet_name', 'security_group_name') debe aparecer -son
    las que NO existen en el esquema real (ver docstring del módulo)."""
    cfg = clone(load_base_config())
    cfg["red_y_aislamiento"]["cloud"] = "azure"
    builder = TopologyBuilder(cfg)
    builder.apply_network_outputs(_FakeAzureOutputs())

    generated = builder.build_sky_gateway_config()["azure"]
    for clave_invalida in ("resource_group", "vnet_name", "security_group_name"):
        assert clave_invalida not in generated, (
            f"'{clave_invalida}' no existe en el esquema 'azure' de SkyPilot"
        )
    assert generated["resource_group_vm"] == "sooniverse-acme-prod-rg"
    # 'remote_identity': ver el docstring de AzureNetworkManager.ensure_remote_identity()
    # -sin esto, SkyPilot exige Microsoft.Authorization/roleAssignments/write,
    # permiso que el Service Principal de este despliegue no tiene.
    assert generated["remote_identity"] == "sooniverse-acme-prod-msi"
    assert generated["vpc_name"] == "sooniverse-acme-prod-vnet"


def test_build_sky_gateway_config_aws_sigue_validando_contra_el_esquema_real():
    """No-regresión: el manifiesto AWS (el que ya está en producción) también
    debe seguir siendo válido contra el esquema real."""
    cfg = load_base_config()
    assert cfg["red_y_aislamiento"].get("cloud", "aws") == "aws"
    builder = TopologyBuilder(cfg)

    generated = builder.build_sky_gateway_config()
    if generated:
        _validate_provider_block("aws", generated.get("aws", {}))


# -- GCP (Fase 7, implementación teórica -ver scripts/gcp_network.py) -------
class _FakeGcpOutputs:
    """Stand-in mínimo de gcp_network.GcpNetworkOutputs -solo los campos que
    build_sky_gateway_config()/build_sky_workers_config() leen."""

    vpc_name = "sooniverse-mi-cliente-gcp-dev-vpc"


def _load_gcp_config():
    with (REPO_ROOT / "clients" / "_ejemplo_gcp" / "config_global.yaml").open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_build_sky_gateway_config_gcp_valida_contra_el_esquema_real():
    cfg = _load_gcp_config()
    builder = TopologyBuilder(cfg)
    builder.apply_network_outputs(_FakeGcpOutputs())

    generated = builder.build_sky_gateway_config()
    assert "gcp" in generated
    _validate_provider_block("gcp", generated["gcp"])


def test_build_sky_workers_config_gcp_valida_contra_el_esquema_real():
    cfg = _load_gcp_config()
    builder = TopologyBuilder(cfg)
    builder.apply_network_outputs(_FakeGcpOutputs())

    generated = builder.build_sky_workers_config(gateway_ip="34.1.2.3")
    assert "gcp" in generated
    _validate_provider_block("gcp", generated["gcp"])


def test_build_sky_gateway_config_gcp_no_usa_security_group_name():
    """El esquema 'gcp' de SkyPilot no tiene ningún equivalente a
    'security_group_name' -ver el docstring de scripts/gcp_network.py sobre
    por qué las reglas de firewall se acotan por sourceRanges en vez de por
    grupo/tag."""
    cfg = _load_gcp_config()
    builder = TopologyBuilder(cfg)
    builder.apply_network_outputs(_FakeGcpOutputs())

    generated = builder.build_sky_gateway_config()["gcp"]
    assert "security_group_name" not in generated
    assert "resource_group_vm" not in generated  # concepto de Azure, no de GCP
    assert generated["vpc_name"] == "sooniverse-mi-cliente-gcp-dev-vpc"
