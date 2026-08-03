#!/usr/bin/env python3
"""
==============================================================================
Sooniverse Infra - Render dinámico del config.yaml de LiteLLM
==============================================================================
Traduce la lista de endpoints privados de los workers vLLM (descubiertos por
SkyPilot) al formato de `model_list` + `router_settings` que consume LiteLLM Proxy.

Se ejecuta en dos momentos:
  1. En el arranque del Nodo Gateway (vía la var de entorno WORKER_ENDPOINTS).
  2. Cada vez que se escala o reemplaza un worker (`scripts/sync_endpoints.py`).

Formato esperado de cada endpoint (JSON):
    {
      "workload_id": "qwen3-5-llm",
      "model_public_name": "sooniverse-qwen3.5",
      "hf_repo": "cyankiwi/Qwen3.5-2B-AWQ-4bit",
      "ip": "10.0.12.31",
      "port": 8007,
      "weight": 1,
      "max_model_len": 16384
    }
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

try:
    import yaml
except ImportError:
    print("[ERROR] Falta 'pyyaml'. Ejecuta: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

HEADER = (
    "# ==============================================================================\n"
    "# LITELLM PROXY CONFIG - GENERADO POR SOONIVERSE (render_litellm_config.py)\n"
    "# NO EDITAR A MANO: se reescribe en cada despliegue / sincronización de workers.\n"
    "# ==============================================================================\n\n"
)


def build_model_list(endpoints: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Cada endpoint es un *deployment* del mismo `model_name` lógico. LiteLLM
    balancea automáticamente entre todos los deployments que comparten nombre.
    """
    model_list: List[Dict[str, Any]] = []

    for ep in endpoints:
        ip = ep.get("ip")
        port = ep.get("port", 8007)
        if not ip:
            continue

        hf_repo = ep.get("hf_repo") or ep.get("model") or "unknown"
        litellm_params: Dict[str, Any] = {
            # Prefijo `openai/` -> LiteLLM habla el dialecto OpenAI que expone vLLM.
            "model": f"openai/{hf_repo}",
            "api_base": f"http://{ip}:{port}/v1",
            # vLLM no valida la key; LiteLLM exige un valor no vacío.
            "api_key": "sooniverse-internal",
            "weight": ep.get("weight", 1),
        }
        if ep.get("max_model_len"):
            litellm_params["max_tokens"] = ep["max_model_len"]
        if ep.get("rpm"):
            litellm_params["rpm"] = ep["rpm"]
        if ep.get("tpm"):
            litellm_params["tpm"] = ep["tpm"]

        model_list.append({
            "model_name": ep.get("model_public_name") or ep.get("workload_id") or "sooniverse-llm",
            "litellm_params": litellm_params,
            "model_info": {
                "id": f"{ep.get('workload_id', 'wl')}-{ip.replace('.', '-')}-{port}",
                "sooniverse_worker_ip": ip,
                "sooniverse_workload": ep.get("workload_id"),
                # Capacidades declaradas en config_global.yaml (ver
                # scripts/test_model_capabilities.py para la verificación real
                # contra el modelo desplegado). Informativo para cualquier
                # cliente que lea /v1/models y quiera adaptar su UI.
                "sooniverse_capabilities": ep.get("capacidades", {}),
            },
        })

    return model_list


def build_config(endpoints: List[Dict[str, Any]], strategy: str, gw: Dict[str, Any]) -> Dict[str, Any]:
    litellm_opts = gw.get("litellm", {}) if gw else {}

    return {
        "model_list": build_model_list(endpoints),

        "router_settings": {
            "routing_strategy": strategy,
            "num_retries": litellm_opts.get("num_retries", 2),
            "timeout": litellm_opts.get("request_timeout", 600),
            # Un worker con `allowed_fails` fallos sale del pool `cooldown_time` segundos.
            "allowed_fails": litellm_opts.get("allowed_fails", 2),
            "cooldown_time": litellm_opts.get("cooldown_time", 30),
            "enable_pre_call_checks": True,
            "redis_host": "os.environ/REDIS_HOST",
            "redis_port": "os.environ/REDIS_PORT",
        },

        "general_settings": {
            "master_key": "os.environ/LITELLM_MASTER_KEY",
            "database_url": "os.environ/DATABASE_URL",
            "store_model_in_db": True,
            # PRIVACIDAD: nunca persistir prompts ni respuestas en SpendLogs.
            "store_prompts_in_spend_logs": False,
            "disable_spend_logs": False,
            "database_connection_pool_limit": 20,
            "proxy_batch_write_at": 10,
        },

        "litellm_settings": {
            "drop_params": True,
            "set_verbose": False,
            # PRIVACIDAD: desactiva el logging del contenido de los mensajes.
            "turn_off_message_logging": True,
            "request_timeout": litellm_opts.get("request_timeout", 600),
            "num_retries": litellm_opts.get("num_retries", 2),
            "telemetry": False,
        },
    }


def load_gateway_section(config_path: Path) -> Dict[str, Any]:
    if not config_path.exists():
        return {}
    try:
        with config_path.open("r", encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("gateway", {}) or {}
    except Exception:  # noqa: BLE001 - el render no debe romper el arranque
        return {}


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent

    parser = argparse.ArgumentParser(description="Render del config.yaml de LiteLLM Proxy.")
    parser.add_argument("--endpoints-json", default="[]",
                        help="Lista de endpoints en JSON (o ruta a un archivo .json)")
    parser.add_argument("--strategy", default="latency-based-routing",
                        help="Estrategia de balanceo del router de LiteLLM")
    parser.add_argument("--config", default=str(repo_root / "config_global.yaml"),
                        help="Contrato central, para heredar los ajustes de gateway.litellm")
    parser.add_argument("--output", default=str(repo_root / "docker_images" / "gateway" / "litellm_config.yaml"),
                        help="Ruta del config.yaml a escribir")
    args = parser.parse_args()

    raw = (args.endpoints_json or "[]").strip() or "[]"

    # Admite tanto JSON inline como una ruta a archivo.
    candidate = Path(raw)
    if not raw.startswith(("[", "{")) and candidate.exists():
        raw = candidate.read_text(encoding="utf-8")

    try:
        endpoints = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"[ERROR] WORKER_ENDPOINTS no es JSON válido: {exc}", file=sys.stderr)
        return 1

    if isinstance(endpoints, dict):
        endpoints = [endpoints]

    gw = load_gateway_section(Path(args.config))
    strategy = args.strategy or gw.get("load_balancing_strategy", "latency-based-routing")
    config = build_config(endpoints, strategy, gw)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        f.write(HEADER)
        yaml.dump(config, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    total = len(config["model_list"])
    if total == 0:
        print(f"[WARNING] {out_path.name} generado SIN deployments. LiteLLM arrancará vacío; "
              f"corre 'python scripts/sync_endpoints.py --apply' tras levantar los workers.")
    else:
        modelos = sorted({m["model_name"] for m in config["model_list"]})
        print(f"[OK] {out_path.name}: {total} deployment(s) en {len(modelos)} modelo(s) "
              f"-> {', '.join(modelos)} | estrategia: {strategy}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
