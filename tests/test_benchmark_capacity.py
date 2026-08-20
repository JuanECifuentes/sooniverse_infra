"""
Pruebas de scripts/benchmark_capacity.py: percentiles, detección de la rodilla,
los cinco motivos de parada, el prompt anti-caché y el transporte del JSON.

No hay red ni base de datos: las peticiones HTTP se simulan monkeypatcheando
_stream_completion, igual que test_model_capabilities_probe.py hace con
_http_post.
"""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import benchmark_capacity as bc  # noqa: E402


# =============================================================================
# HELPERS
# =============================================================================
def muestra(total_ms: float, ttft_ms: float = 50.0, tokens: int = 20, ok: bool = True):
    return bc.RequestSample(ok=ok, status=200 if ok else 500, ttft_ms=ttft_ms if ok else None,
                            total_ms=total_ms, completion_tokens=tokens if ok else 0)


def nivel(concurrencia: int, latencias, duracion=10.0, errores=0, tokens=20):
    muestras = [muestra(ms, tokens=tokens) for ms in latencias]
    muestras += [muestra(0, ok=False) for _ in range(errores)]
    return bc.NivelResultado(concurrencia, duracion, muestras)


# =============================================================================
# PERCENTILES
# =============================================================================
def test_percentil_de_lista_vacia_es_none():
    assert bc._percentil([], 0.95) is None


def test_percentil_de_un_solo_valor():
    assert bc._percentil([42.0], 0.95) == 42


def test_percentil_disc_devuelve_un_valor_observado():
    """percentile_disc no interpola: el resultado tiene que ser uno de los
    valores de la muestra, igual que hace el SQL de usage_hourly."""
    valores = [10.0, 20.0, 30.0, 40.0, 100.0]
    assert bc._percentil(valores, 0.95) in valores
    assert bc._percentil(valores, 0.50) == 30
    assert bc._percentil(valores, 1.0) == 100


# =============================================================================
# MÉTRICAS DERIVADAS DE UN NIVEL
# =============================================================================
def test_tasa_error_y_rps():
    n = nivel(4, [100.0] * 8, duracion=10.0, errores=2)
    assert n.peticiones == 10
    assert n.errores == 2
    assert n.tasa_error_pct == pytest.approx(20.0)
    assert n.rps == pytest.approx(0.8)  # 8 éxitos / 10 s


def test_itl_ignora_respuestas_de_un_solo_token():
    """Con un único token no hay intervalo entre tokens que medir: incluirlo
    daría una división por cero disfrazada."""
    n = bc.NivelResultado(1, 10.0, [muestra(1000.0, ttft_ms=100.0, tokens=1)])
    assert n.itl_medio_ms is None

    n2 = bc.NivelResultado(1, 10.0, [muestra(1100.0, ttft_ms=100.0, tokens=11)])
    assert n2.itl_medio_ms == pytest.approx(100.0)  # (1100-100)/10


def test_as_dict_expone_las_claves_de_la_curva():
    esperadas = {
        "concurrencia", "peticiones", "exitos", "errores", "tasa_error_pct",
        "p50_ms", "p95_ms", "p99_ms", "max_ms", "ttft_p50_ms", "ttft_p95_ms",
        "rps", "tokens_salida_por_seg", "itl_medio_ms", "duracion_seg",
    }
    assert set(nivel(2, [100.0, 200.0]).as_dict()) == esperadas


# =============================================================================
# CRITERIO DE PARADA
# =============================================================================
UMBRALES = {"error_pct": 5.0, "p95_factor": 3.0, "ganancia_min_pct": 10.0}


def test_no_para_cuando_todo_va_bien():
    base = nivel(1, [100.0] * 10, tokens=20)
    actual = nivel(2, [150.0] * 30, tokens=20)   # throughput muy superior
    assert bc.evaluar_parada(actual, base, base, UMBRALES, tiempo_restante=100) is None


def test_para_por_tasa_de_error():
    base = nivel(1, [100.0] * 10)
    actual = nivel(2, [100.0] * 10, errores=5)   # 33 % de error
    assert bc.evaluar_parada(actual, base, None, UMBRALES, 100) == "errores"


def test_para_por_p95_degradado():
    base = nivel(1, [100.0] * 10)
    actual = nivel(8, [400.0] * 10)              # 4x el p95 base, umbral 3x
    assert bc.evaluar_parada(actual, base, None, UMBRALES, 100) == "p95_degradado"


def test_no_para_si_el_p95_crece_por_debajo_del_umbral():
    base = nivel(1, [100.0] * 10)
    actual = nivel(2, [250.0] * 40)              # 2.5x < 3x y con más throughput
    assert bc.evaluar_parada(actual, base, base, UMBRALES, 100) is None


