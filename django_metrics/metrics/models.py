"""
==============================================================================
Modelos de solo-lectura sobre el esquema PostgreSQL `sooniverse`
==============================================================================
Todos los modelos son `managed = False`: el DDL vive en
`database/init_schema.sql` y lo aplica `scripts/db_setup.py`. Django no crea ni
altera estas tablas, solo las consulta y escribe filas.

PRIVACIDAD: ningún modelo expone prompts ni respuestas. Solo contadores de
tokens, timestamps e identificadores de API Key.
"""

from django.contrib.postgres.fields import ArrayField
from django.db import models


class ApiKeyRegistry(models.Model):
    """Registro administrativo de API Keys (espejo de LiteLLM + metadatos de negocio)."""

    ENTORNOS = [("prod", "Producción"), ("dev", "Desarrollo"), ("staging", "Staging")]
    # 'benchmark' marca el tráfico sintético del test de capacidad, que el panel
    # excluye por defecto para no falsear el pico observado.
    PROPOSITOS = [("cliente", "Cliente"), ("benchmark", "Benchmark"), ("sistema", "Sistema")]

    key_alias = models.CharField(max_length=120, verbose_name="Alias")
    litellm_token_hash = models.CharField(max_length=255, null=True, blank=True, unique=True)
    key_prefix = models.CharField(max_length=24, null=True, blank=True)
    cliente_id = models.CharField(max_length=64, default="default")
    entorno = models.CharField(max_length=16, choices=ENTORNOS, default="prod")
    owner_email = models.EmailField(max_length=254, null=True, blank=True)
    descripcion = models.CharField(max_length=500, null=True, blank=True)
    is_active = models.BooleanField(default=True, verbose_name="Activa")
    max_budget_usd = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    tpm_limit = models.IntegerField(null=True, blank=True)
    rpm_limit = models.IntegerField(null=True, blank=True)
    allowed_models = models.JSONField(default=list, blank=True)
    proposito = models.CharField(max_length=24, choices=PROPOSITOS, default="cliente")
    created_at = models.DateTimeField(auto_now_add=False)
    updated_at = models.DateTimeField(auto_now=False)
    deactivated_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = '"sooniverse"."api_key_registry"'
        ordering = ["-created_at"]
        verbose_name = "API Key"
        verbose_name_plural = "API Keys"

    def __str__(self) -> str:
        estado = "activa" if self.is_active else "inactiva"
        return f"{self.key_alias} ({estado})"

    @property
    def estado_label(self) -> str:
        return "ACTIVA" if self.is_active else "INACTIVA"


class TokenUsageEvent(models.Model):
    """Contador de tokens por petición. Sin contenido de la conversación."""

    api_key = models.ForeignKey(
        ApiKeyRegistry, on_delete=models.DO_NOTHING, db_column="api_key_id",
        null=True, blank=True, related_name="events",
    )
    litellm_token_hash = models.CharField(max_length=255, null=True, blank=True)
    litellm_request_id = models.CharField(max_length=255, null=True, blank=True, unique=True)
    model_name = models.CharField(max_length=160, default="unknown")
    worker_endpoint = models.CharField(max_length=255, null=True, blank=True)
    prompt_tokens = models.IntegerField(default=0)
    completion_tokens = models.IntegerField(default=0)
    total_tokens = models.IntegerField(default=0)
    spend_usd = models.DecimalField(max_digits=14, decimal_places=8, default=0)
    # latency_ms y status existían desde el principio pero el ETL nunca los
    # rellenaba (no leía endTime ni status de LiteLLM_SpendLogs): hasta
    # database/004_usage_analytics.sql eran NULL y 'success' para todo.
    latency_ms = models.IntegerField(null=True, blank=True)
    ttft_ms = models.IntegerField(null=True, blank=True, verbose_name="TTFT (ms)")
    status = models.CharField(max_length=24, default="success")
    model_group = models.CharField(max_length=160, null=True, blank=True)
    model_id = models.CharField(max_length=255, null=True, blank=True)
    call_type = models.CharField(max_length=40, null=True, blank=True)
    cache_hit = models.BooleanField(null=True, blank=True)
    event_ts = models.DateTimeField()
    ingested_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = '"sooniverse"."token_usage_event"'
        ordering = ["-event_ts"]
        verbose_name = "Evento de consumo"
        verbose_name_plural = "Eventos de consumo"

    def __str__(self) -> str:
        return f"{self.model_name} · {self.total_tokens} tokens · {self.event_ts:%Y-%m-%d %H:%M}"


