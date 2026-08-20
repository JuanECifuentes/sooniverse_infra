#!/usr/bin/env python3
"""
==============================================================================
Sooniverse Infra - Benchmark de capacidad (rampa acotada)
==============================================================================
Responde con datos medidos, no estimados: "¿cuántas peticiones y cuántos tokens
por minuto aguanta esta infraestructura antes de degradar la respuesta?".

Sube la concurrencia por niveles ([1,2,4,8,16] por defecto) y para en cuanto
aparece la RODILLA: la latencia p95 se dispara respecto al nivel 1, los errores
superan el umbral, o el throughput deja de crecer. NO busca el punto de rotura
-la rampa es acotada a propósito, con un presupuesto de segundos que el
validador del contrato hace cumplir-.

DOS MODOS EN UN SOLO ARCHIVO
  driver (por defecto)  Empuja este script al Gateway por scp y lo ejecuta ahí
                        con --local vía 'sky exec'. Recoge el JSON de vuelta y
                        LO PERSISTE en sooniverse.capacity_benchmark.
  runner (--local)      Mide contra http://127.0.0.1 y escupe el JSON entre
                        centinelas. No toca la base de datos.

  Se mide DESDE EL GATEWAY porque fuera de la VPC el RTT del ISP del operador
  domina el TTFT y limita la concurrencia real: mediríamos la conexión de quien
  lanza el script, no la infraestructura. Desde el Gateway el camino es
  127.0.0.1:80 -> nginx -> litellm -> worker, exactamente el de Open WebUI.

TRÁFICO SINTÉTICO Y MÉTRICAS DE NEGOCIO
  El benchmark emite tráfico real que LiteLLM registra en SpendLogs. Para que no
  contamine el panel, usa una API Key EFÍMERA (duration 1h, borrada al terminar)
  que queda registrada con proposito='benchmark' en sooniverse.api_key_registry.
  El panel la excluye por defecto.

Uso:
    python scripts/benchmark_capacity.py                        # dry-run del plan
    python scripts/benchmark_capacity.py --write-db --json out.json
    python scripts/benchmark_capacity.py --local --gateway-url http://127.0.0.1
    python scripts/benchmark_capacity.py --niveles 1,4,16 --segundos-por-nivel 10
"""

import argparse
import json
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml
except ImportError:
    print("[ERROR] Falta 'pyyaml'. Ejecuta: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

DEFAULT_CONFIG_PATH = REPO_ROOT / "config_global.yaml"
DEFAULT_ENV_PATH = REPO_ROOT / ".env"
REMOTE_ROOT = "/home/ubuntu/sooniverse_infra"
IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_REMOTE_LINE_RE = re.compile(r"^\([^)]*pid=\d+\)\s?(.*)$")

# El runner delimita su payload para que ni un [WARNING] intercalado ni el eco
# de 'sky exec' rompan el parseo del driver.
SENTINEL_BEGIN = "SOONIVERSE_BENCH_JSON_BEGIN"
SENTINEL_END = "SOONIVERSE_BENCH_JSON_END"

DEFAULTS = {
    "niveles_concurrencia": [1, 2, 4, 8, 16],
    "segundos_por_nivel": 20,
    "warmup_segundos": 10,
    "prompt_tokens_objetivo": 512,
    "max_tokens": 128,
    "presupuesto_segundos": 240,
    "umbral_p95_degradacion": 3.0,
    "umbral_error_pct": 5.0,
    "ganancia_minima_throughput_pct": 10.0,
    "factor_usuarios_por_slot": 8,
    "api_key_alias": "sooniverse-benchmark",
    "origen": "gateway",
}

# Texto de relleno para construir prompts de un tamaño objetivo. ~4 caracteres
# por token es la regla de oro habitual para modelos tipo BPE.
CHARS_POR_TOKEN = 4
_LOREM = (
    "El sistema de inferencia procesa solicitudes concurrentes y reparte la carga "
    "entre los nodos disponibles segun la estrategia configurada en el balanceador. "
)


# =============================================================================
# HELPERS COMPARTIDOS (mismo contrato que test_model_capabilities.py)
# =============================================================================
def _read_env_var(key: str, env_path: Path = DEFAULT_ENV_PATH) -> Optional[str]:
    if not env_path.exists():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    return None


def sky_bin() -> Optional[str]:
    return shutil.which("sky")


def gateway_cluster_for(cliente: Dict[str, Any]) -> str:
    return f"sooniverse-{cliente['id']}-{cliente['entorno']}-gw"


def discover_gateway_ip(cliente: Dict[str, Any]) -> Optional[str]:
    sky = sky_bin()
    if not sky:
        return None
    try:
        out = subprocess.run([sky, "status", "--ip", gateway_cluster_for(cliente)],
                             capture_output=True, text=True, timeout=20)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if out.returncode != 0:
        return None
    # 'sky status --ip' imprime "Cluster(s) not found" por stdout cuando no hay
    # clúster: filtrar por forma de IPv4 evita tomarlo por una dirección.
    lines = [l.strip() for l in out.stdout.strip().splitlines() if IPV4_RE.match(l.strip())]
    return lines[-1] if lines else None


def _strip_sky_exec_echo(raw_output: str) -> str:
    """Misma función que sync_endpoints.py:93: 'sky exec' imprime el comando
    como eco ANTES de ejecutarlo y prefija cada línea remota con '(nombre,
    pid=N)'. Sin filtrar ambas capas, un centinela que aparece dentro del
    propio comando enviado se detectaría como si lo hubiera impreso el runner."""
    clean = _ANSI_RE.sub("", raw_output or "")
    return "\n".join(
        m.group(1) for line in clean.splitlines()
        if (m := _REMOTE_LINE_RE.match(line.strip()))
    )


def _parse_sentinel_json(texto: str) -> Optional[Dict[str, Any]]:
    """Extrae el payload delimitado. Tolera ruido antes y después."""
    inicio = texto.find(SENTINEL_BEGIN)
    fin = texto.find(SENTINEL_END, inicio + 1) if inicio >= 0 else -1
    if inicio < 0 or fin < 0:
        return None
    crudo = texto[inicio + len(SENTINEL_BEGIN):fin].strip()
    try:
        return json.loads(crudo)
    except json.JSONDecodeError:
        return None


def _http_json(url: str, payload: Optional[Dict[str, Any]], headers: Dict[str, str],
               method: str = "POST", timeout: int = 30) -> Dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json", **headers}, method=method
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
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"error": str(exc)}


