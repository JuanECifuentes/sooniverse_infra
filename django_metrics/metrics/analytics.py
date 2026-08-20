"""
==============================================================================
Analítica de ritmo de uso: mapa de calor, perfil horario y tiempos muertos
==============================================================================
Responde las tres preguntas de negocio que el dashboard clásico no podía:
"¿en qué momento del fin de semana está parada la máquina?", "¿qué días hay más
interacción?" y, junto con metrics/capacidad.py, "¿está por quedarse corta?".

SQL CRUDO SOLO DONDE ES MATEMÁTICAMENTE OBLIGATORIO
  Las sumas (peticiones, tokens) son recombinables entre buckets, así que se
  agregan con el ORM sobre `usage_hourly`, que ya trae `bucket_local_hour` y
  `bucket_local_isodow` cortados en hora local.
  Los PERCENTILES no son recombinables: promediar los p95 de 13 lunes no da el
  p95 del lunes. Para esos hay que volver a `token_usage_event` con
  `percentile_cont`, que además es un agregado ordenado sin equivalente en el
  ORM de Django. Esas son las únicas dos consultas con cursor crudo de aquí.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

from django.conf import settings
from django.db import connection
from django.db.models import Sum

from . import filtros as ft
from .models import UsageHourly

# La rampa del mapa de calor tiene 6 pasos (--heat-0 .. --heat-5 en
# theme-sooniverse.css). El paso 0 es "sin tráfico", no "poco tráfico".
NIVELES_RAMPA = 5

METRICAS_HEATMAP = {
    "peticiones": ("Peticiones", "pet."),
    "tokens": ("Tokens", "tok"),
    "p95": ("Latencia p95", "ms"),
}


# =============================================================================
# MAPA DE CALOR SEMANAL (7 x 24)
# =============================================================================
@dataclass
class CeldaHeatmap:
    dow: int          # ISO 1=lunes .. 7=domingo
    hora: int         # 0..23
    valor: float
    intensidad: int   # 0..5, índice en la rampa de color
    con_datos: bool   # False = ninguna hora de esa celda tiene bucket agregado


@dataclass
class Heatmap:
    metrica: str
    metrica_label: str
    unidad: str
    celdas: List[CeldaHeatmap] = field(default_factory=list)   # SIEMPRE 168, densa
    maximo: float = 0.0
    cortes: List[float] = field(default_factory=list)
    pico: Optional[Dict[str, Any]] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "metrica": self.metrica,
            "metrica_label": self.metrica_label,
            "unidad": self.unidad,
            "maximo": self.maximo,
            "cortes": self.cortes,
            "pico": self.pico,
            # Claves cortas: son 168 objetos y el payload viaja en cada cambio
            # de lente.
            "celdas": [
                {"d": c.dow, "h": c.hora, "v": c.valor, "i": c.intensidad, "n": c.con_datos}
                for c in self.celdas
            ],
        }


def _base_horaria(f: ft.FiltrosTemporales):
    qs = UsageHourly.objects.filter(bucket_ts__gte=f.inicio, bucket_ts__lt=f.fin)
    if f.api_key_ids:
        qs = qs.filter(api_key_id__in=f.api_key_ids)
    if f.modelos:
        qs = qs.filter(model_name__in=f.modelos)
    if f.dias_semana:
        qs = qs.filter(bucket_local_isodow__in=f.dias_semana)
    if (f.hora_desde, f.hora_hasta) != (0, 23):
        qs = qs.filter(bucket_local_hour__gte=f.hora_desde, bucket_local_hour__lte=f.hora_hasta)
    if f.estado == ft.ESTADO_ERRORES:
        qs = qs.filter(error_count__gt=0)
    if not f.incluir_benchmark:
        qs = ft.excluir_benchmark(qs)
    return qs


def _cortes_por_cuantiles(valores: Sequence[float]) -> List[float]:
    """Cortes de la rampa por cuantiles de las celdas NO vacías, no lineales
    sobre el máximo: una sola hora pico aplanaría visualmente todo lo demás."""
    positivos = sorted(v for v in valores if v > 0)
    if not positivos:
        return []
    if len(positivos) <= NIVELES_RAMPA:
        return positivos[:]
    return [
        positivos[min(len(positivos) - 1, int(len(positivos) * (i + 1) / NIVELES_RAMPA) - 1)]
        for i in range(NIVELES_RAMPA)
    ]


def _intensidad(valor: float, cortes: List[float]) -> int:
    if valor <= 0 or not cortes:
        return 0
    for i, corte in enumerate(cortes):
        if valor <= corte:
            return i + 1
    return len(cortes)


def heatmap_semanal(f: ft.FiltrosTemporales, metrica: str = "peticiones") -> Heatmap:
    if metrica not in METRICAS_HEATMAP:
        metrica = "peticiones"
    label, unidad = METRICAS_HEATMAP[metrica]

    crudo, con_datos = (_heatmap_percentil(f) if metrica == "p95" else _heatmap_suma(f, metrica))

    valores = list(crudo.values())
    cortes = _cortes_por_cuantiles(valores)
    maximo = max(valores) if valores else 0.0

    # Densificación a 168 celdas EN PYTHON: una hora sin tráfico no tiene fila
    # en usage_hourly, y sin las celdas en cero el mapa mentiría. Son 168
    # elementos; hacerlo con un CROSS JOIN obligaría a mantener el mismo SQL
    # duplicado en las dos variantes de métrica.
    celdas: List[CeldaHeatmap] = []
    for dow in range(1, 8):
        for hora in range(24):
            valor = float(crudo.get((dow, hora), 0.0))
            celdas.append(CeldaHeatmap(
                dow=dow, hora=hora, valor=round(valor, 2),
                intensidad=_intensidad(valor, cortes),
                con_datos=(dow, hora) in con_datos,
            ))

    pico = None
    if maximo > 0:
        mejor = max(celdas, key=lambda c: c.valor)
        pico = {"dow": mejor.dow, "hora": mejor.hora, "valor": mejor.valor}

    return Heatmap(metrica=metrica, metrica_label=label, unidad=unidad, celdas=celdas,
                   maximo=round(maximo, 2), cortes=[round(c, 2) for c in cortes], pico=pico)


def _heatmap_suma(f: ft.FiltrosTemporales, metrica: str):
    """ORM: peticiones y tokens son sumas, perfectamente recombinables."""
    campo = "request_count" if metrica == "peticiones" else "total_tokens"
    filas = (
        _base_horaria(f)
        .values("bucket_local_isodow", "bucket_local_hour")
        .annotate(valor=Sum(campo))
        # order_by() vacío es OBLIGATORIO: sin él, Meta.ordering = ['-bucket_ts']
        # se cuela en el GROUP BY y la agregación devuelve una fila por hora
        # concreta en vez de una por celda de la rejilla.
        .order_by()
    )
    crudo = {(r["bucket_local_isodow"], r["bucket_local_hour"]): float(r["valor"] or 0) for r in filas}
    return crudo, set(crudo.keys())


def _heatmap_percentil(f: ft.FiltrosTemporales):
    """SQL crudo obligatorio: `percentile_cont` es un agregado ordenado sin
    equivalente en el ORM, y `usage_hourly.latency_p95_ms` NO sirve aquí porque
    los percentiles horarios no se recombinan entre semanas."""
    where, params = _where_eventos(f)
    sql = f"""
        SELECT EXTRACT(isodow FROM e.event_ts AT TIME ZONE %s)::int AS dow,
               EXTRACT(hour   FROM e.event_ts AT TIME ZONE %s)::int AS hora,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY e.latency_ms) AS valor
          FROM sooniverse.token_usage_event e
          LEFT JOIN sooniverse.api_key_registry k ON k.id = e.api_key_id
         WHERE {where}
           AND e.latency_ms IS NOT NULL
         GROUP BY 1, 2
    """
    zona = settings.TIME_ZONE
    with connection.cursor() as cur:
        cur.execute(sql, [zona, zona, *params])
        filas = cur.fetchall()
    crudo = {(int(d), int(h)): float(v or 0) for d, h, v in filas}
    return crudo, set(crudo.keys())


def _where_eventos(f: ft.FiltrosTemporales) -> Tuple[str, List[Any]]:
    """Cláusula WHERE compartida por las consultas crudas sobre
    token_usage_event. Siempre parametrizada, incluida la zona horaria."""
    zona = settings.TIME_ZONE
    partes = ["e.event_ts >= %s", "e.event_ts < %s"]
    params: List[Any] = [f.inicio, f.fin]

    if f.api_key_ids:
        partes.append("e.api_key_id = ANY(%s)")
        params.append(list(f.api_key_ids))
    if f.modelos:
        partes.append("e.model_name = ANY(%s)")
        params.append(list(f.modelos))
    if f.dias_semana:
        partes.append("EXTRACT(isodow FROM e.event_ts AT TIME ZONE %s)::int = ANY(%s)")
        params.extend([zona, list(f.dias_semana)])
    if (f.hora_desde, f.hora_hasta) != (0, 23):
        partes.append("EXTRACT(hour FROM e.event_ts AT TIME ZONE %s)::int BETWEEN %s AND %s")
        params.extend([zona, f.hora_desde, f.hora_hasta])
    if f.estado == ft.ESTADO_ERRORES:
        partes.append("e.status <> 'success'")
    if not f.incluir_benchmark:
        # Igual que filtros.excluir_benchmark: no descartar las filas sin key.
        partes.append("(e.api_key_id IS NULL OR k.proposito <> 'benchmark')")
    # Un acierto de caché con latency_ms=3 hundiría el p95 y haría creer que la
    # infraestructura es más rápida de lo que es.
    partes.append("e.cache_hit IS NOT TRUE")
    return " AND ".join(partes), params


# =============================================================================
# PERFIL HORARIO (curva de carga 0-23h)
# =============================================================================
@dataclass
class PuntoHorario:
    hora: int
    mediana: float
    p90: float
    pico: float
    dias_observados: int


@dataclass
class PerfilHorario:
    puntos: List[PuntoHorario] = field(default_factory=list)
    techo_pet_hora: Optional[float] = None   # inyectado desde capacidad.py

    def as_dict(self) -> Dict[str, Any]:
        return {
            "techo_pet_hora": self.techo_pet_hora,
            "puntos": [
                {"hora": p.hora, "mediana": p.mediana, "p90": p.p90,
                 "pico": p.pico, "dias": p.dias_observados}
                for p in self.puntos
            ],
        }


def perfil_horario(f: ft.FiltrosTemporales) -> PerfilHorario:
    """Mediana, p90 y pico de peticiones para cada hora del día.

    SQL crudo obligatorio: es un percentil SOBRE UN AGREGADO (la mediana de los
    recuentos por día-hora), o sea CTE + percentile_cont. El ORM no expresa
    ninguna de las dos cosas.
    """
    qs = _base_horaria(f)
    # Se reutiliza el queryset del ORM solo para heredar exactamente los mismos
    # filtros; la agregación de dos niveles va en SQL.
    sql_base, params = qs.query.sql_with_params()

    sql = f"""
        WITH filtrado AS ({sql_base}),
        por_dia_hora AS (
            SELECT bucket_local_date AS dia,
                   bucket_local_hour AS hora,
                   SUM(request_count) AS peticiones
              FROM filtrado
             GROUP BY 1, 2
        )
        SELECT hora,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY peticiones) AS mediana,
               percentile_cont(0.9) WITHIN GROUP (ORDER BY peticiones) AS p90,
               MAX(peticiones) AS pico,
               COUNT(*) AS dias
          FROM por_dia_hora
         GROUP BY hora
         ORDER BY hora
    """
    with connection.cursor() as cur:
        cur.execute(sql, params)
        filas = {int(r[0]): r for r in cur.fetchall()}

    puntos = []
    for hora in range(24):
        r = filas.get(hora)
        puntos.append(PuntoHorario(
            hora=hora,
            mediana=round(float(r[1] or 0), 2) if r else 0.0,
            p90=round(float(r[2] or 0), 2) if r else 0.0,
            pico=round(float(r[3] or 0), 2) if r else 0.0,
            dias_observados=int(r[4]) if r else 0,
        ))
    return PerfilHorario(puntos=puntos)


# =============================================================================
# TIEMPOS MUERTOS
# =============================================================================
@dataclass
class VentanaOciosa:
    inicio: datetime
    fin: datetime
    horas: int
    etiqueta: str
    coste_usd: Decimal


@dataclass
class ResumenOcio:
    ventanas: List[VentanaOciosa] = field(default_factory=list)
    horas_totales: int = 0
    horas_ociosas: int = 0
    horas_sin_datos: int = 0
    pct_ocioso: float = 0.0
    coste_hora_usd: Decimal = Decimal("0")
    coste_ocioso_usd: Decimal = Decimal("0")

    def as_dict(self) -> Dict[str, Any]:
        return {
            "horas_totales": self.horas_totales,
            "horas_ociosas": self.horas_ociosas,
            "horas_sin_datos": self.horas_sin_datos,
            "pct_ocioso": self.pct_ocioso,
            "coste_hora_usd": float(self.coste_hora_usd),
            "coste_ocioso_usd": float(self.coste_ocioso_usd),
            "mostrar_coste": self.coste_hora_usd > 0,
            "ventanas": [
                {"inicio": v.inicio.isoformat(), "fin": v.fin.isoformat(),
                 "horas": v.horas, "etiqueta": v.etiqueta, "coste_usd": float(v.coste_usd)}
                for v in self.ventanas
            ],
        }


_DIAS_CORTOS = {1: "lun", 2: "mar", 3: "mié", 4: "jue", 5: "vie", 6: "sáb", 7: "dom"}


def _etiqueta_ventana(inicio: datetime, fin: datetime) -> str:
    a = inicio.astimezone(ft.tz())
    b = fin.astimezone(ft.tz())
    return f"{_DIAS_CORTOS[a.isoweekday()]} {a:%H:%M} → {_DIAS_CORTOS[b.isoweekday()]} {b:%H:%M}"


def rachas_vacias(rejilla: Sequence[datetime], activas: set) -> List[Tuple[datetime, datetime, int]]:
    """Función PURA: tramos contiguos de la rejilla sin tráfico.

    Se separa del acceso a BD a propósito. Es la lógica con más casos borde de
    todo el módulo (huecos al principio y al final, rejilla fragmentada por los
    filtros de día y franja) y así se puede probar sin PostgreSQL, en un panel
    que hasta ahora no tenía ni un test.

    Una racha se corta cuando hay tráfico O cuando la rejilla salta (p.ej. de
    las 18:00 del viernes a las 08:00 del lunes si se filtró la franja): dos
    horas no adyacentes no forman una ventana continua.
    """
    rachas: List[Tuple[datetime, datetime, int]] = []
    inicio: Optional[datetime] = None
    anterior: Optional[datetime] = None

    for hora in rejilla:
        contigua = anterior is not None and hora - anterior == timedelta(hours=1)
        if hora in activas:
            if inicio is not None:
                rachas.append((inicio, anterior + timedelta(hours=1),
                               int((anterior - inicio).total_seconds() // 3600) + 1))
                inicio = None
        else:
            if inicio is not None and not contigua:
                rachas.append((inicio, anterior + timedelta(hours=1),
                               int((anterior - inicio).total_seconds() // 3600) + 1))
                inicio = hora
            elif inicio is None:
                inicio = hora
        anterior = hora

    if inicio is not None and anterior is not None:
        rachas.append((inicio, anterior + timedelta(hours=1),
                       int((anterior - inicio).total_seconds() // 3600) + 1))
    return rachas


def ventanas_ociosas(f: ft.FiltrosTemporales, top: int = 5) -> ResumenOcio:
    """Tramos más largos sin una sola petición dentro del rango filtrado."""
    activas = set(
        _base_horaria(f).filter(request_count__gt=0)
        .values_list("bucket_ts", flat=True).distinct()
    )
    # Cualquier hora con bucket agregado (aunque sea con 0 peticiones) cuenta
    # como "observada": distinguirla de "sin datos" evita que el % de ocio
    # mienta espectacularmente cuando usage_hourly aún no tiene histórico.
    con_bucket = set(_base_horaria(f).values_list("bucket_ts", flat=True).distinct())

    rejilla = ft.horas_de_la_rejilla(f)
    rachas = rachas_vacias(rejilla, activas)

    coste_hora = Decimal(str(getattr(settings, "METRICS_COSTE_HORA_USD", 0) or 0))
    ventanas = [
        VentanaOciosa(inicio=i, fin=fin, horas=h, etiqueta=_etiqueta_ventana(i, fin),
                      coste_usd=(coste_hora * h).quantize(Decimal("0.0001")))
        for i, fin, h in sorted(rachas, key=lambda r: r[2], reverse=True)[:top]
    ]

    horas_totales = len(rejilla)
    horas_ociosas = sum(1 for h in rejilla if h not in activas)
    horas_sin_datos = sum(1 for h in rejilla if h not in con_bucket)

    return ResumenOcio(
        ventanas=ventanas,
        horas_totales=horas_totales,
        horas_ociosas=horas_ociosas,
        horas_sin_datos=horas_sin_datos,
        pct_ocioso=round(horas_ociosas / horas_totales * 100, 1) if horas_totales else 0.0,
        coste_hora_usd=coste_hora,
        coste_ocioso_usd=(coste_hora * horas_ociosas).quantize(Decimal("0.0001")),
    )


# =============================================================================
# PERCENTILES SOBRE LA VENTANA COMPLETA
# =============================================================================
def percentiles_latencia(f: ft.FiltrosTemporales) -> Dict[str, Optional[int]]:
    """Único camino soportado para un percentil de una ventana > 1 h.
    Delega en la función SQL, que hace el trabajo sobre los eventos crudos."""
    with connection.cursor() as cur:
        cur.execute(
            "SELECT muestras, p50_ms, p95_ms, p99_ms, ttft_p50_ms, ttft_p95_ms "
            "FROM sooniverse.latency_percentiles(%s, %s, %s, %s, FALSE)",
            [f.inicio, f.fin,
             list(f.api_key_ids) or None,
             list(f.modelos) or None],
        )
        fila = cur.fetchone()
    if not fila:
        return {"muestras": 0}
    return {
        "muestras": fila[0], "p50_ms": fila[1], "p95_ms": fila[2],
        "p99_ms": fila[3], "ttft_p50_ms": fila[4], "ttft_p95_ms": fila[5],
    }
