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

from . import analytics, capacidad as cap_mod, filtros as ft, services
from .forms import ApiKeyForm
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
                "total": e.total_tokens,
                "status": e.status,
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


def _bool_post(request, clave: str) -> bool:
    return request.POST.get(clave) in ("1", "true", "on", "yes")


def _parse_filtros(request):
    """Valida los filtros comunes a `metrics_api` y `lente_api`.

    Devuelve `(FiltrosTemporales, None)` o `(None, JsonResponse 400)`. Está
    extraído a propósito: con la validación duplicada en dos endpoints, ambas
    divergirían en la primera corrección de bug.
    """
    desde_raw, hasta_raw = request.POST.get("desde"), request.POST.get("hasta")
    desde, hasta = _date_or_none(desde_raw), _date_or_none(hasta_raw)
    if desde_raw and not desde:
        return None, JsonResponse(
            {"error": f"Fecha 'desde' inválida: '{desde_raw}'. Usa AAAA-MM-DD."}, status=400)
    if hasta_raw and not hasta:
        return None, JsonResponse(
            {"error": f"Fecha 'hasta' inválida: '{hasta_raw}'. Usa AAAA-MM-DD."}, status=400)
    if desde and hasta and desde > hasta:
        return None, JsonResponse({"error": "'desde' no puede ser posterior a 'hasta'."}, status=400)
    if not desde and not hasta:
        desde, hasta = _rango_por_defecto()
    desde = desde or hasta
    hasta = hasta or desde

    dias_raw = request.POST.getlist("dow")
    dias = []
    for valor in dias_raw:
        d = _int_or_none(valor)
        if d is None or not (1 <= d <= 7):
            return None, JsonResponse(
                {"error": f"Día de la semana inválido: '{valor}'. Usa 1 (lunes) a 7 (domingo)."},
                status=400)
        dias.append(d)

    hora_desde = _int_or_none(request.POST.get("hora_desde"))
    hora_hasta = _int_or_none(request.POST.get("hora_hasta"))
    hora_desde = 0 if hora_desde is None else hora_desde
    hora_hasta = 23 if hora_hasta is None else hora_hasta
    if not (0 <= hora_desde <= 23) or not (0 <= hora_hasta <= 23):
        return None, JsonResponse({"error": "La franja horaria debe estar entre 0 y 23."}, status=400)
    if hora_desde > hora_hasta:
        return None, JsonResponse(
            {"error": "'hora_desde' no puede ser posterior a 'hora_hasta'."}, status=400)

    estado = request.POST.get("estado") or ft.ESTADO_TODAS
    if estado not in dict(ft.ESTADOS):
        return None, JsonResponse(
            {"error": "'estado' inválido. Usa 'todas' o 'errores'."}, status=400)

    return ft.FiltrosTemporales(
        desde=desde,
        hasta=hasta,
        api_key_ids=tuple(_ints_or_none(request.POST.getlist("api_key")) or ()),
        modelos=tuple(request.POST.getlist("modelo")),
        dias_semana=tuple(sorted(set(dias))),
        hora_desde=hora_desde,
        hora_hasta=hora_hasta,
        estado=estado,
        incluir_benchmark=_bool_post(request, "incluir_benchmark"),
    ), None