def test_para_por_saturacion_de_throughput():
    """Si un nivel más de concurrencia ya no aporta tokens/s, la GPU está llena
    y todo lo que se añada es cola."""
    base = nivel(1, [100.0] * 10, duracion=10.0, tokens=20)
    previo = nivel(4, [100.0] * 100, duracion=10.0, tokens=20)
    actual = nivel(8, [110.0] * 102, duracion=10.0, tokens=20)  # +2 % de throughput
    assert bc.evaluar_parada(actual, base, previo, UMBRALES, 100) == "saturacion_throughput"


def test_para_por_presupuesto_agotado():
    base = nivel(1, [100.0] * 10, tokens=20)
    actual = nivel(2, [120.0] * 30, tokens=20)
    assert bc.evaluar_parada(actual, base, base, UMBRALES, tiempo_restante=0) == "presupuesto_agotado"


def test_el_error_tiene_prioridad_sobre_la_degradacion():
    base = nivel(1, [100.0] * 10)
    actual = nivel(8, [900.0] * 10, errores=10)
    assert bc.evaluar_parada(actual, base, None, UMBRALES, 100) == "errores"


# =============================================================================
# RESUMEN Y RODILLA
# =============================================================================
WL = {
    "id": "qwen3-5-llm",
    "nombre_publico": "sooniverse-qwen3.5",
    "tipo_instancia": "g6.xlarge",
    "accelerator": "L4",
    "cantidad_gpus": 1,
    "replicas": 1,
    "asignacion_fraccional": {"max_model_len": 16384, "gpu_memory_utilization": 0.95},
    "concurrencia": {"max_num_seqs": 16, "max_num_batched_tokens": 8192},
}
CAP = dict(bc.DEFAULTS)


def test_rodilla_es_el_ultimo_nivel_sano_cuando_hubo_parada():
    """Si la parada la disparó el último nivel medido, ese nivel NO es la
    rodilla: la rodilla es el anterior, que aún cumplía."""
    curva = [nivel(1, [100.0] * 10), nivel(2, [120.0] * 20), nivel(4, [900.0] * 10)]
    r = bc._resumir(curva, "p95_degradado", WL, CAP, None, started=0.0, duracion=60.0)
    assert r["resultado"]["concurrencia_rodilla"] == 2


def test_rodilla_es_el_nivel_maximo_si_nada_degrado():
    curva = [nivel(1, [100.0] * 10), nivel(2, [110.0] * 20), nivel(4, [130.0] * 40)]
    r = bc._resumir(curva, "nivel_maximo", WL, CAP, None, started=0.0, duracion=60.0)
    assert r["resultado"]["concurrencia_rodilla"] == 4
    assert r["resultado"]["motivo_parada"] == "nivel_maximo"


def test_resumen_guarda_el_snapshot_de_configuracion():
    """El techo solo es interpretable junto a la config bajo la que se midió."""
    r = bc._resumir([nivel(1, [100.0] * 5)], "nivel_maximo", WL, CAP, None, 0.0, 10.0)
    cfg = r["configuracion"]
    assert cfg["max_num_seqs"] == 16
    assert cfg["max_num_batched_tokens"] == 8192
    assert cfg["instance_type"] == "g6.xlarge"
    assert cfg["max_model_len"] == 16384


def test_usuarios_estimados_y_factor_auditable():
    r = bc._resumir([nivel(1, [100.0] * 5), nivel(8, [120.0] * 40)],
                    "nivel_maximo", WL, {**CAP, "factor_usuarios_por_slot": 8}, None, 0.0, 10.0)
    assert r["resultado"]["usuarios_estimados"] == 8 * 8
    # El factor no puede quedar como número mágico: viaja en las notas.
    assert r["notas"]["factor_usuarios_por_slot"] == 8
    assert "explicacion_factor" in r["notas"]


def test_resumen_con_curva_vacia_no_revienta():
    r = bc._resumir([], "fallo", WL, CAP, None, 0.0, 0.0)
    assert r["resultado"]["concurrencia_rodilla"] is None
    assert r["curva"] == []


def test_rpm_sostenido_se_deriva_de_la_rodilla():
    curva = [nivel(1, [100.0] * 10, duracion=10.0)]   # 10 éxitos / 10 s = 1 rps
    r = bc._resumir(curva, "nivel_maximo", WL, CAP, None, 0.0, 10.0)
    assert r["resultado"]["rpm_sostenido"] == pytest.approx(60.0)


# =============================================================================
# PROMPT ANTI-CACHÉ
# =============================================================================
def test_prompts_consecutivos_son_distintos():
    """Sin prefijo único, vLLM reutilizaría el KV cacheado y mediríamos el caché
    en vez de la GPU."""
    a = bc.build_prompt(128, "slot0-0-111")
    b = bc.build_prompt(128, "slot0-1-222")
    assert a != b
    assert a.split("]", 1)[1] == b.split("]", 1)[1]  # solo cambia el prefijo


def test_prompt_se_aproxima_al_tamano_objetivo():
    p = bc.build_prompt(512, "n")
    # ~4 caracteres por token, con margen por el prefijo y la instrucción final.
    assert 512 * bc.CHARS_POR_TOKEN <= len(p) <= 512 * bc.CHARS_POR_TOKEN + 120


