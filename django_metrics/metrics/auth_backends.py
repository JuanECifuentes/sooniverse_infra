"""
==============================================================================
Backend de autenticación: usuario O correo, indistintamente
==============================================================================
Django es ahora la ÚNICA fuente de login del clúster (panel + chat, vía SSO
por cabecera de confianza -ver nginx `auth_request` y
`docker_images/openwebui/README.md`). El operador puede escribir su username
o su email en el mismo campo; se resuelve contra ambos, sin distinguir
mayúsculas/minúsculas en el email (los usernames de Django ya son
case-sensitive por defecto y así se dejan).
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.db.models import Q


class UsernameOrEmailBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None or password is None:
            return None

        User = get_user_model()
        try:
            # Dos cuentas con el mismo email (case-insensitive) es un estado de
            # datos inconsistente -no se adivina cuál, se rechaza el login.
            user = User.objects.get(Q(username=username) | Q(email__iexact=username))
        except User.DoesNotExist:
            return None
        except User.MultipleObjectsReturned:
            return None

        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
