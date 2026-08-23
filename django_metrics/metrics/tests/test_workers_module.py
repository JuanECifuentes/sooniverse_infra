"""
Pruebas de metrics.workers -las tres mecánicas detrás de los botones de la
card "Pool vLLM": comprobar salud (requests), reiniciar (SSH via subprocess,
mockeado) y apagar/arrancar (boto3, contra EC2 simulado con moto).
"""

import subprocess
from unittest.mock import MagicMock, patch

import boto3
from django.test import SimpleTestCase
from moto import mock_aws

from metrics import workers
from metrics.models import WorkerNode
from metrics.workers import WorkerActionError

REGION = "us-east-1"


def _fake_worker(**overrides):
    defaults = dict(
        id=1, cluster_name="sooniverse-acme-prod-qwen3-5-llm", private_ip="10.0.128.12",
        port=8007, estado_operativo="sano",
    )
    defaults.update(overrides)
    return WorkerNode(**defaults)


class ComprobarSaludTests(SimpleTestCase):
    def test_responde_ok(self):
        worker = _fake_worker()
        resp = MagicMock(status_code=200)
        resp.raise_for_status.return_value = None
        with patch("metrics.workers.requests.get", return_value=resp) as get:
            mensaje = workers.comprobar_salud(worker)
        get.assert_called_once_with("http://10.0.128.12:8007/health", timeout=workers.HEALTH_TIMEOUT_SECONDS)
        self.assertIn("OK", mensaje)

    def test_no_responde_lanza_worker_action_error(self):
        import requests as requests_mod

        worker = _fake_worker()
        with patch("metrics.workers.requests.get", side_effect=requests_mod.ConnectionError("boom")):
            with self.assertRaises(WorkerActionError):
                workers.comprobar_salud(worker)


class ReiniciarVllmTests(SimpleTestCase):
    def test_sin_clave_ssh_lanza_error_sin_intentar_conectar(self):
        worker = _fake_worker()
        with patch("metrics.workers._ssh_key_path", return_value=None), \
             patch("metrics.workers.subprocess.run") as run:
            with self.assertRaises(WorkerActionError):
                workers.reiniciar_vllm(worker)
        run.assert_not_called()

    def test_ssh_exitoso_devuelve_mensaje_con_salida(self):
        worker = _fake_worker()
        result = MagicMock(returncode=0, stdout="abc123def456\n", stderr="")
        with patch("metrics.workers._ssh_key_path", return_value="/app/.ssh/bastion_key"), \
             patch("metrics.workers.subprocess.run", return_value=result) as run:
            mensaje = workers.reiniciar_vllm(worker)
        self.assertIn("abc123def456", mensaje)
        # Verifica que apunta a la IP del worker, no a la del gateway.
        cmd = run.call_args.args[0]
        self.assertIn("ubuntu@10.0.128.12", cmd)

    def test_ssh_con_codigo_de_error_lanza_worker_action_error(self):
        worker = _fake_worker()
        result = MagicMock(returncode=255, stdout="", stderr="Permission denied")
        with patch("metrics.workers._ssh_key_path", return_value="/app/.ssh/bastion_key"), \
             patch("metrics.workers.subprocess.run", return_value=result):
            with self.assertRaises(WorkerActionError):
                workers.reiniciar_vllm(worker)

    def test_sin_contenedores_en_marcha_lanza_worker_action_error(self):
        worker = _fake_worker()
        result = MagicMock(returncode=0, stdout="SOONIVERSE_NO_CONTAINERS\n", stderr="")
        with patch("metrics.workers._ssh_key_path", return_value="/app/.ssh/bastion_key"), \
             patch("metrics.workers.subprocess.run", return_value=result):
            with self.assertRaises(WorkerActionError):
                workers.reiniciar_vllm(worker)

    def test_timeout_lanza_worker_action_error(self):
        worker = _fake_worker()
        with patch("metrics.workers._ssh_key_path", return_value="/app/.ssh/bastion_key"), \
             patch("metrics.workers.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=30)):
            with self.assertRaises(WorkerActionError):
                workers.reiniciar_vllm(worker)


class ApagarArrancarWorkerTests(SimpleTestCase):
    def test_sin_instance_id_lanza_error_sin_llamar_a_aws(self):
        worker = _fake_worker(instance_id=None)
        with patch("metrics.workers._ec2_client") as client:
            with self.assertRaises(WorkerActionError):
                workers.apagar_worker(worker, REGION)
        client.assert_not_called()

    def test_apagar_worker_detiene_la_instancia_real(self):
        with mock_aws():
            ec2 = boto3.client("ec2", region_name=REGION)
            instance_id = ec2.run_instances(ImageId="ami-12345678", MinCount=1, MaxCount=1)["Instances"][0]["InstanceId"]
            worker = _fake_worker(instance_id=instance_id)

            workers.apagar_worker(worker, REGION)

            estado = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]["State"]["Name"]
            self.assertIn(estado, ("stopping", "stopped"))

    def test_arrancar_worker_arranca_la_instancia_real(self):
        with mock_aws():
            ec2 = boto3.client("ec2", region_name=REGION)
            instance_id = ec2.run_instances(ImageId="ami-12345678", MinCount=1, MaxCount=1)["Instances"][0]["InstanceId"]
            ec2.stop_instances(InstanceIds=[instance_id])
            worker = _fake_worker(instance_id=instance_id)

            workers.arrancar_worker(worker, REGION)

            estado = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]["State"]["Name"]
            self.assertIn(estado, ("pending", "running"))

    def test_apagar_instancia_inexistente_lanza_worker_action_error(self):
        with mock_aws():
            worker = _fake_worker(instance_id="i-0000000000000dead")
            with self.assertRaises(WorkerActionError):
                workers.apagar_worker(worker, REGION)


class SshKeyDisponibilidadTests(SimpleTestCase):
    def test_restart_disponible_false_sin_archivo(self):
        with patch("metrics.workers.SSH_KEY_PATH") as key_path:
            key_path.is_file.return_value = False
            self.assertFalse(workers.restart_disponible())

    def test_restart_disponible_true_con_archivo(self):
        with patch("metrics.workers.SSH_KEY_PATH") as key_path:
            key_path.is_file.return_value = True
            self.assertTrue(workers.restart_disponible())
