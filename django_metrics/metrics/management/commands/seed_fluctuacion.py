"""
Genera consumo sintético con una fluctuación clara en el tiempo (onda seno +
ruido) para poder ver la línea de tendencia de las gráficas subiendo y
bajando, en vez del consumo aproximadamente plano de `seed_demo`.

    python manage.py seed_fluctuacion                  # 60 días de historia
    python manage.py seed_fluctuacion --dias 90 --periodo 14
    python manage.py seed_fluctuacion --clean           # borra TODO lo generado por este comando

Los registros se marcan con el prefijo `fluct-` para que `--clean` sea
preciso y nunca toque métricas reales ni las de `seed_demo`.
"""

import math
import random
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from metrics import services
from metrics.models import ApiKeyAudit, ApiKeyRegistry, TokenUsageEvent, TokenUsageRollup

FLUCT_ALIAS = "fluct-demo"
FLUCT_REQUEST_PREFIX = "fluct-"


class Command(BaseCommand):
    help = "Sembrado/limpieza de consumo sintético con fluctuación (onda seno) para probar la línea de tendencia."

    def add_arguments(self, parser):
        parser.add_argument("--dias", type=int, default=60, help="Días de historia a generar")
        parser.add_argument("--periodo", type=int, default=10, help="Días por ciclo de la onda")
        parser.add_argument("--clean", action="store_true", help="Elimina los datos generados y sale")
        parser.add_argument("--seed", type=int, default=13, help="Semilla del generador (reproducible)")

    @transaction.atomic
    def handle(self, *args, **options):
        if options["clean"]:
            return self._clean()

        rnd = random.Random(options["seed"])
        ahora = timezone.now()
        dias = options["dias"]
        periodo = max(2, options["periodo"])
        modelos = ["sooniverse-qwen3.5", "sooniverse-embeddings"]

        key, _ = ApiKeyRegistry.objects.get_or_create(
            key_alias=FLUCT_ALIAS,
            defaults=dict(
                litellm_token_hash="fluct-hash-1",
                key_prefix=f"sk-fluct…{rnd.randint(1000, 9999)}",
                cliente_id="demo", entorno="dev", is_active=True,
                owner_email="equipo-fluctuacion@demo.local",
                descripcion="Datos sintéticos con fluctuación generados por seed_fluctuacion.",
                max_budget_usd=25, allowed_models=[modelos[0]],
                created_at=ahora - timedelta(days=dias), updated_at=ahora,
            ),
        )

        eventos = 0
        # Onda seno con línea de base y amplitud sobre el total de tokens/día,
        # más ruido leve para que no se vea artificialmente perfecta.
        base, amplitud = 6000, 4500
        for d in range(dias):
            fase = 2 * math.pi * (dias - d) / periodo
            objetivo_dia = max(300, base + amplitud * math.sin(fase) + rnd.randint(-800, 800))

            restante = int(objetivo_dia)
            r = 0
            while restante > 0:
                p = min(restante, rnd.randint(150, 900))
                restante -= p
                c = int(p * rnd.uniform(0.35, 0.75))
                req_id = f"{FLUCT_REQUEST_PREFIX}{key.id}-{d}-{r}"
                r += 1
                if TokenUsageEvent.objects.filter(litellm_request_id=req_id).exists():
                    continue
                TokenUsageEvent.objects.create(
                    api_key_id=key.id, litellm_token_hash=key.litellm_token_hash,
                    litellm_request_id=req_id,
                    model_name=rnd.choice(modelos),
                    worker_endpoint=f"10.0.{rnd.randint(1, 3)}.{rnd.randint(10, 40)}:8007",
                    prompt_tokens=p, completion_tokens=c, total_tokens=p + c,
                    spend_usd=(p + c) * 0.0000004,
                    latency_ms=rnd.randint(110, 1400),
                    status="success" if rnd.random() > 0.03 else "error",
                    event_ts=ahora - timedelta(days=d, hours=rnd.randint(0, 23), minutes=rnd.randint(0, 59)),
                    ingested_at=ahora,
                )
                eventos += 1

        resultado = services.refrescar_metricas(since_days=dias + 5)
        self.stdout.write(self.style.SUCCESS(
            f"Fluctuación sembrada: 1 API Key, {eventos} evento(s) en {dias} día(s) "
            f"(ciclo de {periodo} días), {resultado['filas_agregadas']} fila(s) agregada(s)."
        ))
        self.stdout.write("Para revertir: python manage.py seed_fluctuacion --clean")

    def _clean(self):
        keys = list(ApiKeyRegistry.objects.filter(key_alias=FLUCT_ALIAS))
        ids = [k.id for k in keys]

        eventos = TokenUsageEvent.objects.filter(
            litellm_request_id__startswith=FLUCT_REQUEST_PREFIX
        ).delete()[0]
        if ids:
            TokenUsageRollup.objects.filter(api_key_id__in=ids).delete()
            ApiKeyAudit.objects.filter(api_key_id__in=ids).delete()
        borradas = ApiKeyRegistry.objects.filter(id__in=ids).delete()[0] if ids else 0

        self.stdout.write(self.style.WARNING(
            f"Datos de fluctuación eliminados: {borradas} API Key(s), {eventos} evento(s)."
        ))