class TokenUsageRollup(models.Model):
    """Agregación pre-calculada. `granularity` particiona en daily / weekly / monthly."""

    DAILY, WEEKLY, MONTHLY = "daily", "weekly", "monthly"
    GRANULARITIES = [(DAILY, "Diario"), (WEEKLY, "Semanal"), (MONTHLY, "Mensual")]

    granularity = models.CharField(max_length=10, choices=GRANULARITIES)
    bucket_start = models.DateField(verbose_name="Periodo")
    api_key = models.ForeignKey(
        ApiKeyRegistry, on_delete=models.DO_NOTHING, db_column="api_key_id",
        null=True, blank=True, related_name="rollups",
    )
    model_name = models.CharField(max_length=160, default="unknown")
    request_count = models.BigIntegerField(default=0)
    prompt_tokens = models.BigIntegerField(default=0)
    completion_tokens = models.BigIntegerField(default=0)
    total_tokens = models.BigIntegerField(default=0)
    spend_usd = models.DecimalField(max_digits=16, decimal_places=8, default=0)
    avg_latency_ms = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    error_count = models.BigIntegerField(default=0)
    computed_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = '"sooniverse"."token_usage_rollup"'
        ordering = ["-bucket_start"]
        verbose_name = "Agregación de consumo"
        verbose_name_plural = "Agregaciones de consumo"

    def __str__(self) -> str:
        return f"[{self.granularity}] {self.bucket_start} · {self.total_tokens} tokens"


class ApiKeyAudit(models.Model):
    """Bitácora del ciclo de vida de las API Keys."""

    ACTIONS = [
        ("created", "Creada"), ("updated", "Actualizada"), ("deactivated", "Desactivada"),
        ("reactivated", "Reactivada"), ("deleted", "Eliminada"),
        ("quota_exceeded", "Cuota excedida"), ("rotated", "Rotada"),
    ]

    api_key = models.ForeignKey(
        ApiKeyRegistry, on_delete=models.DO_NOTHING, db_column="api_key_id",
        null=True, blank=True, related_name="audits",
    )
    key_alias = models.CharField(max_length=120, null=True, blank=True)
    action = models.CharField(max_length=32, choices=ACTIONS)
    actor = models.CharField(max_length=254, default="system")
    detalle = models.JSONField(default=dict, blank=True)
    source_ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = '"sooniverse"."api_key_audit"'
        ordering = ["-created_at"]
        verbose_name = "Auditoría de API Key"
        verbose_name_plural = "Auditoría de API Keys"

    def __str__(self) -> str:
        return f"{self.action} · {self.key_alias} · {self.created_at:%Y-%m-%d %H:%M}"


