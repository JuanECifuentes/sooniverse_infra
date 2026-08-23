"""
==============================================================================
Lógica de dominio: agregación de métricas y ciclo de vida de API Keys
==============================================================================
Las vistas quedan delgadas; aquí vive el acceso a datos y las llamadas al proxy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Count, Max, Q, Sum
from django.utils import timezone

from . import workers as workers_mod
from .litellm_client import LiteLLMClient, LiteLLMError
from .models import ApiKeyAudit, ApiKeyRegistry, TokenUsageEvent, TokenUsageRollup, WorkerAction, WorkerNode
from .workers import WorkerActionError

logger = logging.getLogger(__name__)

# Ventana temporal por defecto de cada granularidad (en días hacia atrás)
GRANULARITY_WINDOWS = {
    TokenUsageRollup.DAILY: 30,
    TokenUsageRollup.WEEKLY: 168,   # ~24 semanas
    TokenUsageRollup.MONTHLY: 730,  # ~24 meses
}

GRANULARITY_LABELS = {
    TokenUsageRollup.DAILY: "Diario",
    TokenUsageRollup.WEEKLY: "Semanal",
    TokenUsageRollup.MONTHLY: "Mensual",
}


# =============================================================================
# MÉTRICAS
# =============================================================================
@dataclass
class SeriePunto:
    """Un bucket de la serie temporal, listo para renderizar en la gráfica."""

    periodo: date
    etiqueta: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    request_count: int = 0
    spend_usd: Decimal = Decimal("0")
    error_count: int = 0
    # Alto relativo de la barra (0-100), calculado sobre el máximo de la serie.
    altura_pct: float = 0.0


@dataclass
class ResumenMetricas:
    granularity: str
    granularity_label: str
    api_key_ids: List[int]
    desde: date
    hasta: date
    serie: List[SeriePunto] = field(default_factory=list)
    total_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    request_count: int = 0
    spend_usd: Decimal = Decimal("0")
    error_count: int = 0
    por_modelo: List[Dict[str, Any]] = field(default_factory=list)
    por_api_key: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def ratio_completion(self) -> float:
        return round(self.completion_tokens / self.total_tokens * 100, 1) if self.total_tokens else 0.0

    @property
    def tokens_por_request(self) -> int:
        return round(self.total_tokens / self.request_count) if self.request_count else 0

    @property
    def tasa_error(self) -> float:
        return round(self.error_count / self.request_count * 100, 1) if self.request_count else 0.0


def _etiqueta_periodo(periodo: date, granularity: str) -> str:
    if granularity == TokenUsageRollup.MONTHLY:
        return periodo.strftime("%b %Y")
    if granularity == TokenUsageRollup.WEEKLY:
        return f"S{periodo.isocalendar().week} · {periodo:%d/%m}"
    return periodo.strftime("%d/%m")


def obtener_metricas(
    granularity: str = TokenUsageRollup.DAILY,
    api_key_ids: Optional[List[int]] = None,
    modelos: Optional[List[str]] = None,
    desde: Optional[date] = None,
    hasta: Optional[date] = None,
    *,
    incluir_benchmark: bool = False,
    dias_semana: Optional[Sequence[int]] = None,
    hora_desde: int = 0,
    hora_hasta: int = 23,
    solo_errores: bool = False,
) -> ResumenMetricas:
    """
    Construye el resumen del panel leyendo las agregaciones pre-calculadas.
    `api_key_ids`/`modelos` filtran por una o varias API Keys / modelos a la vez.

    Los cinco primeros parámetros conservan nombre, orden y semántica: `serie_json`
    (contrato externo heredado) y `detalle_api_key` los pasan posicionalmente.
    Todo lo añadido después va keyword-only con default que reproduce el
    comportamiento anterior.
    """
    from .filtros import FiltrosTemporales, excluir_benchmark
    from .models import UsageHourly

    if granularity not in GRANULARITY_WINDOWS:
        granularity = TokenUsageRollup.DAILY

    hasta = hasta or timezone.localdate()
    desde = desde or (hasta - timedelta(days=GRANULARITY_WINDOWS[granularity]))

    usa_horaria = (
        (hora_desde, hora_hasta) != (0, 23)
        or solo_errores
        or (bool(dias_semana) and granularity in (TokenUsageRollup.WEEKLY, TokenUsageRollup.MONTHLY))
    )

    if usa_horaria:
        ventana = FiltrosTemporales(desde=desde, hasta=hasta)
        qs_h = UsageHourly.objects.filter(bucket_ts__gte=ventana.inicio, bucket_ts__lt=ventana.fin)
        if api_key_ids:
            qs_h = qs_h.filter(api_key_id__in=api_key_ids)
        if modelos:
            qs_h = qs_h.filter(model_name__in=modelos)
        if dias_semana:
            qs_h = qs_h.filter(bucket_local_isodow__in=dias_semana)
        if (hora_desde, hora_hasta) != (0, 23):
            qs_h = qs_h.filter(bucket_local_hour__gte=hora_desde, bucket_local_hour__lte=hora_hasta)
        if solo_errores:
            qs_h = qs_h.filter(error_count__gt=0)
        if not incluir_benchmark:
            qs_h = excluir_benchmark(qs_h)

        buckets_raw = (
            qs_h.values("bucket_local_date")
            .annotate(
                prompt_tokens=Sum("prompt_tokens"),
                completion_tokens=Sum("completion_tokens"),
                total_tokens=Sum("total_tokens"),
                request_count=Sum("request_count"),
                spend_usd=Sum("spend_usd"),
                error_count=Sum("error_count"),
            )
            .order_by("bucket_local_date")
        )

        if granularity == TokenUsageRollup.DAILY:
            serie = [
                SeriePunto(
                    periodo=b["bucket_local_date"],
                    etiqueta=_etiqueta_periodo(b["bucket_local_date"], granularity),
                    prompt_tokens=b["prompt_tokens"] or 0,
                    completion_tokens=b["completion_tokens"] or 0,
                    total_tokens=b["total_tokens"] or 0,
                    request_count=b["request_count"] or 0,
                    spend_usd=b["spend_usd"] or Decimal("0"),
                    error_count=b["error_count"] or 0,
                )
                for b in buckets_raw
            ]
        else:
            # Agrupación semanal o mensual sobre los días filtrados
            agrupados: Dict[date, Dict[str, Any]] = {}
            for b in buckets_raw:
                d = b["bucket_local_date"]
                if granularity == TokenUsageRollup.WEEKLY:
                    periodo_inicio = d - timedelta(days=d.weekday())
                else:
                    periodo_inicio = date(d.year, d.month, 1)

                if periodo_inicio not in agrupados:
                    agrupados[periodo_inicio] = {
                        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                        "request_count": 0, "spend_usd": Decimal("0"), "error_count": 0,
                    }
                item = agrupados[periodo_inicio]
                item["prompt_tokens"] += b["prompt_tokens"] or 0
                item["completion_tokens"] += b["completion_tokens"] or 0
                item["total_tokens"] += b["total_tokens"] or 0
                item["request_count"] += b["request_count"] or 0
                item["spend_usd"] += b["spend_usd"] or Decimal("0")
                item["error_count"] += b["error_count"] or 0

            serie = [
                SeriePunto(
                    periodo=p,
                    etiqueta=_etiqueta_periodo(p, granularity),
                    prompt_tokens=datos["prompt_tokens"],
                    completion_tokens=datos["completion_tokens"],
                    total_tokens=datos["total_tokens"],
                    request_count=datos["request_count"],
                    spend_usd=datos["spend_usd"],
                    error_count=datos["error_count"],
                )
                for p, datos in sorted(agrupados.items(), key=lambda x: x[0])
            ]

        pico = max((p.total_tokens for p in serie), default=0)
        for punto in serie:
            punto.altura_pct = round(punto.total_tokens / pico * 100, 2) if pico else 0.0

        resumen = ResumenMetricas(
            granularity=granularity,
            granularity_label=GRANULARITY_LABELS[granularity],
            api_key_ids=list(api_key_ids or []),
            desde=desde,
            hasta=hasta,
            serie=serie,
            total_tokens=sum(p.total_tokens for p in serie),
            prompt_tokens=sum(p.prompt_tokens for p in serie),
            completion_tokens=sum(p.completion_tokens for p in serie),
            request_count=sum(p.request_count for p in serie),
            spend_usd=sum((p.spend_usd for p in serie), Decimal("0")),
            error_count=sum(p.error_count for p in serie),
        )

        resumen.por_modelo = list(
            qs_h.values("model_name")
            .annotate(
                total_tokens=Sum("total_tokens"),
                prompt_tokens=Sum("prompt_tokens"),
                completion_tokens=Sum("completion_tokens"),
                request_count=Sum("request_count"),
                spend_usd=Sum("spend_usd"),
            )
            .order_by("-total_tokens")[:12]
        )

        if not (api_key_ids and len(api_key_ids) == 1):
            resumen.por_api_key = list(
                qs_h.values("api_key_id", "api_key__key_alias", "api_key__is_active")
                .annotate(
                    total_tokens=Sum("total_tokens"),
                    prompt_tokens=Sum("prompt_tokens"),
                    completion_tokens=Sum("completion_tokens"),
                    request_count=Sum("request_count"),
                    spend_usd=Sum("spend_usd"),
                )
                .order_by("-total_tokens")[:25]
            )

        return resumen

    # --- camino estándar sobre TokenUsageRollup -------------------------------
    qs = TokenUsageRollup.objects.filter(
        granularity=granularity, bucket_start__gte=desde, bucket_start__lte=hasta
    )
    if api_key_ids:
        qs = qs.filter(api_key_id__in=api_key_ids)
    if modelos:
        qs = qs.filter(model_name__in=modelos)
    if dias_semana:
        qs = qs.filter(bucket_start__iso_week_day__in=dias_semana)
    if not incluir_benchmark:
        qs = excluir_benchmark(qs)

    # --- serie temporal -------------------------------------------------------
    buckets = (
        qs.values("bucket_start")
        .annotate(
            prompt_tokens=Sum("prompt_tokens"),
            completion_tokens=Sum("completion_tokens"),
            total_tokens=Sum("total_tokens"),
            request_count=Sum("request_count"),
            spend_usd=Sum("spend_usd"),
            error_count=Sum("error_count"),
        )
        .order_by("bucket_start")
    )

    serie = [
        SeriePunto(
            periodo=b["bucket_start"],
            etiqueta=_etiqueta_periodo(b["bucket_start"], granularity),
            prompt_tokens=b["prompt_tokens"] or 0,
            completion_tokens=b["completion_tokens"] or 0,
            total_tokens=b["total_tokens"] or 0,
            request_count=b["request_count"] or 0,
            spend_usd=b["spend_usd"] or Decimal("0"),
            error_count=b["error_count"] or 0,
        )
        for b in buckets
    ]

    pico = max((p.total_tokens for p in serie), default=0)
    for punto in serie:
        punto.altura_pct = round(punto.total_tokens / pico * 100, 2) if pico else 0.0

    resumen = ResumenMetricas(
        granularity=granularity,
        granularity_label=GRANULARITY_LABELS[granularity],
        api_key_ids=list(api_key_ids or []),
        desde=desde,
        hasta=hasta,
        serie=serie,
        total_tokens=sum(p.total_tokens for p in serie),
        prompt_tokens=sum(p.prompt_tokens for p in serie),
        completion_tokens=sum(p.completion_tokens for p in serie),
        request_count=sum(p.request_count for p in serie),
        spend_usd=sum((p.spend_usd for p in serie), Decimal("0")),
        error_count=sum(p.error_count for p in serie),
    )

    # --- desglose por modelo --------------------------------------------------
    resumen.por_modelo = list(
        qs.values("model_name")
        .annotate(
            total_tokens=Sum("total_tokens"),
            prompt_tokens=Sum("prompt_tokens"),
            completion_tokens=Sum("completion_tokens"),
            request_count=Sum("request_count"),
            spend_usd=Sum("spend_usd"),
        )
        .order_by("-total_tokens")[:12]
    )

    # --- desglose por API Key (solo cuando no hay exactamente una key filtrada) -
    if not (api_key_ids and len(api_key_ids) == 1):
        resumen.por_api_key = list(
            qs.values("api_key_id", "api_key__key_alias", "api_key__is_active")
            .annotate(
                total_tokens=Sum("total_tokens"),
                prompt_tokens=Sum("prompt_tokens"),
                completion_tokens=Sum("completion_tokens"),
                request_count=Sum("request_count"),
                spend_usd=Sum("spend_usd"),
            )
            .order_by("-total_tokens")[:25]
        )

    return resumen


PETICIONES_SORT_FIELDS = {"fecha": "event_ts", "coste": "spend_usd"}


def obtener_peticiones(
    api_key_ids: Optional[List[int]] = None,
    modelos: Optional[List[str]] = None,
    desde: Optional[date] = None,
    hasta: Optional[date] = None,
    page: int = 1,
    page_size: int = 30,
    sort_by: str = "fecha",
    sort_dir: str = "desc",
    *,
    incluir_benchmark: bool = False,
    solo_errores: bool = False,
    dias_semana: Optional[Sequence[int]] = None,
    hora_desde: int = 0,
    hora_hasta: int = 23,
) -> Dict[str, Any]:
    """Lista paginada de peticiones individuales (fila a fila) para la card
    de detalle del dashboard. A diferencia de `obtener_metricas`, consulta
    TokenUsageEvent directamente: el rollup no tiene granularidad de petición."""
    from .filtros import FiltrosTemporales, excluir_benchmark

    hasta = hasta or timezone.localdate()
    desde = desde or (hasta - timedelta(days=30))

    # Límites aware y semiabiertos en vez de `event_ts__date__gte`: ese lookup
    # evaluaba la fecha en la zona de la CONEXIÓN (UTC con USE_TZ=True) en vez
    # de en la del panel, y además aplicaba una función sobre la columna, lo que
    # anulaba el índice idx_usage_event_ts.
    ventana = FiltrosTemporales(desde=desde, hasta=hasta)
    qs = TokenUsageEvent.objects.filter(event_ts__gte=ventana.inicio, event_ts__lt=ventana.fin)
    if api_key_ids:
        qs = qs.filter(api_key_id__in=api_key_ids)
    if modelos:
        qs = qs.filter(model_name__in=modelos)
    if solo_errores:
        qs = qs.exclude(status="success")
    if dias_semana:
        qs = qs.filter(event_ts__iso_week_day__in=dias_semana)
    if (hora_desde, hora_hasta) != (0, 23):
        qs = qs.filter(event_ts__hour__gte=hora_desde, event_ts__hour__lte=hora_hasta)
    if not incluir_benchmark:
        qs = excluir_benchmark(qs)

    campo = PETICIONES_SORT_FIELDS.get(sort_by, "event_ts")
    prefijo = "-" if sort_dir == "desc" else ""
    qs = qs.select_related("api_key").order_by(f"{prefijo}{campo}", "-id")

    total = qs.count()
    inicio = (page - 1) * page_size
    items = list(qs[inicio:inicio + page_size])
    total_pages = max(1, -(-total // page_size))  # ceil(total / page_size)

    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": inicio + page_size < total,
    }


def modelos_unicos() -> List[str]:
    """Nombres de modelo con datos, deduplicados sin distinguir mayúsculas ni
    espacios sobrantes. Cubre inconsistencias reales de la BD de pruebas
    (p. ej. 'gpt-4o' y 'GPT-4o ' no deben aparecer como dos opciones)."""
    vistos: Dict[str, str] = {}
    for crudo in TokenUsageRollup.objects.values_list("model_name", flat=True).distinct():
        limpio = (crudo or "").strip()
        if not limpio:
            continue
        clave = limpio.lower()
        vistos.setdefault(clave, limpio)
    return sorted(vistos.values(), key=str.lower)


def resumen_api_keys(solo_activas: bool = False) -> List[Dict[str, Any]]:
    """Listado de API Keys con su consumo acumulado (para la tabla del gestor)."""
    qs = ApiKeyRegistry.objects.all()
    if solo_activas:
        qs = qs.filter(is_active=True)

    return list(
        qs.annotate(
            consumo_total=Sum("events__total_tokens"),
            consumo_prompt=Sum("events__prompt_tokens"),
            consumo_completion=Sum("events__completion_tokens"),
            gasto=Sum("events__spend_usd"),
            peticiones=Count("events"),
            ultimo_uso=Max("events__event_ts"),
        ).values(
            "id", "key_alias", "key_prefix", "cliente_id", "entorno", "is_active",
            "owner_email", "descripcion", "max_budget_usd", "rpm_limit", "tpm_limit",
            "created_at", "deactivated_at", "expires_at", "origen",
            "consumo_total", "consumo_prompt", "consumo_completion",
            "gasto", "peticiones", "ultimo_uso",
        )
    )


def estado_pool() -> Dict[str, Any]:
    """Salud del pool de workers vLLM detrás del balanceador.

    Filtra por el cliente/entorno de ESTE panel (prefijo del nombre de clúster,
    igual que `scripts/sync_endpoints.py::cluster_names`) -sin esto, filas de
    despliegues ya destruidos de OTRO cliente/entorno se pintaban como nodos
    vivos: `worker_node` nunca borra filas, solo las marca `is_healthy=false`.

    `estado_operativo` se RECALCULA aquí en cada carga en vez de confiar
    ciegamente en lo que dejó la última corrida de `sync_endpoints.py`: si
    nadie ha vuelto a sincronizar en `2 x METRICS_REFRESH_INTERVAL`, el dato es
    viejo aunque diga 'sano' -eso es justo lo que 'desincronizado' debe
    capturar. La excepción es 'apagado' (parada deliberada desde el panel, ver
    `workers.py::apagar_worker`): esa sí es una lectura fresca y correcta -un
    nodo apagado a propósito no reaparece en ninguna sincronización futura
    hasta que se arranque, así que "antiguo" y "apagado" son cosas distintas.
    """
    prefix = f"sooniverse-{settings.CLIENTE_ID}-{settings.ENTORNO}-"
    nodos = list(WorkerNode.objects.filter(cluster_name__startswith=prefix))
    cliente = LiteLLMClient()

    litellm_ok = cliente.is_reachable()
    healthy_endpoints: set = set()
    unhealthy_endpoints: set = set()
    if litellm_ok:
        try:
            health = cliente.health()
            healthy_endpoints = {e.get("api_base") for e in health.get("healthy_endpoints", [])}
            unhealthy_endpoints = {e.get("api_base") for e in health.get("unhealthy_endpoints", [])}
        except LiteLLMError as exc:
            logger.warning("No se pudo leer /health de LiteLLM: %s", exc)

    umbral_frescura = timedelta(seconds=2 * settings.METRICS_REFRESH_INTERVAL)
    ahora = timezone.now()

    for nodo in nodos:
        if nodo.estado_operativo == "apagado":
            continue
        obsoleto = not nodo.last_seen_at or (ahora - nodo.last_seen_at) > umbral_frescura
        if obsoleto:
            nodo.estado_operativo = "desincronizado"
        elif not nodo.is_healthy or nodo.endpoint in unhealthy_endpoints:
            nodo.estado_operativo = "degradado"
        elif litellm_ok and nodo.endpoint not in healthy_endpoints:
            # LiteLLM alcanzable pero no reconoce este endpoint en absoluto
            # -nunca se sincronizó litellm_config.yaml con él, o se retiró.
            nodo.estado_operativo = "degradado"
        else:
            nodo.estado_operativo = "sano"

    estado = {
        "nodos": nodos,
        "nodos_totales": len(nodos),
        "nodos_sanos": sum(1 for n in nodos if n.estado_operativo == "sano"),
        "litellm_ok": litellm_ok,
        "modelos": [],
        # Degradación explícita: si falta la clave SSH o boto3/el permiso IAM,
        # los botones correspondientes se deshabilitan en la plantilla en vez
        # de fallar al pulsarlos.
        "restart_disponible": workers_mod.restart_disponible(),
        "ec2_disponible": workers_mod.ec2_disponible(),
    }
    if litellm_ok:
        try:
            estado["modelos"] = cliente.models()
        except LiteLLMError as exc:
            logger.warning("No se pudieron listar los modelos de LiteLLM: %s", exc)

    return estado


def _worker_audit(worker_node: Optional[WorkerNode], accion: str, estado: str, actor: str,
                   mensaje: str = "", ip: Optional[str] = None) -> WorkerAction:
    return WorkerAction.objects.create(
        worker_node=worker_node,
        accion=accion,
        estado=estado,
        actor=actor or "system",
        source_ip=ip,
        mensaje=mensaje or None,
        created_at=timezone.now(),
        finished_at=timezone.now() if estado != "solicitada" else None,
    )


# Estado que deja cada acción exitosa en 'sano en tránsito': el siguiente
# sync_endpoints.py lo corrige a 'sano'/'degradado'/'desincronizado' según lo
# que de verdad encuentre. 'health' no muta nada -es de solo diagnóstico.
_ESTADO_TRAS_ACCION = {"restart": "reiniciando", "stop": "apagado", "start": "reiniciando"}


def ejecutar_accion_worker(worker: WorkerNode, accion: str, actor: str, ip: Optional[str] = None) -> str:
    """Orquesta una acción sobre un worker: registra la auditoría, delega la
    mecánica a `workers.py`, y actualiza `estado_operativo` en éxito. Deja que
    `WorkerActionError` se propague -la vista decide el mensaje al usuario."""
    if accion not in ("health", "restart", "stop", "start"):
        raise WorkerActionError(f"Acción desconocida: {accion}")

    try:
        if accion == "health":
            mensaje = workers_mod.comprobar_salud(worker)
        elif accion == "restart":
            mensaje = workers_mod.reiniciar_vllm(worker)
        elif accion == "stop":
            mensaje = workers_mod.apagar_worker(worker, settings.AWS_REGION)
        else:
            mensaje = workers_mod.arrancar_worker(worker, settings.AWS_REGION)
    except WorkerActionError as exc:
        _worker_audit(worker, accion, "error", actor, str(exc), ip)
        raise

    _worker_audit(worker, accion, "ok", actor, mensaje, ip)

    nuevo_estado = _ESTADO_TRAS_ACCION.get(accion)
    if nuevo_estado:
        worker.estado_operativo = nuevo_estado
        worker.save(update_fields=["estado_operativo"])

    return mensaje


def refrescar_metricas(since_hours: int = 48, since_days: int = 90) -> Dict[str, int]:
    """
    Dispara el ETL desde `LiteLLM_SpendLogs` y recalcula las agregaciones
    daily/weekly/monthly y la horaria, usando las funciones SQL del esquema.

    La zona horaria se pasa EXPLÍCITA. Con `USE_TZ=True` Django fija la conexión
    en UTC, así que sin este parámetro las funciones cortarían los buckets en
    UTC mientras el panel los renderiza en `settings.TIME_ZONE`: 5 h de desfase
    entre el día del bucket y el día que ve el usuario. No se arregla tocando
    `DATABASES.OPTIONS` -fijar la zona de la conexión rompería el manejo de
    timestamptz de Django-, se arregla aquí.
    """
    from django.conf import settings

    zona = settings.TIME_ZONE
    # La ventana horaria se acota: recalcular percentiles sobre 90 días de
    # eventos crudos en cada refresco periódico (cada 5 min) no compensa.
    horas_dias = min(since_days, 30)

    with connection.cursor() as cur:
        cur.execute("SELECT sooniverse.ingest_litellm_spendlogs(%s)", [since_hours])
        ingeridos = cur.fetchone()[0]
        cur.execute("SELECT sooniverse.refresh_usage_rollups(%s, %s)", [since_days, zona])
        agregados = cur.fetchone()[0]
        cur.execute("SELECT sooniverse.refresh_usage_hourly(%s, %s)", [horas_dias, zona])
        horarios = cur.fetchone()[0]
        # Inventario único de API Keys (006_workers_y_login.sql): mantiene el
        # espejo de solo lectura de las keys de Open WebUI al día. Fail-soft
        # en la propia función SQL si Open WebUI aún no migró su esquema.
        cur.execute("SELECT sooniverse.ingest_openwebui_apikeys()")
        keys_openwebui = cur.fetchone()[0]

    return {
        "eventos_ingeridos": ingeridos,
        "filas_agregadas": agregados,
        "buckets_horarios": horarios,
        "keys_openwebui": keys_openwebui,
    }


# =============================================================================
# CICLO DE VIDA DE API KEYS
# =============================================================================
def _audit(key: Optional[ApiKeyRegistry], action: str, actor: str,
           detalle: Optional[Dict[str, Any]] = None, ip: Optional[str] = None) -> None:
    ApiKeyAudit.objects.create(
        api_key_id=key.id if key else None,
        key_alias=key.key_alias if key else None,
        action=action,
        actor=actor or "system",
        detalle=detalle or {},
        source_ip=ip,
        created_at=timezone.now(),
    )


@transaction.atomic
def crear_api_key(
    alias: str,
    owner_email: str = "",
    descripcion: str = "",
    modelos: Optional[List[str]] = None,
    max_budget: Optional[float] = None,
    rpm_limit: Optional[int] = None,
    tpm_limit: Optional[int] = None,
    duration: Optional[str] = None,
    expires_at: Optional[date] = None,
    actor: str = "system",
    ip: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Emite la key en LiteLLM y la registra localmente.

    Devuelve `{'registro': ApiKeyRegistry, 'key_plaintext': 'sk-...'}`.
    La key en claro se muestra UNA sola vez y nunca se persiste.
    """
    cliente = LiteLLMClient()
    respuesta = cliente.generate_key(
        alias=alias,
        models=modelos,
        max_budget=max_budget,
        rpm_limit=rpm_limit,
        tpm_limit=tpm_limit,
        duration=duration,
        metadata={"owner_email": owner_email} if owner_email else None,
    )

    key_plaintext = respuesta.get("key", "")
    token_hash = respuesta.get("token") or respuesta.get("token_id") or key_plaintext

    ahora = timezone.now()
    registro = ApiKeyRegistry.objects.create(
        key_alias=alias,
        litellm_token_hash=token_hash,
        key_prefix=(key_plaintext[:8] + "…" + key_plaintext[-4:]) if len(key_plaintext) > 14 else None,
        cliente_id=_settings_cliente(),
        entorno=_settings_entorno(),
        owner_email=owner_email or None,
        descripcion=descripcion or None,
        is_active=True,
        max_budget_usd=max_budget,
        rpm_limit=rpm_limit,
        tpm_limit=tpm_limit,
        allowed_models=modelos or [],
        created_at=ahora,
        updated_at=ahora,
        # Antes siempre quedaba en NULL aunque el operador eligiera una
        # vigencia en el formulario -'duration' solo viajaba a LiteLLM (que
        # calcula su propio vencimiento interno), nunca se reflejaba aquí.
        expires_at=(
            timezone.make_aware(datetime.combine(expires_at, datetime.min.time()))
            if expires_at else None
        ),
    )

    _audit(registro, "created", actor, {
        "modelos": modelos or [],
        "max_budget_usd": max_budget,
        "rpm_limit": rpm_limit,
        "tpm_limit": tpm_limit,
        "duration": duration,
    }, ip)

    return {"registro": registro, "key_plaintext": key_plaintext}


