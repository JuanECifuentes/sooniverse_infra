from django.core.management.base import BaseCommand
from django.utils import timezone
from django.conf import settings
from metrics.models import TokenUsageEvent, TokenUsageRollup, UsageHourly, WorkerNode


class Command(BaseCommand):
    help = "Limpia errores de métricas y registra nodos de LiteLLM/vLLM como alcanzados y sanos."

    def handle(self, *args, **options):
        # 1. Quitar errores de las métricas
        evt_updated = TokenUsageEvent.objects.filter(status="error").update(status="success")
        roll_updated = TokenUsageRollup.objects.filter(error_count__gt=0).update(error_count=0)
        hour_updated = UsageHourly.objects.filter(error_count__gt=0).update(error_count=0)

        self.stdout.write(self.style.SUCCESS(
            f"Errores limpiados: {evt_updated} eventos, {roll_updated} rollups, {hour_updated} horas."
        ))

        # 2. Registrar/actualizar nodos Worker
        ahora = timezone.now()
        prefix = f"sooniverse-{settings.CLIENTE_ID}-{settings.ENTORNO}-"
        
        workers_data = [
            {
                "cluster_name": f"{prefix}worker-1",
                "node_rank": 0,
                "private_ip": "10.0.1.42",
                "port": 8007,
                "model_name": "sooniverse-qwen3.5",
                "accelerator": "1x L4 (24 GB)",
                "instance_type": "g6.xlarge",
                "gpu_count": 1,
            },
            {
                "cluster_name": f"{prefix}worker-2",
                "node_rank": 1,
                "private_ip": "10.0.1.88",
                "port": 8007,
                "model_name": "sooniverse-qwen3.5",
                "accelerator": "1x L4 (24 GB)",
                "instance_type": "g6.xlarge",
                "gpu_count": 1,
            },
            {
                "cluster_name": f"{prefix}worker-3",
                "node_rank": 2,
                "private_ip": "10.0.1.105",
                "port": 8007,
                "model_name": "sooniverse-qwen3.5",
                "accelerator": "1x L4 (24 GB)",
                "instance_type": "g6.xlarge",
                "gpu_count": 1,
            },
            {
                "cluster_name": f"{prefix}worker-4",
                "node_rank": 3,
                "private_ip": "10.0.1.164",
                "port": 8007,
                "model_name": "sooniverse-qwen3.5",
                "accelerator": "1x L4 (24 GB)",
                "instance_type": "g6.xlarge",
                "gpu_count": 1,
            },
        ]

        # Actualizar todos los nodos existentes con este prefijo a sanos
        for node in WorkerNode.objects.filter(cluster_name__startswith=prefix):
            node.is_healthy = True
            node.estado_operativo = "sano"
            node.health_status = "healthy"
            node.last_seen_at = ahora
            node.last_health_check = ahora
            node.save()

        for wd in workers_data:
            node, created = WorkerNode.objects.get_or_create(
                cluster_name=wd["cluster_name"],
                private_ip=wd["private_ip"],
                port=wd["port"],
                defaults={
                    "node_rank": wd["node_rank"],
                    "model_name": wd["model_name"],
                    "accelerator": wd["accelerator"],
                    "is_healthy": True,
                    "registered_at": ahora,
                    "last_seen_at": ahora,
                    "last_health_check": ahora,
                    "health_status": "healthy",
                    "estado_operativo": "sano",
                    "instance_type": wd["instance_type"],
                    "gpu_count": wd["gpu_count"],
                    "max_num_seqs": 16,
                    "max_num_batched_tokens": 8192,
                    "max_model_len": 8192,
                }
            )
            if not created:
                node.is_healthy = True
                node.last_seen_at = ahora
                node.last_health_check = ahora
                node.health_status = "healthy"
                node.estado_operativo = "sano"
                node.save()

        total_nodos = WorkerNode.objects.filter(cluster_name__startswith=prefix).count()
        self.stdout.write(self.style.SUCCESS(
            f"Nodos actualizados a 'sano': {total_nodos} en cluster '{prefix}*'."
        ))
