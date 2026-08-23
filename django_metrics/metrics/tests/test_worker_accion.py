"""
Pruebas de metrics.views.worker_accion y metrics.services.ejecutar_accion_worker.

Mismo enfoque que test_validacion_vistas.py: RequestFactory + mocks, sin BD
real -WorkerNode/WorkerAction son managed=False (la tabla vive en el esquema
'sooniverse' de PostgreSQL, no en la BD de pruebas de Django) y el resto de
este archivo de tests del proyecto evita deliberadamente tocar una BD real
desde `manage.py test`.
"""

from unittest.mock import MagicMock, patch

from django.contrib.messages.storage.fallback import FallbackStorage
from django.http import Http404
from django.test import RequestFactory, SimpleTestCase

from metrics import services, views
from metrics.models import WorkerAction, WorkerNode
from metrics.workers import WorkerActionError

rf = RequestFactory()


def _staff_request(method="post", **post_data):
    req = getattr(rf, method)("/panel/metrics/workers/1/health/", post_data)
    req.user = MagicMock(username="opeardor", is_active=True, is_staff=True)
    req.session = {}
    req._messages = FallbackStorage(req)
    return req


def _fake_worker(**overrides):
    defaults = dict(
        id=1, cluster_name="sooniverse-acme-prod-qwen3-5-llm", private_ip="10.0.128.12",
        port=8007, estado_operativo="sano", instance_id="i-0abc123",
    )
    defaults.update(overrides)
    return WorkerNode(**defaults)


class WorkerAccionViewTests(SimpleTestCase):
    def test_get_rejected_by_require_post(self):
        req = _staff_request("get")
        resp = views.worker_accion(req, node_id=1, accion="health")
        self.assertEqual(resp.status_code, 405)

    def test_accion_desconocida_no_llama_al_servicio(self):
        req = _staff_request()
        with patch("metrics.views.services.ejecutar_accion_worker") as ejecutar:
            resp = views.worker_accion(req, node_id=1, accion="borrar-todo")
        ejecutar.assert_not_called()
        self.assertEqual(resp.status_code, 302)

    def test_nodo_de_otro_cliente_entorno_da_404(self):
        req = _staff_request()
        with patch("metrics.views.get_object_or_404", side_effect=Http404):
            with self.assertRaises(Http404):
                views.worker_accion(req, node_id=999, accion="health")

    def test_accion_exitosa_muestra_mensaje_de_exito_y_redirige(self):
        req = _staff_request(next="/panel/metrics/")
        worker = _fake_worker()
        with patch("metrics.views.get_object_or_404", return_value=worker), \
             patch("metrics.views.services.ejecutar_accion_worker", return_value="Responde OK") as ejecutar:
            resp = views.worker_accion(req, node_id=1, accion="health")

        ejecutar.assert_called_once()
        self.assertEqual(ejecutar.call_args.args[0], worker)
        self.assertEqual(ejecutar.call_args.args[1], "health")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, "/panel/metrics/")

    def test_worker_action_error_muestra_mensaje_de_error_sin_excepcion(self):
        req = _staff_request()
        worker = _fake_worker()
        with patch("metrics.views.get_object_or_404", return_value=worker), \
             patch("metrics.views.services.ejecutar_accion_worker",
                   side_effect=WorkerActionError("no responde")):
            resp = views.worker_accion(req, node_id=1, accion="restart")

        self.assertEqual(resp.status_code, 302)
        mensajes = list(req._messages)
        self.assertTrue(any("no responde" in str(m) for m in mensajes))


