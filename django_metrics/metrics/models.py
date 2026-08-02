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

from django.db import models


class ApiKeyRegistry(models.Model):
    """Registro administrativo de API Keys (espejo de LiteLLM + metadatos de negocio)."""

    ENTORNOS = [("prod", "Producción"), ("dev", "Desarrollo"), ("staging", "Staging")]

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
    latency_ms = models.IntegerField(null=True, blank=True)
    status = models.CharField(max_length=24, default="success")
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
