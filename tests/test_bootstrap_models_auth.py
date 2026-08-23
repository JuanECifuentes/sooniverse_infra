"""
Pruebas de docker_images/openwebui/overlay/sooniverse/bootstrap_models.py::authenticate.

Cubre el bug real encontrado al confirmar el comportamiento exacto de
Open WebUI v0.11.0 contra su código fuente: con
WEBUI_AUTH_TRUSTED_EMAIL_HEADER activo (SSO por cabecera de confianza, ver
docker_images/openwebui/README.md), /signup y /signin por contraseña quedan
bloqueados incondicionalmente -así que la cuenta técnica de bootstrap debe
autenticarse con la MISMA cabecera de confianza, no con email+password.
"""

import importlib
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
OVERLAY_DIR = REPO_ROOT / "docker_images" / "openwebui" / "overlay"


def _reload_bootstrap_models(monkeypatch, **env):
    """Recarga el módulo con las env vars deseadas -lee TRUSTED_EMAIL_HEADER/
    BOOTSTRAP_EMAIL a nivel de módulo en el momento del import."""
    sys.path.insert(0, str(OVERLAY_DIR))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    for key in ("WEBUI_AUTH_TRUSTED_EMAIL_HEADER", "OPENWEBUI_BOOTSTRAP_PASSWORD"):
        if key not in env:
            monkeypatch.delenv(key, raising=False)

    if "sooniverse.bootstrap_models" in sys.modules:
        del sys.modules["sooniverse.bootstrap_models"]
    if "sooniverse" in sys.modules:
        del sys.modules["sooniverse"]

    module = importlib.import_module("sooniverse.bootstrap_models")
    return module


def test_modo_sso_autentica_con_cabecera_de_confianza(monkeypatch):
    mod = _reload_bootstrap_models(
        monkeypatch,
        WEBUI_AUTH_TRUSTED_EMAIL_HEADER="X-Sooniverse-Email",
        OPENWEBUI_BOOTSTRAP_EMAIL="bootstrap@sooniverse.internal",
    )

    with patch.object(mod, "_http", return_value={"status": 200, "json": {"token": "abc123"}}) as http:
        token = mod.authenticate()

    assert token == "abc123"
    call = http.call_args
    assert call.args[0] == "POST"
    assert call.args[1].endswith("/api/v1/auths/signin")
    assert call.kwargs["extra_headers"] == {"X-Sooniverse-Email": "bootstrap@sooniverse.internal"}
    # Bug real confirmado contra una instancia viva: /signin valida su body
    # (SigninForm: email+password obligatorios) ANTES de mirar la cabecera de
    # confianza -un body vacío devuelve 422 sin llegar siquiera a evaluar el
    # SSO. Los valores se ignoran cuando la cabecera gana, pero deben existir.
    assert call.args[2]["email"] == "bootstrap@sooniverse.internal"
    assert call.args[2]["password"]


def test_modo_sso_sin_password_no_falla_por_password_faltante(monkeypatch):
    """Antes de este fix, un despliegue con SSO activo Y sin
    OPENWEBUI_BOOTSTRAP_PASSWORD configurada fallaría igual -pero por la razón
    equivocada. Con el fix, el modo SSO ni siquiera mira la password."""
    mod = _reload_bootstrap_models(
        monkeypatch,
        WEBUI_AUTH_TRUSTED_EMAIL_HEADER="X-Sooniverse-Email",
    )
    assert mod.BOOTSTRAP_PASSWORD == ""

    with patch.object(mod, "_http", return_value={"status": 200, "json": {"token": "xyz"}}):
        token = mod.authenticate()
    assert token == "xyz"


def test_modo_sso_fallo_de_signin_lanza_bootstrap_error(monkeypatch):
    mod = _reload_bootstrap_models(monkeypatch, WEBUI_AUTH_TRUSTED_EMAIL_HEADER="X-Sooniverse-Email")

    with patch.object(mod, "_http", return_value={"status": 400, "json": {"detail": "boom"}}):
        try:
            mod.authenticate()
            assert False, "se esperaba BootstrapError"
        except mod.BootstrapError as exc:
            assert "SSO" in str(exc)


def test_modo_password_sigue_funcionando_sin_sso(monkeypatch):
    mod = _reload_bootstrap_models(
        monkeypatch,
        OPENWEBUI_BOOTSTRAP_PASSWORD="clave-de-prueba",
        OPENWEBUI_BOOTSTRAP_EMAIL="bootstrap@sooniverse.internal",
    )
    assert mod.TRUSTED_EMAIL_HEADER == ""

    respuestas = [
        {"status": 400, "json": {"detail": "ya existe"}},  # signup falla (cuenta ya creada)
        {"status": 200, "json": {"token": "signin-token"}},  # signin exitoso
    ]
    with patch.object(mod, "_http", side_effect=respuestas) as http:
        token = mod.authenticate()

    assert token == "signin-token"
    assert http.call_count == 2
    signup_call, signin_call = http.call_args_list
    assert signup_call.args[1].endswith("/api/v1/auths/signup")
    assert signin_call.args[1].endswith("/api/v1/auths/signin")
    assert signin_call.args[2] == {"email": "bootstrap@sooniverse.internal", "password": "clave-de-prueba"}


def test_modo_password_sin_password_configurada_lanza_error_explicito(monkeypatch):
    mod = _reload_bootstrap_models(monkeypatch)
    try:
        mod.authenticate()
        assert False, "se esperaba BootstrapError"
    except mod.BootstrapError as exc:
        assert "OPENWEBUI_BOOTSTRAP_PASSWORD" in str(exc)
