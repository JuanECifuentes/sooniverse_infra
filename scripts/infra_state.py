#!/usr/bin/env python3
"""
==============================================================================
Sooniverse Infra - Interfaz de estado de infraestructura (Fase 1 / Fase 2)
==============================================================================
Define el contrato `InfraStateStore` que `AwsNetworkManager` (scripts/aws_network.py)
usa para registrar qué recursos AWS creó, en qué despliegue y en qué orden deben
destruirse. Es el mecanismo de propiedad: el destroy solo borra lo que aparece
aquí Y cuyos tags AWS coinciden con el `deployment_id` registrado.

Esta versión contiene únicamente una implementación EN MEMORIA (`InMemoryInfraStateStore`),
pensada para pruebas unitarias (con moto) y para desarrollo local sin PostgreSQL.
La implementación persistente en PostgreSQL (transaccional, con las tablas
`sooniverse.infra_deployment` / `infra_resource` / `infra_event`) es objeto de la
Fase 2 de este proyecto (ver PROMPT_CLAUDE_CODE_sooniverse_red.md, sección 4) y
reemplazará esta clase sin cambiar la interfaz `InfraStateStore`, de modo que
`aws_network.py` no requiere cambios cuando eso ocurra.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol


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
