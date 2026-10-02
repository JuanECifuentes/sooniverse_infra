"""
Pruebas de scripts/ensure_openwebui_key.py: el hash que se registra en
sooniverse.api_key_registry tiene que ser sha256(key) -lo que LiteLLM escribe
en LiteLLM_SpendLogs.api_key- y nunca el campo 'token' de la respuesta de
'/key/generate' (que es la key EN CLARO). Ese era el motivo real de que el
consumo del chat apareciera como "(sin registro)" en el panel.

Sin red ni base de datos: se monkeypatchean _http_json, wait_for_litellm y
db_setup.connect, igual que test_benchmark_capacity.py.
"""

import hashlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import db_setup  # noqa: E402
import ensure_openwebui_key as eok  # noqa: E402


class _FakeCursor:
    def __init__(self, log):
        self.log = log

    def execute(self, sql, params=None):
        self.log.append((" ".join(sql.split()), params))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, log):
        self.log = log
        self.closed = False

    def cursor(self):
        return _FakeCursor(self.log)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def sql_log(monkeypatch):
    log = []
    monkeypatch.setattr(db_setup, "connect", lambda *a, **k: _FakeConn(log))
    monkeypatch.setattr(db_setup, "resolve_db_config", lambda *a, **k: {})
    return log


def _env(tmp_path, **extra):
    vals = {"LITELLM_MASTER_KEY": "sk-master", "CLIENTE_ID": "acme", "ENTORNO": "prod", **extra}
    path = tmp_path / ".env"
    path.write_text("".join(f"{k}={v}\n" for k, v in vals.items()), encoding="utf-8")
    return path


def _run(monkeypatch, env_path, *extra_args):
    monkeypatch.setattr(sys, "argv", ["ensure_openwebui_key.py", "--env-file", str(env_path), *extra_args])
    return eok.main()


def _inserted_hash(sql_log):
    inserts = [p for sql, p in sql_log if sql.startswith("INSERT INTO sooniverse.api_key_registry")]
    assert len(inserts) == 1
    return inserts[0][1]


def test_litellm_token_hash_es_sha256_hex():
    assert eok.litellm_token_hash("sk-abc") == hashlib.sha256(b"sk-abc").hexdigest()


def test_key_nueva_registra_sha256_y_no_el_token_en_claro(tmp_path, monkeypatch, sql_log, capsys):
    env_path = _env(tmp_path)
    monkeypatch.setattr(eok, "wait_for_litellm", lambda: True)
    # Forma real de la respuesta de LiteLLM: 'token' == key en claro.
    monkeypatch.setattr(eok, "_http_json", lambda *a, **k: {
        "status": 200, "json": {"key": "sk-nueva", "token": "sk-nueva", "token_id": "irrelevante"}})

    assert _run(monkeypatch, env_path) == 0

    assert _inserted_hash(sql_log) == hashlib.sha256(b"sk-nueva").hexdigest()
    assert "OPENWEBUI_LITELLM_API_KEY=sk-nueva" in env_path.read_text(encoding="utf-8")
    assert "SOONIVERSE_OPENWEBUI_KEY_CREATED=1" in capsys.readouterr().out


def test_key_existente_repara_el_registro_sin_emitir_otra(tmp_path, monkeypatch, sql_log, capsys):
    env_path = _env(tmp_path, OPENWEBUI_LITELLM_API_KEY="sk-vieja")

    def _no_http(*a, **k):
        raise AssertionError("no debe llamar a LiteLLM si la key ya existe")

    monkeypatch.setattr(eok, "_http_json", _no_http)
    monkeypatch.setattr(eok, "wait_for_litellm", _no_http)

    assert _run(monkeypatch, env_path) == 0

    assert _inserted_hash(sql_log) == hashlib.sha256(b"sk-vieja").hexdigest()
    # Desactiva filas viejas con otro hash (p.ej. la que guardó la key en claro).
    assert any(sql.startswith("UPDATE sooniverse.api_key_registry") for sql, _ in sql_log)
    # Re-atribuye el consumo huérfano ya ingerido.
    assert any(sql.startswith("UPDATE sooniverse.token_usage_event") for sql, _ in sql_log)
    out = capsys.readouterr().out
    assert "SOONIVERSE_OPENWEBUI_KEY_CREATED=1" not in out
    assert "registro verificado" in out


def test_fallo_de_bd_no_aborta(tmp_path, monkeypatch, capsys):
    env_path = _env(tmp_path, OPENWEBUI_LITELLM_API_KEY="sk-vieja")

    def _explota(*a, **k):
        raise RuntimeError("bd caída")

    monkeypatch.setattr(db_setup, "connect", _explota)
    monkeypatch.setattr(db_setup, "resolve_db_config", lambda *a, **k: {})

    assert _run(monkeypatch, env_path) == 0
    assert "[WARNING]" in capsys.readouterr().out


def test_base_url_configurable(tmp_path, monkeypatch, sql_log):
    env_path = _env(tmp_path)
    urls = []
    # main() reasigna el global: monkeypatch lo restaura al terminar el test.
    monkeypatch.setattr(eok, "NGINX_BASE_URL", "http://localhost")
    monkeypatch.setattr(eok, "wait_for_litellm", lambda: True)

    def _http(method, url, *a, **k):
        urls.append(url)
        return {"status": 200, "json": {"key": "sk-x"}}

    monkeypatch.setattr(eok, "_http_json", _http)

    assert _run(monkeypatch, env_path, "--base-url", "http://proxy/") == 0
    assert urls == ["http://proxy/key/generate"]
