#!/usr/bin/env python
"""Utilidad de línea de comandos de Django para el panel Sooniverse."""
import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sooniverse_panel.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "No se pudo importar Django. ¿Está instalado y el entorno virtual activo?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
