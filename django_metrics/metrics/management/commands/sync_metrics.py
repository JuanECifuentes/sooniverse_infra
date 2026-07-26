"""
Refresca las métricas: ETL desde LiteLLM_SpendLogs + recálculo de agregaciones.

Ejecutado periódicamente por el entrypoint del contenedor (METRICS_REFRESH_INTERVAL)
o a mano:  python manage.py sync_metrics --since-hours 168
"""

from django.core.management.base import BaseCommand, CommandError
from django.db.utils import ProgrammingError

from metrics import services


class Command(BaseCommand):
    help = "Ingesta contadores de tokens desde LiteLLM y recalcula daily/weekly/monthly."

    def add_arguments(self, parser):
        parser.add_argument("--since-hours", type=int, default=48,
                            help="Ventana de SpendLogs a ingerir (horas)")
        parser.add_argument("--since-days", type=int, default=90,
                            help="Ventana de eventos a re-agregar (días)")
        parser.add_argument("--quiet", action="store_true", help="Silencia la salida en éxito")

    def handle(self, *args, **options):
        try:
            resultado = services.refrescar_metricas(
                since_hours=options["since_hours"], since_days=options["since_days"]
            )
        except ProgrammingError as exc:
            raise CommandError(
                "El esquema `sooniverse` no está inicializado. "
                f"Ejecuta `python scripts/db_setup.py` primero. Detalle: {exc}"
            ) from exc

        if not options["quiet"]:
            self.stdout.write(self.style.SUCCESS(
                f"Eventos ingeridos: {resultado['eventos_ingeridos']} | "
                f"Filas agregadas: {resultado['filas_agregadas']}"
            ))
