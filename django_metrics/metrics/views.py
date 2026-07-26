"""
==============================================================================
Vistas del panel de Métricas y API Keys
==============================================================================
Todas requieren staff autenticado: el panel expone gestión de credenciales.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
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
    api_key_id = None
    modelo = None
    dias = None

    if form.is_valid():
        granularity = form.cleaned_data.get("granularity") or TokenUsageRollup.DAILY
        api_key_id = _int_or_none(form.cleaned_data.get("api_key"))
        modelo = form.cleaned_data.get("modelo") or None
        dias = form.cleaned_data.get("dias")

    metricas = services.obtener_metricas(
        granularity=granularity, api_key_id=api_key_id, dias=dias, modelo=modelo
    )

    contexto = {
        "seccion": "metricas",
        "form": form,
        "metricas": metricas,
        "granularities": TokenUsageRollup.GRANULARITIES,
        "granularity_activa": granularity,
        "api_key_activa": api_key_id,
        "api_keys": api_keys,
        "pool": services.estado_pool(),
        "query_base": request.GET.urlencode(),
    }
    return render(request, "metrics/dashboard.html", contexto)


@staff_member_required
def serie_json(request):
    """Endpoint JSON de la serie temporal (para integraciones externas)."""
    metricas = services.obtener_metricas(
        granularity=request.GET.get("granularity", TokenUsageRollup.DAILY),
        api_key_id=_int_or_none(request.GET.get("api_key")),
        dias=_int_or_none(request.GET.get("dias")),
        modelo=request.GET.get("modelo") or None,
    )

    return JsonResponse({
        "granularity": metricas.granularity,
        "api_key_id": metricas.api_key_id,
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
    form = ApiKeyForm(request.POST or None)
    key_emitida = None

    if request.method == "POST" and form.is_valid():
        datos = form.cleaned_data
        try:
            resultado = services.crear_api_key(
                alias=datos["key_alias"],
                owner_email=datos.get("owner_email") or "",
                descripcion=datos.get("descripcion") or "",
                modelos=datos.get("modelos") or None,
                max_budget=float(datos["max_budget"]) if datos.get("max_budget") is not None else None,
                rpm_limit=datos.get("rpm_limit"),
                tpm_limit=datos.get("tpm_limit"),
                duration=datos.get("duration") or None,
                actor=_actor(request),
                ip=_ip(request),
            )
            key_emitida = resultado["key_plaintext"]
            messages.success(
                request,
                f"API Key '{datos['key_alias']}' creada. Cópiala ahora: no se volverá a mostrar.",
            )
            form = ApiKeyForm()
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
        "consumo_global": sum((f["consumo_total"] or 0) for f in filas),
        "gasto_global": sum((f["gasto"] or Decimal("0")) for f in filas),
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
