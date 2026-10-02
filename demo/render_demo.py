#!/usr/bin/env python3
"""
==============================================================================
Sooniverse Demo - Render de la configuración del Gateway contra un proveedor
externo (DeepInfra u otro OpenAI-compatible)
==============================================================================
Complemento AISLADO del despliegue principal: no usa SkyPilot, ni workers
vLLM, ni config_global.yaml. Lee `demo/.env.demo` y escribe en
`demo/generated/`:

  litellm_config.yaml   -> un único modelo público (DEMO_MODEL_NAME) que LiteLLM
                           enruta a DeepInfra con DEEPINFRA_API_KEY (la key del
                           proveedor nunca sale del servidor). Mismos
                           router/general/litellm_settings que producción
                           (scripts/render_litellm_config.py::build_config).
  nginx.conf            -> el nginx INTERNO del stack: mismas rutas y SSO que
                           producción (scripts/render_gateway_stack.py::
                           _nginx_locations_block), solo HTTP en :80. El TLS lo
                           termina el Nginx del host del VPS.
  nginx-host-<dominio>.conf
                        -> server block listo para el Nginx del host (desde
                           demo/nginx-host.conf.template).

Uso (lo invoca demo/deploy_demo.sh dentro del contenedor 'tools'):
    python demo/render_demo.py --env-file demo/.env.demo
"""

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List

DEMO_DIR = Path(__file__).resolve().parent
REPO_ROOT = DEMO_DIR.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import yaml  # noqa: E402

from db_setup import parse_env_file  # noqa: E402
from render_gateway_stack import (  # noqa: E402
    _NGINX_PROXY_COMMON,
    _NGINX_STREAMING_COMMON,
    _nginx_locations_block,
)
from render_litellm_config import build_config  # noqa: E402

DEMO_HEADER = (
    "# ==============================================================================\n"
    "# ARCHIVO GENERADO POR demo/render_demo.py A PARTIR DE demo/.env.demo\n"
    "# Se sobrescribe en cada './demo/deploy_demo.sh'. Cambia .env.demo, no este archivo.\n"
    "# ==============================================================================\n"
)

REQUIRED_KEYS = ("DEMO_DOMAIN", "DEMO_MODEL_ID", "DEEPINFRA_API_KEY", "LITELLM_MASTER_KEY")