# =============================================================================
# MÓDULO DE MÉTRICAS
# =============================================================================
@staff_member_required
def dashboard(request):
    """
    Panel de consumo de tokens con particiones Diaria / Semanal / Mensual y
    filtro por API Key específica.

    Vista de solo lectura: siempre pinta la ventana/agrupación por defecto,
    nunca lee filtros desde la querystring (el filtrado en vivo se hace por
    fetch POST a `metrics_api`, sin reflejarse en la URL). Así, recargar la
    página siempre limpia los filtros en vez de conservarlos.
    """
    api_keys = services.resumen_api_keys()
    modelos = services.modelos_unicos()
    granularity = TokenUsageRollup.DAILY
    desde, hasta = _rango_por_defecto()

    metricas = services.obtener_metricas(granularity=granularity, desde=desde, hasta=hasta)
    peticiones = services.obtener_peticiones(desde=desde, hasta=hasta)

    f = ft.FiltrosTemporales(desde=desde, hasta=hasta)
    ocio = analytics.ventanas_ociosas(f)

    payload = _metricas_payload(metricas, [], ocio=ocio.as_dict(), filtros_eco=f.eco())
    payload["requests"] = _peticiones_payload(peticiones)

    contexto = {
        "seccion": "metricas",
        "metricas": metricas,
        "peticiones": peticiones,
        # Incluye 'hourly', que no está en TokenUsageRollup.GRANULARITIES porque
        # esa granularidad la sirve otra tabla (usage_hourly).
        "granularities": ft.GRANULARIDADES_PANEL,
        "granularity_activa": granularity,
        "modelos_activos": [],
        "api_key_ids_activos": [],
        "desde_activa": desde,
        "hasta_activa": hasta,
        "api_keys": api_keys,
        "modelos": modelos,
        "dias_semana": ft.DIAS_SEMANA,
        "estados": ft.ESTADOS,
        "horas": list(range(24)),
        "ocio": ocio,
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


def _metricas_payload(metricas, api_key_ids, *, ocio=None, filtros_eco=None, comparativa=None):
    """Serializa un ResumenMetricas al contrato JSON que consume el panel
    (metrics-filters.js/metrics-charts.js). Usado tanto por `metrics_api` como
    por el bootstrap inicial que renderiza `dashboard` para el primer pintado.

    Los parámetros añadidos son keyword-only con default None: `api_key_detalle`
    sigue llamando con dos argumentos posicionales.
    """
    payload = {
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
            "tasa_error": metricas.tasa_error,
        },
        "series": {
            "labels": [p.etiqueta for p in metricas.serie],
            "total_tokens": [p.total_tokens for p in metricas.serie],
            "prompt_tokens": [p.prompt_tokens for p in metricas.serie],
            "completion_tokens": [p.completion_tokens for p in metricas.serie],
            "request_count": [p.request_count for p in metricas.serie],
            "altura_pct": [p.altura_pct for p in metricas.serie],
            # Instante ISO de cada bucket, para el tooltip y para poder mapear
            # una celda del mapa de calor a un momento concreto.
            "periodos": [p.periodo.isoformat() for p in metricas.serie],
        },
        "por_modelo": [
            {**m, "spend_usd": float(m.get("spend_usd") or 0)} for m in metricas.por_modelo
        ],
        "por_api_key": [
            {**k, "spend_usd": float(k.get("spend_usd") or 0)} for k in metricas.por_api_key
        ] if metricas.por_api_key else [],
        "mostrar_desglose_api_key": not (api_key_ids and len(api_key_ids) == 1),
    }
    # Claves nuevas, todas aditivas: ninguna existente cambia de nombre ni forma.
    if ocio is not None:
        payload["tiempos_muertos"] = ocio
    if filtros_eco is not None:
        payload["filtros_eco"] = filtros_eco
    if comparativa is not None:
        payload["comparativa"] = comparativa
    return payload


