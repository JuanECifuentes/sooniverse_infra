"""
Cubre la causa secundaria #5 del bug de login del admin: el is_superuser de
Django NUNCA se propagaba al role de sooniverse."user" en Open WebUI -el
admin humano del panel quedaba como 'user' normal en el chat, sin panel de
admin de Open WebUI ni poder gestionar modelos/usuarios ahí.

DJANGO_SUPERUSER_EMAIL (nueva env var del servicio openwebui-bootstrap, ver
scripts/render_gateway_stack.py) se promueve con la MISMA lógica SQL que ya
usaba ensure_bootstrap_is_admin() para la cuenta técnica -ver
_promote_email_to_admin(), extraída de esa función en este cambio.
"""

import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

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


def _fake_connect(rowcount):
    """Simula psycopg2.connect(...) para el patrón
    'with conn: with conn.cursor() as cur: cur.execute(...); cur.rowcount'."""
    cur = MagicMock()
    cur.rowcount = rowcount
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False

    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False

    connect = MagicMock(return_value=conn)
    return connect, conn, cur


def test_sin_django_superuser_email_no_toca_la_bd(monkeypatch):
    mod = _reload_bootstrap_models(monkeypatch)
    assert "DJANGO_SUPERUSER_EMAIL" not in __import__("os").environ or not __import__("os").environ.get(
        "DJANGO_SUPERUSER_EMAIL"
    )

    with patch("psycopg2.connect") as connect:
        mod.ensure_django_admin_is_owui_admin()
        connect.assert_not_called()


def test_promueve_al_admin_humano_si_su_fila_existe(monkeypatch):
    mod = _reload_bootstrap_models(
        monkeypatch,
        DJANGO_SUPERUSER_EMAIL="admin@sooniverse.co",
        DB_NAME="db", DB_USER="u", DB_PASSWORD="p", DB_HOST="h", DB_PORT="5432",
        DATABASE_SCHEMA="sooniverse",
    )
    connect, conn, cur = _fake_connect(rowcount=1)

    with patch("psycopg2.connect", connect):
        mod.ensure_django_admin_is_owui_admin()

    connect.assert_called_once()
    # La promoción usa el email de DJANGO_SUPERUSER_EMAIL, no el de la cuenta
    # técnica de bootstrap.
    update_call = [c for c in cur.execute.call_args_list if "UPDATE" in c.args[0]]
    assert len(update_call) == 1
    assert update_call[0].args[1] == ("admin", "admin@sooniverse.co", "admin")


def test_no_falla_si_el_admin_humano_nunca_inicio_sesion(monkeypatch):
    """Sin fila 'user' todavía (nunca entró por SSO), rowcount=0 -no debe
    lanzar; se reintenta en la siguiente corrida del bootstrap."""
    mod = _reload_bootstrap_models(
        monkeypatch,
        DJANGO_SUPERUSER_EMAIL="admin@sooniverse.co",
        DB_NAME="db", DB_USER="u", DB_PASSWORD="p", DB_HOST="h", DB_PORT="5432",
    )
    connect, conn, cur = _fake_connect(rowcount=0)

    with patch("psycopg2.connect", connect):
        mod.ensure_django_admin_is_owui_admin()  # no debe lanzar


def test_falla_soft_si_no_hay_conexion(monkeypatch):
    mod = _reload_bootstrap_models(
        monkeypatch,
        DJANGO_SUPERUSER_EMAIL="admin@sooniverse.co",
        DB_NAME="db", DB_USER="u", DB_PASSWORD="p", DB_HOST="h", DB_PORT="5432",
    )
    with patch("psycopg2.connect", side_effect=Exception("sin red")):
        mod.ensure_django_admin_is_owui_admin()  # no debe lanzar


def test_promocion_de_la_cuenta_tecnica_sigue_funcionando_tras_el_refactor(monkeypatch):
    """No-regresión: ensure_bootstrap_is_admin() se reescribió para reusar
    _promote_email_to_admin(); debe seguir promoviendo BOOTSTRAP_EMAIL."""
    mod = _reload_bootstrap_models(
        monkeypatch,
        OPENWEBUI_BOOTSTRAP_EMAIL="bootstrap@sooniverse.internal",
        DB_NAME="db", DB_USER="u", DB_PASSWORD="p", DB_HOST="h", DB_PORT="5432",
    )
    connect, conn, cur = _fake_connect(rowcount=1)

    with patch("psycopg2.connect", connect):
        promoted = mod.ensure_bootstrap_is_admin()

    assert promoted is True
    update_call = [c for c in cur.execute.call_args_list if "UPDATE" in c.args[0]]
    assert update_call[0].args[1] == ("admin", "bootstrap@sooniverse.internal", "admin")
