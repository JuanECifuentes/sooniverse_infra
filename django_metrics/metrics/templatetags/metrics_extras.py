"""Filtros de formato específicos del panel de métricas."""

from django import template
from django.contrib.humanize.templatetags.humanize import intcomma

register = template.Library()


def _formato_compacto(numero, divisor, sufijo):
    valor = numero / divisor
    texto = f"{valor:.1f}"
    if texto.endswith(".0"):
        texto = texto[:-2]
    return f"{texto.replace('.', ',')}{sufijo}"


@register.filter
def human_tokens(value):
    """Formatea un contador de tokens: por debajo de 100.000 usa el mismo
    separador de miles que `intcomma`; de 100.000 a menos de 1.000.000 lo
    expresa en miles (p. ej. 125.000 -> "125K"); por encima, en millones
    (p. ej. 2.500.000 -> "2,5M")."""
    try:
        numero = int(value)
    except (TypeError, ValueError):
        return value

    if abs(numero) >= 1_000_000:
        return _formato_compacto(numero, 1_000_000, "M")
    if abs(numero) >= 100_000:
        return _formato_compacto(numero, 1_000, "K")
    return intcomma(numero)


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