@staff_member_required
@require_POST
def metrics_api(request):
    """
    Endpoint JSON consumido por el filtrado asíncrono del panel (metrics-filters.js).
    Acepta valores repetidos para `api_key` y `modelo` (selección múltiple).

    POST (no GET): el filtrado es una consulta bajo demanda, no un estado de
    navegación — así nunca queda reflejado ni cacheado en la URL de la página.
    """
    granularity = request.POST.get("granularity") or TokenUsageRollup.DAILY
    if granularity not in dict(ft.GRANULARIDADES_PANEL):
        return JsonResponse(
            {"error": f"Agrupación inválida: '{granularity}'. Usa uno de: "
                      f"{', '.join(v for v, _ in ft.GRANULARIDADES_PANEL)}."},
            status=400,
        )

    f, error = _parse_filtros(request)
    if error:
        return error

    api_key_ids = list(f.api_key_ids) or None
    modelos_filtro = list(f.modelos) or None

    page = _int_or_none(request.POST.get("page")) or 1
    page_size = _int_or_none(request.POST.get("page_size")) or 30
    if page < 1:
        return JsonResponse({"error": "'page' debe ser mayor o igual a 1."}, status=400)
    if not (1 <= page_size <= 200):
        return JsonResponse({"error": "'page_size' debe estar entre 1 y 200."}, status=400)

    sort_by = request.POST.get("sort") or "fecha"
    if sort_by not in services.PETICIONES_SORT_FIELDS:
        return JsonResponse(
            {"error": f"'sort' inválido: '{sort_by}'. Usa uno de: "
                      f"{', '.join(services.PETICIONES_SORT_FIELDS)}."},
            status=400,
        )
    sort_dir = request.POST.get("dir") or "desc"
    if sort_dir not in ("asc", "desc"):
        return JsonResponse({"error": "'dir' inválido. Usa 'asc' o 'desc'."}, status=400)

    metricas = services.obtener_metricas(
        granularity=granularity,
        api_key_ids=api_key_ids,
        modelos=modelos_filtro,
        desde=f.desde,
        hasta=f.hasta,
        incluir_benchmark=f.incluir_benchmark,
        dias_semana=f.dias_semana,
        hora_desde=f.hora_desde,
        hora_hasta=f.hora_hasta,
        solo_errores=(f.estado == ft.ESTADO_ERRORES),
    )
    peticiones = services.obtener_peticiones(
        api_key_ids=api_key_ids,
        modelos=modelos_filtro,
        desde=f.desde,
        hasta=f.hasta,
        page=page,
        page_size=page_size,
        sort_by=sort_by,
        sort_dir=sort_dir,
        incluir_benchmark=f.incluir_benchmark,
        solo_errores=(f.estado == ft.ESTADO_ERRORES),
        dias_semana=f.dias_semana,
        hora_desde=f.hora_desde,
        hora_hasta=f.hora_hasta,
    )

    # Los tiempos muertos SÍ viajan en el camino caliente (a diferencia del mapa
    # de calor): están siempre visibles, la consulta es barata y son ~600 bytes.
    ocio = analytics.ventanas_ociosas(f).as_dict()

    comparativa = None
    if _bool_post(request, "comparar"):
        comparativa = _comparativa(f, granularity)

    payload = _metricas_payload(metricas, api_key_ids, ocio=ocio,
                                filtros_eco=f.eco(), comparativa=comparativa)
    payload["requests"] = _peticiones_payload(peticiones)
    return JsonResponse(payload)


def _delta_pct(actual, anterior):
    if not anterior:
        return None
    return round((actual - anterior) / anterior * 100, 1)


def _comparativa(f, granularity):
    """Deltas contra el periodo inmediatamente anterior del mismo tamaño.

    Solo números, sin serie fantasma en la gráfica: duplicar datasets duplica la
    tinta y un '+12,4 %' en monoespaciada responde igual de bien.
    """
    prev = f.periodo_anterior()
    m = services.obtener_metricas(
        granularity=granularity,
        api_key_ids=list(prev.api_key_ids) or None,
        modelos=list(prev.modelos) or None,
        desde=prev.desde,
        hasta=prev.hasta,
        incluir_benchmark=prev.incluir_benchmark,
        dias_semana=prev.dias_semana,
        hora_desde=prev.hora_desde,
        hora_hasta=prev.hora_hasta,
        solo_errores=(prev.estado == ft.ESTADO_ERRORES),
    )
    actual = services.obtener_metricas(
        granularity=granularity,
        api_key_ids=list(f.api_key_ids) or None,
        modelos=list(f.modelos) or None,
        desde=f.desde,
        hasta=f.hasta,
        incluir_benchmark=f.incluir_benchmark,
        dias_semana=f.dias_semana,
        hora_desde=f.hora_desde,
        hora_hasta=f.hora_hasta,
        solo_errores=(f.estado == ft.ESTADO_ERRORES),
    )
    return {
        "desde": prev.desde.isoformat(),
        "hasta": prev.hasta.isoformat(),
        "delta_pct": {
            "total_tokens": _delta_pct(actual.total_tokens, m.total_tokens),
            "request_count": _delta_pct(actual.request_count, m.request_count),
            "tokens_por_request": _delta_pct(actual.tokens_por_request, m.tokens_por_request),
            "tasa_error": _delta_pct(actual.tasa_error, m.tasa_error),
        },
    }


