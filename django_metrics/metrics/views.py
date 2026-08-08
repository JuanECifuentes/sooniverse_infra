"""
==============================================================================
Vistas del panel de Métricas y API Keys
==============================================================================
Todas requieren staff autenticado: el panel expone gestión de credenciales.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import services
from .forms import ApiKeyForm, FiltroMetricasForm
from .litellm_client import LiteLLMError
from .models import ApiKeyRegistry, TokenUsageRollup

logger = logging.getLogger(__name__)


def _actor(request) -> str:
    return getattr(request.user, "username", None) or "anonimo"


def _ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return (forwarded.split(",")[0].strip() or request.META.get("REMOTE_ADDR")) or None


def _int_or_none(value):
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _ints_or_none(values):
    enteros = [v for v in (_int_or_none(v) for v in values) if v is not None]
    return enteros or None


def _date_or_none(value):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _peticiones_payload(resultado):
    return {
        "items": [
            {
                "ts": e.event_ts.isoformat(),
                "model": e.model_name,
                "input": e.prompt_tokens,
                "output": e.completion_tokens,
                "api_key": (e.api_key.key_alias if e.api_key_id and e.api_key else None) or "(sin registro)",
            }
            for e in resultado["items"]
        ],
        "page": resultado["page"],
        "page_size": resultado["page_size"],
        "total": resultado["total"],
        "total_pages": resultado["total_pages"],
        "has_prev": resultado["has_prev"],
        "has_next": resultado["has_next"],
    }


def _rango_por_defecto():
    hasta = timezone.localdate()
    return hasta - timedelta(days=settings.METRICS_DEFAULT_WINDOW_DAYS), hasta


# =============================================================================
# MÓDULO DE MÉTRICAS
# =============================================================================
@staff_member_required
def dashboard(request):
    """
    Panel de consumo de tokens con particiones Diaria / Semanal / Mensual y
    filtro por API Key específica.
    """
    api_keys = services.resumen_api_keys()
    modelos = sorted(
        TokenUsageRollup.objects.values_list("model_name", flat=True).distinct()
    )

    form = FiltroMetricasForm(request.GET or None, api_keys=api_keys, modelos=modelos)
    granularity = TokenUsageRollup.DAILY
    api_key_ids = []
    modelos_activos = []
    desde_default, hasta_default = _rango_por_defecto()
    desde, hasta = desde_default, hasta_default

    if form.is_valid():
        granularity = form.cleaned_data.get("granularity") or TokenUsageRollup.DAILY
        api_key_ids = _ints_or_none(form.cleaned_data.get("api_key") or []) or []
        modelos_activos = form.cleaned_data.get("modelo") or []
        desde = form.cleaned_data.get("desde") or desde_default
        hasta = form.cleaned_data.get("hasta") or hasta_default

    metricas = services.obtener_metricas(
        granularity=granularity, api_key_ids=api_key_ids, modelos=modelos_activos,
        desde=desde, hasta=hasta,
    )
    peticiones = services.obtener_peticiones(
        api_key_ids=api_key_ids, modelos=modelos_activos, desde=desde, hasta=hasta,
    )

    payload = _metricas_payload(metricas, api_key_ids)
    payload["requests"] = _peticiones_payload(peticiones)

    contexto = {
        "seccion": "metricas",
        "form": form,
        "metricas": metricas,
        "peticiones": peticiones,
        "granularities": TokenUsageRollup.GRANULARITIES,
        "granularity_activa": granularity,
        "api_key_ids_activos": api_key_ids,
        "modelos_activos": modelos_activos,
        "desde_activa": desde,
        "hasta_activa": hasta,
        "api_keys": api_keys,
        "modelos": modelos,
        "pool": services.estado_pool(),
        "metricas_payload": payload,
    }
    return render(request, "metrics/dashboard.html", contexto)


@staff_member_required
def serie_json(request):
    """Endpoint JSON de la serie temporal (para integraciones externas).
    Contrato heredado: valores únicos de `api_key`/`modelo` y ventana en `dias`."""
    api_key_id = _int_or_none(request.GET.get("api_key"))
    modelo = request.GET.get("modelo") or None
    dias = _int_or_none(request.GET.get("dias"))
    hasta = timezone.localdate()
    desde = (hasta - timedelta(days=dias)) if dias else None

    metricas = services.obtener_metricas(
        granularity=request.GET.get("granularity", TokenUsageRollup.DAILY),
        api_key_ids=[api_key_id] if api_key_id else None,
        modelos=[modelo] if modelo else None,
        desde=desde,
        hasta=hasta if dias else None,
    )

    return JsonResponse({
        "granularity": metricas.granularity,
        "api_key_id": metricas.api_key_ids[0] if len(metricas.api_key_ids) == 1 else None,
        "desde": metricas.desde.isoformat(),
        "hasta": metricas.hasta.isoformat(),
        "totales": {
            "prompt_tokens": metricas.prompt_tokens,
            "completion_tokens": metricas.completion_tokens,
            "total_tokens": metricas.total_tokens,
            "request_count": metricas.request_count,
            "spend_usd": float(metricas.spend_usd),
            "error_count": metricas.error_count,
        },
        "serie": [
            {
                "periodo": p.periodo.isoformat(),
                "etiqueta": p.etiqueta,
                "prompt_tokens": p.prompt_tokens,
                "completion_tokens": p.completion_tokens,
                "total_tokens": p.total_tokens,
                "request_count": p.request_count,
                "spend_usd": float(p.spend_usd),
            }
            for p in metricas.serie
        ],
        "por_modelo": [
            {**m, "spend_usd": float(m.get("spend_usd") or 0)} for m in metricas.por_modelo
        ],
    })


def _metricas_payload(metricas, api_key_ids):
    """Serializa un ResumenMetricas al contrato JSON que consume el panel
    (metrics-filters.js/metrics-charts.js). Usado tanto por `metrics_api` como
    por el bootstrap inicial que renderiza `dashboard` para el primer pintado."""
    return {
        "granularity": metricas.granularity,
        "granularity_label": metricas.granularity_label,
        "desde": metricas.desde.isoformat(),
        "hasta": metricas.hasta.isoformat(),
        "summary": {
            "request_count": metricas.request_count,
            "prompt_tokens": metricas.prompt_tokens,
            "completion_tokens": metricas.completion_tokens,
            "total_tokens": metricas.total_tokens,
            "spend_usd": float(metricas.spend_usd),
            "error_count": metricas.error_count,
            "tokens_por_request": metricas.tokens_por_request,
            "ratio_completion": metricas.ratio_completion,
        },
        "series": {
            "labels": [p.etiqueta for p in metricas.serie],
            "total_tokens": [p.total_tokens for p in metricas.serie],
            "prompt_tokens": [p.prompt_tokens for p in metricas.serie],
            "completion_tokens": [p.completion_tokens for p in metricas.serie],
            "request_count": [p.request_count for p in metricas.serie],
            "altura_pct": [p.altura_pct for p in metricas.serie],
        },
        "por_modelo": [
            {**m, "spend_usd": float(m.get("spend_usd") or 0)} for m in metricas.por_modelo
        ],
        "por_api_key": [
            {**k, "spend_usd": float(k.get("spend_usd") or 0)} for k in metricas.por_api_key
        ] if metricas.por_api_key else [],
        "mostrar_desglose_api_key": not (api_key_ids and len(api_key_ids) == 1),
    }


@staff_member_required
def metrics_api(request):
    """
    Endpoint JSON consumido por el filtrado asíncrono del panel (metrics-filters.js).
    Acepta valores repetidos para `api_key` y `modelo` (selección múltiple).
    """
    granularity = request.GET.get("granularity") or TokenUsageRollup.DAILY
    if granularity not in dict(TokenUsageRollup.GRANULARITIES):
        return JsonResponse(
            {"error": f"Agrupación inválida: '{granularity}'. Usa uno de: "
                      f"{', '.join(v for v, _ in TokenUsageRollup.GRANULARITIES)}."},
            status=400,
        )

    desde_raw, hasta_raw = request.GET.get("desde"), request.GET.get("hasta")
    desde, hasta = _date_or_none(desde_raw), _date_or_none(hasta_raw)
    if desde_raw and not desde:
        return JsonResponse({"error": f"Fecha 'desde' inválida: '{desde_raw}'. Usa AAAA-MM-DD."}, status=400)
    if hasta_raw and not hasta:
        return JsonResponse({"error": f"Fecha 'hasta' inválida: '{hasta_raw}'. Usa AAAA-MM-DD."}, status=400)
    if desde and hasta and desde > hasta:
        return JsonResponse({"error": "'desde' no puede ser posterior a 'hasta'."}, status=400)
    if not desde and not hasta:
        desde, hasta = _rango_por_defecto()

    api_key_ids = _ints_or_none(request.GET.getlist("api_key"))
    modelos_filtro = request.GET.getlist("modelo") or None

    page = _int_or_none(request.GET.get("page")) or 1
    page_size = _int_or_none(request.GET.get("page_size")) or 30
    if page < 1:
        return JsonResponse({"error": "'page' debe ser mayor o igual a 1."}, status=400)
    if not (1 <= page_size <= 200):
        return JsonResponse({"error": "'page_size' debe estar entre 1 y 200."}, status=400)

    sort_by = request.GET.get("sort") or "fecha"
    if sort_by not in services.PETICIONES_SORT_FIELDS:
        return JsonResponse(
            {"error": f"'sort' inválido: '{sort_by}'. Usa uno de: "
                      f"{', '.join(services.PETICIONES_SORT_FIELDS)}."},
            status=400,
        )
    sort_dir = request.GET.get("dir") or "desc"
    if sort_dir not in ("asc", "desc"):
        return JsonResponse({"error": "'dir' inválido. Usa 'asc' o 'desc'."}, status=400)

    metricas = services.obtener_metricas(
        granularity=granularity, api_key_ids=api_key_ids, modelos=modelos_filtro,
        desde=desde, hasta=hasta,
    )
    peticiones = services.obtener_peticiones(
        api_key_ids=api_key_ids, modelos=modelos_filtro, desde=desde, hasta=hasta,
        page=page, page_size=page_size, sort_by=sort_by, sort_dir=sort_dir,
    )

    payload = _metricas_payload(metricas, api_key_ids)
    payload["requests"] = _peticiones_payload(peticiones)
    return JsonResponse(payload)


@staff_member_required
@require_POST
def refrescar(request):
    """Fuerza el ETL desde LiteLLM y el recálculo de agregaciones."""
    try:
        resultado = services.refrescar_metricas()
        messages.success(
            request,
            f"Métricas actualizadas: {resultado['eventos_ingeridos']} evento(s) nuevo(s), "
            f"{resultado['filas_agregadas']} fila(s) agregada(s).",
        )
    except Exception as exc:  # noqa: BLE001 - se reporta al operador en la UI
        logger.exception("Fallo al refrescar métricas")
        messages.error(request, f"No se pudo refrescar: {exc}")

    destino = request.POST.get("next") or reverse("metrics:dashboard")
    return redirect(destino)


# =============================================================================
# GESTOR DE API KEYS
# =============================================================================
@staff_member_required
def api_keys(request):
    """Listado, creación y monitoreo de consumo por API Key."""
    modelos_disponibles = sorted(set(
        TokenUsageRollup.objects.values_list("model_name", flat=True)
    ))
    form = ApiKeyForm(request.POST or None, modelos=modelos_disponibles)
    key_emitida = None

    if request.method == "POST" and form.is_valid():
        datos = form.cleaned_data
        vigencia = datos.get("vigencia")
        duracion = f"{(vigencia - date.today()).days}d" if vigencia else None
        try:
            resultado = services.crear_api_key(
                alias=datos["key_alias"],
                owner_email=datos.get("owner_email") or "",
                descripcion=datos.get("descripcion") or "",
                modelos=datos.get("modelos") or None,
                rpm_limit=datos.get("rpm_limit"),
                tpm_limit=datos.get("tpm_limit"),
                duration=duracion,
                actor=_actor(request),
                ip=_ip(request),
            )
            key_emitida = resultado["key_plaintext"]
            messages.success(
                request,
                f"API Key '{datos['key_alias']}' creada. Cópiala ahora: no se volverá a mostrar.",
            )
            form = ApiKeyForm(modelos=modelos_disponibles)
        except LiteLLMError as exc:
            messages.error(request, f"LiteLLM rechazó la emisión: {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Fallo creando API Key")
            messages.error(request, f"Error inesperado al crear la key: {exc}")

    filas = services.resumen_api_keys()
    activas = [f for f in filas if f["is_active"]]

    contexto = {
        "seccion": "apikeys",
        "form": form,
        "key_emitida": key_emitida,
        "filas": filas,
        "total_keys": len(filas),
        "total_activas": len(activas),
        "consumo_prompt_global": sum((f["consumo_prompt"] or 0) for f in filas),
        "consumo_completion_global": sum((f["consumo_completion"] or 0) for f in filas),
        "litellm_url": settings.LITELLM_BASE_URL,
    }
    return render(request, "metrics/apikeys.html", contexto)


@staff_member_required
def api_key_detalle(request, key_id: int):
    """Consumo histórico y auditoría de una API Key concreta."""
    granularity = request.GET.get("granularity", TokenUsageRollup.DAILY)
    try:
        contexto = services.detalle_api_key(key_id, granularity=granularity)
    except ApiKeyRegistry.DoesNotExist:
        messages.error(request, "La API Key solicitada no existe.")
        return redirect("metrics:api_keys")

    contexto["seccion"] = "apikeys"
    contexto["granularities"] = TokenUsageRollup.GRANULARITIES
    contexto["granularity_activa"] = contexto["metricas"].granularity
    return render(request, "metrics/apikey_detail.html", contexto)


@staff_member_required
@require_POST
def api_key_toggle(request, key_id: int):
    """Desactiva o reactiva una API Key (nunca la borra: preserva el histórico)."""
    accion = request.POST.get("accion", "desactivar")
    try:
        if accion == "reactivar":
            registro = services.reactivar_api_key(key_id, actor=_actor(request), ip=_ip(request))
            messages.success(request, f"API Key '{registro.key_alias}' reactivada.")
        else:
            registro = services.desactivar_api_key(key_id, actor=_actor(request), ip=_ip(request))
            messages.warning(request, f"API Key '{registro.key_alias}' desactivada.")
    except ApiKeyRegistry.DoesNotExist:
        messages.error(request, "La API Key solicitada no existe.")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Fallo cambiando el estado de la API Key %s", key_id)
        messages.error(request, f"No se pudo cambiar el estado: {exc}")

    return redirect(request.POST.get("next") or reverse("metrics:api_keys"))
