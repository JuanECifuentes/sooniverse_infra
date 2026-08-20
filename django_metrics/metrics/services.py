"""
==============================================================================
Lógica de dominio: agregación de métricas y ciclo de vida de API Keys
==============================================================================
Las vistas quedan delgadas; aquí vive el acceso a datos y las llamadas al proxy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.db import connection, transaction
from django.db.models import Count, Max, Q, Sum
from django.utils import timezone

from .litellm_client import LiteLLMClient, LiteLLMError
from .models import ApiKeyAudit, ApiKeyRegistry, TokenUsageEvent, TokenUsageRollup, WorkerNode

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
) -> ResumenMetricas:
    """
    Construye el resumen del panel leyendo las agregaciones pre-calculadas.
    `api_key_ids`/`modelos` filtran por una o varias API Keys / modelos a la vez.

    Los cinco primeros parámetros conservan nombre, orden y semántica: `serie_json`
    (contrato externo heredado) y `detalle_api_key` los pasan posicionalmente.
    Todo lo añadido después va keyword-only con default que reproduce el
    comportamiento anterior.
    """
    if granularity not in GRANULARITY_WINDOWS:
        granularity = TokenUsageRollup.DAILY

    hasta = hasta or timezone.localdate()
    desde = desde or (hasta - timedelta(days=GRANULARITY_WINDOWS[granularity]))

    qs = TokenUsageRollup.objects.filter(
        granularity=granularity, bucket_start__gte=desde, bucket_start__lte=hasta
    )
    if api_key_ids:
        qs = qs.filter(api_key_id__in=api_key_ids)
    if modelos:
        qs = qs.filter(model_name__in=modelos)
    if not incluir_benchmark:
        from .filtros import excluir_benchmark
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
            "created_at", "deactivated_at", "expires_at",
            "consumo_total", "consumo_prompt", "consumo_completion",
            "gasto", "peticiones", "ultimo_uso",
        )
    )


def estado_pool() -> Dict[str, Any]:
    """Salud del pool de workers vLLM detrás del balanceador."""
    nodos = list(WorkerNode.objects.all())
    cliente = LiteLLMClient()

    estado = {
        "nodos": nodos,
        "nodos_totales": len(nodos),
        "nodos_sanos": sum(1 for n in nodos if n.is_healthy),
        "litellm_ok": cliente.is_reachable(),
        "modelos": [],
    }
    if estado["litellm_ok"]:
        try:
            estado["modelos"] = cliente.models()
        except LiteLLMError as exc:
            logger.warning("No se pudieron listar los modelos de LiteLLM: %s", exc)

    return estado


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

    return {
        "eventos_ingeridos": ingeridos,
        "filas_agregadas": agregados,
        "buckets_horarios": horarios,
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
        expires_at=None,
    )

    _audit(registro, "created", actor, {
        "modelos": modelos or [],
        "max_budget_usd": max_budget,
        "rpm_limit": rpm_limit,
        "tpm_limit": tpm_limit,
        "duration": duration,
    }, ip)

    return {"registro": registro, "key_plaintext": key_plaintext}


@transaction.atomic
def desactivar_api_key(key_id: int, actor: str = "system", ip: Optional[str] = None) -> ApiKeyRegistry:
    registro = ApiKeyRegistry.objects.get(pk=key_id)

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
        "eventos_recientes": list(
            TokenUsageEvent.objects.filter(api_key_id=key_id)
            .values("event_ts", "model_name", "prompt_tokens", "completion_tokens",
                    "total_tokens", "spend_usd", "status", "latency_ms")[:50]
        ),
    }


# -- helpers de configuración --------------------------------------------------
def _settings_cliente() -> str:
    from django.conf import settings
    return settings.CLIENTE_ID


def _settings_entorno() -> str:
    from django.conf import settings
    entorno = settings.ENTORNO
    return entorno if entorno in ("prod", "dev", "staging") else "prod"