@staff_member_required
@require_POST
def lente_api(request):
    """Mapa de calor y perfil horario.

    Endpoint APARTE de `metrics_api` por dos razones concretas:
      a) `metrics_api` ya lanza cinco consultas, y el mapa en modo p95 escanea
         token_usage_event crudo con percentile_cont -la consulta más cara del
         panel-. Pagarla en cada cambio de filtro para un usuario que está en la
         lente "Serie" (la de por defecto) sería una regresión del caso común.
      b) Estas lentes NO dependen de page/page_size/sort/dir, que son justo los
         parámetros que más se reenvían: cada clic de paginación dispara
         applyFilters() y recalcularía 168 celdas para nada.
    """
    f, error = _parse_filtros(request)
    if error:
        return error

    lente = request.POST.get("lente") or "heatmap"
    if lente not in ("heatmap", "perfil"):
        return JsonResponse({"error": "'lente' inválida. Usa 'heatmap' o 'perfil'."}, status=400)

    metrica = request.POST.get("metrica") or "peticiones"
    if metrica not in analytics.METRICAS_HEATMAP:
        return JsonResponse(
            {"error": f"'metrica' inválida: '{metrica}'. Usa uno de: "
                      f"{', '.join(analytics.METRICAS_HEATMAP)}."},
            status=400,
        )
    if metrica == "p95" and f.dias > ft.P95_MAX_DIAS:
        return JsonResponse(
            {"error": f"La latencia p95 solo se puede calcular sobre rangos de hasta "
                      f"{ft.P95_MAX_DIAS} días (pediste {f.dias})."},
            status=400,
        )

    if lente == "heatmap":
        datos = analytics.heatmap_semanal(f, metrica).as_dict()
    else:
        perfil = analytics.perfil_horario(f)
        corrida = cap_mod.ultima_corrida(settings.CLIENTE_ID, settings.ENTORNO)
        if corrida and corrida.rpm_sostenido:
            perfil.techo_pet_hora = round(float(corrida.rpm_sostenido) * 60, 2)
        datos = perfil.as_dict()

    return JsonResponse({"lente": lente, "datos": datos, "filtros_eco": f.eco()})


@staff_member_required
def capacidad(request):
    """Techo medido de la infraestructura, margen y proyección.

    Igual que `dashboard`, ignora la querystring a propósito: pinta la última
    corrida y la ventana por defecto. El filtrado en vivo va por `capacidad_api`.
    """
    desde, hasta = _rango_por_defecto()
    f = ft.FiltrosTemporales(desde=desde, hasta=hasta)
    payload = cap_mod.payload_capacidad(settings.CLIENTE_ID, settings.ENTORNO, f)
    return render(request, "metrics/capacidad.html", {
        "seccion": "capacidad",
        "desde_activa": desde,
        "hasta_activa": hasta,
        "capacidad_payload": payload,
        "corrida": payload["corrida"],
        "margen": payload["margen"],
        "proyeccion": payload["proyeccion"],
        "corridas": payload["corridas"],
    })


@staff_member_required
@require_POST
def capacidad_api(request):
    """Recalcula la ficha de capacidad al cambiar de corrida o de rango."""
    f, error = _parse_filtros(request)
    if error:
        return error
    run_id = request.POST.get("corrida") or None
    return JsonResponse(
        cap_mod.payload_capacidad(settings.CLIENTE_ID, settings.ENTORNO, f, run_id=run_id)
    )


@staff_member_required
@require_POST
def refrescar(request):
    """Fuerza el ETL desde LiteLLM y el recálculo de agregaciones."""
    try:
        resultado = services.refrescar_metricas()
        messages.success(
            request,
            f"Métricas actualizadas: {resultado['eventos_ingeridos']} evento(s) nuevo(s), "
            f"{resultado['filas_agregadas']} fila(s) agregada(s), "
            f"{resultado['buckets_horarios']} bucket(s) horario(s).",
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
    modelos_disponibles = services.modelos_unicos()
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
    """Consumo histórico y auditoría de una API Key concreta.

    Al igual que `dashboard`, siempre pinta la agrupación/ventana por
    defecto: el filtrado en vivo (agrupación, desde, hasta) va por fetch POST
    a `metrics_api` vía apikey-detail.js, nunca por querystring."""
    desde, hasta = _rango_por_defecto()
    try:
        contexto = services.detalle_api_key(key_id, granularity=TokenUsageRollup.DAILY, desde=desde, hasta=hasta)
    except ApiKeyRegistry.DoesNotExist:
        messages.error(request, "La API Key solicitada no existe.")
        return redirect("metrics:api_keys")

    contexto["seccion"] = "apikeys"
    contexto["granularities"] = TokenUsageRollup.GRANULARITIES
    contexto["granularity_activa"] = contexto["metricas"].granularity
    contexto["metricas_payload"] = _metricas_payload(contexto["metricas"], [key_id])
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
