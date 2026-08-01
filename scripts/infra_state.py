#!/usr/bin/env python3
"""
==============================================================================
Sooniverse Infra - Estado de infraestructura (Fase 1 / Fase 2)
==============================================================================
Define el contrato `InfraStateStore` que `AwsNetworkManager` (scripts/aws_network.py)
usa para registrar qué recursos AWS creó, en qué despliegue y en qué orden deben
destruirse. Es el mecanismo de propiedad: el destroy solo borra lo que aparece
aquí Y cuyos tags AWS coinciden con el `deployment_id` registrado.

Dos implementaciones:
  - `InMemoryInfraStateStore`: sin persistencia, para pruebas unitarias (moto)
    y desarrollo local sin PostgreSQL.
  - `PostgresInfraStateStore`: persistente, transaccional, sobre las tablas
    `sooniverse.infra_deployment` / `infra_resource` / `infra_event`
    (database/002_infra_state.sql). Es la fuente de verdad en producción.
    Si PostgreSQL no es alcanzable, sus métodos fallan de inmediato -y como
    `AwsNetworkManager.__init__` llama a `open_deployment()` antes de crear
    cualquier recurso en AWS, el aprovisionamiento aborta antes de tocar AWS.

Además de PostgreSQL, `PostgresInfraStateStore` escribe un espejo local
`.sooniverse_state.<client>-<env>.json` tras cada cambio de estado (best-effort,
nunca lanza si falla). Sirve para recuperación manual si la BD se pierde a
media operación; la fuente de verdad sigue siendo PostgreSQL.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

logger = logging.getLogger("sooniverse.infra_state")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = REPO_ROOT / ".env"

# Claves que jamás deben aparecer en `config_snapshot` (filtrado recursivo por
# substring, insensible a mayúsculas: cubre DB_PASSWORD, LITELLM_MASTER_KEY,
# LITELLM_SALT_KEY, AWS_SECRET_ACCESS_KEY, AWS_ACCESS_KEY_ID, SECRET_KEY,
# DJANGO_SUPERUSER_PASSWORD y cualquier variante futura con estas palabras).
SECRET_KEY_MARKERS = ("password", "secret", "master_key", "salt_key", "access_key", "master-key")


class InfraStateStore(Protocol):
    """Contrato mínimo que `AwsNetworkManager` necesita de una capa de estado."""

    def open_deployment(
        self,
        client_id: str,
        environment: str,
        region: str,
        config_hash: Optional[str] = None,
        config_snapshot: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Abre (o recupera) el despliegue activo para (cliente, entorno, región) y
        devuelve su `deployment_id` (UUID v4 en string)."""
        ...

    def get_active_deployment(
        self, client_id: str, environment: str, region: str
    ) -> Optional[Dict[str, Any]]:
        """Devuelve el despliegue activo (status no en {destroyed, error}) o None."""
        ...

    def set_deployment_status(
        self, deployment_id: str, status: str, error: Optional[str] = None
    ) -> None:
        ...

    def record_resource(self, deployment_id: str, **fields: Any) -> None:
        """UPSERT de un recurso. Campos esperados: resource_type, component, aws_id,
        aws_arn, region, availability_zone, parent_aws_id, delete_order,
        managed_by_us, state, attributes."""
        ...

    def mark_resource_state(self, deployment_id: str, aws_id: str, state: str) -> None:
        ...

    def list_resources(self, deployment_id: str, only_active: bool = True) -> List[Dict[str, Any]]:
        ...

    def resources_in_delete_order(self, deployment_id: str) -> List[Dict[str, Any]]:
        """Recursos del despliegue ordenados por `delete_order` ascendente (el orden
        en que deben eliminarse)."""
        ...

    def log_event(
        self,
        deployment_id: str,
        phase: str,
        action: str,
        status: str,
        message: Optional[str] = None,
        duration_ms: Optional[int] = None,
    ) -> None:
        ...

    def close_deployment(self, deployment_id: str) -> None:
        ...


@dataclass
class _Deployment:
    deployment_id: str
    client_id: str
    environment: str
    region: str
    status: str = "planning"
    config_hash: Optional[str] = None
    config_snapshot: Optional[Dict[str, Any]] = None
    last_error: Optional[str] = None


@dataclass
class _Resource:
    resource_type: str
    component: str
    aws_id: Optional[str] = None
    aws_arn: Optional[str] = None
    region: Optional[str] = None
    availability_zone: Optional[str] = None
    parent_aws_id: Optional[str] = None
    delete_order: int = 0
    managed_by_us: bool = True
    state: str = "creating"
    attributes: Dict[str, Any] = field(default_factory=dict)


