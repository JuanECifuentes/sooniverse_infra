"""
==============================================================================
IAM dedicado a apagar/arrancar workers desde el panel (NUNCA el despliegue)
==============================================================================
El panel (django_metrics/metrics/workers.py::apagar_worker/arrancar_worker)
necesita credenciales AWS con `ec2:StartInstances`/`StopInstances` sobre las
instancias de workers de ESTE cliente/entorno. Sin esto, la única forma de
que esos botones funcionaran era pegar en `.env` las credenciales del propio
operador de despliegue -normalmente con permisos amplios (VPC, IAM, EC2 sin
restricción)- exactamente lo que el contrato de este despliegue prohíbe:
la cuenta que corre `generate_infra.py --run` NUNCA debe ser la misma que
queda embebida en el Gateway, ejecutándose sin supervisión indefinidamente.

Este módulo crea (una sola vez, idempotente) un usuario IAM SEPARADO con el
permiso mínimo posible -start/stop sobre instancias EC2 con las tags exactas
de este cliente/entorno, más `DescribeInstances` (no soporta scoping por
recurso, ver la documentación de IAM para EC2)- y emite un access key para
él. Ese usuario JAMÁS se usa para desplegar: lo crean las credenciales del
despliegue (que sí necesitan permiso IAM para esto), pero el access key que
genera viaja al `.env` del Gateway para que `workers.py` lo use en boto3.

Best-effort en TODO: si las credenciales del despliegue no tienen permiso IAM
(`iam:CreateUser`/`PutUserPolicy`/`CreateAccessKey`), se devuelve `None` y el
despliegue sigue -exactamente igual que 'dominio'/'capacidad'. Sin este
usuario, `workers.py::ec2_disponible()` sigue devolviendo `False` (fail-closed)
y el panel oculta/deshabilita los botones Apagar/Arrancar en vez de fallar.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

WORKER_CONTROL_POLICY_NAME = "sooniverse-worker-control"
# Límite duro de AWS: máximo 2 access keys activos por usuario IAM.
MAX_ACCESS_KEYS = 2


def worker_control_username(cliente_id: str, entorno: str) -> str:
    """Nombre determinista -permite localizar el usuario sin necesitar tags
    (IAM no ofrece un equivalente a 'describe-instances --filters' cómodo
    para listar usuarios por tag)."""
    return f"sooniverse-{cliente_id}-{entorno}-worker-ctrl"[:64]


def _policy_document(region: str, account_id: str, cliente_id: str, entorno: str) -> Dict[str, Any]:
    """Las condiciones de tag deben coincidir con lo que SkyPilot escribe DE
    VERDAD en la instancia EC2 del worker -no con el prefijo 'sooniverse:'
    que usa `aws_network.py` para VPC/SG/NAT/EIP, que es un espacio de tags
    DISTINTO. `TopologyBuilder.build_worker()` (generate_infra.py) le pasa a
    SkyPilot `labels: {**tags_obligatorios, rol: 'worker', workload: id}`, y
    SkyPilot traduce ese `labels:` en tags EC2 SIN prefijo: 'cliente_id',
    'entorno', 'rol', 'workload' -confirmado contra el código de
    generate_infra.py, no una suposición. Usar las claves equivocadas aquí
    dejaría la política sin efecto (0 instancias matchean la condición) sin
    ningún error visible: el start/stop simplemente fallaría con
    'UnauthorizedOperation' para TODOS los workers."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "DescribeInstancesNoSoportaScopingPorRecurso",
                "Effect": "Allow",
                "Action": "ec2:DescribeInstances",
                "Resource": "*",
            },
            {
                "Sid": "StartStopSoloWorkersDeEsteClienteEntorno",
                "Effect": "Allow",
                "Action": ["ec2:StartInstances", "ec2:StopInstances"],
                "Resource": f"arn:aws:ec2:{region}:{account_id}:instance/*",
                "Condition": {
                    "StringEquals": {
                        "aws:ResourceTag/cliente_id": cliente_id,
                        "aws:ResourceTag/entorno": entorno,
                        "aws:ResourceTag/rol": "worker",
                    }
                },
            },
        ],
    }


