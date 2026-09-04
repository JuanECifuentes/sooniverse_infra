"""Filtros de formato específicos del panel de métricas."""

from django import template

register = template.Library()

SEPARADOR_MILES = "."


def _formato_compacto(numero, divisor, sufijo):
    valor = numero / divisor
    texto = f"{valor:.1f}"
    if texto.endswith(".0"):
        texto = texto[:-2]
    return f"{texto.replace('.', ',')}{sufijo}"


def _con_separador_de_miles(numero):
    """Agrupación explícita con punto, según el manual de imagen (§2.1).

    NO se delega en `intcomma`: el locale `es` de Django agrupa con un espacio
    duro (U+00A0), así que `99999` salía como "99 999" en el render del
    servidor y como "99.999" en el del cliente (`toLocaleString('es-ES')` en
    format.js). El mismo número cambiaba de aspecto al tocar cualquier filtro,
    porque el primer pintado lo hace la plantilla y los siguientes el fetch.
    """
    return f"{numero:,}".replace(",", SEPARADOR_MILES)


@register.filter
def human_tokens(value):
    """Formatea un contador de tokens: por debajo de 100.000, separador de
    miles; de 100.000 a menos de 1.000.000, en miles (125.000 -> "125K"); por
    encima, en millones (2.500.000 -> "2,5M").

    Espejo de `static/js/format.js::fmtTok`. La tabla de casos canónica está en
    `metrics/tests/test_formato.py`; si cambias un umbral aquí, cámbialo allí.
    """
    if value is None:
        # Un contador ausente es cero en este panel (todas las columnas son
        # NOT NULL DEFAULT 0; un None solo llega de un agregado vacío). Sin
        # esto la plantilla renderizaba literalmente "None".
        return "0"
    try:
        numero = int(value)
    except (TypeError, ValueError):
        return value

    if abs(numero) >= 1_000_000:
        return _formato_compacto(numero, 1_000_000, "M")
    if abs(numero) >= 100_000:
        return _formato_compacto(numero, 1_000, "K")
    return _con_separador_de_miles(numero)


@register.filter
def unique_ci(valores):
    """Deduplica una lista de strings sin distinguir mayúsculas ni espacios
    sobrantes, conservando la primera forma vista. Para listas guardadas en
    BD (como `allowed_models`) que puedan tener duplicados por errores de
    captura, evitando repetir el mismo tag varias veces en la interfaz."""
    vistos = {}
    for valor in valores or []:
        limpio = (valor or "").strip()
        if not limpio:
            continue
        vistos.setdefault(limpio.lower(), limpio)
    return list(vistos.values())


@register.filter
def friendly_key_alias(alias):
    """Ver `metrics.models.friendly_key_alias` (misma lógica, expuesta como
    filtro para las plantillas)."""
    from ..models import friendly_key_alias as _friendly_key_alias

    return _friendly_key_alias(alias)


@register.filter
def es_admin_cred(usuario) -> bool:
    """True si la cuenta tiene el rol Administrador (tab de credenciales).
    Lo consume credenciales.html para las badges de rol y el atributo
    data-admin que lee el modal de modificación. Import diferido para no
    crear dependencias al cargar templatetags."""
    from ..credenciales import es_admin_credenciales

    try:
        return es_admin_credenciales(usuario)
    except Exception:
        return False