class InMemoryInfraStateStore:
    """Implementación en memoria de `InfraStateStore`. No persiste entre procesos.

    Solo para tests y desarrollo local sin PostgreSQL. Nunca usar en producción:
    si el proceso muere a media creación, este estado se pierde y el destroy no
    sabría qué limpiar (justo el escenario que la versión PostgreSQL de la Fase 2
    está diseñada para evitar).
    """

    def __init__(self) -> None:
        self._deployments: Dict[str, _Deployment] = {}
        self._resources: Dict[str, Dict[str, _Resource]] = {}
        self._events: List[Dict[str, Any]] = []

    def open_deployment(
        self,
        client_id: str,
        environment: str,
        region: str,
        config_hash: Optional[str] = None,
        config_snapshot: Optional[Dict[str, Any]] = None,
    ) -> str:
        existing = self.get_active_deployment(client_id, environment, region)
        if existing:
            return existing["deployment_id"]

        deployment_id = str(uuid.uuid4())
        self._deployments[deployment_id] = _Deployment(
            deployment_id=deployment_id,
            client_id=client_id,
            environment=environment,
            region=region,
            status="creating",
            config_hash=config_hash,
            config_snapshot=config_snapshot,
        )
        self._resources[deployment_id] = {}
        return deployment_id

    def get_active_deployment(
        self, client_id: str, environment: str, region: str
    ) -> Optional[Dict[str, Any]]:
        for dep in self._deployments.values():
            if (
                dep.client_id == client_id
                and dep.environment == environment
                and dep.region == region
                and dep.status not in ("destroyed", "error")
            ):
                return dep.__dict__.copy()
        return None

    def set_deployment_status(
        self, deployment_id: str, status: str, error: Optional[str] = None
    ) -> None:
        dep = self._deployments[deployment_id]
        dep.status = status
        if error is not None:
            dep.last_error = error

    def record_resource(self, deployment_id: str, **fields: Any) -> None:
        aws_id = fields.get("aws_id")
        key = aws_id or f"{fields.get('resource_type')}:{fields.get('component')}"
        bucket = self._resources.setdefault(deployment_id, {})
        current = bucket.get(key)
        if current is None:
            bucket[key] = _Resource(**{k: v for k, v in fields.items() if k in _Resource.__dataclass_fields__})
        else:
            for k, v in fields.items():
                if k in _Resource.__dataclass_fields__:
                    setattr(current, k, v)

    def mark_resource_state(self, deployment_id: str, aws_id: str, state: str) -> None:
        bucket = self._resources.get(deployment_id, {})
        if aws_id in bucket:
            bucket[aws_id].state = state

    def list_resources(self, deployment_id: str, only_active: bool = True) -> List[Dict[str, Any]]:
        bucket = self._resources.get(deployment_id, {})
        out = []
        for res in bucket.values():
            if only_active and res.state in ("deleted",):
                continue
            out.append(res.__dict__.copy())
        return out

    def resources_in_delete_order(self, deployment_id: str) -> List[Dict[str, Any]]:
        resources = self.list_resources(deployment_id, only_active=True)
        return sorted(resources, key=lambda r: r["delete_order"])

    def log_event(
        self,
        deployment_id: str,
        phase: str,
        action: str,
        status: str,
        message: Optional[str] = None,
        duration_ms: Optional[int] = None,
    ) -> None:
        self._events.append(
            {
                "deployment_id": deployment_id,
                "phase": phase,
                "action": action,
                "status": status,
                "message": message,
                "duration_ms": duration_ms,
            }
        )

    def close_deployment(self, deployment_id: str) -> None:
        self.set_deployment_status(deployment_id, "destroyed")


def _strip_secrets(value: Any) -> Any:
    """Elimina recursivamente cualquier clave cuyo nombre contenga un marcador
    de secreto (ver SECRET_KEY_MARKERS). Usado antes de guardar `config_snapshot`."""
    if isinstance(value, dict):
        cleaned = {}
        for k, v in value.items():
            if any(marker in k.lower() for marker in SECRET_KEY_MARKERS):
                continue
            cleaned[k] = _strip_secrets(v)
        return cleaned
    if isinstance(value, list):
        return [_strip_secrets(v) for v in value]
    return value


