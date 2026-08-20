"""
==============================================================================
Filtros temporales: el ÚNICO sitio donde se resuelve la zona horaria
==============================================================================
Este módulo existe para que ninguna consulta del panel vuelva a escribir un
`AT TIME ZONE` ni un `ZoneInfo` a mano.

Contexto del bug que evita: `token_usage_rollup` se cortaba con la zona horaria
de la SESIÓN de PostgreSQL. Django, con `USE_TZ=True`, fija la conexión en UTC,
mientras el panel renderiza en `settings.TIME_ZONE` (America/Bogota): había 5 h
de desfase entre el día del bucket y el día que veía el usuario, y el corte
cambiaba según quién disparara el ETL. `database/004_usage_analytics.sql` lo
arregló en el motor; aquí se mantiene la coherencia del lado del ORM.

REGLAS PARA TODA CONSULTA NUEVA
  1. Acotar SIEMPRE con `campo__gte=f.inicio` / `campo__lt=f.fin` (intervalo
     semiabierto, límites *aware*). NUNCA con `__date__gte`: ese lookup evalúa
     la fecha en la zona de la conexión Y aplica una función sobre la columna,
     lo que además anula el índice `idx_usage_event_ts`.
  2. Para agrupar por hora o día de la semana, usar las columnas
     `bucket_local_hour` / `bucket_local_isodow` de `usage_hourly`, que ya vienen
     cortadas en hora local. Solo si hay que ir a `token_usage_event` se usa
     `EXTRACT(... AT TIME ZONE %s)`, siempre parametrizado, nunca con la zona
     como literal.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from typing import List, Optional, Tuple

from django.conf import settings

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    from backports.zoneinfo import ZoneInfo  # type: ignore[import-not-found]


def tz():
    """Zona de reporte del panel. Debe coincidir con la que usan las funciones
    de agregación SQL (`sooniverse.app_setting.reporting_timezone`, que
    `scripts/db_setup.py` sincroniza desde `.env:TIME_ZONE`)."""
    return ZoneInfo(settings.TIME_ZONE)


DAILY = "daily"
WEEKLY = "weekly"
MONTHLY = "monthly"

GRANULARIDADES_PANEL = [
    (DAILY, "Diario"),
    (WEEKLY, "Semanal"),
    (MONTHLY, "Mensual"),
]
# El mapa de calor en modo p95 escanea token_usage_event crudo con
# percentile_cont: es la consulta más cara del panel y necesita un tope duro.
P95_MAX_DIAS = 90

ESTADO_TODAS = "todas"
ESTADO_ERRORES = "errores"
ESTADOS = [(ESTADO_TODAS, "Todas"), (ESTADO_ERRORES, "Solo errores")]

DIAS_SEMANA = [
    (1, "Lun"), (2, "Mar"), (3, "Mié"), (4, "Jue"), (5, "Vie"), (6, "Sáb"), (7, "Dom"),
]
DIAS_LABORABLES = (1, 2, 3, 4, 5)
DIAS_FIN_DE_SEMANA = (6, 7)


@dataclass(frozen=True)
class FiltrosTemporales:
    """Estado completo de filtrado de una consulta del panel."""

    desde: date
    hasta: date
    api_key_ids: Tuple[int, ...] = ()
    modelos: Tuple[str, ...] = ()
    dias_semana: Tuple[int, ...] = ()      # ISO 1=lunes .. 7=domingo; () = todos
    hora_desde: int = 0                    # 0-23 inclusive
    hora_hasta: int = 23                   # 0-23 inclusive
    estado: str = ESTADO_TODAS
    incluir_benchmark: bool = False

    # -- límites absolutos, aware, semiabiertos [inicio, fin) ------------------
    @property
    def inicio(self) -> datetime:
        return datetime.combine(self.desde, time.min, tzinfo=tz())

    @property
    def fin(self) -> datetime:
        return datetime.combine(self.hasta + timedelta(days=1), time.min, tzinfo=tz())

    @property
    def dias(self) -> int:
        return (self.hasta - self.desde).days + 1

    @property
    def usa_dimension_horaria(self) -> bool:
        """True si el filtro necesita granularidad sub-diaria. `token_usage_rollup`
        es diario y no puede responder a esto: hay que ir a `usage_hourly`."""
        return bool(self.dias_semana) or (self.hora_desde, self.hora_hasta) != (0, 23)

    @property
    def horas_seleccionadas(self) -> List[int]:
        return list(range(self.hora_desde, self.hora_hasta + 1))

    @property
    def dias_seleccionados(self) -> Tuple[int, ...]:
        return self.dias_semana or tuple(d for d, _ in DIAS_SEMANA)

    def periodo_anterior(self) -> "FiltrosTemporales":
        """Mismo tamaño de ventana, inmediatamente antes. Para el modo comparar."""
        span = self.hasta - self.desde
        nuevo_hasta = self.desde - timedelta(days=1)
        return replace(self, hasta=nuevo_hasta, desde=nuevo_hasta - span)

    def eco(self) -> dict:
        """Lo que el cliente necesita para pintar los chips de filtro activo.
        Un filtro aplicado que no se ve en pantalla es un usuario engañado."""
        return {
            "dow": list(self.dias_semana),
            "hora_desde": self.hora_desde,
            "hora_hasta": self.hora_hasta,
            "estado": self.estado,
            "benchmark_excluida": not self.incluir_benchmark,
            "dias": self.dias,
        }


def horas_de_la_rejilla(f: FiltrosTemporales) -> List[datetime]:
    """Todas las horas del rango que pasan los filtros de día y franja.

    Se construye sumando `timedelta(hours=1)` a un datetime *aware* en vez de
    con `range(24)` por día: así un cambio de `TIME_ZONE` a una zona con horario
    de verano no rompe la rejilla en silencio.
    """
    salida: List[datetime] = []
    actual = f.inicio
    fin = f.fin
    dias_ok = set(f.dias_seleccionados)
    while actual < fin:
        local = actual.astimezone(tz())
        if local.isoweekday() in dias_ok and f.hora_desde <= local.hour <= f.hora_hasta:
            salida.append(actual)
        actual += timedelta(hours=1)
    return salida


def rango_por_defecto(dias: int = 30) -> Tuple[date, date]:
    from django.utils import timezone as dj_timezone
    hasta = dj_timezone.localdate()
    return hasta - timedelta(days=dias), hasta


def ids_de_benchmark() -> List[int]:
    """API Keys marcadas como tráfico sintético del test de capacidad.

    Se filtra por la columna `proposito` (contrato con CHECK en la BD) y no por
    el alias: un alias es texto libre que el operador puede reutilizar.
    """
    from .models import ApiKeyRegistry
    return list(
        ApiKeyRegistry.objects.filter(proposito="benchmark").values_list("id", flat=True)
    )


def excluir_benchmark(qs, campo: str = "api_key"):
    """Quita el tráfico del benchmark SIN perder las filas sin API Key registrada.

    Trampa de Django que hay que evitar: `qs.exclude(api_key__proposito=...)`
    genera un NOT IN sobre un LEFT JOIN que **también descarta las filas con
    api_key_id NULL** (eventos cuya key no está en el registro), que son
    legítimas y suelen ser mayoría en un despliegue recién hecho.
    """
    from django.db.models import Q
    return qs.filter(Q(**{f"{campo}__isnull": True}) | ~Q(**{f"{campo}__proposito": "benchmark"}))
