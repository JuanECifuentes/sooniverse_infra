"""
Pruebas del parche local a SkyPilot (patches/skypilot/) y su detección en
generate_infra.py::_check_skypilot_azure_patch().

Por qué existe: el parche (TrustedLaunch + Secure Boot deshabilitado en
'sky/provision/azure/instance.py') vive SOLO en el venv instalado -no en el
paquete de PyPI- y se pierde en cualquier reinstalación de la librería. Sin
un preflight que lo detecte, eso se descubre recién a los ~30-40 min de un
'sky launch' ya en curso (ver README 7.0.1). Estas pruebas no requieren 'az'
ni ninguna nube; mockean 'importlib.util.find_spec' para no depender de que
el venv de CI tenga skypilot instalado y parcheado.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from generate_infra import _check_skypilot_azure_patch  # noqa: E402

PATCH_FILE = REPO_ROOT / "patches" / "skypilot" / "0.13.0-azure-trustedlaunch-no-secureboot.patch"
APPLY_SCRIPT = REPO_ROOT / "scripts" / "apply_skypilot_patches.sh"


def test_patch_file_versionado_en_git():
    assert PATCH_FILE.exists(), "Falta el parche versionado de SkyPilot para Azure."
    content = PATCH_FILE.read_text(encoding="utf-8")
    assert "sky/provision/azure/instance.py" in content
    assert "secure_boot_enabled=False" in content
    assert "security_profile" in content


def test_apply_script_existe_y_es_ejecutable():
    assert APPLY_SCRIPT.exists(), "Falta scripts/apply_skypilot_patches.sh"
    import os
    import stat

    mode = os.stat(APPLY_SCRIPT).st_mode
    assert mode & stat.S_IXUSR, "scripts/apply_skypilot_patches.sh debe ser ejecutable (chmod +x)."


def _fake_spec_for(tmp_path: Path, content: str):
    instance_file = tmp_path / "instance.py"
    instance_file.write_text(content, encoding="utf-8")
    spec = MagicMock()
    spec.origin = str(instance_file)
    return spec


def test_check_skypilot_azure_patch_pasa_si_esta_aplicado(tmp_path):
    spec = _fake_spec_for(
        tmp_path,
        "security_profile=compute.SecurityProfile(\n"
        "    security_type='TrustedLaunch',\n"
        "    uefi_settings=compute.UefiSettings(\n"
        "        secure_boot_enabled=False, v_tpm_enabled=True)))\n",
    )
    with patch("importlib.util.find_spec", return_value=spec):
        _check_skypilot_azure_patch()  # no debe lanzar


def test_check_skypilot_azure_patch_falla_si_no_esta_aplicado(tmp_path):
    spec = _fake_spec_for(
        tmp_path,
        "priority=node_config['azure_arm_parameters'].get('priority', None))\n",
    )
    with patch("importlib.util.find_spec", return_value=spec):
        with pytest.raises(RuntimeError, match="apply_skypilot_patches.sh"):
            _check_skypilot_azure_patch()


def test_check_skypilot_azure_patch_sin_sky_instalado_no_falla():
    """Sin skypilot instalado, este preflight no debe ser el que reporte el
    problema -'sky check azure' (más abajo en el flujo) da un mensaje mejor."""
    with patch("importlib.util.find_spec", side_effect=ImportError("no module named sky")):
        _check_skypilot_azure_patch()  # no debe lanzar


def test_check_skypilot_azure_patch_spec_none_no_falla():
    with patch("importlib.util.find_spec", return_value=None):
        _check_skypilot_azure_patch()  # no debe lanzar


def test_check_skypilot_azure_patch_contra_el_venv_real_instalado():
    """Si este entorno de pruebas SÍ tiene skypilot instalado, valida que el
    preflight real (sin mocks) refleje el estado real del venv: el parche
    debería estar aplicado tras scripts/apply_skypilot_patches.sh. Si
    skypilot no está instalado en este entorno, no hay nada que comprobar."""
    import importlib.util

    spec = importlib.util.find_spec("sky.provision.azure.instance")
    if spec is None or not spec.origin:
        pytest.skip("skypilot no está instalado en este entorno de pruebas.")
    _check_skypilot_azure_patch()