class ApiKeyNoGestionableError(Exception):
    """Se intentó desactivar/reactivar una key que no es de LiteLLM (p.ej. un
    espejo de solo lectura de Open WebUI, ver ApiKeyRegistry.gestionable)."""


@transaction.atomic
def desactivar_api_key(key_id: int, actor: str = "system", ip: Optional[str] = None) -> ApiKeyRegistry:
    registro = ApiKeyRegistry.objects.get(pk=key_id)
    if not registro.gestionable:
        raise ApiKeyNoGestionableError(
            f"La API Key '{registro.key_alias}' es un espejo de Open WebUI: no se puede "
            "desactivar desde el panel."
        )

    if registro.litellm_token_hash:
        try:
            LiteLLMClient().block_key(registro.litellm_token_hash)
        except LiteLLMError as exc:
            # El registro local se marca igual: la key deja de aparecer como válida
            # en el panel aunque el proxy esté momentáneamente inalcanzable.
            logger.warning("No se pudo bloquear la key en LiteLLM: %s", exc)

    registro.is_active = False
    registro.deactivated_at = timezone.now()
    registro.save(update_fields=["is_active", "deactivated_at"])

    _audit(registro, "deactivated", actor, {}, ip)
    return registro


@transaction.atomic
def reactivar_api_key(key_id: int, actor: str = "system", ip: Optional[str] = None) -> ApiKeyRegistry:
    registro = ApiKeyRegistry.objects.get(pk=key_id)
    if not registro.gestionable:
        raise ApiKeyNoGestionableError(
            f"La API Key '{registro.key_alias}' es un espejo de Open WebUI: no se puede "
            "reactivar desde el panel."
        )

    if registro.litellm_token_hash:
        try:
            LiteLLMClient().unblock_key(registro.litellm_token_hash)
        except LiteLLMError as exc:
            logger.warning("No se pudo desbloquear la key en LiteLLM: %s", exc)

    registro.is_active = True
    registro.deactivated_at = None
    registro.save(update_fields=["is_active", "deactivated_at"])

    _audit(registro, "reactivated", actor, {}, ip)
    return registro


def detalle_api_key(
    key_id: int,
    granularity: str = TokenUsageRollup.DAILY,
    desde: Optional[date] = None,
    hasta: Optional[date] = None,
) -> Dict[str, Any]:
    registro = ApiKeyRegistry.objects.get(pk=key_id)
    return {
        "registro": registro,
        "metricas": obtener_metricas(granularity=granularity, api_key_ids=[key_id], desde=desde, hasta=hasta),
        "auditoria": list(ApiKeyAudit.objects.filter(api_key_id=key_id)[:50]),
    }


# -- helpers de configuración --------------------------------------------------
def _settings_cliente() -> str:
    from django.conf import settings
    return settings.CLIENTE_ID


def _settings_entorno() -> str:
    from django.conf import settings
    entorno = settings.ENTORNO
    return entorno if entorno in ("prod", "dev", "staging") else "prod"
