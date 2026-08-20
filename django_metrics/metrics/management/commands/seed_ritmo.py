"""
Consumo sintético con FORMA HORARIA Y SEMANAL realista.

`seed_demo` y `seed_fluctuacion` reparten los eventos con
`hours=randint(0, 23)`, es decir, uniformemente a lo largo del día. Eso sirve
para ver la serie temporal, pero deja el mapa de calor plano y hace imposible
probar la detección de tiempos muertos: con tráfico uniforme no hay ninguna
franja ociosa que encontrar.

Este comando genera un patrón de oficina reconocible:
  - laborables con dos picos (media mañana y media tarde) y valle de comida,
  - noches prácticamente muertas,
  - fines de semana con una fracción pequeña del tráfico,
  - latencia correlacionada con la carga (a más concurrencia, peor p95),
  - una tasa de error que sube en los picos.

    python manage.py seed_ritmo                    # 45 días
    python manage.py seed_ritmo --dias 90
    python manage.py seed_ritmo --con-benchmark    # + una key de benchmark, para
                                                   #   probar el filtro de exclusión
    python manage.py seed_ritmo --clean            # borra SOLO lo suyo

Los registros llevan el prefijo `ritmo-` para que `--clean` nunca toque métricas
reales ni las de los otros seeders.
"""

import math
import random
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from metrics import services
from metrics.models import ApiKeyAudit, ApiKeyRegistry, TokenUsageEvent, TokenUsageRollup

RITMO_ALIAS = "ritmo-demo"
BENCH_ALIAS = "ritmo-benchmark"
RITMO_REQUEST_PREFIX = "ritmo-"

# Peso relativo del tráfico por hora local (0-23). Dos jorobas y valle de comida.
PERFIL_HORARIO = [
    0.02, 0.01, 0.01, 0.01, 0.01, 0.02,   # 00-05 madrugada muerta
    0.05, 0.15, 0.45, 0.85, 1.00, 0.90,   # 06-11 arranque y pico de mañana
    0.55, 0.40, 0.75, 0.95, 0.88, 0.70,   # 12-17 comida y pico de tarde
    0.40, 0.22, 0.12, 0.08, 0.05, 0.03,   # 18-23 caída
]
# Factor por día ISO (1=lunes .. 7=domingo).
PERFIL_SEMANAL = {1: 1.0, 2: 1.05, 3: 1.0, 4: 0.95, 5: 0.8, 6: 0.12, 7: 0.05}


