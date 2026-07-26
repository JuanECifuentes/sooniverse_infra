"""
Genera datos sintéticos de consumo para validar el panel sin tráfico real.

    python manage.py seed_demo              # crea key demo + 45 días de eventos
    python manage.py seed_demo --dias 90
    python manage.py seed_demo --clean      # borra TODO lo generado por este comando

Los registros se marcan con el prefijo `demo-` para que `--clean` sea preciso y
nunca toque métricas reales.
"""

import random
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from metrics import services
from metrics.models import ApiKeyAudit, ApiKeyRegistry, TokenUsageEvent, TokenUsageRollup

DEMO_ALIAS_PREFIX = "demo-"
DEMO_REQUEST_PREFIX = "demo-"


class Command(BaseCommand):
    help = "Sembrado/limpieza de métricas sintéticas para validar el panel."

    def add_arguments(self, parser):
        parser.add_argument("--dias", type=int, default=45, help="Días de historia a generar")
        parser.add_argument("--keys", type=int, default=2, help="Número de API Keys demo")
        parser.add_argument("--clean", action="store_true", help="Elimina los datos demo y sale")
        parser.add_argument("--seed", type=int, default=7, help="Semilla del generador (reproducible)")

    @transaction.atomic
    def handle(self, *args, **options):
        if options["clean"]:
            return self._clean()

        rnd = random.Random(options["seed"])
        ahora = timezone.now()
        modelos = ["sooniverse-qwen3.5", "sooniverse-embeddings"]

        creadas = []
        for i in range(options["keys"]):
            alias = f"{DEMO_ALIAS_PREFIX}app-{i + 1}"
            key, _ = ApiKeyRegistry.objects.get_or_create(
                key_alias=alias,
                defaults=dict(
                    litellm_token_hash=f"demo-hash-{i + 1}",
                    key_prefix=f"sk-demo{i + 1}…{rnd.randint(1000, 9999)}",
                    cliente_id="demo", entorno="dev", is_active=(i % 3 != 2),
                    owner_email=f"equipo{i + 1}@demo.local",
                    descripcion="Datos sintéticos generados por seed_demo.",
                    max_budget_usd=25, allowed_models=[modelos[0]],
                    created_at=ahora - timedelta(days=options["dias"]), updated_at=ahora,
                ),
            )
            creadas.append(key)

        eventos = 0
        for key in creadas:
            for d in range(options["dias"]):
                # Varias peticiones por día para que las agregaciones sean realistas.
                for r in range(rnd.randint(1, 6)):
                    req_id = f"{DEMO_REQUEST_PREFIX}{key.id}-{d}-{r}"
                    if TokenUsageEvent.objects.filter(litellm_request_id=req_id).exists():
                        continue
                    p, c = rnd.randint(180, 2400), rnd.randint(80, 1800)
                    TokenUsageEvent.objects.create(
                        api_key_id=key.id, litellm_token_hash=key.litellm_token_hash,
                        litellm_request_id=req_id,
                        model_name=rnd.choice(modelos),
                        worker_endpoint=f"10.0.{rnd.randint(1, 3)}.{rnd.randint(10, 40)}:8007",
                        prompt_tokens=p, completion_tokens=c, total_tokens=p + c,
                        spend_usd=(p + c) * 0.0000004,
                        latency_ms=rnd.randint(110, 1400),
                        status="success" if rnd.random() > 0.04 else "error",
                        event_ts=ahora - timedelta(days=d, hours=rnd.randint(0, 23)),
                        ingested_at=ahora,
                    )
                    eventos += 1

        resultado = services.refrescar_metricas(since_days=options["dias"] + 5)
        self.stdout.write(self.style.SUCCESS(
            f"Demo sembrada: {len(creadas)} API Key(s), {eventos} evento(s), "
            f"{resultado['filas_agregadas']} fila(s) agregada(s)."
        ))
        self.stdout.write("Para revertir: python manage.py seed_demo --clean")

    def _clean(self):
        keys = list(ApiKeyRegistry.objects.filter(key_alias__startswith=DEMO_ALIAS_PREFIX))
        ids = [k.id for k in keys]

        eventos = TokenUsageEvent.objects.filter(
            litellm_request_id__startswith=DEMO_REQUEST_PREFIX
        ).delete()[0]
        if ids:
            TokenUsageRollup.objects.filter(api_key_id__in=ids).delete()
            ApiKeyAudit.objects.filter(api_key_id__in=ids).delete()
        borradas = ApiKeyRegistry.objects.filter(id__in=ids).delete()[0] if ids else 0

        self.stdout.write(self.style.WARNING(
            f"Datos demo eliminados: {borradas} API Key(s), {eventos} evento(s)."
        ))
