"""
bootstrap_models.build_model_form debe publicar el modelo para TODOS los
usuarios. En Open WebUI v0.11 una lista vacía de access_grants significa
PRIVADO (solo admins/dueño), no "sin restricciones": lo público es el grant
comodín user:'*':read (backend/open_webui/models/access_grants.py). Con []
cualquier usuario con rol 'user' -todo el que entra por SSO- veía "No results
found" en el selector de modelos (confirmado en la prueba local del demo).
"""

import importlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OVERLAY_DIR = REPO_ROOT / "docker_images" / "openwebui" / "overlay"

CAPS = {
    "effective_vision": False,
    "effective_tool_calling": False,
    "effective_json_object": True,
    "max_model_len": 16384,
    "max_output_tokens": None,
}


def _bootstrap_models():
    sys.path.insert(0, str(OVERLAY_DIR))
    for mod in ("sooniverse.bootstrap_models", "sooniverse"):
        sys.modules.pop(mod, None)
    return importlib.import_module("sooniverse.bootstrap_models")


def test_modelo_publico_para_todos_los_usuarios():
    form = _bootstrap_models().build_model_form("sooniverse-ia", CAPS)
    assert {"principal_type": "user", "principal_id": "*", "permission": "read"} in form["access_grants"]


def test_access_grants_nunca_es_none():
    # None explícito revienta update_model_by_id() con ValidationError -> 500.
    form = _bootstrap_models().build_model_form("sooniverse-ia", CAPS)
    assert isinstance(form["access_grants"], list) and form["access_grants"]
