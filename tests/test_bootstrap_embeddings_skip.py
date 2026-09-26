"""
Cubre el gap #6 de embeddings (Fase 4): antes, bootstrap_models.py registraba
CUALQUIER id devuelto por GET /v1/models de LiteLLM como modelo de chat
seleccionable en Open WebUI -incluidos los de embeddings, que no exponen
/v1/chat/completions y le fallarían a cualquier mensaje del usuario.

fetch_embedding_model_names() lee litellm_config.yaml directamente (sin
depender de PyYAML, no garantizado en la imagen base de Open WebUI) para
saber qué 'model_name' tiene 'model_info.mode: embedding' -GET /v1/models no
expone ese campo en su formato de respuesta OpenAI estándar.
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


SAMPLE_CONFIG = """model_list:
- model_name: sooniverse-qwen3.5
  litellm_params:
    model: openai/x
    api_base: http://10.0.0.5:8007/v1
    api_key: sooniverse-internal
    weight: 1
  model_info:
    id: qwen3-5-llm-10-0-0-5-8007
    sooniverse_worker_ip: 10.0.0.5
    sooniverse_workload: qwen3-5-llm
    sooniverse_capabilities: {}
    supports_vision: false
    supports_function_calling: false
- model_name: sooniverse-embeddings
  litellm_params:
    model: openai/y
    api_base: http://10.0.0.6:8009/v1
    api_key: sooniverse-internal
    weight: 1
  model_info:
    id: gte-embed-10-0-0-6-8009
    sooniverse_worker_ip: 10.0.0.6
    sooniverse_workload: gte-embed
    sooniverse_capabilities: {}
    supports_vision: false
    supports_function_calling: false
    mode: embedding
"""


def test_fetch_embedding_model_names_detecta_solo_los_de_embedding(monkeypatch, tmp_path):
    config_path = tmp_path / "litellm_config.yaml"
    config_path.write_text(SAMPLE_CONFIG, encoding="utf-8")
    mod = _reload_bootstrap_models(monkeypatch, LITELLM_CONFIG_PATH=str(config_path))

    nombres = mod.fetch_embedding_model_names()

    assert nombres == {"sooniverse-embeddings"}


def test_fetch_embedding_model_names_vacio_sin_ninguno(monkeypatch, tmp_path):
    config_path = tmp_path / "litellm_config.yaml"
    config_path.write_text(
        "model_list:\n- model_name: sooniverse-qwen3.5\n  model_info:\n    id: x\n",
        encoding="utf-8",
    )
    mod = _reload_bootstrap_models(monkeypatch, LITELLM_CONFIG_PATH=str(config_path))

    assert mod.fetch_embedding_model_names() == set()


def test_fetch_embedding_model_names_falla_soft_sin_archivo(monkeypatch, tmp_path):
    mod = _reload_bootstrap_models(monkeypatch, LITELLM_CONFIG_PATH=str(tmp_path / "no-existe.yaml"))
    assert mod.fetch_embedding_model_names() == set()  # no debe lanzar


def test_main_no_registra_el_modelo_de_embeddings_en_open_webui(monkeypatch, tmp_path):
    config_path = tmp_path / "litellm_config.yaml"
    config_path.write_text(SAMPLE_CONFIG, encoding="utf-8")
    mod = _reload_bootstrap_models(
        monkeypatch,
        LITELLM_CONFIG_PATH=str(config_path),
        DB_NAME="db", DB_USER="u", DB_PASSWORD="p", DB_HOST="h", DB_PORT="5432",
    )

    monkeypatch.setattr(mod, "wait_for_openwebui", lambda: None)
    monkeypatch.setattr(mod, "authenticate", lambda: "token-abc")
    monkeypatch.setattr(mod, "ensure_bootstrap_is_admin", lambda: False)
    monkeypatch.setattr(mod, "ensure_django_admin_is_owui_admin", lambda: None)
    monkeypatch.setattr(mod, "ensure_default_user_role_is_user", lambda token: None)
    monkeypatch.setattr(mod, "fetch_litellm_models", lambda: ["sooniverse-qwen3.5", "sooniverse-embeddings"])
    monkeypatch.setattr(mod, "fetch_capabilities_by_model", lambda: {})

    upserted = []
    monkeypatch.setattr(
        mod, "upsert_model",
        lambda token, model_id, form, existing_ids: upserted.append(model_id) or True,
    )

    with patch.object(mod, "_http") as http:
        http.return_value = {"status": 200, "json": []}  # GET /api/v1/models/base
        exit_code = mod.main()

    assert exit_code == 0
    assert upserted == ["sooniverse-qwen3.5"]  # 'sooniverse-embeddings' excluido