# =============================================================================
# TRANSPORTE DEL JSON ENTRE DRIVER Y RUNNER
# =============================================================================
def test_parse_sentinel_json_ignora_ruido_alrededor():
    payload = {"run_id": "abc", "curva": []}
    salida = (
        "[bench] warmup 10s...\n"
        "[WARNING] algo irrelevante\n"
        f"{bc.SENTINEL_BEGIN}\n{json.dumps(payload)}\n{bc.SENTINEL_END}\n"
        "Shared connection to 1.2.3.4 closed.\n"
    )
    assert bc._parse_sentinel_json(salida) == payload


def test_parse_sentinel_json_devuelve_none_sin_centinelas():
    assert bc._parse_sentinel_json("no hay nada aquí") is None


def test_parse_sentinel_json_devuelve_none_con_json_corrupto():
    salida = f"{bc.SENTINEL_BEGIN}\n{{roto,,}}\n{bc.SENTINEL_END}"
    assert bc._parse_sentinel_json(salida) is None


def test_strip_sky_exec_echo_descarta_el_eco_del_comando():
    """'sky exec' imprime el comando entero antes de ejecutarlo. Si el comando
    contiene el centinela (y lo contiene), sin filtrar se parsearía el eco."""
    crudo = (
        f"Command to run: python3 benchmark.py --json - # {bc.SENTINEL_BEGIN}\n"
        "(sooniverse-gw, pid=123) [bench] nivel concurrencia=1\n"
        f"(sooniverse-gw, pid=123) {bc.SENTINEL_BEGIN}\n"
    )
    limpio = bc._strip_sky_exec_echo(crudo)
    assert "Command to run" not in limpio
    assert limpio.count(bc.SENTINEL_BEGIN) == 1


# =============================================================================
# CONFIGURACIÓN
# =============================================================================
def test_los_flags_de_cli_pisan_al_contrato():
    class Args:
        niveles = "1,4"
        segundos_por_nivel = 5
        warmup = 0
        prompt_tokens = None
        max_tokens = None
        presupuesto_segundos = None

    cap = bc.resolve_capacidad({"capacidad": {"segundos_por_nivel": 60}}, Args())
    assert cap["niveles_concurrencia"] == [1, 4]
    assert cap["segundos_por_nivel"] == 5
    assert cap["warmup_segundos"] == 0
    assert cap["max_tokens"] == bc.DEFAULTS["max_tokens"]   # no tocado -> default


def test_selecciona_solo_workloads_de_texto():
    config = {"workloads": [
        {"id": "a", "tipo_tarea": "llm-texto"},
        {"id": "b", "tipo_tarea": "embeddings"},
    ]}
    assert [w["id"] for w in bc._seleccionar_workloads(config, None)] == ["a"]
    assert [w["id"] for w in bc._seleccionar_workloads(config, "b")] == ["b"]


# =============================================================================
# PERSISTENCIA BEST-EFFORT
# =============================================================================
def test_write_benchmark_to_db_no_propaga_si_la_bd_no_esta(monkeypatch):
    """La medición ya se hizo y viaja en el JSON: un fallo de BD avisa, no aborta."""
    import db_setup

    def explota(_config):
        raise db_setup.DbSetupError("host inalcanzable")

    monkeypatch.setattr(db_setup, "connect", lambda *a, **k: explota(None))
    resultado = bc._resumir([nivel(1, [100.0] * 5)], "nivel_maximo", WL, CAP, None, 0.0, 10.0)
    assert bc.write_benchmark_to_db({"cliente": {"id": "acme", "entorno": "prod"}}, resultado) is False


def test_ensure_benchmark_key_devuelve_none_si_no_se_emite(monkeypatch, capsys):
    monkeypatch.setattr(bc, "_http_json", lambda *a, **k: {"status": 401, "json": {}})
    assert bc.ensure_benchmark_key("http://127.0.0.1", "sk-master", "alias", ["m"]) is None
    assert "no se podrá excluir" in capsys.readouterr().out.lower().replace("no se podra", "no se podrá")


def test_ensure_benchmark_key_no_expone_la_key_en_el_hash(monkeypatch):
    monkeypatch.setattr(bc, "_http_json", lambda *a, **k: {
        "status": 200, "json": {"key": "sk-secreta", "token": "hash-abc"}})
    k = bc.ensure_benchmark_key("http://127.0.0.1", "sk-master", "alias", ["m"])
    assert k["token_hash"] == "hash-abc"
    # Lo que se persiste es el hash; la key en claro solo vive en memoria.
    resultado = bc._resumir([nivel(1, [100.0] * 5)], "nivel_maximo", WL, CAP, k, 0.0, 10.0)
    assert resultado["parametros"]["benchmark_key_hash"] == "hash-abc"
    assert "sk-secreta" not in json.dumps(resultado)
