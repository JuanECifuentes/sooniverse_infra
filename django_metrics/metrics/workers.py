"""
==============================================================================
Acciones sobre workers vLLM desde el panel (card "Pool vLLM")
==============================================================================
Tres acciones, tres mecanismos distintos, cada uno con su propio requisito de
infraestructura -si el requisito falta, la función correspondiente levanta
WorkerActionError con un mensaje accionable; las vistas usan las funciones
`*_disponible()` para deshabilitar el botón en la plantilla en vez de fallar
al pulsarlo:

- Comprobar salud: GET /health directo al worker. El Security Group ya
  permite Gateway->worker en el puerto vLLM (aws_network.py), y el contenedor
  'metrics' sale a la VPC con la ENI del Gateway -no requiere nada nuevo.
- Reiniciar: SSH al worker (a través de la red interna, el contenedor ya vive
  dentro de la VPC) con la clave que SkyPilot generó para el clúster del
  Gateway. Reinicia TODOS los contenedores Docker del host -un worker vLLM es
  una instancia dedicada que no corre nada más, así que es equivalente a
  reiniciar vLLM sin tener que conocer el nombre exacto del servicio/directorio.
- Apagar / Arrancar: boto3 sobre la instancia EC2 (stop/start-instances). Es
  lo único que de verdad deja de cobrar cómputo. Usa credenciales AWS
  DEDICADAS (usuario IAM separado del despliegue, ver
  scripts/aws_iam_worker_control.py) con permiso ÚNICAMENTE de start/stop/
  describe sobre los workers de este cliente/entorno -`ec2_disponible()`
  verifica ese permiso con un DryRun real antes de mostrar el botón, y
  `_require_ec2_permiso()` lo vuelve a comprobar en el servidor antes de
  ejecutar la acción, por si alguien la dispara sin pasar por la plantilla.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Optional

import requests

from .models import WorkerNode

logger = logging.getLogger(__name__)

HEALTH_TIMEOUT_SECONDS = 5
SSH_TIMEOUT_SECONDS = 30
EC2_TIMEOUT_SECONDS = 30
# La clave real vive en ~/.sky/generated/ssh-keys/<gateway_cluster>.key EN LA
# MÁQUINA DEL OPERADOR/CI que corre 'sky launch' -nunca en el propio Gateway.
# scripts/generate_infra.py::TopologyBuilder.build_gateway() la copia ahí vía
# file_mounts en cada despliegue (si ya existe: en el primer 'sky launch' de
# un clúster todavía no existe, SkyPilot la genera como efecto secundario de
# esa misma corrida), y render_gateway_stack.py la monta en esta ruta fija
# dentro del contenedor si el archivo host existe.
SSH_KEY_PATH = Path("/app/.ssh/bastion_key")


class WorkerActionError(Exception):
    """Fallo ejecutando una acción sobre un worker. El mensaje es user-facing."""


def _ssh_key_path() -> Optional[Path]:
    return SSH_KEY_PATH if SSH_KEY_PATH.is_file() else None


def restart_disponible() -> bool:
    return _ssh_key_path() is not None


def ec2_disponible(instance_id: Optional[str] = None, region: Optional[str] = None) -> bool:
    """Comprueba con un DryRun real -no solo "boto3 está instalado"- si las
    credenciales AWS configuradas EN ESTE GATEWAY (`AWS_ACCESS_KEY_ID`/
    `AWS_SECRET_ACCESS_KEY` del usuario IAM dedicado que crea
    `scripts/aws_iam_worker_control.py`, NUNCA las del despliegue automático)
    pueden apagar/arrancar instancias EC2.

    Con `instance_id`, `DryRun=True` contra ESE recurso le devuelve a AWS la
    respuesta definitiva a esta pregunta exacta, sin ejecutar la acción:
    'DryRunOperation' -> autorizado (la llamada real habría funcionado);
    'UnauthorizedOperation' -> las credenciales existen pero no tienen el
    permiso. Cualquier otro resultado (sin credenciales, credenciales
    inválidas, instancia inexistente, sin red...) se trata como NO disponible
    -fail-closed, igual que el resto de `*_disponible()`: antes de esto,
    'ec2_disponible' era `True` con solo tener boto3 instalado, aunque
    `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` estuvieran vacías o
    pertenecieran a un usuario sin ningún permiso EC2 -el botón se mostraba
    habilitado y fallaba recién al pulsarlo con un error de IAM críptico.

    Sin `instance_id` (pool vacío, nada que probar de verdad) solo confirma
    que hay credenciales cargadas."""
    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError:
        return False

    try:
        ec2 = boto3.client("ec2", region_name=region)
        if instance_id:
            ec2.stop_instances(InstanceIds=[instance_id], DryRun=True)
        else:
            ec2.describe_instances(DryRun=True)
        return False  # inalcanzable en la práctica: DryRun siempre lanza ClientError
    except ClientError as exc:
        return exc.response.get("Error", {}).get("Code") == "DryRunOperation"
    except Exception:  # noqa: BLE001 - sin credenciales/red/etc. -> fail-closed
        return False


# -----------------------------------------------------------------------------
# Comprobar salud
# -----------------------------------------------------------------------------
def comprobar_salud(worker: WorkerNode) -> str:
    """GET /health directo al worker. De solo lectura/diagnóstico: NUNCA
    escribe is_healthy/estado_operativo -esa es responsabilidad exclusiva de
    `scripts/sync_endpoints.py`, la única fuente que debe escribir ese estado
    (si esta acción también escribiera, tendríamos dos escritores compitiendo
    con la misma columna en dos cadencias distintas)."""
    url = f"http://{worker.private_ip}:{worker.port}/health"
    try:
        resp = requests.get(url, timeout=HEALTH_TIMEOUT_SECONDS)
        resp.raise_for_status()
        return f"Responde OK ({resp.status_code}) en {worker.private_ip}:{worker.port}"
    except requests.RequestException as exc:
        raise WorkerActionError(f"No responde en {worker.private_ip}:{worker.port}: {exc}") from exc


# -----------------------------------------------------------------------------
# Reiniciar vLLM (SSH)
# -----------------------------------------------------------------------------
def reiniciar_vllm(worker: WorkerNode) -> str:
    """Reinicia todos los contenedores Docker del worker por SSH directo -sin
    pasar por un ProxyJump explícito: el contenedor 'metrics' ya vive en la red
    del Gateway (misma VPC), así que su tráfico saliente ya usa la ENI que el
    Security Group de workers acepta en el puerto 22 (SG->SG)."""
    key_path = _ssh_key_path()
    if not key_path:
        raise WorkerActionError(
            "No se encontró la clave SSH del clúster Gateway en este contenedor; "
            "el reinicio remoto no está disponible en este despliegue."
        )

    remote_cmd = "sudo docker restart $(sudo docker ps -q) 2>&1 || echo SOONIVERSE_NO_CONTAINERS"
    cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", f"ConnectTimeout={SSH_TIMEOUT_SECONDS}",
        "-i", str(key_path),
        f"ubuntu@{worker.private_ip}",
        remote_cmd,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=SSH_TIMEOUT_SECONDS + 15,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkerActionError(f"SSH a {worker.private_ip} excedió el tiempo de espera") from exc

    salida = (result.stdout or "").strip()
    error = (result.stderr or "").strip()
    if result.returncode != 0:
        raise WorkerActionError(f"SSH falló (código {result.returncode}): {error or salida}")
    if "SOONIVERSE_NO_CONTAINERS" in salida:
        raise WorkerActionError("Conectado por SSH, pero no se encontró ningún contenedor Docker en marcha.")
    return f"Contenedor(es) reiniciado(s): {salida or '(sin salida)'}"


# -----------------------------------------------------------------------------
# Apagar / Arrancar (boto3, EC2)
# -----------------------------------------------------------------------------
def _ec2_client(region: str):
    import boto3

    return boto3.client("ec2", region_name=region)


def _require_ec2_permiso(worker: WorkerNode, region: str) -> None:
    """Repite en el servidor la misma comprobación que oculta el botón en la
    plantilla -nunca confiar solo en que el cliente no mande la petición: un
    POST directo a `/workers/<id>/stop/` sin pasar por la UI se ejecutaría
    igual si esta función no existiera."""
    if not ec2_disponible(instance_id=worker.instance_id, region=region):
        raise WorkerActionError(
            "Las credenciales AWS de este despliegue no tienen permiso para apagar/arrancar "
            "instancias EC2 (o no están configuradas). Revisa que exista el usuario IAM dedicado "
            "a esta acción y que 'AWS_ACCESS_KEY_ID'/'AWS_SECRET_ACCESS_KEY' en el .env del "
            "Gateway sean los suyos -nunca los del despliegue automático."
        )


def apagar_worker(worker: WorkerNode, region: str) -> str:
    if not worker.instance_id:
        raise WorkerActionError(
            "Este nodo no tiene 'instance_id' registrado (se puebla desde "
            "scripts/sync_endpoints.py); no se puede apagar desde el panel."
        )
    _require_ec2_permiso(worker, region)
    try:
        ec2 = _ec2_client(region)
        ec2.stop_instances(InstanceIds=[worker.instance_id])
    except Exception as exc:  # noqa: BLE001 - boto3 lanza excepciones de tipos variados (ClientError, etc.)
        raise WorkerActionError(f"No se pudo apagar {worker.instance_id}: {exc}") from exc
    return f"Apagando instancia {worker.instance_id} (stop-instances solicitado)"


def arrancar_worker(worker: WorkerNode, region: str) -> str:
    if not worker.instance_id:
        raise WorkerActionError(
            "Este nodo no tiene 'instance_id' registrado (se puebla desde "
            "scripts/sync_endpoints.py); no se puede arrancar desde el panel."
        )
    _require_ec2_permiso(worker, region)
    try:
        ec2 = _ec2_client(region)
        ec2.start_instances(InstanceIds=[worker.instance_id])
    except Exception as exc:  # noqa: BLE001
        raise WorkerActionError(f"No se pudo arrancar {worker.instance_id}: {exc}") from exc
    return f"Arrancando instancia {worker.instance_id} (start-instances solicitado)"