# =============================================================================
# MODELO DE DATOS
# =============================================================================
@dataclass
class RequestSample:
    ok: bool
    status: Optional[int]
    ttft_ms: Optional[float]
    total_ms: float
    completion_tokens: int
    error: Optional[str] = None


@dataclass
class NivelResultado:
    concurrencia: int
    duracion_seg: float
    muestras: List[RequestSample] = field(default_factory=list)

    @property
    def exitos(self) -> List[RequestSample]:
        return [m for m in self.muestras if m.ok]

    @property
    def peticiones(self) -> int:
        return len(self.muestras)

    @property
    def errores(self) -> int:
        return self.peticiones - len(self.exitos)

    @property
    def tasa_error_pct(self) -> float:
        return (self.errores / self.peticiones * 100) if self.peticiones else 0.0

    @property
    def rps(self) -> float:
        return (len(self.exitos) / self.duracion_seg) if self.duracion_seg > 0 else 0.0

    @property
    def tokens_salida_por_seg(self) -> float:
        total = sum(m.completion_tokens for m in self.exitos)
        return (total / self.duracion_seg) if self.duracion_seg > 0 else 0.0

    @property
    def itl_medio_ms(self) -> Optional[float]:
        """Inter-token latency: cuánto tarda cada token después del primero.
        Es la métrica que percibe el usuario como 'velocidad de escritura'."""
        valores = [
            (m.total_ms - m.ttft_ms) / (m.completion_tokens - 1)
            for m in self.exitos
            if m.ttft_ms is not None and m.completion_tokens > 1
        ]
        return round(statistics.fmean(valores), 2) if valores else None

    def p(self, q: float) -> Optional[int]:
        return _percentil([m.total_ms for m in self.exitos], q)

    def ttft_p(self, q: float) -> Optional[int]:
        return _percentil([m.ttft_ms for m in self.exitos if m.ttft_ms is not None], q)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "concurrencia": self.concurrencia,
            "peticiones": self.peticiones,
            "exitos": len(self.exitos),
            "errores": self.errores,
            "tasa_error_pct": round(self.tasa_error_pct, 3),
            "p50_ms": self.p(0.50),
            "p95_ms": self.p(0.95),
            "p99_ms": self.p(0.99),
            "max_ms": self.p(1.0),
            "ttft_p50_ms": self.ttft_p(0.50),
            "ttft_p95_ms": self.ttft_p(0.95),
            "rps": round(self.rps, 3),
            "tokens_salida_por_seg": round(self.tokens_salida_por_seg, 2),
            "itl_medio_ms": self.itl_medio_ms,
            "duracion_seg": round(self.duracion_seg, 2),
        }


