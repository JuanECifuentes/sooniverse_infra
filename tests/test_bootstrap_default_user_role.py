"""
Cubre un bug real encontrado en una prueba de despliegue real: Open WebUI
solo lee DEFAULT_USER_ROLE (env var) para SEMBRAR su tabla `config` la
primerísima vez que arranca; en cada reinicio posterior lee el valor YA
PERSISTIDO ahí, ignorando la env var. Con un despliegue donde ese valor ya
quedó sembrado como 'pending' (el default de fábrica) antes de que se
corrigiera la env var, cualquier usuario nuevo autenticado por SSO quedaba
'pending' -bloqueado hasta aprobación manual dentro del propio Open WebUI-,
pese a que Django (login_required) ya gatekeepea todo el acceso.
"""

import importlib
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
OVERLAY_DIR = REPO_ROOT / "docker_images" / "openwebui" / "overlay"


def _reload_bootstrap_models(monkeypatch, **env):
    sys.path.insert(0, str(OVERLAY_DIR))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    if "sooniverse.bootstrap_models" in sys.modules:
        del sys.modules["sooniverse.bootstrap_models"]
    if "sooniverse" in sys.modules:
        del sys.modules["sooniverse"]
    return importlib.import_module("sooniverse.bootstrap_models")


def test_corrige_default_user_role_pending_a_user(monkeypatch):
    mod = _reload_bootstrap_models(monkeypatch)

    admin_config = {"DEFAULT_USER_ROLE": "pending", "ENABLE_SIGNUP": False, "WEBUI_URL": "https://ia.example.com"}

    with patch.object(mod, "_http") as http:
        http.side_effect = [
            {"status": 200, "json": dict(admin_config)},  # GET
            {"status": 200, "json": {**admin_config, "DEFAULT_USER_ROLE": "user"}},  # POST
        ]
        mod.ensure_default_user_role_is_user("token-abc")

    get_call, post_call = http.call_args_list
    assert get_call.args[0] == "GET"
    assert get_call.args[1].endswith("/api/v1/auths/admin/config")
    assert post_call.args[0] == "POST"
    assert post_call.args[1].endswith("/api/v1/auths/admin/config")
    posted_body = post_call.args[2]
    assert posted_body["DEFAULT_USER_ROLE"] == "user"
    # No debe tocar el resto de la config de admin al corregir solo este campo.
    assert posted_body["ENABLE_SIGNUP"] is False
    assert posted_body["WEBUI_URL"] == "https://ia.example.com"


def test_no_hace_post_si_ya_es_user(monkeypatch):
    mod = _reload_bootstrap_models(monkeypatch)

    with patch.object(mod, "_http") as http:
        http.return_value = {"status": 200, "json": {"DEFAULT_USER_ROLE": "user"}}
        mod.ensure_default_user_role_is_user("token-abc")

    assert http.call_count == 1  # solo el GET, nunca un POST innecesario


def test_falla_soft_si_no_se_puede_leer_la_config(monkeypatch):
    mod = _reload_bootstrap_models(monkeypatch)

    with patch.object(mod, "_http") as http:
        http.return_value = {"status": 403, "json": {}}
        mod.ensure_default_user_role_is_user("token-abc")  # no debe lanzar

    assert http.call_count == 1