class WorkerNode(models.Model):
    """Inventario del pool vLLM sincronizado por `scripts/sync_endpoints.py`."""

    cluster_name = models.CharField(max_length=160)
    node_rank = models.IntegerField(default=0)
    private_ip = models.CharField(max_length=64)
    port = models.IntegerField(default=8007)
    model_name = models.CharField(max_length=160, null=True, blank=True)
    accelerator = models.CharField(max_length=64, null=True, blank=True)
    is_healthy = models.BooleanField(default=True)
    registered_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    # Ficha de hardware y planificador: sin ella, un techo de capacidad medido
    # no es interpretable (el mismo modelo con max_num_seqs=2 y con 16 da
    # números completamente distintos).
    instance_type = models.CharField(max_length=64, null=True, blank=True)
    gpu_count = models.SmallIntegerField(null=True, blank=True)
    max_num_seqs = models.IntegerField(null=True, blank=True)
    max_num_batched_tokens = models.IntegerField(null=True, blank=True)
    max_model_len = models.IntegerField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = '"sooniverse"."worker_node"'
        ordering = ["cluster_name", "node_rank"]
        verbose_name = "Nodo worker"
        verbose_name_plural = "Nodos worker"

    def __str__(self) -> str:
        return f"{self.private_ip}:{self.port} ({self.cluster_name})"

    @property
    def endpoint(self) -> str:
        return f"http://{self.private_ip}:{self.port}/v1"


class UsageHourly(models.Model):
    """Agregación horaria cortada en la zona de reporte del panel.

    `bucket_local_hour` / `bucket_local_isodow` vienen precalculados por
    `sooniverse.refresh_usage_hourly`, así que el mapa de calor agrupa por ellos
    directamente y no necesita EXTRACT ni conversiones de zona en el ORM.

    OJO: los percentiles de esta tabla son POR HORA y NO son recombinables.
    Promediar los p95 de 13 lunes no da el p95 del lunes. Para un percentil
    sobre una ventana mayor hay que volver a `token_usage_event`, vía
    `sooniverse.latency_percentiles()` (ver metrics/analytics.py).
    Lo que SÍ se puede recombinar es `latency_sum_ms` / `latency_count`.
    """

    bucket_ts = models.DateTimeField(verbose_name="Hora")
    tz_name = models.TextField()
    bucket_local_date = models.DateField()
    bucket_local_hour = models.SmallIntegerField()
    bucket_local_isodow = models.SmallIntegerField(verbose_name="Día ISO (1=lunes)")
    api_key = models.ForeignKey(
        ApiKeyRegistry, on_delete=models.DO_NOTHING, db_column="api_key_id",
        null=True, blank=True, related_name="horas",
    )
    model_name = models.CharField(max_length=160, default="unknown")
    request_count = models.BigIntegerField(default=0)
    error_count = models.BigIntegerField(default=0)
    cache_hit_count = models.BigIntegerField(default=0)
    prompt_tokens = models.BigIntegerField(default=0)
    completion_tokens = models.BigIntegerField(default=0)
    total_tokens = models.BigIntegerField(default=0)
    spend_usd = models.DecimalField(max_digits=16, decimal_places=8, default=0)
    latency_p50_ms = models.IntegerField(null=True, blank=True)
    latency_p95_ms = models.IntegerField(null=True, blank=True)
    latency_p99_ms = models.IntegerField(null=True, blank=True)
    latency_max_ms = models.IntegerField(null=True, blank=True)
    ttft_p50_ms = models.IntegerField(null=True, blank=True)
    ttft_p95_ms = models.IntegerField(null=True, blank=True)
    latency_sum_ms = models.BigIntegerField(default=0)
    latency_count = models.BigIntegerField(default=0)
    computed_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = '"sooniverse"."usage_hourly"'
        ordering = ["-bucket_ts"]
        verbose_name = "Agregación horaria"
        verbose_name_plural = "Agregaciones horarias"

    def __str__(self) -> str:
        return f"{self.bucket_local_date} {self.bucket_local_hour:02d}h · {self.request_count} pet."


