#!/usr/bin/env python3
"""
==============================================================================
Sooniverse Infra - Test de capacidades reales por modelo
==============================================================================
Cada workload declara en config_global.yaml qué capacidades tiene de verdad
('capacidades: {vision, tool_calling, tool_call_parser}'), y esas banderas son
las que scripts/generate_infra.py usa para decidir qué flags de vLLM activar
(--enable-auto-tool-choice, --limit-mm-per-prompt). Este script NO confía en
lo declarado: sondea el modelo YA DESPLEGADO, a través del Gateway público
-el mismo camino que un cliente real (Open WebUI, /v1/chat/completions)-, con
peticiones mínimas reales:

  - Visión: un mensaje con una imagen 1x1 real embebida en base64.
  - Tool calling: una petición con 'tools' + 'tool_choice: auto' real.

y compara el resultado observado contra lo declarado. Un mismatch peligroso
(declaraste que SÍ soporta algo, pero el modelo lo rechaza) es el error que
motivó este script: Open WebUI mandándole tool_choice="auto" a un modelo que
vLLM nunca arrancó con --enable-auto-tool-choice.

Uso:
    python scripts/test_model_capabilities.py
    python scripts/test_model_capabilities.py --config clients/acme/config_global.yaml
"""

import argparse
import base64
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import yaml
except ImportError:
    print("[ERROR] Falta 'pyyaml'. Ejecuta: pip install pyyaml")
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config_global.yaml"
IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")

# PNG de 1x1 píxel transparente: la imagen real más pequeña posible, evita
# depender de un archivo externo o de acceso a Internet para la prueba.
TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)

VISION_UNSUPPORTED_MARKERS = ("image", "multimodal", "mm_per_prompt", "vision", "video")
TOOL_CALLING_UNSUPPORTED_MARKERS = ("tool_choice", "tool choice", "tool-call-parser", "tool_call_parser")


@dataclass
class ProbeResult:
    supported: Optional[bool]  # True/False = concluyente, None = inconcluso (error inesperado)
    detail: str


def artifacts_dir_for(config_path: Path, config: Dict[str, Any]) -> Path:
    """Misma regla que generate_infra.artifacts_dir_for / verify_deployment.py
    (duplicada a propósito, ver esos docstrings): raíz del repo si --config es
    el config_global.yaml raíz, `.artifacts/<cliente>-<entorno>/` si no."""
    try:
        is_default_root_config = config_path.resolve() == DEFAULT_CONFIG_PATH.resolve()
    except OSError:
        is_default_root_config = False
    if is_default_root_config:
        return REPO_ROOT
    cliente = config["cliente"]
    return REPO_ROOT / ".artifacts" / f"{cliente['id']}-{cliente['entorno']}"


def _read_env_var(key: str) -> Optional[str]:
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    return None


def discover_gateway_ip(cliente: Dict[str, Any]) -> Optional[str]:
    gateway_cluster = f"sooniverse-{cliente['id']}-{cliente['entorno']}-gw"
    try:
        out = subprocess.run(["sky", "status", "--ip", gateway_cluster], capture_output=True, text=True, timeout=20)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if out.returncode != 0:
        return None
    lines = [l.strip() for l in out.stdout.strip().splitlines() if IPV4_RE.match(l.strip())]
    return lines[-1] if lines else None


def _http_post(url: str, payload: Dict[str, Any], headers: Dict[str, str], timeout: int = 30) -> Dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json", **headers}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                return {"status": resp.status, "json": json.loads(body)}
            except json.JSONDecodeError:
                return {"status": resp.status, "text": body}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return {"status": exc.code, "json": json.loads(body)}
        except json.JSONDecodeError:
            return {"status": exc.code, "text": body}
    except (urllib.error.URLError, TimeoutError) as exc:
        return {"error": str(exc)}


def _error_message(resp: Dict[str, Any]) -> str:
    if "error" in resp:
        return str(resp["error"])
    body = resp.get("json", resp.get("text", ""))
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)
        return str(err or body)
    return str(body)


