"""
Pruebas de scripts/render_gateway_stack.py para parametrización dinámica de LiteLLM.
"""

import sys
from pathlib import Path
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from render_gateway_stack import render_nginx_conf, render_docker_compose  # noqa: E402


def _load_base_config():
    with (REPO_ROOT / "config_global.yaml").open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_default_litellm_base_url_renders_properly():
    cfg = _load_base_config()
    # Con el valor por defecto http://litellm:4000
    nginx_conf = render_nginx_conf(cfg)
    assert "upstream sooniverse_litellm { server litellm:4000;     }" in nginx_conf

    compose_yml = render_docker_compose(cfg)
    assert "OPENAI_API_BASE_URL: http://litellm:4000/v1" in compose_yml
    assert "LITELLM_BASE_URL: http://litellm:4000" in compose_yml
    assert '--port", "4000"' in compose_yml
    assert "http://localhost:4000/health/liveliness" in compose_yml


def test_custom_litellm_base_url_renders_dynamically():
    cfg = _load_base_config()
    cfg["gateway"]["litellm"]["base_url"] = "http://custom-proxy.internal:5000"

    nginx_conf = render_nginx_conf(cfg)
    assert "upstream sooniverse_litellm { server custom-proxy.internal:5000;     }" in nginx_conf

    compose_yml = render_docker_compose(cfg)
    assert "OPENAI_API_BASE_URL: http://custom-proxy.internal:5000/v1" in compose_yml
    assert "LITELLM_BASE_URL: http://custom-proxy.internal:5000" in compose_yml
    assert '--port", "5000"' in compose_yml
    assert "http://localhost:5000/health/liveliness" in compose_yml
