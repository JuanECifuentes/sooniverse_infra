"""
Pruebas de sooniverse.ingest_openwebui_apikeys() (database/006_workers_y_login.sql)
contra la base PostgreSQL real (la de .env). Requiere que las tablas de Open
WebUI ('user', 'api_key') ya existan en el esquema 'sooniverse' -las crea su
propio Alembic al arrancar el contenedor, nunca db_setup.py; si este despliegue
todavía no corrió Open WebUI ni una sola vez, el módulo entero se salta.

Usa un usuario de prueba aislado (email/id con el prefijo "pytest-owui-") que
nunca colisiona con cuentas reales, y limpia sus propias filas al terminar.
"""

import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from db_setup import DbSetupError, connect, resolve_db_config  # noqa: E402

ENV_PATH = REPO_ROOT / ".env"


def _connect():
    return connect(resolve_db_config(ENV_PATH))


def _owui_tables_exist() -> bool:
    try:
        conn = _connect()
    except DbSetupError:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('sooniverse.\"user\"'), to_regclass('sooniverse.api_key')")
            user_tbl, key_tbl = cur.fetchone()
            return user_tbl is not None and key_tbl is not None
    finally:
        conn.close()


pytestmark = pytest.mark.skipif(
    not _owui_tables_exist(),
    reason="Tablas de Open WebUI ('user'/'api_key') no existen todavía en esta BD",
)


@pytest.fixture
def owui_user():
    """Crea un usuario de prueba en sooniverse.\"user\" y lo limpia al terminar
    (incluida cualquier key + fila de api_key_registry que haya generado)."""
    user_id = f"pytest-owui-{uuid.uuid4().hex[:8]}"
    email = f"{user_id}@example.com"
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sooniverse."user" (id, email, username, role, name,
                    last_active_at, updated_at, created_at)
                VALUES (%s, %s, %s, 'user', %s, 1700000000, 1700000000, 1700000000)
                """,
                (user_id, email, user_id, "Pytest Owui"),
            )
        conn.commit()
    finally:
        conn.close()

    yield user_id, email

    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sooniverse.api_key_registry WHERE openwebui_user_id = %s", (user_id,))
            cur.execute("DELETE FROM sooniverse.api_key WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM sooniverse.\"user\" WHERE id = %s", (user_id,))
        conn.commit()
    finally:
        conn.close()


def _insert_key(user_id: str, key_id: str, plaintext_key: str):
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sooniverse.api_key (id, user_id, key, created_at, updated_at)
                VALUES (%s, %s, %s, 1700000000, 1700000000)
                """,
                (key_id, user_id, plaintext_key),
            )
        conn.commit()
    finally:
        conn.close()


def _run_ingest():
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT sooniverse.ingest_openwebui_apikeys()")
            result = cur.fetchone()[0]
        conn.commit()
        return result
    finally:
        conn.close()


def _fetch_registry_row(user_id: str):
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT key_alias, key_prefix, owner_email, origen, litellm_token_hash, is_active "
                "FROM sooniverse.api_key_registry WHERE openwebui_user_id = %s",
                (user_id,),
            )
            return cur.fetchone()
    finally:
        conn.close()


def test_ingesta_nunca_guarda_la_clave_en_claro(owui_user):
    user_id, email = owui_user
    plaintext = "sk-owui-secreto-de-verdad-1234"
    _insert_key(user_id, f"key_{user_id}", plaintext)

    _run_ingest()
    row = _fetch_registry_row(user_id)

    assert row is not None
    key_alias, key_prefix, owner_email, origen, token_hash, is_active = row
    assert plaintext not in (key_alias or "")
    assert plaintext not in (key_prefix or "")
    assert plaintext not in (token_hash or "")
    assert origen == "openwebui"
    assert owner_email == email
    assert is_active is True
    assert key_prefix == "sk-owui-…1234"


def test_ingesta_es_idempotente(owui_user):
    user_id, _ = owui_user
    _insert_key(user_id, f"key_{user_id}", "sk-owui-otraclave5678")

    _run_ingest()
    _run_ingest()

    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM sooniverse.api_key_registry WHERE openwebui_user_id = %s", (user_id,)
            )
            assert cur.fetchone()[0] == 1
    finally:
        conn.close()


def test_key_borrada_en_openwebui_desaparece_del_registro(owui_user):
    user_id, _ = owui_user
    key_id = f"key_{user_id}"
    _insert_key(user_id, key_id, "sk-owui-clave-temporal")
    _run_ingest()
    assert _fetch_registry_row(user_id) is not None

    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sooniverse.api_key WHERE id = %s", (key_id,))
        conn.commit()
    finally:
        conn.close()

    _run_ingest()
    assert _fetch_registry_row(user_id) is None


def test_sin_keys_no_inserta_nada(owui_user):
    user_id, _ = owui_user
    _run_ingest()
    assert _fetch_registry_row(user_id) is None
