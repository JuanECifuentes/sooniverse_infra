"""
==============================================================================
Capacidad: techo medido, margen disponible y proyección
==============================================================================
Responde "¿está por quedarse corta la infraestructura?" cruzando dos cosas:
  - el TECHO medido por scripts/benchmark_capacity.py (sooniverse.capacity_benchmark)
  - el PICO observado en el tráfico real (sooniverse.token_usage_event)

TRAMPA QUE HAY QUE RESPETAR AQUÍ
  El techo sale del benchmark, pero el pico observado tiene que seguir
  EXCLUYENDO la key del benchmark. Si no, el pico observado sería siempre el
  propio test de estrés (que satura la máquina a propósito) y el semáforo
  estaría en rojo permanente sin que nadie tuviera un problema real.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from django.db import connection
from django.db.models import Max
from django.db.models.functions import TruncWeek

from . import filtros as ft
from .models import CapacityBenchmark, UsageHourly

# Umbrales del semáforo de margen.
UMBRAL_ATENCION = 60.0   # por debajo: verde
UMBRAL_CRITICO = 85.0    # por encima: rojo
# Por debajo de este r², la tendencia es ruido y NO se da un número de semanas.
R2_MINIMO = 0.5
# Con menos puntos que esto, una regresión lineal no significa nada.
MIN_SEMANAS_PROYECCION = 4


@dataclass
class Margen:
    techo_rpm: Optional[float] = None
    techo_tokens_min: Optional[float] = None
    techo_concurrencia: Optional[int] = None
    techo_es_tope_probado: bool = False
    pico_rpm: float = 0.0
    pico_ts: Optional[str] = None
    uso_pct: Optional[float] = None
    semaforo: str = "sin-datos"        # ok | warn | danger | sin-datos
    etiqueta: str = "Sin medición de capacidad"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "techo_rpm": self.techo_rpm,
            "techo_tokens_min": self.techo_tokens_min,
            "techo_concurrencia": self.techo_concurrencia,
            "techo_es_tope_probado": self.techo_es_tope_probado,
            "pico_rpm": self.pico_rpm,
            "pico_ts": self.pico_ts,
            "uso_pct": self.uso_pct,
            "semaforo": self.semaforo,
            "etiqueta": self.etiqueta,
        }


@dataclass
class Proyeccion:
    picos_semanales: List[Tuple[str, float]] = field(default_factory=list)
    pendiente_rpm_semana: float = 0.0
    r2: float = 0.0
    semanas_al_techo: Optional[float] = None
    confiable: bool = False
    explicacion: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "picos_semanales": [{"semana": s, "pico_rpm": v} for s, v in self.picos_semanales],
            "pendiente_rpm_semana": self.pendiente_rpm_semana,
            "r2": self.r2,
            "semanas_al_techo": self.semanas_al_techo,
            "confiable": self.confiable,
            "explicacion": self.explicacion,
            "semanas_observadas": len(self.picos_semanales),
        }


# =============================================================================
# CORRIDAS DE BENCHMARK
# =============================================================================
def corridas(cliente_id: str, entorno: str, limite: int = 20) -> List[CapacityBenchmark]:
    return list(
        CapacityBenchmark.objects
        .filter(client_id=cliente_id, environment=entorno)
        .order_by("-finished_at")[:limite]
    )


def ultima_corrida(cliente_id: str, entorno: str,
                   run_id: Optional[str] = None) -> Optional[CapacityBenchmark]:
    qs = CapacityBenchmark.objects.filter(client_id=cliente_id, environment=entorno)
    if run_id:
        return qs.filter(run_id=run_id).first()
    return qs.order_by("-finished_at").first()


# =============================================================================
# PICO OBSERVADO
# =============================================================================
def pico_por_minuto(f: ft.FiltrosTemporales) -> Tuple[float, Optional[str]]:
    """Máximo de peticiones en un minuto dentro del rango.

    SQL crudo: es un MAX sobre un COUNT agrupado (agregado sobre agregado), que
    en el ORM exige una Subquery bastante retorcida.

    `date_trunc('minute', ...)` NO necesita AT TIME ZONE aquí: los minutos
    coinciden en cualquier desplazamiento horario entero, y así el WHERE sigue
    usando el índice idx_usage_event_ts. No lo "arregles" añadiéndoselo.
    """
    partes = ["e.event_ts >= %s", "e.event_ts < %s"]
    params: List[Any] = [f.inicio, f.fin]
    if f.api_key_ids:
        partes.append("e.api_key_id = ANY(%s)")
        params.append(list(f.api_key_ids))
    if f.modelos:
        partes.append("e.model_name = ANY(%s)")
        params.append(list(f.modelos))
    # Ver la cabecera del módulo: el pico observado SIEMPRE excluye el benchmark.
    partes.append("(e.api_key_id IS NULL OR k.proposito <> 'benchmark')")

    sql = f"""
        SELECT c, minuto FROM (
            SELECT COUNT(*) AS c, date_trunc('minute', e.event_ts) AS minuto
              FROM sooniverse.token_usage_event e
              LEFT JOIN sooniverse.api_key_registry k ON k.id = e.api_key_id
             WHERE {' AND '.join(partes)}
             GROUP BY 2
        ) t
        ORDER BY c DESC
        LIMIT 1
    """
    with connection.cursor() as cur:
        cur.execute(sql, params)
        fila = cur.fetchone()
    if not fila:
        return 0.0, None
    return float(fila[0]), fila[1].isoformat() if fila[1] else None


# =============================================================================
# MARGEN
# =============================================================================
def margen_capacidad(f: ft.FiltrosTemporales, corrida: Optional[CapacityBenchmark]) -> Margen:
    pico_rpm, pico_ts = pico_por_minuto(f)
    m = Margen(pico_rpm=round(pico_rpm, 2), pico_ts=pico_ts)

    if not corrida or not corrida.rpm_sostenido:
        m.etiqueta = ("Sin medición de capacidad todavía: corre la fase "
                      "'capacidad' del despliegue para conocer el techo.")
        return m

    m.techo_rpm = float(corrida.rpm_sostenido)
    m.techo_tokens_min = float(corrida.tokens_salida_por_min or 0) or None
    m.techo_concurrencia = corrida.concurrencia_rodilla
    m.techo_es_tope_probado = corrida.rodilla_es_el_tope_probado

    if m.techo_rpm <= 0:
        return m

    m.uso_pct = round(pico_rpm / m.techo_rpm * 100, 1)
    if m.uso_pct < UMBRAL_ATENCION:
        m.semaforo, holgura = "ok", "margen holgado"
    elif m.uso_pct < UMBRAL_CRITICO:
        m.semaforo, holgura = "warn", "margen ajustado"
    else:
        m.semaforo, holgura = "danger", "sin margen"
    m.etiqueta = f"{m.uso_pct:.0f} % del techo · {holgura}"

    if m.techo_es_tope_probado:
        m.etiqueta += " (el techo real puede ser mayor: la prueba no encontró degradación)"
    return m


# =============================================================================
# PROYECCIÓN
# =============================================================================
def regresion_lineal(xs: List[float], ys: List[float]) -> Tuple[float, float, float]:
    """Mínimos cuadrados en Python puro -> (pendiente, intersección, r²).

    Sin numpy y sin BD: así se puede probar con una tabla de casos, que es
    justo lo que da confianza en un número que va a leer una persona para
    decidir si gasta dinero en más infraestructura.
    """
    n = len(xs)
    if n < 2:
        return 0.0, (ys[0] if ys else 0.0), 0.0

    media_x = sum(xs) / n
    media_y = sum(ys) / n
    sxx = sum((x - media_x) ** 2 for x in xs)
    if sxx == 0:
        return 0.0, media_y, 0.0

    sxy = sum((x - media_x) * (y - media_y) for x, y in zip(xs, ys))
    pendiente = sxy / sxx
    interseccion = media_y - pendiente * media_x

    syy = sum((y - media_y) ** 2 for y in ys)
    if syy == 0:
        # Serie perfectamente plana: el ajuste es exacto pero no hay tendencia.
        return pendiente, interseccion, 1.0
    residuos = sum((y - (pendiente * x + interseccion)) ** 2 for x, y in zip(xs, ys))
    r2 = max(0.0, 1 - residuos / syy)
    return pendiente, interseccion, r2


def _picos_semanales(f: ft.FiltrosTemporales, semanas: int = 8) -> List[Tuple[date, float]]:
    """Pico de peticiones POR HORA de cada semana, sobre usage_hourly."""
    from datetime import timedelta
    desde = f.hasta - timedelta(weeks=semanas)
    qs = UsageHourly.objects.filter(
        bucket_ts__gte=ft.FiltrosTemporales(desde=desde, hasta=f.hasta).inicio,
        bucket_ts__lt=f.fin,
    )
    qs = ft.excluir_benchmark(qs)
    filas = (
        qs.annotate(semana=TruncWeek("bucket_ts", tzinfo=ft.tz()))
        .values("semana")
        .annotate(pico=Max("request_count"))
        .order_by("semana")
    )
    return [(r["semana"].date(), float(r["pico"] or 0)) for r in filas if r["semana"]]


def proyeccion_techo(f: ft.FiltrosTemporales, margen: Margen) -> Proyeccion:
    """Cuántas semanas quedan hasta alcanzar el techo, al ritmo actual.

    Con pocos puntos y mucho ruido, una regresión lineal produce números
    convincentes y falsos. Por eso hay una puerta explícita: si r² < 0.5, la
    pendiente es plana o descendente, o hay menos de 4 semanas observadas, NO
    se devuelve un número. "Sin tendencia clara" es una respuesta mejor que
    "≈6 semanas" inventado.
    """
    picos = _picos_semanales(f)
    p = Proyeccion(picos_semanales=[(d.isoformat(), v) for d, v in picos])

    if len(picos) < MIN_SEMANAS_PROYECCION:
        p.explicacion = (f"Hacen falta al menos {MIN_SEMANAS_PROYECCION} semanas de datos "
                         f"(hay {len(picos)}).")
        return p

    xs = list(range(len(picos)))
    ys = [v for _, v in picos]
    pendiente, interseccion, r2 = regresion_lineal([float(x) for x in xs], ys)
    p.pendiente_rpm_semana = round(pendiente, 3)
    p.r2 = round(r2, 3)

    if pendiente <= 0:
        p.explicacion = "El uso no está creciendo; no hay fecha de saturación que proyectar."
        return p
    if r2 < R2_MINIMO:
        p.explicacion = (f"La tendencia es demasiado ruidosa para proyectar "
                         f"(r²={r2:.2f} < {R2_MINIMO}).")
        return p
    if not margen.techo_rpm:
        p.explicacion = "Sin techo medido no se puede calcular cuándo se alcanza."
        return p

    # El techo está en peticiones/minuto y los picos semanales en peticiones/hora:
    # se compara contra el techo por hora equivalente.
    techo_hora = margen.techo_rpm * 60
    actual = pendiente * (len(picos) - 1) + interseccion
    if actual >= techo_hora:
        p.semanas_al_techo = 0.0
    else:
        p.semanas_al_techo = round((techo_hora - actual) / pendiente, 1)
    p.confiable = True
    p.explicacion = (f"Sobre {len(picos)} semanas observadas, con r²={r2:.2f}.")
    return p


# =============================================================================
# FICHA COMPLETA PARA LA PÁGINA DE CAPACIDAD
# =============================================================================
def payload_capacidad(cliente_id: str, entorno: str, f: ft.FiltrosTemporales,
                      run_id: Optional[str] = None) -> Dict[str, Any]:
    corrida = ultima_corrida(cliente_id, entorno, run_id)
    m = margen_capacidad(f, corrida)
    p = proyeccion_techo(f, m)

    ficha = None
    if corrida:
        ficha = {
            "run_id": str(corrida.run_id),
            "workload_id": corrida.workload_id,
            "model_public_name": corrida.model_public_name,
            "hardware": corrida.hardware_label,
            "instance_type": corrida.instance_type,
            "accelerator": corrida.accelerator,
            "gpu_count": corrida.gpu_count,
            "replicas": corrida.replicas,
            "max_num_seqs": corrida.max_num_seqs,
            "max_num_batched_tokens": corrida.max_num_batched_tokens,
            "max_model_len": corrida.max_model_len,
            "lb_strategy": corrida.lb_strategy,
            "origen": corrida.origen,
            "motivo_parada": corrida.motivo_parada,
            "motivo_label": corrida.motivo_label,
            "p95_base_ms": corrida.p95_base_ms,
            "p95_rodilla_ms": corrida.p95_rodilla_ms,
            "ttft_p95_base_ms": corrida.ttft_p95_base_ms,
            "usuarios_estimados": corrida.usuarios_estimados,
            "tasa_error_pct": float(corrida.tasa_error_pct or 0),
            "finished_at": corrida.finished_at.isoformat(),
            "curva": corrida.curva,
            "notas": corrida.notas,
        }

    return {
        "margen": m.as_dict(),
        "proyeccion": p.as_dict(),
        "corrida": ficha,
        "corridas": [
            {"run_id": str(c.run_id), "etiqueta": f"{c.finished_at:%d/%m/%Y %H:%M} · {c.workload_id}"}
            for c in corridas(cliente_id, entorno)
        ],
    }