class EjecutarAccionWorkerTests(SimpleTestCase):
    def test_health_no_muta_estado_operativo(self):
        worker = _fake_worker(estado_operativo="sano")
        with patch("metrics.services.workers_mod.comprobar_salud", return_value="OK") as fn, \
             patch.object(WorkerAction.objects, "create") as audit, \
             patch.object(WorkerNode, "save") as save:
            mensaje = services.ejecutar_accion_worker(worker, "health", actor="tester")

        fn.assert_called_once_with(worker)
        audit.assert_called_once()
        self.assertEqual(audit.call_args.kwargs["estado"], "ok")
        save.assert_not_called()
        self.assertEqual(mensaje, "OK")

    def test_stop_exitoso_marca_apagado(self):
        worker = _fake_worker(estado_operativo="sano")
        with patch("metrics.services.workers_mod.apagar_worker", return_value="Apagando") as fn, \
             patch.object(WorkerAction.objects, "create") as audit, \
             patch.object(WorkerNode, "save") as save:
            services.ejecutar_accion_worker(worker, "stop", actor="tester")

        fn.assert_called_once()
        self.assertEqual(worker.estado_operativo, "apagado")
        save.assert_called_once_with(update_fields=["estado_operativo"])
        self.assertEqual(audit.call_args.kwargs["estado"], "ok")

    def test_restart_exitoso_marca_reiniciando(self):
        worker = _fake_worker(estado_operativo="sano")
        with patch("metrics.services.workers_mod.reiniciar_vllm", return_value="Reiniciado"), \
             patch.object(WorkerAction.objects, "create"), \
             patch.object(WorkerNode, "save"):
            services.ejecutar_accion_worker(worker, "restart", actor="tester")
        self.assertEqual(worker.estado_operativo, "reiniciando")

    def test_fallo_registra_auditoria_de_error_y_propaga(self):
        worker = _fake_worker(estado_operativo="sano")
        with patch("metrics.services.workers_mod.apagar_worker",
                   side_effect=WorkerActionError("sin permiso IAM")), \
             patch.object(WorkerAction.objects, "create") as audit, \
             patch.object(WorkerNode, "save") as save:
            with self.assertRaises(WorkerActionError):
                services.ejecutar_accion_worker(worker, "stop", actor="tester")

        self.assertEqual(audit.call_args.kwargs["estado"], "error")
        save.assert_not_called()
        # El estado_operativo original no se toca en un fallo.
        self.assertEqual(worker.estado_operativo, "sano")

    def test_accion_desconocida_lanza_worker_action_error(self):
        worker = _fake_worker()
        with self.assertRaises(WorkerActionError):
            services.ejecutar_accion_worker(worker, "borrar-todo", actor="tester")


class EstadoPoolDerivacionTests(SimpleTestCase):
    """estado_pool() deriva estado_operativo en cada carga -no confía en lo
    que dejó la última sincronización si los datos ya son viejos."""

    def _run(self, nodos, litellm_ok=True, health=None):
        cliente = MagicMock()
        cliente.is_reachable.return_value = litellm_ok
        cliente.health.return_value = health or {"healthy_endpoints": [], "unhealthy_endpoints": []}
        cliente.models.return_value = []
        with patch("metrics.services.WorkerNode.objects") as manager, \
             patch("metrics.services.LiteLLMClient", return_value=cliente):
            manager.filter.return_value = nodos
            return services.estado_pool()

    def test_nodo_apagado_nunca_se_sobrescribe(self):
        from django.utils import timezone
        worker = _fake_worker(estado_operativo="apagado", last_seen_at=timezone.now())
        estado = self._run([worker])
        self.assertEqual(worker.estado_operativo, "apagado")
        self.assertEqual(estado["nodos_sanos"], 0)

    def test_nodo_sin_sincronizar_reciente_es_desincronizado(self):
        from datetime import timedelta

        from django.utils import timezone
        worker = _fake_worker(
            estado_operativo="sano", is_healthy=True,
            last_seen_at=timezone.now() - timedelta(hours=2),
        )
        with self.settings(METRICS_REFRESH_INTERVAL=300):
            estado = self._run([worker])
        self.assertEqual(worker.estado_operativo, "desincronizado")
        self.assertEqual(estado["nodos_sanos"], 0)

    def test_nodo_fresco_y_sano_en_litellm_es_sano(self):
        from django.utils import timezone
        worker = _fake_worker(estado_operativo="sano", is_healthy=True, last_seen_at=timezone.now())
        health = {"healthy_endpoints": [{"api_base": worker.endpoint}], "unhealthy_endpoints": []}
        estado = self._run([worker], health=health)
        self.assertEqual(worker.estado_operativo, "sano")
        self.assertEqual(estado["nodos_sanos"], 1)

    def test_nodo_fresco_pero_ausente_del_pool_litellm_es_degradado(self):
        from django.utils import timezone
        worker = _fake_worker(estado_operativo="sano", is_healthy=True, last_seen_at=timezone.now())
        estado = self._run([worker], health={"healthy_endpoints": [], "unhealthy_endpoints": []})
        self.assertEqual(worker.estado_operativo, "degradado")
        self.assertEqual(estado["nodos_sanos"], 0)
