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
  - response_format=json_object: lo que usan las tareas automáticas de Open
    WebUI (título, tags, autocompletado...); un vLLM sin soporte de grammar
    estructurada las rechaza con 400, tumbando esas funciones en la interfaz.
  - streaming: confirma que llegan chunks SSE reales.

y compara el resultado observado contra lo declarado. Un mismatch peligroso
(declaraste que SÍ soporta algo, pero el modelo lo rechaza) es el error que
motivó este script: Open WebUI mandándole tool_choice="auto" a un modelo que
vLLM nunca arrancó con --enable-auto-tool-choice.

Política fail-closed (ver database/003_model_capabilities.sql): un sondeo
inconcluso (error de red, timeout, worker aún arrancando) NUNCA se trata como
"soportado". Este script reintenta automáticamente los sondeos inconclusos
antes de darlos por definitivos, para absorber el arranque lento de vLLM.

Uso:
    python scripts/test_model_capabilities.py
    python scripts/test_model_capabilities.py --config clients/acme/config_global.yaml
    python scripts/test_model_capabilities.py --write-db          # persiste en sooniverse.model_capability
    python scripts/test_model_capabilities.py --json -             # imprime JSON a stdout
    python scripts/test_model_capabilities.py --gateway-ip 1.2.3.4 # sin depender de 'sky status'
