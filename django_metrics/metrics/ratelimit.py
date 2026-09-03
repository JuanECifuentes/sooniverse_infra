"""
==============================================================================
Rate limiting por IP sobre el cache de Django (sin dependencias externas)
==============================================================================
Blinda los endpoints que exponen información del panel contra automatizaciones
( scraping, fuerza bruta en el login, spam de acciones caras como el ETL o las
acciones de worker). Los límites viven en settings.RATELIMITS, configurables
por entorno.

Mecanismo: ventana fija con el cache framework. `cache.add` arranca el contador
(timeout = ventana) y `cache.incr` lo avanza; ambas operaciones son atómicas
por sí mismas, así que el peor caso bajo carrera es un par de peticiones extra
dentro de la ventana —suficiente para el modelo de amenaza (automatización
burda), sin exigir Redis.

Nota de despliegue: con el LocMemCache por defecto el contador es POR PROCESO
gunicorn (3 workers => ~3x el límite efectivo). Los defaults ya asumen esa
holgura; si algún día se quiere un límite exacto entre procesos, basta con
apuntar CACHES a un Redis compartido —el código no cambia.

IP del cliente: detrás de nginx (única entrada pública) la cabecera fiable es
`X-Real-IP`, que el proxy fija a $remote_addr SOBREESCRIBIENDO cualquier valor
mandado por el cliente (X-Forwarded-For se compone con $proxy_add_x_forwarded_
for, así que su primer elemento ES suplantables y no se usa salvo como último
recurso). Sin proxy (runserver local), manda REMOTE_ADDR.
"""

from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import redirect

_UNIDAD_SEGUNDOS = {"s": 1, "m": 60, "h": 3600}


def client_ip(request):
    """IP del cliente con la jerarquía de confianza documentada arriba."""
    real = (request.META.get("HTTP_X_REAL_IP") or "").strip()
    if real:
        return real
    remote = (request.META.get("REMOTE_ADDR") or "").strip()
    if remote:
        return remote
    forwarded = (request.META.get("HTTP_X_FORWARDED_FOR") or "").strip()
    return forwarded.split(",")[0].strip() or None


def _parse_rate(rate: str):
    """'10/m' -> (10, 60). Lanza ValueError si el formato no es válido."""
    cantidad, _, unidad = rate.strip().partition("/")
    if unidad not in _UNIDAD_SEGUNDOS or not cantidad.isdigit() or int(cantidad) <= 0:
        raise ValueError(f"RATELIMITS['{rate}']: formato esperado '<N>/<s|m|h>'")
    return int(cantidad), _UNIDAD_SEGUNDOS[unidad]


def _respuesta_429(request, view_name: str, retry_after: int):
    """JSON para los endpoints de datos (.json / api/), redirección con mensaje
    para las páginas (el usuario ve la alerta en su vista, no una página fea)."""
    path = request.path
    if path.endswith(".json") or "/api/" in path:
        resp = JsonResponse({"error": "Demasiadas peticiones."}, status=429)
    else:
        # El middleware de mensajes está en toda respuesta real; el guard cubre
        # tests que construyen la request sin la cadena de middleware.
        if hasattr(request, "_messages"):
            messages.warning(
                request,
                "Demasiadas peticiones desde tu conexión. Espera un momento e inténtalo de nuevo.",
            )
        resp = redirect(path)
    resp["Retry-After"] = str(retry_after)
    resp.headers.setdefault("Cache-Control", "no-store")
    return resp


def rate_limit(nombre: str, methods=None):
    """Decorador de fábrica: `@rate_limit("api")` aplica el límite
    settings.RATELIMITS['api'] por IP y por vista. `methods` restringe el
    conteo a ciertos verbos (p. ej. solo POST en el login): los GET no
    consumen la ventana. Se resuelve en CADA petición para que los tests
    puedan sobreescribir settings.RATELIMITS sin recargar el módulo."""

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if methods is not None and request.method not in methods:
                return view(request, *args, **kwargs)

            rate = settings.RATELIMITS.get(nombre)
            if not rate:  # sin límite configurado => sin límite
                return view(request, *args, **kwargs)
            limite, ventana = _parse_rate(rate)

            ip = client_ip(request) or "desconocida"
            key = f"sv-rl:{nombre}:{view.__name__}:{ip}"
            if cache.add(key, 1, timeout=ventana):
                peticiones = 1
            else:
                try:
                    peticiones = cache.incr(key)
                except ValueError:
                    # La entrada expiró entre add e incr: reabrir ventana.
                    cache.add(key, 1, timeout=ventana)
                    peticiones = 1

            if peticiones > limite:
                return _respuesta_429(request, view.__name__, ventana)
            return view(request, *args, **kwargs)

        return wrapped

    return decorator