class PostgresInfraStateStore:
    """Implementación de `InfraStateStore` sobre PostgreSQL (esquema `sooniverse`,
    tablas creadas por database/002_infra_state.sql). Reutiliza la conexión de
    `scripts/db_setup.py` (mismo `.env`, mismo patrón `resolve_db_config`/`connect`).
    """

    def __init__(self, env_path: Optional[Path] = None, mirror_dir: Optional[Path] = None) -> None:
        self.env_path = Path(env_path) if env_path else DEFAULT_ENV_PATH
        self.mirror_dir = Path(mirror_dir) if mirror_dir else REPO_ROOT
        self._conn = None

    # -- conexión ---------------------------------------------------------
    def _connect(self):
        import sys

        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        from db_setup import connect, resolve_db_config  # type: ignore[import-not-found]

        if self._conn is None or self._conn.closed:
            config = resolve_db_config(self.env_path)
            self._conn = connect(config)
        return self._conn

    def ping(self) -> None:
        """Verifica que PostgreSQL es alcanzable. Debe llamarse (o llamarse
        implícitamente vía `open_deployment`) ANTES de crear cualquier recurso
        en AWS: si esto falla, el aprovisionamiento debe abortar sin tocar AWS."""
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute("SELECT 1")

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()

    # -- despliegues --------------------------------------------------------
    def open_deployment(
        self,
        client_id: str,
        environment: str,
        region: str,
        config_hash: Optional[str] = None,
        config_snapshot: Optional[Dict[str, Any]] = None,
    ) -> str:
        from psycopg2.extras import Json

        existing = self.get_active_deployment(client_id, environment, region)
        if existing:
            logger.info("[ESTADO] Despliegue activo existente reutilizado: %s", existing["deployment_id"])
            return existing["deployment_id"]

        deployment_id = str(uuid.uuid4())
        snapshot = _strip_secrets(config_snapshot) if config_snapshot is not None else None

        conn = self._connect()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO sooniverse.infra_deployment
                        (deployment_id, client_id, environment, region, status, config_hash, config_snapshot)
                    VALUES (%s, %s, %s, %s, 'creating', %s, %s)
                    """,
                    (deployment_id, client_id, environment, region, config_hash,
                     Json(snapshot) if snapshot is not None else None),
                )
                cur.execute(
                    """
                    INSERT INTO sooniverse.infra_event (deployment_id, phase, action, status, message)
                    VALUES (%s, 'network', 'open_deployment', 'ok', %s)
                    """,
                    (deployment_id, f"Despliegue abierto para {client_id}/{environment}/{region}"),
                )
        logger.info("[ESTADO] Despliegue creado: %s (%s/%s/%s)", deployment_id, client_id, environment, region)
        self._mirror(deployment_id)
        return deployment_id

    def get_active_deployment(
        self, client_id: str, environment: str, region: str
    ) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT deployment_id, client_id, environment, region, cloud, status,
                       managed_network, config_hash, created_at, updated_at, destroyed_at, last_error
                FROM sooniverse.infra_deployment
                WHERE client_id = %s AND environment = %s AND region = %s
                  AND status NOT IN ('destroyed', 'error')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (client_id, environment, region),
            )
            row = cur.fetchone()
            if row is None:
                return None
            cols = [d[0] for d in cur.description]
            result = dict(zip(cols, row))
            result["deployment_id"] = str(result["deployment_id"])
            return result

    def set_deployment_status(
        self, deployment_id: str, status: str, error: Optional[str] = None
    ) -> None:
        conn = self._connect()
        with conn:
            with conn.cursor() as cur:
                if status == "destroyed":
                    cur.execute(
                        """
                        UPDATE sooniverse.infra_deployment
                        SET status = %s, last_error = COALESCE(%s, last_error),
                            updated_at = now(), destroyed_at = now()
                        WHERE deployment_id = %s
                        """,
                        (status, error, deployment_id),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE sooniverse.infra_deployment
                        SET status = %s, last_error = COALESCE(%s, last_error), updated_at = now()
                        WHERE deployment_id = %s
                        """,
                        (status, error, deployment_id),
                    )
                cur.execute(
                    """
                    INSERT INTO sooniverse.infra_event (deployment_id, phase, action, status, message)
                    VALUES (%s, 'network', 'set_deployment_status', %s, %s)
                    """,
                    (deployment_id, "error" if status == "error" else "ok", f"status -> {status}"),
                )
        self._mirror(deployment_id)

    # -- recursos -------------------------------------------------------------
    def record_resource(self, deployment_id: str, **fields: Any) -> None:
        from psycopg2.extras import Json

        resource_type = fields["resource_type"]
        component = fields["component"]
        aws_id = fields.get("aws_id")
        aws_arn = fields.get("aws_arn")
        region = fields.get("region")
        availability_zone = fields.get("availability_zone")
        parent_aws_id = fields.get("parent_aws_id")
        delete_order = fields.get("delete_order", 999)
        managed_by_us = fields.get("managed_by_us", True)
        state = fields.get("state", "creating")
        attributes = fields.get("attributes", {})

        conn = self._connect()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO sooniverse.infra_resource
                        (deployment_id, resource_type, component, aws_id, aws_arn, region,
                         availability_zone, parent_aws_id, delete_order, managed_by_us, state, attributes)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (deployment_id, resource_type, aws_id) DO UPDATE SET
                        aws_arn = EXCLUDED.aws_arn,
                        availability_zone = COALESCE(EXCLUDED.availability_zone, sooniverse.infra_resource.availability_zone),
                        parent_aws_id = COALESCE(EXCLUDED.parent_aws_id, sooniverse.infra_resource.parent_aws_id),
                        managed_by_us = EXCLUDED.managed_by_us,
                        state = EXCLUDED.state,
                        attributes = sooniverse.infra_resource.attributes || EXCLUDED.attributes,
                        deleted_at = CASE WHEN EXCLUDED.state = 'deleted' THEN now()
                                          ELSE sooniverse.infra_resource.deleted_at END
                    """,
                    (deployment_id, resource_type, component, aws_id, aws_arn, region,
                     availability_zone, parent_aws_id, delete_order, managed_by_us, state, Json(attributes)),
                )
                cur.execute(
                    """
                    INSERT INTO sooniverse.infra_event (deployment_id, phase, action, resource_ref, status, message)
                    VALUES (%s, 'network', 'record_resource', %s, 'ok', %s)
                    """,
                    (deployment_id, aws_id, f"{component} -> {state}"),
                )
        self._mirror(deployment_id)

    def mark_resource_state(self, deployment_id: str, aws_id: str, state: str) -> None:
        conn = self._connect()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE sooniverse.infra_resource
                    SET state = %s,
                        deleted_at = CASE WHEN %s = 'deleted' THEN now() ELSE deleted_at END
                    WHERE deployment_id = %s AND aws_id = %s
                    """,
                    (state, state, deployment_id, aws_id),
                )
                cur.execute(
                    """
                    INSERT INTO sooniverse.infra_event (deployment_id, phase, action, resource_ref, status, message)
                    VALUES (%s, 'destroy', 'mark_resource_state', %s, 'ok', %s)
                    """,
                    (deployment_id, aws_id, f"-> {state}"),
                )
        self._mirror(deployment_id)

    def list_resources(self, deployment_id: str, only_active: bool = True) -> List[Dict[str, Any]]:
        conn = self._connect()
        query = """
            SELECT resource_type, component, aws_id, aws_arn, region, availability_zone,
                   parent_aws_id, delete_order, managed_by_us, state, attributes, created_at, deleted_at
            FROM sooniverse.infra_resource
            WHERE deployment_id = %s
        """
        if only_active:
            query += " AND state != 'deleted'"
        query += " ORDER BY delete_order ASC"

        with conn.cursor() as cur:
            cur.execute(query, (deployment_id,))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def resources_in_delete_order(self, deployment_id: str) -> List[Dict[str, Any]]:
        return self.list_resources(deployment_id, only_active=True)

    # -- auditoría / cierre ----------------------------------------------------
    def log_event(
        self,
        deployment_id: str,
        phase: str,
        action: str,
        status: str,
        message: Optional[str] = None,
        duration_ms: Optional[int] = None,
    ) -> None:
        conn = self._connect()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO sooniverse.infra_event (deployment_id, phase, action, status, message, duration_ms)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (deployment_id, phase, action, status, message, duration_ms),
                )

    def close_deployment(self, deployment_id: str) -> None:
        self.set_deployment_status(deployment_id, "destroyed")

    # -- espejo local (best-effort) ---------------------------------------------
    def _mirror(self, deployment_id: str) -> None:
        try:
            conn = self._connect()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT client_id, environment, region, status FROM sooniverse.infra_deployment "
                    "WHERE deployment_id = %s",
                    (deployment_id,),
                )
                row = cur.fetchone()
            if row is None:
                return
            client_id, environment, region, status = row

            payload = {
                "deployment_id": deployment_id,
                "client_id": client_id,
                "environment": environment,
                "region": region,
                "status": status,
                "resources": self.list_resources(deployment_id, only_active=False),
                "mirrored_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            mirror_path = self.mirror_dir / f".sooniverse_state.{client_id}-{environment}.json"
            mirror_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - el espejo es best-effort, nunca debe romper el flujo
            logger.warning("[ESTADO] No se pudo escribir el espejo local: %s", exc)
