from django.contrib import admin

from .models import ApiKeyAudit, ApiKeyRegistry, TokenUsageRollup, WorkerNode


@admin.register(ApiKeyRegistry)
class ApiKeyRegistryAdmin(admin.ModelAdmin):
    list_display = ("key_alias", "key_prefix", "cliente_id", "entorno", "is_active",
                    "max_budget_usd", "created_at")
    list_filter = ("is_active", "entorno", "cliente_id")
    search_fields = ("key_alias", "owner_email", "key_prefix")
    readonly_fields = ("litellm_token_hash", "key_prefix", "created_at", "updated_at")


@admin.register(TokenUsageRollup)
class TokenUsageRollupAdmin(admin.ModelAdmin):
    list_display = ("bucket_start", "granularity", "api_key", "model_name",
                    "request_count", "total_tokens", "spend_usd")
    list_filter = ("granularity", "model_name")
    date_hierarchy = "bucket_start"

    def has_add_permission(self, request):
        return False  # Las agregaciones se calculan en PostgreSQL.


@admin.register(ApiKeyAudit)
class ApiKeyAuditAdmin(admin.ModelAdmin):
    list_display = ("created_at", "action", "key_alias", "actor", "source_ip")
    list_filter = ("action",)
    search_fields = ("key_alias", "actor")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False  # La bitácora es inmutable.


@admin.register(WorkerNode)
class WorkerNodeAdmin(admin.ModelAdmin):
    list_display = ("private_ip", "port", "cluster_name", "model_name",
                    "accelerator", "is_healthy", "last_seen_at")
    list_filter = ("is_healthy", "cluster_name")