def probe_vision(gateway_ip: str, model: str, headers: Dict[str, str]) -> ProbeResult:
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "¿Qué ves en la imagen? Responde en una palabra."},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{TINY_PNG_B64}"}},
            ],
        }],
        "max_tokens": 8,
    }
    resp = _http_post(f"http://{gateway_ip}/v1/chat/completions", payload, headers)

    if resp.get("status") == 200 and "choices" in resp.get("json", {}):
        return ProbeResult(True, "aceptó la imagen y respondió")

    msg = _error_message(resp)
    if any(marker in msg.lower() for marker in VISION_UNSUPPORTED_MARKERS):
        return ProbeResult(False, msg)
    return ProbeResult(None, f"error inesperado (no concluyente): {msg}")


def probe_tool_calling(gateway_ip: str, model: str, headers: Dict[str, str]) -> ProbeResult:
    tools = [{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Obtiene el clima actual de una ciudad",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }]
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "¿Qué clima hace en Bogotá?"}],
        "tools": tools,
        "tool_choice": "auto",
        "max_tokens": 16,
    }
    resp = _http_post(f"http://{gateway_ip}/v1/chat/completions", payload, headers)

    if resp.get("status") == 200 and "choices" in resp.get("json", {}):
        return ProbeResult(True, "aceptó tools+tool_choice y respondió")

    msg = _error_message(resp)
    if any(marker in msg.lower() for marker in TOOL_CALLING_UNSUPPORTED_MARKERS):
        return ProbeResult(False, msg)
    return ProbeResult(None, f"error inesperado (no concluyente): {msg}")


def _fmt(result: ProbeResult) -> str:
    if result.supported is True:
        return "SI"
    if result.supported is False:
        return "NO"
    return "?"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sondea cada modelo desplegado para confirmar sus capacidades reales (visión, tool calling)."
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    args = parser.parse_args()

    config_path = Path(args.config)
    with config_path.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    cliente = config["cliente"]
    gateway_ip = discover_gateway_ip(cliente)
    if not gateway_ip:
        print(f"[N/A] No se encontró una IP de Gateway activa para '{cliente['id']}-{cliente['entorno']}'.")
        return 0

    master_key = _read_env_var("LITELLM_MASTER_KEY")
    headers = {"Authorization": f"Bearer {master_key}"} if master_key else {}

    print(f"\n{'MODELO':<26}{'CAPACIDAD':<15}{'DECLARADO':<11}{'PROBADO':<9}DETALLE")
    print("-" * 100)

    mismatches_peligrosos = []
    for wl in config.get("workloads", []):
        model = wl.get("nombre_publico", wl["id"])
        capacidades = wl.get("capacidades", {})
        declared_vision = capacidades.get("vision", True)
        declared_tools = capacidades.get("tool_calling", False)

        vision_result = probe_vision(gateway_ip, model, headers)
        tools_result = probe_tool_calling(gateway_ip, model, headers)

        for capacidad, declarado, probado in (
            ("vision", declared_vision, vision_result),
            ("tool_calling", declared_tools, tools_result),
        ):
            print(f"{model:<26}{capacidad:<15}{str(declarado):<11}{_fmt(probado):<9}{probado.detail}")
            if declarado and probado.supported is False:
                mismatches_peligrosos.append(
                    f"{model}.{capacidad}: declaraste 'true' pero el modelo lo rechazó -> {probado.detail}"
                )

    if mismatches_peligrosos:
        print("\n[FALLO] Capacidades declaradas que el modelo NO soporta de verdad:")
        for m in mismatches_peligrosos:
            print(f"  - {m}")
        print("\nCorrige 'capacidades' en config_global.yaml para el workload afectado y re-despliega.")
        return 1

    print("\n[OK] Todas las capacidades declaradas coinciden con lo que el modelo soporta de verdad.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