"""

import argparse
import base64
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import yaml
except ImportError:
    print("[ERROR] Falta 'pyyaml'. Ejecuta: pip install pyyaml")
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

DEFAULT_CONFIG_PATH = REPO_ROOT / "config_global.yaml"
DEFAULT_ENV_PATH = REPO_ROOT / ".env"
IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")

# PNG de 1x1 píxel transparente: la imagen real más pequeña posible, evita
# depender de un archivo externo o de acceso a Internet para la prueba.
TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)

VISION_UNSUPPORTED_MARKERS = ("image", "multimodal", "mm_per_prompt", "vision", "video")
TOOL_CALLING_UNSUPPORTED_MARKERS = ("tool_choice", "tool choice", "tool-call-parser", "tool_call_parser")
JSON_OBJECT_UNSUPPORTED_MARKERS = (
    "response_format", "json_object", "json_schema", "guided_grammar", "guided_json",
    "xgrammar", "structured output", "outlines",
)
STREAMING_UNSUPPORTED_MARKERS = ("stream", "streaming not supported")

RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (5, 15, 30)


@dataclass
class ProbeResult:
    supported: Optional[bool]  # True/False = concluyente, None = inconcluso (error inesperado)
    detail: str


def artifacts_dir_for(config_path: Path, config: Dict[str, Any]) -> Path:
    """Misma regla que generate_infra.artifacts_dir_for / sync_endpoints.configure_paths_for:
    raíz del repo si --config es el config_global.yaml raíz, `.artifacts/<cliente>-<entorno>/`
    si no. Usada aquí SOLO para dónde escribir los artefactos de esta corrida
    (--json, .sooniverse_capabilities.json que lee render_gateway_stack.py);
    la conexión a PostgreSQL y LITELLM_MASTER_KEY siguen leyéndose del `.env`
    raíz (una única BD/gateway compartidos entre clientes en este repo, igual
    que hacen db_setup.py/sync_endpoints.py)."""
    try:
        is_default_root_config = config_path.resolve() == DEFAULT_CONFIG_PATH.resolve()
    except OSError:
        is_default_root_config = False
    if is_default_root_config:
        return REPO_ROOT
    cliente = config["cliente"]
    out = REPO_ROOT / ".artifacts" / f"{cliente['id']}-{cliente['entorno']}"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _read_env_var(key: str, env_path: Path = DEFAULT_ENV_PATH) -> Optional[str]:
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


def _is_client_error(resp: Dict[str, Any]) -> bool:
    status = resp.get("status")
    return isinstance(status, int) and 400 <= status < 500


def _looks_unsupported(resp: Dict[str, Any], markers: Tuple[str, ...]) -> bool:
    """Endurecido respecto a la versión original: antes clasificaba como 'NO
    soportado' cualquier error cuyo mensaje mencionara un marcador, sin
    importar el código HTTP. Un 5xx transitorio (worker caído, timeout de
    LiteLLM) que por casualidad mencione 'image' ya no cuenta como rechazo
    real -exige además que el HTTP sea 4xx (error del cliente, no del server)."""
    if not _is_client_error(resp):
        return False
    msg = _error_message(resp).lower()
    return any(marker in msg for marker in markers)


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
    if _looks_unsupported(resp, VISION_UNSUPPORTED_MARKERS):
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
    if _looks_unsupported(resp, TOOL_CALLING_UNSUPPORTED_MARKERS):
        return ProbeResult(False, msg)
    return ProbeResult(None, f"error inesperado (no concluyente): {msg}")


def probe_json_object(gateway_ip: str, model: str, headers: Dict[str, str]) -> ProbeResult:
    """Lo que usan las tareas automáticas de Open WebUI (título/tags/autocompletado/
    follow-up: ver render_gateway_stack.py). vLLM sin backend de grammar
    (xgrammar/outlines) rechaza response_format=json_object con 400."""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Responde solo con un objeto JSON: {\"ok\": true}"}],
        "response_format": {"type": "json_object"},
        "max_tokens": 16,
    }
    resp = _http_post(f"http://{gateway_ip}/v1/chat/completions", payload, headers)

    if resp.get("status") == 200 and "choices" in resp.get("json", {}):
        return ProbeResult(True, "aceptó response_format=json_object y respondió")

    msg = _error_message(resp)
    if _looks_unsupported(resp, JSON_OBJECT_UNSUPPORTED_MARKERS):
        return ProbeResult(False, msg)
    return ProbeResult(None, f"error inesperado (no concluyente): {msg}")


def probe_streaming(gateway_ip: str, model: str, headers: Dict[str, str]) -> ProbeResult:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Cuenta hasta 3."}],
        "stream": True,
        "max_tokens": 8,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"http://{gateway_ip}/v1/chat/completions",
        data=data,
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                return ProbeResult(None, f"error inesperado (no concluyente): HTTP {resp.status}")
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if line.startswith("data:") and line != "data: [DONE]":
                    return ProbeResult(True, "recibió al menos un chunk SSE")
            return ProbeResult(False, "conexión cerrada sin chunks SSE")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        resp_dict = {"status": exc.code, "text": body}
        try:
            resp_dict = {"status": exc.code, "json": json.loads(body)}
        except json.JSONDecodeError:
            pass
        msg = _error_message(resp_dict)
        if _looks_unsupported(resp_dict, STREAMING_UNSUPPORTED_MARKERS):
            return ProbeResult(False, msg)
        return ProbeResult(None, f"error inesperado (no concluyente): {msg}")
    except (urllib.error.URLError, TimeoutError) as exc:
        return ProbeResult(None, f"error inesperado (no concluyente): {exc}")


def _probe_with_retry(
    probe_fn: Callable[..., ProbeResult], *args: Any,
    attempts: int = RETRY_ATTEMPTS, backoff: Tuple[int, ...] = RETRY_BACKOFF_SECONDS,
) -> Tuple[ProbeResult, int]:
    """Solo reintenta sondeos INCONCLUSOS (supported is None) -absorbe el
    arranque lento de vLLM tras un despliegue reciente-. Un resultado
    concluyente (True/False) nunca se reintenta: repetir una respuesta
    definitiva no la vuelve más definitiva."""
    result = probe_fn(*args)
    for attempt in range(1, attempts):
        if result.supported is not None:
            return result, attempt
        time.sleep(backoff[min(attempt - 1, len(backoff) - 1)])
        result = probe_fn(*args)
    return result, attempts


def _fmt(result: ProbeResult) -> str:
    if result.supported is True:
        return "SI"
    if result.supported is False:
        return "NO"
    return "?"


def write_capabilities_to_db(config: Dict[str, Any], rows: List[Dict[str, Any]]) -> bool:
    """UPSERT en sooniverse.model_capability. Best-effort: si la BD no está
    accesible, se avisa pero no se aborta (el sondeo en sí ya se hizo)."""
    try:
        from db_setup import DbSetupError, connect, resolve_db_config  # type: ignore[import-not-found]
    except ImportError as exc:
        print(f"[WARNING] No se pudo importar db_setup ({exc}); no se persiste en BD.")
        return False

    try:
        conn = connect(resolve_db_config(DEFAULT_ENV_PATH))
    except DbSetupError as exc:
        print(f"[WARNING] Sin acceso a PostgreSQL ({exc}); no se persiste en BD.")
        return False

    cliente = config["cliente"]
    try:
        with conn.cursor() as cur:
            for row in rows:
                cur.execute(
                    """
                    INSERT INTO sooniverse.model_capability
                        (client_id, environment, model_public_name, workload_id,
                         declared_vision, declared_tool_calling, tool_call_parser,
                         probed_vision, probed_tool_calling, probed_json_object, probed_streaming,
                         max_model_len, probe_detail, probe_attempts, probed_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (client_id, environment, model_public_name) DO UPDATE SET
                        workload_id            = EXCLUDED.workload_id,
                        declared_vision         = EXCLUDED.declared_vision,
                        declared_tool_calling   = EXCLUDED.declared_tool_calling,
                        tool_call_parser        = EXCLUDED.tool_call_parser,
                        probed_vision           = EXCLUDED.probed_vision,
                        probed_tool_calling     = EXCLUDED.probed_tool_calling,
                        probed_json_object      = EXCLUDED.probed_json_object,
                        probed_streaming        = EXCLUDED.probed_streaming,
                        max_model_len           = EXCLUDED.max_model_len,
                        probe_detail            = EXCLUDED.probe_detail,
                        probe_attempts          = EXCLUDED.probe_attempts,
                        probed_at               = NOW()
                    """,
                    (
                        cliente["id"], cliente["entorno"], row["model"], row["workload_id"],
                        row["declared_vision"], row["declared_tools"], row.get("tool_call_parser"),
                        row["vision"].supported, row["tools"].supported,
                        row["json_object"].supported, row["streaming"].supported,
                        row.get("max_model_len"),
                        json.dumps({
                            "vision": row["vision"].detail,
                            "tool_calling": row["tools"].detail,
                            "json_object": row["json_object"].detail,
                            "streaming": row["streaming"].detail,
                        }),
                        row["attempts"],
                    ),
                )
        conn.commit()
        print(f"[OK] {len(rows)} modelo(s) persistido(s) en sooniverse.model_capability")
        return True
    except Exception as exc:  # noqa: BLE001 - persistencia best-effort
        conn.rollback()
        print(f"[WARNING] Falló la escritura en sooniverse.model_capability: {exc}")
        return False
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sondea cada modelo desplegado para confirmar sus capacidades reales "
                    "(visión, tool calling, response_format=json_object, streaming)."
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--gateway-ip", default=None,
                        help="IP pública del Gateway; si se omite, se descubre con 'sky status --ip'")
    parser.add_argument("--write-db", action="store_true",
                        help="Persiste el resultado en sooniverse.model_capability (lo usa "
                             "generate_infra.py en la fase 'capabilities'; en uso manual es opt-in)")
    parser.add_argument("--json", default=None,
                        help="Escribe el resultado como JSON en esta ruta ('-' para stdout)")
    args = parser.parse_args()

    config_path = Path(args.config)
    with config_path.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    cliente = config["cliente"]
    gateway_ip = args.gateway_ip or discover_gateway_ip(cliente)
    if not gateway_ip:
        print(f"[N/A] No se encontró una IP de Gateway activa para '{cliente['id']}-{cliente['entorno']}'.")
        return 0

    master_key = _read_env_var("LITELLM_MASTER_KEY")
    headers = {"Authorization": f"Bearer {master_key}"} if master_key else {}

    print(f"\n{'MODELO':<26}{'CAPACIDAD':<15}{'DECLARADO':<11}{'PROBADO':<9}{'INTENTOS':<10}DETALLE")
    print("-" * 110)

    mismatches_peligrosos = []
    rows_for_db: List[Dict[str, Any]] = []

    for wl in config.get("workloads", []):
        model = wl.get("nombre_publico", wl["id"])
        capacidades = wl.get("capacidades", {})
        declared_vision = capacidades.get("vision", True)
        declared_tools = capacidades.get("tool_calling", False)

        vision_result, vision_attempts = _probe_with_retry(probe_vision, gateway_ip, model, headers)
        tools_result, tools_attempts = _probe_with_retry(probe_tool_calling, gateway_ip, model, headers)
        json_result, json_attempts = _probe_with_retry(probe_json_object, gateway_ip, model, headers)
        stream_result, stream_attempts = _probe_with_retry(probe_streaming, gateway_ip, model, headers)

        for capacidad, declarado, probado, intentos in (
            ("vision", declared_vision, vision_result, vision_attempts),
            ("tool_calling", declared_tools, tools_result, tools_attempts),
            ("json_object", "n/a", json_result, json_attempts),
            ("streaming", "n/a", stream_result, stream_attempts),
        ):
            print(f"{model:<26}{capacidad:<15}{str(declarado):<11}{_fmt(probado):<9}{intentos:<10}{probado.detail}")
            if declarado is True and probado.supported is False:
                mismatches_peligrosos.append(
                    f"{model}.{capacidad}: declaraste 'true' pero el modelo lo rechazó -> {probado.detail}"
                )

        rows_for_db.append({
            "model": model,
            "workload_id": wl["id"],
            "declared_vision": bool(declared_vision),
            "declared_tools": bool(declared_tools),
            "tool_call_parser": capacidades.get("tool_call_parser"),
            "max_model_len": (wl.get("asignacion_fraccional", {}) or {}).get("max_model_len"),
            "vision": vision_result,
            "tools": tools_result,
            "json_object": json_result,
            "streaming": stream_result,
            "attempts": max(vision_attempts, tools_attempts, json_attempts, stream_attempts),
        })

    if mismatches_peligrosos:
        print("\n[FALLO] Capacidades declaradas que el modelo NO soporta de verdad:")
        for m in mismatches_peligrosos:
            print(f"  - {m}")
        print("\nCorrige 'capacidades' en config_global.yaml para el workload afectado y re-despliega.")

    if args.write_db:
        write_capabilities_to_db(config, rows_for_db)

    if args.json:
        payload = {
            "gateway_ip": gateway_ip,
            "models": [
                {
                    "model": r["model"],
                    "workload_id": r["workload_id"],
                    "declared_vision": r["declared_vision"],
                    "declared_tool_calling": r["declared_tools"],
                    "effective_vision": r["declared_vision"] and r["vision"].supported is True,
                    "effective_tool_calling": r["declared_tools"] and r["tools"].supported is True,
                    "effective_json_object": r["json_object"].supported is True,
                    "effective_streaming": r["streaming"].supported is True,
                    "attempts": r["attempts"],
                }
                for r in rows_for_db
            ],
        }
        text = json.dumps(payload, indent=2, ensure_ascii=False)
        if args.json == "-":
            print(text)
        else:
            out_path = Path(args.json)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(text, encoding="utf-8")
            print(f"[OK] Resultado escrito en {out_path}")

    if mismatches_peligrosos:
        return 1

    print("\n[OK] Todas las capacidades declaradas coinciden con lo que el modelo soporta de verdad.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
