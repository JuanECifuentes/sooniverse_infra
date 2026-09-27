"""
Pruebas de Azure BYOC (Azure Lighthouse, ver onboarding/azure-byoc-terraform/
y el docstring de scripts/azure_network.py):

  1. ConfigValidator: 'cliente.modo: byoc' + 'cloud: azure' exige
     'red_y_aislamiento.azure_subscription_id' (GUID válido); modo 'hosted'
     lo acepta como override opcional pero sigue validando el formato.
  2. build_network_spec_from_config() propaga ese campo a AzureNetworkSpec
     (antes de este cambio, el campo existía en el dataclass pero nunca se
     rellenaba desde el contrato -ver azure_network.py::AzureNetworkManager,
     que ya sabía usarlo).
  3. azure_network.ensure_azure_cli_subscription(): antes de cualquier 'sky
     launch/exec/down/status' en Azure, el perfil por defecto del CLI 'az'
     debe apuntar a la suscripción del cliente (SkyPilot la resuelve SOLO de
     ahí, no de las variables de entorno AZURE_*) -mockeado con subprocess,
     no requiere 'az' real ni una suscripción real.

No requiere ninguna nube real ni PostgreSQL.
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from generate_infra import (  # noqa: E402
    ConfigValidationError,
    ConfigValidator,
    build_network_spec_from_config,
)

VALID_SUBSCRIPTION_ID = "11111111-2222-3333-4444-555555555555"


def load_base_config():
    with (REPO_ROOT / "config_global.yaml").open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def clone(config):
    return yaml.safe_load(yaml.dump(config))


def azure_byoc_config():
    """Config base + 'cloud: azure' + 'modo: byoc' + una suscripción válida,
    el caso "todo correcto" del que parten los tests de rechazo."""
    cfg = clone(load_base_config())
    cfg["red_y_aislamiento"]["cloud"] = "azure"
    cfg["cliente"]["modo"] = "byoc"
    cfg["red_y_aislamiento"]["azure_subscription_id"] = VALID_SUBSCRIPTION_ID
    return cfg


# -- ConfigValidator -----------------------------------------------------------
def test_azure_byoc_config_valida_es_aceptada():
    ConfigValidator.validate(azure_byoc_config())


def test_azure_byoc_sin_subscription_id_falla():
    cfg = azure_byoc_config()
    del cfg["red_y_aislamiento"]["azure_subscription_id"]
    with pytest.raises(ConfigValidationError, match="azure_subscription_id"):
        ConfigValidator.validate(cfg)


def test_azure_byoc_subscription_id_vacio_falla():
    cfg = azure_byoc_config()
    cfg["red_y_aislamiento"]["azure_subscription_id"] = ""
    with pytest.raises(ConfigValidationError, match="azure_subscription_id"):
        ConfigValidator.validate(cfg)


@pytest.mark.parametrize(
    "bad_guid",
    ["no-es-un-guid", "11111111-2222-3333-4444", "111111112222333344445555555555555"],
)
def test_azure_byoc_subscription_id_formato_invalido_falla(bad_guid):
    cfg = azure_byoc_config()
    cfg["red_y_aislamiento"]["azure_subscription_id"] = bad_guid
    with pytest.raises(ConfigValidationError, match="GUID"):
        ConfigValidator.validate(cfg)


def test_azure_hosted_sin_subscription_id_no_requiere_nada():
    """Modo 'hosted' (comportamiento histórico): sin 'azure_subscription_id',
    AzureNetworkManager cae a AZURE_SUBSCRIPTION_ID de '.env' -no debe exigirse
    el campo, solo AWS/Azure BYOC lo requieren."""
    cfg = clone(load_base_config())
    cfg["red_y_aislamiento"]["cloud"] = "azure"
    cfg["cliente"]["modo"] = "hosted"
    assert "azure_subscription_id" not in cfg["red_y_aislamiento"]
    ConfigValidator.validate(cfg)


def test_azure_hosted_con_subscription_id_invalido_falla():
    """El override es opcional en 'hosted', pero si está presente debe ser un
    GUID válido igual que en BYOC (evita un typo silencioso)."""
    cfg = clone(load_base_config())
    cfg["red_y_aislamiento"]["cloud"] = "azure"
    cfg["cliente"]["modo"] = "hosted"
    cfg["red_y_aislamiento"]["azure_subscription_id"] = "no-es-un-guid"
    with pytest.raises(ConfigValidationError, match="GUID"):
        ConfigValidator.validate(cfg)


def test_aws_byoc_no_exige_azure_subscription_id():
    """La validación de 'azure_subscription_id' es exclusiva de 'cloud: azure'
    -AWS BYOC sigue usando 'aws_profile', sin cruzarse entre sí."""
    cfg = clone(load_base_config())
    cfg["cliente"]["modo"] = "byoc"
    assert cfg["red_y_aislamiento"].get("cloud", "aws") == "aws"
    ConfigValidator.validate(cfg)


# -- build_network_spec_from_config -------------------------------------------
def test_build_network_spec_propaga_subscription_id_byoc():
    cfg = azure_byoc_config()
    spec = build_network_spec_from_config(cfg)
    assert spec.subscription_id == VALID_SUBSCRIPTION_ID


def test_build_network_spec_hosted_sin_override_da_none():
    """None => AzureNetworkManager/_default_credential caen a
    AZURE_SUBSCRIPTION_ID de '.env' (modo 'hosted' intacto)."""
    cfg = clone(load_base_config())
    cfg["red_y_aislamiento"]["cloud"] = "azure"
    spec = build_network_spec_from_config(cfg)
    assert spec.subscription_id is None


# -- azure_network.ensure_azure_cli_subscription ------------------------------
from azure_network import ensure_azure_cli_subscription  # noqa: E402


def test_ensure_azure_cli_subscription_none_no_hace_nada():
    with patch("azure_network.subprocess.run") as mock_run:
        ensure_azure_cli_subscription(None)
    mock_run.assert_not_called()


def test_ensure_azure_cli_subscription_ya_activa_no_hace_nada():
    with patch("azure_network.shutil.which", return_value="/usr/bin/az"), \
         patch("azure_network.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=VALID_SUBSCRIPTION_ID + "\n", stderr="")
        ensure_azure_cli_subscription(VALID_SUBSCRIPTION_ID)
    # Solo 'az account show' -nunca 'az account set' ni 'sky api stop'.
    assert mock_run.call_count == 1
    assert mock_run.call_args[0][0][:2] == ["az", "account"]


def test_ensure_azure_cli_subscription_cambia_y_reinicia_sky():
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["az", "account", "show"]:
            return MagicMock(returncode=0, stdout="otra-suscripcion-distinta\n", stderr="")
        if cmd[:3] == ["az", "account", "set"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    def fake_which(name):
        return f"/usr/bin/{name}"

    with patch("azure_network.shutil.which", side_effect=fake_which), \
         patch("azure_network.subprocess.run", side_effect=fake_run):
        ensure_azure_cli_subscription(VALID_SUBSCRIPTION_ID)

    assert ["az", "account", "show", "--query", "id", "-o", "tsv"] in calls
    assert ["az", "account", "set", "--subscription", VALID_SUBSCRIPTION_ID] in calls
    assert ["/usr/bin/sky", "api", "stop"] in calls


def test_ensure_azure_cli_subscription_set_falla_da_error_claro():
    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["az", "account", "show"]:
            return MagicMock(returncode=0, stdout="otra-suscripcion\n", stderr="")
        return MagicMock(returncode=1, stdout="", stderr="ERROR: subscription not found")

    with patch("azure_network.shutil.which", return_value="/usr/bin/az"), \
         patch("azure_network.subprocess.run", side_effect=fake_run):
        with pytest.raises(RuntimeError, match="Lighthouse"):
            ensure_azure_cli_subscription(VALID_SUBSCRIPTION_ID)


def test_ensure_azure_cli_subscription_sin_az_cli_falla_claro():
    with patch("azure_network.shutil.which", return_value=None):
        with pytest.raises(RuntimeError, match="az"):
            ensure_azure_cli_subscription(VALID_SUBSCRIPTION_ID)