def _percentil(valores: List[float], q: float) -> Optional[int]:
    """percentile_disc: devuelve un valor REALMENTE observado, sin interpolar.
    Misma semántica que la del esquema SQL (database/004_usage_analytics.sql),
    para que los números del benchmark y los del panel sean comparables."""
    if not valores:
        return None
    ordenados = sorted(valores)
    idx = max(0, min(len(ordenados) - 1, int(-(-q * len(ordenados) // 1)) - 1))
    return int(round(ordenados[idx]))


# =============================================================================
# RUNNER: generación de carga
# =============================================================================
def build_prompt(tokens_objetivo: int, nonce: str) -> str:
    """Prompt de tamaño objetivo con un prefijo ÚNICO por petición.

    El nonce no es decorativo: vLLM cachea prefijos, así que sin él la segunda
    petición y siguientes reutilizarían el KV ya calculado y estaríamos midiendo
    el caché en vez de la GPU -el throughput saldría inflado varias veces-.
    """
    relleno = _LOREM * max(1, (tokens_objetivo * CHARS_POR_TOKEN) // len(_LOREM) + 1)
    cuerpo = relleno[: tokens_objetivo * CHARS_POR_TOKEN]
    return f"[{nonce}] {cuerpo}\n\nResume el texto anterior."


def _stream_completion(url: str, payload: Dict[str, Any], headers: Dict[str, str],
                       timeout: int) -> RequestSample:
    """Una petición en streaming, cronometrada.

    Se usa stream=True para poder medir el TTFT (el primer chunk SSE con
    contenido). Sin streaming solo se obtiene la latencia total y es imposible
    separar "el modelo tarda en arrancar" de "el modelo genera lento", que es
    justo la distinción que hace falta para diagnosticar la saturación.
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json", **headers}, method="POST"
    )
    t0 = time.perf_counter()
    ttft: Optional[float] = None
    tokens = 0
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                linea = raw.decode("utf-8", errors="replace").strip()
                if not linea.startswith("data:"):
                    continue
                cuerpo = linea[5:].strip()
                if cuerpo == "[DONE]":
                    break
                try:
                    chunk = json.loads(cuerpo)
                except json.JSONDecodeError:
                    continue
                delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
                if delta.get("content"):
                    tokens += 1
                    if ttft is None:
                        ttft = (time.perf_counter() - t0) * 1000
            total = (time.perf_counter() - t0) * 1000
            return RequestSample(True, resp.status, ttft, total, tokens)
    except urllib.error.HTTPError as exc:
        total = (time.perf_counter() - t0) * 1000
        detalle = exc.read().decode("utf-8", errors="replace")[:200]
        return RequestSample(False, exc.code, None, total, 0, detalle)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        total = (time.perf_counter() - t0) * 1000
        return RequestSample(False, None, None, total, 0, str(exc)[:200])


def correr_nivel(gateway_url: str, model: str, headers: Dict[str, str], concurrencia: int,
                 segundos: int, prompt_tokens: int, max_tokens: int,
                 descartar: bool = False) -> NivelResultado:
    """Bucle cerrado: `concurrencia` hilos lanzando peticiones sin pausa durante
    `segundos`. ThreadPoolExecutor y no asyncio porque urllib es bloqueante:
    meterlo en un event loop exigiría run_in_executor (o sea, hilos igualmente)
    o reescribir el cliente HTTP sobre sockets crudos. Con N <= 32 y trabajo
    puramente de E/S, los hilos son la opción correcta."""
    url = f"{gateway_url.rstrip('/')}/v1/chat/completions"
    fin = time.monotonic() + segundos
    muestras: List[RequestSample] = []
    t0 = time.monotonic()

    def trabajador(slot: int) -> List[RequestSample]:
        propias: List[RequestSample] = []
        i = 0
        while time.monotonic() < fin:
            nonce = f"{slot}-{i}-{random.randint(0, 10**9)}"
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": build_prompt(prompt_tokens, nonce)}],
                "max_tokens": max_tokens,
                "temperature": 0,
                "stream": True,
            }
            propias.append(_stream_completion(url, payload, headers, timeout=segundos + 120))
            i += 1
        return propias

    with ThreadPoolExecutor(max_workers=concurrencia) as pool:
        for lote in pool.map(trabajador, range(concurrencia)):
            muestras.extend(lote)

    duracion = time.monotonic() - t0
    if descartar:
        return NivelResultado(concurrencia, duracion, [])
    return NivelResultado(concurrencia, duracion, muestras)


def evaluar_parada(nivel: NivelResultado, base: Optional[NivelResultado],
                   previo: Optional[NivelResultado], umbrales: Dict[str, float],
                   tiempo_restante: float) -> Optional[str]:
    """Decide si la rampa debe pararse tras este nivel. Devuelve el motivo o None."""
    if nivel.tasa_error_pct > umbrales["error_pct"]:
        return "errores"

    base_p95 = base.p(0.95) if base else None
    nivel_p95 = nivel.p(0.95)
    if base_p95 and nivel_p95 and nivel_p95 > base_p95 * umbrales["p95_factor"]:
        return "p95_degradado"

    # Si un nivel más de concurrencia ya no aporta throughput, la GPU está
    # saturada y todo lo que se añada es cola: seguir subiendo solo empeora la
    # latencia sin mover el techo.
    if previo and previo.tokens_salida_por_seg > 0:
        ganancia = (nivel.tokens_salida_por_seg - previo.tokens_salida_por_seg) \
            / previo.tokens_salida_por_seg * 100
        if ganancia < umbrales["ganancia_min_pct"]:
            return "saturacion_throughput"

    if tiempo_restante <= 0:
        return "presupuesto_agotado"
    return None


# =============================================================================
# RUNNER: API key efímera
# =============================================================================
def ensure_benchmark_key(gateway_url: str, master_key: str, alias: str,
                         modelos: List[str]) -> Optional[Dict[str, str]]:
    """Crea una API Key EFÍMERA para esta corrida.

    Es efímera por diseño y no reutilizable: la regla del proyecto es no
    persistir nunca una key en claro (api_key_registry solo guarda el hash), así
    que "recuperar la de la vez pasada" es imposible sin romperla. Se compensa
    con duration corta y presupuesto ínfimo.
    """
    payload = {
        "key_alias": f"{alias}-{uuid.uuid4().hex[:8]}",
        "models": modelos,
        "duration": "1h",
        "max_budget": 1.0,
        "metadata": {"gestionado_por": "sooniverse", "proposito": "benchmark"},
    }
    resp = _http_json(f"{gateway_url.rstrip('/')}/key/generate", payload,
                      {"Authorization": f"Bearer {master_key}"})
    cuerpo = resp.get("json") or {}
    key = cuerpo.get("key")
    if not key:
        print(f"[WARNING] No se pudo emitir la API key del benchmark "
              f"(status={resp.get('status')}); se usará la master key. El tráfico NO se "
              f"podrá excluir del panel.")
        return None
    return {
        "key": key,
        "token_hash": cuerpo.get("token") or cuerpo.get("token_id") or "",
        "alias": payload["key_alias"],
    }


def delete_benchmark_key(gateway_url: str, master_key: str, key: str) -> None:
    resp = _http_json(f"{gateway_url.rstrip('/')}/key/delete", {"keys": [key]},
                      {"Authorization": f"Bearer {master_key}"})
    if resp.get("status") != 200:
        print(f"[WARNING] No se pudo borrar la API key del benchmark "
              f"(status={resp.get('status')}). Caducará sola en 1 h.")


# =============================================================================
# RUNNER: orquestación de la corrida
# =============================================================================
def run_benchmark(gateway_url: str, wl: Dict[str, Any], cap: Dict[str, Any],
                  master_key: str) -> Dict[str, Any]:
    modelo = wl.get("nombre_publico", wl["id"])
    niveles = cap["niveles_concurrencia"]
    umbrales = {
        "error_pct": float(cap["umbral_error_pct"]),
        "p95_factor": float(cap["umbral_p95_degradacion"]),
        "ganancia_min_pct": float(cap["ganancia_minima_throughput_pct"]),
    }

    bench_key = ensure_benchmark_key(gateway_url, master_key, cap["api_key_alias"], [modelo])
    headers = {"Authorization": f"Bearer {(bench_key or {}).get('key', master_key)}"}

    started = time.time()
    t_inicio = time.monotonic()
    presupuesto = float(cap["presupuesto_segundos"])
    curva: List[NivelResultado] = []
    motivo = "nivel_maximo"

    try:
        if cap["warmup_segundos"] > 0:
            print(f"[bench] warmup {cap['warmup_segundos']}s (resultados descartados)...")
            correr_nivel(gateway_url, modelo, headers, 1, cap["warmup_segundos"],
                         cap["prompt_tokens_objetivo"], cap["max_tokens"], descartar=True)

        for nivel_c in niveles:
            restante = presupuesto - (time.monotonic() - t_inicio)
            if restante < cap["segundos_por_nivel"]:
                motivo = "presupuesto_agotado"
                print(f"[bench] presupuesto agotado antes del nivel {nivel_c}; se para.")
                break

            print(f"[bench] nivel concurrencia={nivel_c} durante {cap['segundos_por_nivel']}s...")
            res = correr_nivel(gateway_url, modelo, headers, nivel_c, cap["segundos_por_nivel"],
                               cap["prompt_tokens_objetivo"], cap["max_tokens"])
            curva.append(res)
            print(f"[bench]   {res.peticiones} pet · {res.rps:.2f} rps · "
                  f"p95={res.p(0.95)}ms · errores={res.tasa_error_pct:.1f}%")

            restante = presupuesto - (time.monotonic() - t_inicio)
            parada = evaluar_parada(res, curva[0] if curva else None,
                                    curva[-2] if len(curva) > 1 else None,
                                    umbrales, restante)
            if parada:
                motivo = parada
                print(f"[bench] parada: {parada}")
                break
    finally:
        if bench_key:
            delete_benchmark_key(gateway_url, master_key, bench_key["key"])

    return _resumir(curva, motivo, wl, cap, bench_key, started, time.monotonic() - t_inicio)


def _resumir(curva: List[NivelResultado], motivo: str, wl: Dict[str, Any], cap: Dict[str, Any],
             bench_key: Optional[Dict[str, str]], started: float,
             duracion: float) -> Dict[str, Any]:
    """La rodilla es el ÚLTIMO nivel que no disparó ninguna condición de parada.
    Si la parada la disparó el propio último nivel, la rodilla es el anterior."""
    sanos = curva[:-1] if (motivo != "nivel_maximo" and len(curva) > 1) else curva
    rodilla = sanos[-1] if sanos else None
    base = curva[0] if curva else None
    factor = cap["factor_usuarios_por_slot"]
    frac = wl.get("asignacion_fraccional", {}) or {}
    conc = wl.get("concurrencia", {}) or {}

    return {
        "run_id": str(uuid.uuid4()),
        "workload_id": wl["id"],
        "model_public_name": wl.get("nombre_publico", wl["id"]),
        "configuracion": {
            "instance_type": wl.get("tipo_instancia"),
            "accelerator": wl.get("accelerator"),
            "gpu_count": wl.get("cantidad_gpus"),
            "replicas": wl.get("replicas", 1),
            "max_num_seqs": conc.get("max_num_seqs"),
            "max_num_batched_tokens": conc.get("max_num_batched_tokens"),
            "max_model_len": frac.get("max_model_len"),
            "gpu_memory_utilization": frac.get("gpu_memory_utilization"),
        },
        "parametros": {
            "niveles_concurrencia": cap["niveles_concurrencia"],
            "prompt_tokens_objetivo": cap["prompt_tokens_objetivo"],
            "max_tokens": cap["max_tokens"],
            "segundos_por_nivel": cap["segundos_por_nivel"],
            "warmup_segundos": cap["warmup_segundos"],
            "origen": cap["origen"],
            "benchmark_key_alias": (bench_key or {}).get("alias"),
            "benchmark_key_hash": (bench_key or {}).get("token_hash"),
        },
        "resultado": {
            "concurrencia_rodilla": rodilla.concurrencia if rodilla else None,
            "rpm_sostenido": round(rodilla.rps * 60, 2) if rodilla else None,
            "tokens_salida_por_min": round(rodilla.tokens_salida_por_seg * 60, 2) if rodilla else None,
            "p50_base_ms": base.p(0.50) if base else None,
            "p95_base_ms": base.p(0.95) if base else None,
            "ttft_p50_base_ms": base.ttft_p(0.50) if base else None,
            "ttft_p95_base_ms": base.ttft_p(0.95) if base else None,
            "p95_rodilla_ms": rodilla.p(0.95) if rodilla else None,
            "ttft_p95_rodilla_ms": rodilla.ttft_p(0.95) if rodilla else None,
            "itl_medio_rodilla_ms": rodilla.itl_medio_ms if rodilla else None,
            "tasa_error_pct": round(rodilla.tasa_error_pct, 3) if rodilla else 0.0,
            "motivo_parada": motivo,
            "usuarios_estimados": int(rodilla.concurrencia * factor) if rodilla else None,
        },
        "curva": [n.as_dict() for n in curva],
        "notas": {
            "factor_usuarios_por_slot": factor,
            "explicacion_factor": (
                "usuarios_estimados = concurrencia_rodilla x factor. Una persona chateando "
                "mantiene una peticion activa solo una fraccion del tiempo (lee, escribe, "
                "piensa); el factor traduce slots concurrentes a personas."
            ),
        },
        "started_at": started,
        "duracion_total_seg": round(duracion, 2),
    }


# =============================================================================
# DRIVER: empujar al gateway y ejecutar remoto
# =============================================================================
def _gateway_ssh_target(gateway_cluster: str) -> Optional[Dict[str, str]]:
    """IP pública + clave SSH que SkyPilot ya generó para el clúster.
    Es la base del transporte por scp: 'sky rsync' NO existe en la versión de
    SkyPilot de este repo (0.13.0 responde "Error: No such command 'rsync'",
    confirmado en despliegue real), así que no vale con reintentar.
    Mismo patrón que scripts/sync_openwebui_models.py:59."""
    ip = None
    sky = sky_bin()
    if not sky:
        return None
    try:
        out = subprocess.run([sky, "status", "--ip", gateway_cluster],
                             capture_output=True, text=True, timeout=20)
        if out.returncode == 0:
            lineas = [l.strip() for l in out.stdout.strip().splitlines() if IPV4_RE.match(l.strip())]
            ip = lineas[-1] if lineas else None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

    key_path = Path.home() / ".sky" / "generated" / "ssh-keys" / f"{gateway_cluster}.key"
    if not ip or not key_path.exists():
        return None
    return {"ip": ip, "key": str(key_path)}


def _push_self(gateway_cluster: str) -> bool:
    """Sincroniza ESTE archivo al Gateway. Es necesario: scripts/ solo se copia
    en el 'sky launch' del Gateway (file_mounts), así que un
    `generate_infra.py --run --only capacidad` en frío encontraría allí una
    copia vieja del script -o ninguna, la primera vez-."""
    destino = f"{REMOTE_ROOT}/scripts/benchmark_capacity.py"
    target = _gateway_ssh_target(gateway_cluster)
    if target:
        cmd = ["scp", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
               "-i", target["key"], str(Path(__file__).resolve()), f"ubuntu@{target['ip']}:{destino}"]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=120)
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
            detalle = exc.stderr if isinstance(exc, subprocess.CalledProcessError) else str(exc)
            print(f"[WARNING] scp del script falló: {detalle}")

    sky = sky_bin()
    if not sky:
        return False
    print("[INFO] scp no disponible; usando 'sky exec' + heredoc como transporte.")
    payload = Path(__file__).resolve().read_text(encoding="utf-8")
    script = f"cat > {destino} <<'SOONIVERSE_EOF'\n{payload}\nSOONIVERSE_EOF\n"
    try:
        subprocess.run([sky, "exec", gateway_cluster, script], check=True,
                       capture_output=True, text=True, timeout=180)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"[ERROR] No se pudo copiar el script al Gateway: {exc}")
        return False


def run_remote(gateway_cluster: str, wl_id: str, cap: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Ejecuta este mismo script en el Gateway con --local y recoge su JSON.

    Los parámetros efectivos viajan por línea de comandos, NO se leen del
    config_global.yaml remoto: esa copia puede estar desfasada respecto a la
    local si el operador editó el contrato sin relanzar el Gateway.
    """
    sky = sky_bin()
    if not sky:
        print("[WARNING] 'sky' no está en el PATH; no se puede ejecutar el benchmark remoto.")
        return None
    if not _push_self(gateway_cluster):
        return None

    niveles = ",".join(str(n) for n in cap["niveles_concurrencia"])
    remoto = (
        f"cd {REMOTE_ROOT} && python3 scripts/benchmark_capacity.py --local "
        f"--config config_global.yaml --gateway-url http://127.0.0.1 "
        f"--workload {wl_id} --niveles {niveles} "
        f"--segundos-por-nivel {cap['segundos_por_nivel']} "
        f"--warmup {cap['warmup_segundos']} "
        f"--prompt-tokens {cap['prompt_tokens_objetivo']} "
        f"--max-tokens {cap['max_tokens']} "
        f"--presupuesto-segundos {cap['presupuesto_segundos']} "
        f"--json -"
    )
    presupuesto = int(cap["presupuesto_segundos"])
    print(f"[EXEC] sky exec {gateway_cluster} '<benchmark {wl_id}>'")
    try:
        proc = subprocess.run([sky, "exec", gateway_cluster, remoto],
                              capture_output=True, text=True, timeout=presupuesto + 300)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        print(f"[WARNING] El benchmark remoto no respondió: {exc}")
        return None

    salida = _strip_sky_exec_echo((proc.stdout or "") + "\n" + (proc.stderr or ""))
    for linea in salida.splitlines():
        if linea.startswith(("[bench]", "[WARNING]", "[ERROR]")):
            print(f"  {linea}")

    resultado = _parse_sentinel_json(salida)
    if resultado is None:
        print(f"[WARNING] No se encontró el JSON del benchmark en la salida remota "
              f"(código {proc.returncode}).")
    return resultado


# =============================================================================
# DRIVER: persistencia
# =============================================================================
def write_benchmark_to_db(config: Dict[str, Any], resultado: Dict[str, Any]) -> bool:
    """UPSERT en sooniverse.capacity_benchmark. Best-effort: si la BD no está
    accesible se avisa pero no se aborta (la medición ya se hizo y viaja en el
    artefacto JSON). Mismo contrato que
    test_model_capabilities.write_capabilities_to_db."""
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
    cfg = resultado["configuracion"]
    par = resultado["parametros"]
    res = resultado["resultado"]
    gw = config.get("gateway", {}) or {}

    try:
        with conn.cursor() as cur:
            _registrar_benchmark_key(cur, cliente, par)

            cur.execute(
                """
                INSERT INTO sooniverse.capacity_benchmark (
                    run_id, client_id, environment, deployment_id, workload_id, model_public_name,
                    instance_type, accelerator, gpu_count, replicas,
                    max_num_seqs, max_num_batched_tokens, max_model_len, gpu_memory_utilization,
                    lb_strategy,
                    niveles_concurrencia, prompt_tokens_objetivo, max_tokens,
                    segundos_por_nivel, warmup_segundos, streaming, origen,
                    benchmark_key_alias, benchmark_key_hash,
                    concurrencia_rodilla, rpm_sostenido, tokens_salida_por_min,
                    p50_base_ms, p95_base_ms, ttft_p50_base_ms, ttft_p95_base_ms,
                    p95_rodilla_ms, ttft_p95_rodilla_ms, itl_medio_rodilla_ms,
                    tasa_error_pct, motivo_parada, usuarios_estimados,
                    curva, notas, duracion_total_seg, started_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s,
                    %s, %s, %s,
                    %s, %s, TRUE, %s,
                    %s, %s,
                    %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s, to_timestamp(%s)
                )
                ON CONFLICT (run_id) DO UPDATE SET
                    curva          = EXCLUDED.curva,
                    notas          = EXCLUDED.notas,
                    motivo_parada  = EXCLUDED.motivo_parada,
                    finished_at    = NOW()
                """,
                (
                    resultado["run_id"], cliente["id"], cliente["entorno"],
                    _deployment_id(cliente), resultado["workload_id"], resultado["model_public_name"],
                    cfg["instance_type"], cfg["accelerator"], cfg["gpu_count"], cfg["replicas"],
                    cfg["max_num_seqs"], cfg["max_num_batched_tokens"], cfg["max_model_len"],
                    cfg["gpu_memory_utilization"],
                    gw.get("load_balancing_strategy"),
                    par["niveles_concurrencia"], par["prompt_tokens_objetivo"], par["max_tokens"],
                    par["segundos_por_nivel"], par["warmup_segundos"], par["origen"],
                    par["benchmark_key_alias"], par["benchmark_key_hash"],
                    res["concurrencia_rodilla"], res["rpm_sostenido"], res["tokens_salida_por_min"],
                    res["p50_base_ms"], res["p95_base_ms"],
                    res["ttft_p50_base_ms"], res["ttft_p95_base_ms"],
                    res["p95_rodilla_ms"], res["ttft_p95_rodilla_ms"], res["itl_medio_rodilla_ms"],
                    res["tasa_error_pct"], res["motivo_parada"], res["usuarios_estimados"],
                    json.dumps(resultado["curva"]), json.dumps(resultado["notas"]),
                    resultado["duracion_total_seg"], resultado["started_at"],
                ),
            )

            # Baja de inmediato los eventos que acaba de generar el benchmark, ya
            # con api_key_id resuelto contra la fila que se acaba de registrar.
            cur.execute("SELECT sooniverse.ingest_litellm_spendlogs(1)")
        conn.commit()
        print(f"[OK] Corrida {resultado['run_id'][:8]} persistida en sooniverse.capacity_benchmark")
        return True
    except Exception as exc:  # noqa: BLE001 - persistencia best-effort
        conn.rollback()
        print(f"[WARNING] Falló la escritura en sooniverse.capacity_benchmark: {exc}")
        return False
    finally:
        conn.close()


def _registrar_benchmark_key(cur, cliente: Dict[str, Any], par: Dict[str, Any]) -> None:
    """Deja como mucho una key de benchmark activa y registra la de esta corrida
    con proposito='benchmark' para que el panel pueda excluir su tráfico."""
    token_hash = par.get("benchmark_key_hash")
    if not token_hash:
        return

    cur.execute(
        "UPDATE sooniverse.api_key_registry SET is_active = FALSE, deactivated_at = NOW() "
        "WHERE proposito = 'benchmark' AND cliente_id = %s AND entorno = %s AND is_active",
        (cliente["id"], cliente["entorno"]),
    )
    cur.execute(
        """
        INSERT INTO sooniverse.api_key_registry
            (key_alias, litellm_token_hash, cliente_id, entorno, is_active, proposito,
             descripcion, created_at, updated_at)
        VALUES (%s, %s, %s, %s, FALSE, 'benchmark',
                'Key efímera del benchmark de capacidad (ya revocada).', NOW(), NOW())
        ON CONFLICT (litellm_token_hash) DO UPDATE SET
            proposito = 'benchmark', key_alias = EXCLUDED.key_alias, updated_at = NOW()
        """,
        (par.get("benchmark_key_alias") or "sooniverse-benchmark", token_hash,
         cliente["id"], cliente["entorno"]),
    )


def _deployment_id(cliente: Dict[str, Any]) -> Optional[str]:
    """Best-effort, mismo patrón que sync_endpoints._current_network_context."""
    try:
        from infra_state import PostgresInfraStateStore  # type: ignore[import-not-found]
        store = PostgresInfraStateStore(env_path=DEFAULT_ENV_PATH)
        activo = store.get_active_deployment(cliente["id"], cliente["entorno"])
        return activo.get("deployment_id") if activo else None
    except Exception:  # noqa: BLE001 - dato informativo, nunca bloquea
        return None


# =============================================================================
# CLI
# =============================================================================
def resolve_capacidad(config: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    cap = {**DEFAULTS, **(config.get("capacidad") or {})}
    if args.niveles:
        cap["niveles_concurrencia"] = [int(n) for n in args.niveles.split(",") if n.strip()]
    for flag, clave in (("segundos_por_nivel", "segundos_por_nivel"),
                        ("warmup", "warmup_segundos"),
                        ("prompt_tokens", "prompt_tokens_objetivo"),
                        ("max_tokens", "max_tokens"),
                        ("presupuesto_segundos", "presupuesto_segundos")):
        valor = getattr(args, flag, None)
        if valor is not None:
            cap[clave] = valor
    return cap


def _seleccionar_workloads(config: Dict[str, Any], wl_id: Optional[str]) -> List[Dict[str, Any]]:
    workloads = config.get("workloads") or []
    if wl_id:
        return [wl for wl in workloads if wl["id"] == wl_id]
    # Solo cargas de texto: un benchmark de chat contra un modelo de embeddings
    # no mide nada útil.
    return [wl for wl in workloads if wl.get("tipo_tarea", "llm-texto") == "llm-texto"]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark de capacidad con rampa acotada de concurrencia."
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--workload", default=None, help="Solo este workload (por id)")
    parser.add_argument("--gateway-ip", default=None,
                        help="IP del Gateway; si se omite, se descubre con 'sky status --ip'")
    parser.add_argument("--gateway-url", default="http://127.0.0.1",
                        help="URL base a medir en modo --local")
    parser.add_argument("--local", action="store_true",
                        help="Modo runner: mide desde aquí y emite el JSON entre centinelas")
    parser.add_argument("--niveles", default=None, help="Ej. '1,2,4,8,16' (override del contrato)")
    parser.add_argument("--segundos-por-nivel", type=int, default=None, dest="segundos_por_nivel")
    parser.add_argument("--warmup", type=int, default=None)
    parser.add_argument("--prompt-tokens", type=int, default=None, dest="prompt_tokens")
    parser.add_argument("--max-tokens", type=int, default=None, dest="max_tokens")
    parser.add_argument("--presupuesto-segundos", type=int, default=None, dest="presupuesto_segundos")
    parser.add_argument("--write-db", action="store_true", help="Persiste en sooniverse.capacity_benchmark")
    parser.add_argument("--json", default=None, metavar="RUTA", help="Artefacto JSON ('-' = stdout)")
    parser.add_argument("--dry-run", action="store_true", help="Imprime el plan de rampa y sale")
    args = parser.parse_args()

    config_path = Path(args.config)
    with config_path.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    cliente = config["cliente"]
    cap = resolve_capacidad(config, args)
    workloads = _seleccionar_workloads(config, args.workload)

    if not workloads:
        print("[N/A] No hay workloads de tipo 'llm-texto' que medir.")
        return 0

    if args.dry_run:
        total = len(cap["niveles_concurrencia"]) * cap["segundos_por_nivel"] + cap["warmup_segundos"]
        print(f"[PLAN] {len(workloads)} workload(s) · niveles {cap['niveles_concurrencia']} · "
              f"{cap['segundos_por_nivel']}s/nivel · warmup {cap['warmup_segundos']}s "
              f"≈ {total}s de GPU por workload (presupuesto {cap['presupuesto_segundos']}s)")
        return 0

    resultados: List[Dict[str, Any]] = []

    if args.local:
        # --- RUNNER: mide aquí mismo -------------------------------------------
        master_key = _read_env_var("LITELLM_MASTER_KEY", Path(REPO_ROOT / ".env")) \
            or os.environ.get("LITELLM_MASTER_KEY") or ""
        if not master_key:
            print("[ERROR] Sin LITELLM_MASTER_KEY: no se puede emitir la key del benchmark.",
                  file=sys.stderr)
            return 1
        for wl in workloads:
            resultados.append(run_benchmark(args.gateway_url, wl, cap, master_key))
    else:
        # --- DRIVER: ejecuta en el Gateway y persiste ---------------------------
        cluster = gateway_cluster_for(cliente)
        gateway_ip = args.gateway_ip or discover_gateway_ip(cliente)
        if not gateway_ip:
            print(f"[N/A] No se encontró un Gateway activo para "
                  f"'{cliente['id']}-{cliente['entorno']}'; se omite el benchmark.")
            return 0
        for wl in workloads:
            r = run_remote(cluster, wl["id"], cap)
            if r:
                resultados.append(r)

    if not resultados:
        print("[WARNING] El benchmark no produjo ningún resultado.")
        return 0

    _imprimir_tabla(resultados)

    if args.write_db and not args.local:
        for r in resultados:
            write_benchmark_to_db(config, r)

    if args.json:
        payload = {"cliente": cliente["id"], "entorno": cliente["entorno"], "corridas": resultados}
        if args.json == "-":
            # Delimitado para que el driver lo recorte de una salida con ruido.
            print(SENTINEL_BEGIN)
            print(json.dumps(payload if not args.local else resultados[0]))
            print(SENTINEL_END)
        else:
            out = Path(args.json)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            print(f"[OK] Artefacto escrito en {out}")

    return 0


def _imprimir_tabla(resultados: List[Dict[str, Any]]) -> None:
    for r in resultados:
        res = r["resultado"]
        print(f"\n=== {r['model_public_name']} ({r['workload_id']}) ===")
        print(f"{'CONC':>5} {'PET':>5} {'RPS':>7} {'TOK/S':>8} {'P95 ms':>8} "
              f"{'TTFT95':>8} {'ERR %':>6}")
        for n in r["curva"]:
            print(f"{n['concurrencia']:>5} {n['peticiones']:>5} {n['rps']:>7.2f} "
                  f"{n['tokens_salida_por_seg']:>8.1f} {str(n['p95_ms']):>8} "
                  f"{str(n['ttft_p95_ms']):>8} {n['tasa_error_pct']:>6.1f}")
        print(f"  rodilla: concurrencia={res['concurrencia_rodilla']} · "
              f"{res['rpm_sostenido']} pet/min · {res['tokens_salida_por_min']} tok/min · "
              f"~{res['usuarios_estimados']} usuarios · parada={res['motivo_parada']}")


if __name__ == "__main__":
    sys.exit(main())