def build_model_list(env: Dict[str, str]) -> List[Dict[str, Any]]:
    model_id = env["DEMO_MODEL_ID"].strip()
    model_name = env.get("DEMO_MODEL_NAME", "").strip() or "sooniverse-demo"
    api_base = env.get("DEMO_API_BASE", "").strip()

    if api_base:
        # Cualquier proveedor OpenAI-compatible (Together, Groq, OpenRouter,
        # un vLLM propio...): mismo DEEPINFRA_API_KEY como credencial genérica.
        litellm_params = {
            "model": f"openai/{model_id}",
            "api_base": api_base.rstrip("/"),
            "api_key": "os.environ/DEEPINFRA_API_KEY",
        }
    else:
        # Proveedor nativo de LiteLLM: conoce la URL y la tabla de precios de
        # DeepInfra, así el panel muestra coste real además de tokens.
        litellm_params = {
            "model": f"deepinfra/{model_id}",
            "api_key": "os.environ/DEEPINFRA_API_KEY",
        }

    # Modelos con razonamiento (Qwen3/3.5, etc.): sin esto "piensan" miles de
    # tokens antes de responder incluso a un saludo -en la prueba local del
    # demo, Qwen3.5-9B agotó los 4096 max_tokens del chat sin llegar a
    # contestar-. LiteLLM reenvía 'extra_body' tal cual al proveedor; los que
    # no conocen 'chat_template_kwargs' lo ignoran.
    if env.get("DEMO_DISABLE_THINKING", "true").strip().lower() in ("true", "1", "yes", "si", "sí"):
        litellm_params["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}

    return [{
        "model_name": model_name,
        "litellm_params": litellm_params,
        # bootstrap_models.py lee 'mode' para no exponer embeddings como chat.
        "model_info": {"mode": "chat", "proveedor": "demo-externo"},
    }]


def render_litellm(env: Dict[str, str]) -> str:
    config = build_config([], "simple-shuffle", {})
    config["model_list"] = build_model_list(env)
    return DEMO_HEADER + yaml.safe_dump(config, sort_keys=False, allow_unicode=True)


def render_nginx() -> str:
    # El Nginx del host termina TLS y manda 'X-Forwarded-Proto: https'; se
    # propaga tal cual a Django (SECURE_PROXY_SSL_HEADER/CSRF) y Open WebUI.
    # Si no viene (prueba local directa a 127.0.0.1:<puerto>), cae a $scheme.
    forwarded_proto = "$sooniverse_forwarded_proto"
    return f"""{DEMO_HEADER}
# Ruteo idéntico al Gateway de producción:
#   /              -> Open WebUI (chat, WebSocket), protegido por el login del panel
#   /v1/, /key/... -> LiteLLM Proxy (API OpenAI-compatible) -> proveedor externo
#   /panel/        -> Django (métricas y API keys)
#   /healthz       -> 200 fijo de nginx

map $http_upgrade $connection_upgrade {{
    default upgrade;
    ''      close;
}}

map $http_x_forwarded_proto $sooniverse_forwarded_proto {{
    default $http_x_forwarded_proto;
    ''      $scheme;
}}

upstream sooniverse_webui   {{ server open-webui:8080; }}
upstream sooniverse_litellm {{ server litellm:4000;    }}
upstream sooniverse_metrics {{ server metrics:8000;    }}

server {{
    listen 80;
    server_name _;

    # Redirecciones relativas (Location: /panel/...): este nginx escucha en
    # :80 en claro detrás del Nginx del host, así que una absoluta saldría
    # como http://<host>/... sin el https ni el puerto públicos.
    absolute_redirect off;

    # IP real del cliente: el único par que llega aquí es el Nginx del host
    # (el puerto solo se publica en 127.0.0.1 y entra por el bridge de Docker).
    set_real_ip_from  10.0.0.0/8;
    set_real_ip_from  172.16.0.0/12;
    set_real_ip_from  192.168.0.0/16;
    real_ip_header    X-Forwarded-For;
    real_ip_recursive on;
{_NGINX_PROXY_COMMON}    proxy_set_header X-Forwarded-Proto {forwarded_proto};
{_NGINX_STREAMING_COMMON}
{_nginx_locations_block(forwarded_proto)}}}
"""


def render_host_nginx(env: Dict[str, str]) -> str:
    template = (DEMO_DIR / "nginx-host.conf.template").read_text(encoding="utf-8")
    return (template
            .replace("__DEMO_DOMAIN__", env["DEMO_DOMAIN"].strip())
            .replace("__DEMO_HTTP_PORT__", env.get("DEMO_HTTP_PORT", "").strip() or "8088"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", default=str(DEMO_DIR / ".env.demo"))
    parser.add_argument("--output-dir", default=str(DEMO_DIR / "generated"))
    args = parser.parse_args()

    env = parse_env_file(Path(args.env_file))
    missing = [k for k in REQUIRED_KEYS if not env.get(k, "").strip()]
    if missing:
        print(f"[ERROR] Faltan variables en {args.env_file}: {', '.join(missing)}", file=sys.stderr)
        return 1

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    (out / "litellm_config.yaml").write_text(render_litellm(env), encoding="utf-8")
    (out / "nginx.conf").write_text(render_nginx(), encoding="utf-8")
    host_conf = out / f"nginx-host-{env['DEMO_DOMAIN'].strip()}.conf"
    host_conf.write_text(render_host_nginx(env), encoding="utf-8")

    model = build_model_list(env)[0]
    print(f"[OK] {out / 'litellm_config.yaml'} -> modelo '{model['model_name']}' = "
          f"{model['litellm_params']['model']}")
    print(f"[OK] {out / 'nginx.conf'}")
    print(f"[OK] {host_conf} (para /etc/nginx/sites-available/ del host)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
