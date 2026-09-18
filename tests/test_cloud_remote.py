"""
Pruebas de scripts/cloud_remote.py: usuario y raíz remota por nube.

No requiere ninguna nube ni PostgreSQL -es pura lógica de mapeo. Fija en un
test el hecho verificado contra el código fuente instalado de SkyPilot
0.13.0 (sky/templates/{aws,azure-ray,gcp-ray}.yml.j2): el usuario SSH remoto
NO es 'ubuntu' en todas las nubes -antes de scripts/cloud_remote.py, el repo
lo asumía así en 4 scripts distintos, roto en Azure/GCP.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from cloud_remote import remote_root_for, remote_user_for  # noqa: E402


def test_aws_user_is_ubuntu():
    assert remote_user_for("aws") == "ubuntu"


def test_azure_user_is_azureuser_not_ubuntu():
    # Verificado contra sky/templates/azure-ray.yml.j2:58 (ssh_user: azureuser).
    assert remote_user_for("azure") == "azureuser"
    assert remote_user_for("azure") != "ubuntu"


def test_gcp_user_is_gcpuser_not_ubuntu():
    # Verificado contra sky/templates/gcp-ray.yml.j2:87 (ssh_user: gcpuser).
    assert remote_user_for("gcp") == "gcpuser"
    assert remote_user_for("gcp") != "ubuntu"


def test_none_cloud_defaults_to_aws_for_backwards_compatibility():
    """Ningún config_global.yaml anterior a 'red_y_aislamiento.cloud' lo
    declaraba; su ausencia debe seguir comportándose como AWS."""
    assert remote_user_for(None) == "ubuntu"


def test_unknown_cloud_raises():
    with pytest.raises(ValueError):
        remote_user_for("digitalocean")


def test_remote_root_matches_user_home():
    assert remote_root_for("aws") == "/home/ubuntu/sooniverse_infra"
    assert remote_root_for("azure") == "/home/azureuser/sooniverse_infra"
    assert remote_root_for("gcp") == "/home/gcpuser/sooniverse_infra"


def test_remote_root_none_defaults_to_aws():
    assert remote_root_for(None) == "/home/ubuntu/sooniverse_infra"