class CapacityBenchmark(models.Model):
    """Una corrida de scripts/benchmark_capacity.py: el techo medido de la
    infraestructura junto al snapshot de configuración bajo el que se midió."""

    MOTIVOS = [
        ("nivel_maximo", "La rampa terminó sin degradar"),
        ("p95_degradado", "Latencia p95 degradada"),
        ("errores", "Tasa de error superada"),
        ("saturacion_throughput", "El throughput dejó de crecer"),
        ("presupuesto_agotado", "Presupuesto de tiempo agotado"),
        ("fallo", "Fallo de la corrida"),
    ]

    run_id = models.UUIDField(unique=True)
    client_id = models.TextField()
    environment = models.TextField()
    deployment_id = models.UUIDField(null=True, blank=True)
    workload_id = models.TextField()
    model_public_name = models.TextField()

    instance_type = models.TextField(null=True, blank=True)
    accelerator = models.TextField(null=True, blank=True)
    gpu_count = models.IntegerField(null=True, blank=True)
    replicas = models.IntegerField(null=True, blank=True)
    max_num_seqs = models.IntegerField(null=True, blank=True)
    max_num_batched_tokens = models.IntegerField(null=True, blank=True)
    max_model_len = models.IntegerField(null=True, blank=True)
    gpu_memory_utilization = models.DecimalField(max_digits=4, decimal_places=3, null=True, blank=True)
    enforce_eager = models.BooleanField(null=True, blank=True)
    quantization = models.TextField(null=True, blank=True)
    vllm_version = models.TextField(null=True, blank=True)
    lb_strategy = models.TextField(null=True, blank=True)

    # INTEGER[] en PostgreSQL, no JSONB: psycopg2 ya devuelve una lista de
    # Python, así que un JSONField reventaría al intentar json.loads() sobre
    # ella. ArrayField es el mapeo correcto para esta columna.
    niveles_concurrencia = ArrayField(models.IntegerField(), default=list)
    prompt_tokens_objetivo = models.IntegerField()
    max_tokens = models.IntegerField()
    segundos_por_nivel = models.IntegerField()
    warmup_segundos = models.IntegerField(default=0)
    streaming = models.BooleanField(default=True)
    origen = models.TextField(default="gateway")
    benchmark_key_alias = models.TextField(null=True, blank=True)
    benchmark_key_hash = models.TextField(null=True, blank=True)

    concurrencia_rodilla = models.IntegerField(null=True, blank=True)
    rpm_sostenido = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    tokens_salida_por_min = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    tokens_totales_por_min = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    p50_base_ms = models.IntegerField(null=True, blank=True)
    p95_base_ms = models.IntegerField(null=True, blank=True)
    ttft_p50_base_ms = models.IntegerField(null=True, blank=True)
    ttft_p95_base_ms = models.IntegerField(null=True, blank=True)
    p95_rodilla_ms = models.IntegerField(null=True, blank=True)
    ttft_p95_rodilla_ms = models.IntegerField(null=True, blank=True)
    itl_medio_rodilla_ms = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    tasa_error_pct = models.DecimalField(max_digits=6, decimal_places=3, default=0)
    motivo_parada = models.CharField(max_length=40, choices=MOTIVOS)
    usuarios_estimados = models.IntegerField(null=True, blank=True)

    curva = models.JSONField(default=list)
    notas = models.JSONField(default=dict)
    duracion_total_seg = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = '"sooniverse"."capacity_benchmark"'
        ordering = ["-finished_at"]
        verbose_name = "Corrida de capacidad"
        verbose_name_plural = "Corridas de capacidad"

    def __str__(self) -> str:
        return f"{self.model_public_name} · rodilla {self.concurrencia_rodilla} · {self.finished_at:%Y-%m-%d}"

    @property
    def hardware_label(self) -> str:
        piezas = [self.instance_type or "—"]
        if self.accelerator:
            piezas.append(f"{self.gpu_count or 1}×{self.accelerator}")
        return " · ".join(piezas)

    @property
    def motivo_label(self) -> str:
        return dict(self.MOTIVOS).get(self.motivo_parada, self.motivo_parada)

    @property
    def rodilla_es_el_tope_probado(self) -> bool:
        """True cuando la rampa acabó sin doler: el techo real puede ser MAYOR
        que el medido, y decirlo importa para no infradimensionar."""
        return self.motivo_parada == "nivel_maximo"