def ensure_worker_control_user(
    session: Any,
    region: str,
    cliente_id: str,
    entorno: str,
    deployment_id: Optional[str] = None,
    existing_access_key_id: Optional[str] = None,
) -> Optional[Dict[str, str]]:
    """Devuelve `{"AWS_ACCESS_KEY_ID": ..., "AWS_SECRET_ACCESS_KEY": ...}` SOLO
    si emitió un access key NUEVO (primera vez, o `existing_access_key_id` ya
    no es válido) -si el existente sigue sirviendo, devuelve `None` para que
    quien llama no reescriba `.env` sin necesidad. También `None` si el
    aprovisionamiento falla por cualquier motivo (best-effort, nunca lanza)."""
    try:
        from botocore.exceptions import ClientError
    except ImportError:
        print("[IAM] boto3/botocore no disponible; se omite el usuario de control de workers.")
        return None

    try:
        iam = session.client("iam")
        sts = session.client("sts")
        account_id = sts.get_caller_identity()["Account"]
    except Exception as exc:  # noqa: BLE001 - best-effort desde el arranque
        print(f"[IAM] No se pudo inicializar IAM/STS ({exc}); se omite el usuario de control de workers.")
        return None

    username = worker_control_username(cliente_id, entorno)

    try:
        try:
            iam.get_user(UserName=username)
            print(f"[IAM] Usuario '{username}' ya existe -reutilizando.")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "NoSuchEntity":
                raise
            tags = [
                {"Key": "sooniverse:managed", "Value": "true"},
                {"Key": "sooniverse:client-id", "Value": cliente_id},
                {"Key": "sooniverse:environment", "Value": entorno},
                {"Key": "sooniverse:component", "Value": "worker-control"},
                {"Key": "gestionado_por", "Value": "sooniverse"},
            ]
            if deployment_id:
                tags.append({"Key": "sooniverse:deployment-id", "Value": deployment_id})
            iam.create_user(UserName=username, Tags=tags)
            print(f"[IAM] Usuario '{username}' creado (solo start/stop/describe de sus workers).")

        # Idempotente y barato: reafirma la política en cada corrida, por si
        # el cliente_id/entorno/región cambiaron o la política todavía no
        # existía (usuario creado por una versión anterior de este script).
        iam.put_user_policy(
            UserName=username,
            PolicyName=WORKER_CONTROL_POLICY_NAME,
            PolicyDocument=json.dumps(_policy_document(region, account_id, cliente_id, entorno)),
        )

        if existing_access_key_id:
            try:
                iam.get_access_key_last_used(AccessKeyId=existing_access_key_id)
                print(
                    f"[IAM] Access key existente ({existing_access_key_id[:8]}...) sigue activa -se conserva."
                )
                return None
            except ClientError:
                pass  # la key en .env ya no existe (o es de otro usuario) -se emite una nueva abajo

        existentes = iam.list_access_keys(UserName=username).get("AccessKeyMetadata", [])
        if len(existentes) >= MAX_ACCESS_KEYS:
            for key in existentes:
                iam.delete_access_key(UserName=username, AccessKeyId=key["AccessKeyId"])
            print(f"[IAM] Access keys huérfanas de '{username}' eliminadas antes de emitir una nueva.")

        nueva = iam.create_access_key(UserName=username)["AccessKey"]
        print(f"[IAM] Access key nueva emitida para '{username}' (AWS_ACCESS_KEY_ID={nueva['AccessKeyId'][:8]}...).")
        return {
            "AWS_ACCESS_KEY_ID": nueva["AccessKeyId"],
            "AWS_SECRET_ACCESS_KEY": nueva["SecretAccessKey"],
        }
    except ClientError as exc:
        print(
            "[WARNING] No se pudo aprovisionar el usuario IAM de apagar/arrancar workers "
            f"(¿faltan permisos IAM en las credenciales del despliegue?): {exc}. "
            "El botón 'Apagar'/'Arrancar' del panel quedará deshabilitado hasta resolverlo."
        )
        return None


def delete_worker_control_user(session: Any, cliente_id: str, entorno: str) -> bool:
    """Best-effort: borra el usuario de control de workers (access keys +
    política inline + el usuario) al destruir el despliegue. Nunca lanza -si
    falla, el usuario queda huérfano en la cuenta y se puede limpiar a mano
    desde la consola IAM sin que eso bloquee `destroy_infra.py`."""
    try:
        from botocore.exceptions import ClientError
    except ImportError:
        return False

    username = worker_control_username(cliente_id, entorno)
    try:
        iam = session.client("iam")
        try:
            iam.get_user(UserName=username)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "NoSuchEntity":
                return True  # ya no existe -nada que borrar
            raise

        for key in iam.list_access_keys(UserName=username).get("AccessKeyMetadata", []):
            iam.delete_access_key(UserName=username, AccessKeyId=key["AccessKeyId"])
        try:
            iam.delete_user_policy(UserName=username, PolicyName=WORKER_CONTROL_POLICY_NAME)
        except ClientError:
            pass
        iam.delete_user(UserName=username)
        print(f"[IAM] Usuario '{username}' (control de workers) eliminado.")
        return True
    except Exception as exc:  # noqa: BLE001 - nunca debe tumbar destroy_infra.py
        print(f"[WARNING] No se pudo eliminar el usuario IAM '{username}': {exc}")
        return False
