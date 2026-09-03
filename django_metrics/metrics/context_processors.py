"""Contexto global de marca e identidad de despliegue."""

from django.conf import settings


def branding(_request):
    return {
        "MARCA_NOMBRE_1": "Sooni",  # renderizado en blanco puro
        "MARCA_NOMBRE_2": "verse",  # renderizado con cosmic-gradient
        "MARCA_TAGLINE": "ADVANCED TECH UNIVERSE",
        "CLIENTE_ID": settings.CLIENTE_ID,
        "ENTORNO": settings.ENTORNO,
        "LITELLM_BASE_URL": settings.LITELLM_BASE_URL,
        "PUBLIC_BASE_URL": settings.PUBLIC_BASE_URL,
        "CHAT_URL": settings.CHAT_URL,
    }
