"""
Crea (idempotentemente) el superusuario del panel a partir de variables de entorno.

Usado por el entrypoint del contenedor cuando DJANGO_SUPERUSER_PASSWORD está definida.
"""

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Asegura la existencia del superusuario definido en DJANGO_SUPERUSER_*."

    def handle(self, *args, **options):
        username = os.environ.get("DJANGO_SUPERUSER_USERNAME", "admin")
        password = os.environ.get("DJANGO_SUPERUSER_PASSWORD", "")
        email = os.environ.get("DJANGO_SUPERUSER_EMAIL", "admin@sooniverse.co")

        if not password:
            self.stdout.write("DJANGO_SUPERUSER_PASSWORD vacía: no se crea superusuario.")
            return

        User = get_user_model()
        user, creado = User.objects.get_or_create(
            username=username,
            defaults={"email": email, "is_staff": True, "is_superuser": True},
        )

        # Mantiene privilegios y contraseña alineados con el entorno en cada arranque.
        user.email = email
        user.is_staff = True
        user.is_superuser = True
        user.set_password(password)
        user.save()

        verbo = "creado" if creado else "actualizado"
        self.stdout.write(self.style.SUCCESS(f"Superusuario '{username}' {verbo}."))
