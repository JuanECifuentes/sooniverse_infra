"""
demo/render_demo.py: configuración del Gateway del demo contra un proveedor
externo (DeepInfra u OpenAI-compatible), sin workers vLLM.
"""

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "demo"))

import render_demo as rd  # noqa: E402

BASE_ENV = {
    "DEMO_DOMAIN": "demo.example.com",
    "DEMO_HTTP_PORT": "8999",
    "DEMO_MODEL_ID": "Qwen/Qwen3.5-9B",
    "DEMO_MODEL_NAME": "sooniverse-ia",
    "DEEPINFRA_API_KEY": "di-test",
    "LITELLM_MASTER_KEY": "sk-master",
}


def _params(**extra):
    return rd.build_model_list({**BASE_ENV, **extra})[0]["litellm_params"]


def test_deepinfra_nativo_sin_api_base():
    p = _params()
    assert p["model"] == "deepinfra/Qwen/Qwen3.5-9B"
    assert "api_base" not in p
    # La key del proveedor nunca va en claro en el config: se lee del entorno.
    assert p["api_key"] == "os.environ/DEEPINFRA_API_KEY"


def test_openai_compatible_con_api_base():
    p = _params(DEMO_API_BASE="https://api.deepinfra.com/v1/")
    assert p["model"] == "openai/Qwen/Qwen3.5-9B"
    assert p["api_base"] == "https://api.deepinfra.com/v1"


def test_razonamiento_desactivado_por_defecto():
    # Sin esto Qwen3.5 agotaba los max_tokens "pensando" sin llegar a responder.
    assert _params()["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}


def test_razonamiento_se_puede_reactivar():
    assert "extra_body" not in _params(DEMO_DISABLE_THINKING="false")


def test_litellm_config_conserva_privacidad_y_master_key_por_entorno():
    cfg = yaml.safe_load(rd.render_litellm(BASE_ENV))
    assert [m["model_name"] for m in cfg["model_list"]] == ["sooniverse-ia"]
    assert cfg["general_settings"]["master_key"] == "os.environ/LITELLM_MASTER_KEY"
    assert cfg["general_settings"]["store_prompts_in_spend_logs"] is False
    assert cfg["litellm_settings"]["turn_off_message_logging"] is True
    assert "di-test" not in rd.render_litellm(BASE_ENV)


def test_nginx_interno_redirecciones_relativas_y_proto_del_host():
    conf = rd.render_nginx()
    # Detrás del Nginx del host: una redirección absoluta saldría como
    # http://<host>/... sin https ni puerto públicos.
    assert "absolute_redirect off;" in conf
    assert "proxy_set_header X-Forwarded-Proto $sooniverse_forwarded_proto;" in conf
    assert "location = /_auth" in conf and "auth_request /_auth;" in conf


def test_server_block_del_host_apunta_al_puerto_local():
    conf = rd.render_host_nginx(BASE_ENV)
    assert "server_name demo.example.com;" in conf
    assert "proxy_pass http://127.0.0.1:8999;" in conf
    assert "__DEMO_" not in conf