class Command(BaseCommand):
    help = ("Siembra consumo sintético con forma horaria y semanal realista, "
            "para poder probar el mapa de calor y los tiempos muertos.")

    def add_arguments(self, parser):
        parser.add_argument("--dias", type=int, default=45, help="Días de historia a generar")
        parser.add_argument("--pico", type=int, default=14,
                            help="Peticiones por hora en el momento más cargado")
        parser.add_argument("--con-benchmark", action="store_true",
                            help="Añade una API Key con proposito='benchmark' y un pico "
                                 "sintético, para probar el filtro de exclusión del panel")
        parser.add_argument("--clean", action="store_true", help="Elimina lo generado y sale")
        parser.add_argument("--seed", type=int, default=7, help="Semilla (reproducible)")

    @transaction.atomic
    def handle(self, *args, **options):
        if options["clean"]:
            return self._clean()

        rnd = random.Random(options["seed"])
        ahora = timezone.localtime()
        dias = options["dias"]
        pico = max(1, options["pico"])
        modelos = ["sooniverse-qwen3.5", "sooniverse-embeddings"]

        key = self._key(RITMO_ALIAS, "cliente", ahora, dias, modelos)
        eventos = self._sembrar_trafico(rnd, key, ahora, dias, pico, modelos)

        eventos_bench = 0
        if options["con_benchmark"]:
            bench = self._key(BENCH_ALIAS, "benchmark", ahora, dias, modelos)
            eventos_bench = self._sembrar_benchmark(rnd, bench, ahora, modelos)

        resultado = services.refrescar_metricas(since_hours=dias * 24 + 48,
                                                since_days=dias + 5)
        self.stdout.write(self.style.SUCCESS(
            f"Ritmo sembrado: {eventos} evento(s) de cliente"
            + (f" + {eventos_bench} de benchmark" if eventos_bench else "")
            + f" en {dias} día(s). "
            f"{resultado['filas_agregadas']} fila(s) de rollup, "
            f"{resultado['buckets_horarios']} bucket(s) horario(s)."
        ))
        self.stdout.write("Para revertir: python manage.py seed_ritmo --clean")

    # -- generación ------------------------------------------------------------
    def _key(self, alias, proposito, ahora, dias, modelos):
        key, _ = ApiKeyRegistry.objects.get_or_create(
            key_alias=alias,
            defaults=dict(
                litellm_token_hash=f"{alias}-hash",
                key_prefix=f"sk-{alias[:6]}…demo",
                cliente_id="demo", entorno="dev", is_active=True,
                proposito=proposito,
                owner_email=f"{alias}@demo.local",
                descripcion=f"Datos sintéticos de seed_ritmo ({proposito}).",
                max_budget_usd=50, allowed_models=modelos,
                created_at=ahora - timedelta(days=dias), updated_at=ahora,
            ),
        )
        # get_or_create no actualiza si ya existía: el propósito es load-bearing
        # para el filtro del panel, así que se fuerza.
        if key.proposito != proposito:
            key.proposito = proposito
            key.save(update_fields=["proposito"])
        return key

    def _sembrar_trafico(self, rnd, key, ahora, dias, pico, modelos):
        nuevos = []
        # Tendencia suave al alza para que la proyección del techo tenga algo
        # que ajustar (y para que el r² supere la puerta de confianza).
        for d in range(dias):
            momento_dia = ahora - timedelta(days=d)
            factor_dia = PERFIL_SEMANAL.get(momento_dia.isoweekday(), 0.5)
            crecimiento = 1.0 + 0.35 * (dias - d) / dias

            for hora in range(24):
                intensidad = PERFIL_HORARIO[hora] * factor_dia * crecimiento
                cuantas = int(pico * intensidad * rnd.uniform(0.75, 1.25))
                if cuantas <= 0:
                    continue

                # Carga alta => latencia peor y más errores. Sin esta
                # correlación, el mapa de calor de p95 saldría plano y no
                # probaría nada.
                carga = min(1.0, cuantas / pico)
                for i in range(cuantas):
                    req_id = f"{RITMO_REQUEST_PREFIX}{key.id}-{d}-{hora}-{i}"
                    p = rnd.randint(180, 1200)
                    c = int(p * rnd.uniform(0.3, 0.8))
                    base_ms = 220 + 1800 * carga ** 2
                    latencia = max(60, int(rnd.gauss(base_ms, base_ms * 0.35)))
                    ttft = max(30, int(latencia * rnd.uniform(0.08, 0.30)))
                    fallo = rnd.random() < (0.005 + 0.05 * carga ** 3)
                    nuevos.append(TokenUsageEvent(
                        api_key_id=key.id,
                        litellm_token_hash=key.litellm_token_hash,
                        litellm_request_id=req_id,
                        model_name=rnd.choices(modelos, weights=[0.85, 0.15])[0],
                        model_group=modelos[0],
                        worker_endpoint=f"10.0.{rnd.randint(1, 2)}.{rnd.randint(10, 20)}:8007",
                        call_type="acompletion",
                        cache_hit=False,
                        prompt_tokens=p, completion_tokens=c, total_tokens=p + c,
                        spend_usd=(p + c) * 0.0000004,
                        latency_ms=latencia,
                        ttft_ms=ttft,
                        status="error" if fallo else "success",
                        event_ts=momento_dia.replace(
                            hour=hora, minute=rnd.randint(0, 59), second=rnd.randint(0, 59),
                            microsecond=0),
                        ingested_at=ahora,
                    ))

        return self._insertar(nuevos)

    def _sembrar_benchmark(self, rnd, key, ahora, modelos):
        """Ráfaga corta y brutal, como la del test de estrés real. Si el filtro
        de exclusión del panel funciona, este pico NO debe aparecer en el mapa
        de calor con la casilla desactivada."""
        nuevos = []
        momento = ahora - timedelta(days=1)
        for i in range(600):
            nuevos.append(TokenUsageEvent(
                api_key_id=key.id,
                litellm_token_hash=key.litellm_token_hash,
                litellm_request_id=f"{RITMO_REQUEST_PREFIX}bench-{i}",
                model_name=modelos[0], model_group=modelos[0],
                worker_endpoint="10.0.1.10:8007",
                call_type="acompletion", cache_hit=False,
                prompt_tokens=512, completion_tokens=128, total_tokens=640,
                spend_usd=0.000256,
                latency_ms=rnd.randint(800, 9000),
                ttft_ms=rnd.randint(200, 3000),
                status="success",
                event_ts=momento.replace(hour=3, minute=rnd.randint(0, 4),
                                         second=rnd.randint(0, 59), microsecond=0),
                ingested_at=ahora,
            ))
        return self._insertar(nuevos)

    def _insertar(self, nuevos):
        if not nuevos:
            return 0
        TokenUsageEvent.objects.bulk_create(nuevos, batch_size=2000, ignore_conflicts=True)
        return len(nuevos)

    # -- limpieza --------------------------------------------------------------
    def _clean(self):
        ids = list(
            ApiKeyRegistry.objects.filter(key_alias__in=[RITMO_ALIAS, BENCH_ALIAS])
            .values_list("id", flat=True)
        )
        eventos = TokenUsageEvent.objects.filter(
            litellm_request_id__startswith=RITMO_REQUEST_PREFIX
        ).delete()[0]
        if ids:
            TokenUsageRollup.objects.filter(api_key_id__in=ids).delete()
            ApiKeyAudit.objects.filter(api_key_id__in=ids).delete()
        borradas = ApiKeyRegistry.objects.filter(id__in=ids).delete()[0] if ids else 0

        # Los buckets horarios de esas keys se quedarían huérfanos: se recalculan.
        services.refrescar_metricas(since_days=400)
        self.stdout.write(self.style.WARNING(
            f"Datos de ritmo eliminados: {borradas} API Key(s), {eventos} evento(s)."
        ))
