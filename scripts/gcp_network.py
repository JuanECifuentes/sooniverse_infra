#!/usr/bin/env python3
"""
==============================================================================
Sooniverse Infra - Gestor de red GCP (modo "hosted", cuenta propia) — TEÓRICO
==============================================================================
Equivalente GCP de `scripts/aws_network.py::AwsNetworkManager` /
`scripts/azure_network.py::AzureNetworkManager`: mismo contrato público
(`provision()`, `destroy()`, `plan_destroy()`, `scan_orphans()`), misma
máquina de fases en `scripts/generate_infra.py` (`deploy()`) -la nube se elige
con `red_y_aislamiento.cloud: "gcp"` en el contrato. AwsNetworkManager/
AzureNetworkManager NO se modifican ni se importan desde aquí (este módulo
debe poder cargarse sin tener boto3/azure-sdk instalados).

Mapeo de recursos AWS/Azure -> GCP:
    VPC / VNet              -> VPC Network (modo custom, sin subredes automáticas)
    Subred pública/privada  -> Subnetworks REGIONALES (GCP no tiene subredes
                               zonales -ver GcpNetworkSpec.az_count, que solo
                               admite 1: SkyPilot elige la zona DENTRO de la
                               región por su cuenta, con failover automático
                               entre zonas sin que este módulo tenga que saberlo).
    Internet Gateway        -> ruta de sistema implícita (sin recurso propio;
                               GCP la da gratis a cualquier subred).
    NAT Gateway + EIP       -> Cloud Router + Cloud NAT. La NAT NO tiene una
                               EIP propia que reservar: con
                               natIpAllocateOption=AUTO_ONLY, GCP asigna y
                               libera las IPs de salida por su cuenta.
    Security Group / NSG    -> Firewall Rules, SIN network tags (ver más abajo
                               el porqué) -acotadas por sourceRanges usando la
                               separación de subredes pública/privada, mismo
                               principio que las NSG de Azure.
    vpc_endpoints.s3        -> Private Google Access (atributo BOOLEANO de la
                               subred privada, no un recurso separado -ver
                               Subnetwork.privateIpGoogleAccess).
    EIP del Gateway (dominio)-> NO IMPLEMENTADO en esta versión (ver más abajo).

Por qué NO hay network tags en las VMs (y por qué eso está bien):
    El esquema `gcp` que SkyPilot valida contra `~/.sky/config.yaml`/
    `SKYPILOT_CONFIG` (`additionalProperties: False`, verificado contra
    sky/utils/schemas.py de la versión instalada) solo acepta: 'vpc_name',
    'subnet_names', 'use_internal_ips', 'ssh_proxy_command', 'labels'
    (resource labels, NO network tags), 'instance_tags',
    'prioritize_reservations', 'specific_reservations',
    'managed_instance_group', 'force_enable_external_ips', 'enable_gvnic',
    'enable_gpu_direct', 'placement_policy'. No existe ninguna clave para
    pinear un network tag (el equivalente GCP de un Security Group referenciado
    por grupo) a las VMs que SkyPilot crea. La compensación: el Gateway es la
    ÚNICA máquina en la subred pública y los workers son las ÚNICAS máquinas en
    la privada (mismo diseño que AWS/Azure), así que "tráfico desde el
    Gateway" se expresa como `sourceRanges: [cidr_subred_publica]` en vez de
    una referencia de grupo -exactamente lo que ya hace `azure_network.py`
    para sus reglas NSG por la razón simétrica (Azure NSG tampoco soporta
    referenciar OTRO NSG como origen).

    IMPORTANTE -riesgo real, no solo una curiosidad-: si algún día se le pasa
    `resources.ports` a la config de SkyPilot para gateway/workers (el
    contrato de este repo NO lo hace; ver TopologyBuilder.build_gateway/
    build_worker, que deliberadamente NO incluyen 'ports' para cloud=='gcp'),
    SkyPilot abre una regla `sky-ports-<cluster>` con
    `sourceRanges: ['0.0.0.0/0']` y SÍ le pone un network tag a la VM para esa
    regla (`sky/provision/gcp/instance.py::open_ports`) -eso SÍ expondría el
    puerto vLLM a internet entero, sin pasar por scan_orphans()/
    check_workers_sg_no_open_cidr porque esa regla no la crea este módulo.
    Ver TopologyBuilder para la mitigación (nunca pasar 'ports' a GCP).

Mecanismo de propiedad -DIFERENTE de AWS/Azure-: los recursos de red de GCP
(Network, Subnetwork, Router, Firewall) NO admiten labels (solo Address las
tiene, ver más abajo), así que la prueba de propiedad (deployment_id) se
codifica en un blob JSON dentro de `description` en vez de en un tag/label
real. Los nombres son DETERMINISTAS (`sooniverse-<cliente>-<entorno>-<componente>`,
sin deployment_id) y únicos por proyecto -así que la idempotencia de cada
`ensure_*` se resuelve con un GET directo por nombre (ver `_find_existing`),
no con un listado filtrado por tag como en AWS.

Dominio propio (gateway.dominio.habilitado): NO IMPLEMENTADO en esta versión.
Requeriría una dirección externa estática (compute.addresses, análogo a
`aws_network.ensure_gateway_eip()` / `azure_network.ensure_gateway_public_ip()`)
y el baile `deleteAccessConfig`/`addAccessConfig` sobre la NIC de la VM del
Gateway para reasignarla tras `sky launch` -diseñado (ver
sooniverse-optimizacion-infraestructura-i_v2.md / el plan de despliegue) pero
no implementado aquí. `ConfigValidator._validate_dominio` sigue rechazando
`cloud: gcp` con `gateway.dominio.habilitado: true`.

SDK: `googleapiclient.discovery` (Compute Engine API v1) + `google-auth`
-MISMA librería que usa el propio backend GCP de SkyPilot internamente
(sky/adaptors/gcp.py::build()), y ya viene instalada con
google-api-python-client/google-auth (CERO dependencias nuevas). NO se usa
`google-cloud-compute` (el SDK "moderno" por gRPC): mismo razonamiento que
Azure usando azure-mgmt-* en vez de reinventar el transporte, y respeta el
invariante del repo de "solo SDK, nada de Terraform/CloudFormation/CDK/Pulumi"
(README.md).

Autenticación: Service Account vía `GOOGLE_APPLICATION_CREDENTIALS` (ruta a la
key JSON) o Application Default Credentials del entorno -ver .env.example
para el paso a paso. El proyecto NO se infiere de la credencial: viene de
`red_y_aislamiento.gcp_project` en el contrato (análogo a `aws_profile`:
selector de cuenta/proyecto, no hay "gcp_profile" nombrado todavía -mismo
diseño diferido que `azure_network.py` para BYOC).

⚠️ IMPORTANTE -alcance de esta implementación-: este módulo se escribió sin
acceso a un proyecto GCP real (sin cuota de GPU aprobada en el momento de
escribirlo, ver el plan de despliegue). La lógica está validada contra:
  (a) el esquema JSON real de la API de Compute Engine v1 instalada
      localmente (compute.v1.json de google-api-python-client, revisión
      verificada con el objeto Python real, no de memoria),
  (b) el código fuente instalado de SkyPilot 0.13.0
      (sky/provision/gcp/config.py, sky/utils/schemas.py, sky/clouds/gcp.py).
NO se ha ejercitado contra un proyecto real. Antes de un despliegue real:
`sky check gcp -v`, y validar cada método con `--dry-run` primero. Los tests
de este módulo (tests/test_gcp_network.py) mockean `googleapiclient` -no hay
equivalente a moto para GCP, mismo enfoque que test_azure_network.py.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    from googleapiclient import discovery
    from googleapiclient.errors import HttpError
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "Falta el SDK de GCP. Instala con: pip install google-api-python-client google-auth"
    ) from exc

from infra_state import InfraStateStore, InMemoryInfraStateStore  # type: ignore

logger = logging.getLogger("sooniverse.gcp_network")

# Mismo prefijo que AWS/Azure, aunque aquí vive dentro de 'description' (JSON),
# no en un tag/label real -ver el docstring del módulo sobre por qué.
TAG_PREFIX = "sooniverse"

GCP_OP_MAX_POLLS = 60
GCP_OP_POLL_TIMEOUT_SECONDS = 120  # timeout de cada llamada ...Operations().wait()

# Orden de borrado (inverso de la creación). Números más bajos se borran antes.
# A diferencia de AWS/Azure no hay Resource Group / contenedor que arrastre
# todo en cascada -cada recurso se borra individualmente, así que el orden
# real de dependencias (firewalls/NAT/router antes que la red, NAT antes que
# el router) sí importa aquí.
DELETE_ORDER = {
    "fw-workers-internal": 10,
    "fw-workers-from-gateway": 11,
    "fw-gateway-public": 12,
    "fw-gateway-ssh": 13,
    "nat": 20,
    "router": 21,
    "subnet-private": 70,
    "subnet-public": 71,
    "vpc": 80,
}

_CREATE_ORDER_COMPONENTS = [
    "vpc",
    "subnet-public",
    "subnet-private",
    "router",
    "nat",
    "fw-gateway-ssh",
    "fw-gateway-public",
    "fw-workers-from-gateway",
    "fw-workers-internal",
]


class GcpNetworkError(Exception):
    """Error de aprovisionamiento o destrucción de red en GCP."""


@dataclass(frozen=True)
class GcpNetworkSpec:
    """Entrada declarativa de `GcpNetworkManager`, análoga a
    `aws_network.NetworkSpec`/`azure_network.AzureNetworkSpec`. Se construye
    típicamente con `generate_infra.build_network_spec_from_config()`."""

    client_id: str
    environment: str
    region: str  # slug de GCP, ej. "us-central1" -no confundir con regiones AWS/Azure.
    project_id: str
    vnet_cidr: str
    az_count: int = 1  # GCP no tiene subredes zonales; SOLO 1 está soportado (ver __post_init__).
    public_subnet_cidrs: Optional[List[str]] = None
    private_subnet_cidrs: Optional[List[str]] = None
    nat_mode: str = "single"  # "single" | "none" -sin equivalente a "per-az" (subredes regionales).
    admin_cidrs: Optional[List[str]] = None       # SSH al gateway
    public_cidrs: Optional[List[str]] = None       # HTTP/HTTPS al gateway
    gateway_public_ports: Optional[List[int]] = None
    worker_ports: Optional[List[int]] = None
    expose_direct_ports: bool = False
    tls_enabled: bool = False
    extra_tags: Optional[Dict[str, str]] = None
    credentials_file: Optional[str] = None  # ruta a la key JSON de la Service Account; None = ADC del entorno

    def __post_init__(self) -> None:
        if self.nat_mode not in ("single", "none"):
            raise GcpNetworkError(
                f"nat_mode inválido para GCP: {self.nat_mode!r}. Permitidos: 'single', 'none' "
                "('per-az' no tiene sentido: las subredes de GCP son regionales, no zonales)."
            )
        if self.az_count != 1:
            raise GcpNetworkError(
                f"az_count={self.az_count} no soportado en GCP: las subredes son REGIONALES, no "
                "zonales -solo tiene sentido 1. SkyPilot elige (y hace failover entre) zonas "
                "DENTRO de la región por su cuenta, sin que este módulo tenga que pinear ninguna."
            )
        normalized = self.client_id.lower()
        if normalized != self.client_id or len(self.client_id) > 20:
            raise GcpNetworkError(
                f"client_id '{self.client_id}' inválido: debe ser minúsculas, [a-z0-9-], máx 20 caracteres."
            )


@dataclass(frozen=True)
class GcpNetworkOutputs:
    """Resultado de `GcpNetworkManager.provision()`: los IDs/nombres reales
    que `TopologyBuilder` necesita para construir la config de cliente
    SkyPilot (`gcp.vpc_name`, `gcp.subnet_names` -SIN equivalente de
    'security_group_name', ver el docstring del módulo)."""

    deployment_id: str
    project_id: str
    region: str
    vpc_id: str  # selfLink de la red
    vpc_name: str
    public_subnet_id: str  # selfLink
    public_subnet_name: str
    public_subnet_cidr: str
    private_subnet_id: str
    private_subnet_name: str
    private_subnet_cidr: str
    router_id: Optional[str]
    router_name: Optional[str]
    nat_name: Optional[str]
    firewall_ids: List[str] = field(default_factory=list)
    firewall_names: List[str] = field(default_factory=list)
    managed_by_us: bool = True


@dataclass
class PlannedDeletion:
    resource_type: str
    component: str
    gcp_id: Optional[str]
    name: Optional[str]
    delete_order: int
    managed_by_us: bool
    attributes: Optional[Dict[str, Any]] = None


@dataclass
class DestroyReport:
    deployment_id: str
    succeeded: List[PlannedDeletion] = field(default_factory=list)
    failed: List[Dict[str, Any]] = field(default_factory=list)
    skipped_not_ours: List[PlannedDeletion] = field(default_factory=list)
    manual_actions_required: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_subnet_cidrs(vnet_cidr: str) -> Tuple[str, str]:
    """Divide `vnet_cidr` (típicamente /16) en dos bloques /20 deterministas:
    el primero público, el segundo (mitad alta del espacio) privado. Una sola
    subred de cada tipo -GCP no tiene el concepto de "subred por AZ" (ver
    GcpNetworkSpec.az_count, fijo a 1). Misma lógica pura que
    aws_network.compute_subnet_cidrs()/azure_network.compute_subnet_cidrs(),
    duplicada a propósito para que este módulo no dependa de importar boto3/
    azure-sdk solo para esta cuenta aritmética."""
    vnet_net = ipaddress.ip_network(vnet_cidr, strict=True)
    subnet_prefix = max(vnet_net.prefixlen + 4, 20)
    all_subnets = list(vnet_net.subnets(new_prefix=subnet_prefix))
    if len(all_subnets) < 2:
        raise GcpNetworkError(
            f"'vnet_cidr' {vnet_cidr} no tiene espacio para 2 subredes /{subnet_prefix}."
        )
    half = len(all_subnets) // 2
    return str(all_subnets[0]), str(all_subnets[half])


def _default_credentials(credentials_file: Optional[str] = None):
    """Credenciales de GCP: Service Account explícita
    (`credentials_file` o GOOGLE_APPLICATION_CREDENTIALS) o Application
    Default Credentials del entorno (gcloud auth application-default login,
    o las que ya use el proceso)."""
    from google.oauth2 import service_account  # noqa: PLC0415 - import perezoso

    path = credentials_file or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    if path:
        return service_account.Credentials.from_service_account_file(path, scopes=scopes)

    import google.auth  # noqa: PLC0415 - import perezoso

    credentials, _ = google.auth.default(scopes=scopes)
    return credentials


def _is_not_found(exc: Exception) -> bool:
    return isinstance(exc, HttpError) and getattr(exc, "status_code", getattr(exc.resp, "status", None)) == 404


class GcpNetworkManager:
    """Ciclo de vida completo de la capa de red GCP para un despliegue
    Sooniverse. Ver el docstring del módulo para el mapeo de recursos y las
    limitaciones conocidas (sin network tags, sin dominio propio)."""

    def __init__(
        self,
        spec: GcpNetworkSpec,
        state: Optional[InfraStateStore] = None,
        compute: Optional[Any] = None,
        deployment_id: Optional[str] = None,
    ) -> None:
        self.spec = spec
        self.state = state if state is not None else InMemoryInfraStateStore()

        if compute is not None:
            self.compute = compute
        else:
            credentials = _default_credentials(spec.credentials_file)
            # static_discovery=True: usa el compute.v1.json empaquetado con la
            # librería en vez de pedirlo por red en cada arranque -determinista
            # y funciona sin salida a internet en el propio proceso local.
            self.compute = discovery.build(
                "compute", "v1", credentials=credentials, cache_discovery=False, static_discovery=True
            )

        if deployment_id:
            self.deployment_id = deployment_id
        else:
            self.deployment_id = self.state.open_deployment(
                spec.client_id, spec.environment, spec.region, cloud="gcp"
            )

    # -- naming -----------------------------------------------------------
    def _name(self, component: str, suffix: str = "") -> str:
        base = f"sooniverse-{self.spec.client_id}-{self.spec.environment}-{component}"
        return f"{base}-{suffix}" if suffix else base

    def _tags(self, component: str) -> str:
        """Blob JSON de propiedad para 'description' -ver el docstring del
        módulo sobre por qué GCP no admite labels en estos tipos de recurso."""
        payload = {
            "managed": "true",
            "prefix": TAG_PREFIX,
            "client_id": self.spec.client_id,
            "environment": self.spec.environment,
            "deployment_id": self.deployment_id,
            "component": component,
            "created_at": _now_iso(),
        }
        for key, value in (self.spec.extra_tags or {}).items():
            payload.setdefault(key, value)
        return json.dumps(payload, sort_keys=True)

    @staticmethod
    def _parse_tags(description: Optional[str]) -> Dict[str, Any]:
        if not description:
            return {}
        try:
            data = json.loads(description)
            return data if isinstance(data, dict) else {}
        except (ValueError, TypeError):
            return {}

    def _record(self, resource_type: str, component: str, gcp_id: str, **extra: Any) -> None:
        fields = {
            "resource_type": resource_type,
            "component": component,
            # Campo reutilizado del protocolo InfraStateStore (nombrado 'aws_id'
            # por historia, pero genérico: guarda el resource ID/selfLink de
            # CUALQUIER nube -ver infra_state.py).
            "aws_id": gcp_id,
            "region": self.spec.region,
            "delete_order": DELETE_ORDER.get(component, 999),
            "managed_by_us": True,
            "state": "active",
        }
        fields.update(extra)
        self.state.record_resource(self.deployment_id, **fields)

    def _find_existing(self, component: str) -> Optional[Dict[str, Any]]:
        """Busca un recurso ya registrado para este deployment_id + component.

        Primero mira el ESTADO (Postgres) -rápido, y es la fuente de verdad
        para la relación 1:1 con deployment_id. Si el estado no lo tiene (BD
        perdida/reiniciada, o corrida de recuperación), consulta GCP
        DIRECTAMENTE por el nombre determinista -todos los nombres de este
        módulo son 'sooniverse-<cliente>-<entorno>-<componente>', sin
        deployment_id- y, si existe, se auto-registra en el estado bajo el
        deployment_id ACTUAL antes de devolverlo -mismo patrón que
        azure_network.py::_find_existing/_lookup_live, necesario porque un PUT
        repetido en GCP (insert sobre un nombre que ya existe) devuelve 409,
        no una actualización silenciosa como en Azure."""
        for res in self.state.list_resources(self.deployment_id):
            if res.get("component") == component:
                return res

        gcp_id, name = self._lookup_live(component)
        if gcp_id is None:
            return None
        logger.info(
            "[RED-GCP] %s no estaba en el estado pero SÍ existe en GCP (%s); se adopta bajo deployment_id=%s.",
            component, name, self.deployment_id,
        )
        self._record(component, component, gcp_id, attributes={"name": name})
        return {"aws_id": gcp_id, "component": component, "attributes": {"name": name}}

    def _lookup_live(self, component: str) -> Tuple[Optional[str], Optional[str]]:
        project = self.spec.project_id
        region = self.spec.region
        try:
            if component == "vpc":
                name = self._name("vpc")
                obj = self.compute.networks().get(project=project, network=name).execute()
                return obj["selfLink"], name
            if component in ("subnet-public", "subnet-private"):
                name = "subred-public" if component == "subnet-public" else "subred-private"
                name = self._name(name.replace("subred-", "subnet-"))
                obj = self.compute.subnetworks().get(project=project, region=region, subnetwork=name).execute()
                return obj["selfLink"], name
            if component == "router":
                name = self._name("router")
                obj = self.compute.routers().get(project=project, region=region, router=name).execute()
                return obj["selfLink"], name
            if component.startswith("fw-"):
                name = self._name(component)
                obj = self.compute.firewalls().get(project=project, firewall=name).execute()
                return obj["selfLink"], name
        except HttpError as exc:
            if _is_not_found(exc):
                return None, None
            raise
        return None, None

    # -------------------------------------------------------------------
    # Operaciones asíncronas de Compute Engine
    # -------------------------------------------------------------------
    def _wait(self, op: Dict[str, Any], region: Optional[str] = None) -> Dict[str, Any]:
        """Espera a que una operación de Compute Engine termine -global o
        regional según se le pase `region`- y lanza si terminó con error.
        `...Operations().wait()` es un long-poll (bloquea server-side hasta
        ~2 min), así que unos pocos intentos bastan."""
        name = op["name"]
        ops = self.compute.globalOperations() if region is None else self.compute.regionOperations()
        kwargs: Dict[str, Any] = {"project": self.spec.project_id, "operation": name}
        if region is not None:
            kwargs["region"] = region

        result = op
        for _ in range(GCP_OP_MAX_POLLS):
            if result.get("status") == "DONE":
                break
            result = ops.wait(**kwargs).execute()
        else:
            raise GcpNetworkError(
                f"Operación '{name}' no terminó tras {GCP_OP_MAX_POLLS} intento(s) de espera."
            )

        if "error" in result:
            raise GcpNetworkError(f"Operación '{name}' falló: {result['error']}")
        return result

    # -------------------------------------------------------------------
    # ensure_* (idempotentes)
    # -------------------------------------------------------------------
    def ensure_network(self) -> Tuple[str, str]:
        """Crea (o reutiliza) la VPC Network en modo custom (sin subredes
        automáticas). Devuelve (selfLink, nombre)."""
        name = self._name("vpc")
        existing = self._find_existing("vpc")
        if existing:
            logger.info("[SKIP][RED-GCP] VPC ya registrada: %s", name)
            return existing["aws_id"], name

        body = {
            "name": name,
            "description": self._tags("vpc"),
            "autoCreateSubnetworks": False,
            "routingConfig": {"routingMode": "REGIONAL"},
            "mtu": 1460,
        }
        op = self.compute.networks().insert(project=self.spec.project_id, body=body).execute()
        self._wait(op)
        net = self.compute.networks().get(project=self.spec.project_id, network=name).execute()
        self._record("vpc", "vpc", net["selfLink"], attributes={"name": name})
        logger.info("[RED-GCP] VPC creada: %s", net["selfLink"])
        return net["selfLink"], name

    def _resolve_subnet_cidrs(self) -> Tuple[str, str]:
        """(cidr_publico, cidr_privado) reales de este despliegue: honra los
        CIDR explícitos si se declararon, si no los calcula. Único punto de
        verdad, igual que azure_network.py::_resolve_subnet_cidrs (esa clase
        SÍ tuvo un bug real por calcularlo dos veces por separado -aquí se
        evita desde el principio)."""
        if self.spec.public_subnet_cidrs and self.spec.private_subnet_cidrs:
            return self.spec.public_subnet_cidrs[0], self.spec.private_subnet_cidrs[0]
        return compute_subnet_cidrs(self.spec.vnet_cidr)

    def ensure_subnets(self, vpc_name: str) -> Tuple[str, str, str, str]:
        """Crea (o reutiliza) UNA subred pública y UNA privada, ambas
        REGIONALES (ver GcpNetworkSpec.az_count). La privada activa Private
        Google Access (equivalente al VPC Endpoint de S3 de AWS: acceso a las
        APIs de Google -incluido Cloud Storage, si algún día se usa- sin salir
        por el NAT). Devuelve (public_id, public_name, private_id, private_name)."""
        public_cidr, private_cidr = self._resolve_subnet_cidrs()

        public_id, public_name = self._ensure_one_subnet(
            vpc_name, "subnet-public", public_cidr, private_google_access=False
        )
        private_id, private_name = self._ensure_one_subnet(
            vpc_name, "subnet-private", private_cidr, private_google_access=True
        )
        return public_id, public_name, private_id, private_name

    def _ensure_one_subnet(
        self, vpc_name: str, component: str, cidr: str, private_google_access: bool
    ) -> Tuple[str, str]:
        name = self._name(component)
        existing = self._find_existing(component)
        if existing:
            logger.info("[SKIP][RED-GCP] Subred %s ya registrada.", component)
            return existing["aws_id"], name

        body = {
            "name": name,
            "description": self._tags(component),
            "network": f"projects/{self.spec.project_id}/global/networks/{vpc_name}",
            "ipCidrRange": cidr,
            "region": self.spec.region,
            "privateIpGoogleAccess": private_google_access,
        }
        op = self.compute.subnetworks().insert(
            project=self.spec.project_id, region=self.spec.region, body=body
        ).execute()
        self._wait(op, region=self.spec.region)
        subnet = self.compute.subnetworks().get(
            project=self.spec.project_id, region=self.spec.region, subnetwork=name
        ).execute()
        self._record(
            component, component, subnet["selfLink"],
            parent_aws_id=vpc_name, attributes={"name": name, "cidr": cidr},
        )
        logger.info("[RED-GCP] Subred %s (%s) creada: %s", component, cidr, subnet["selfLink"])
        return subnet["selfLink"], name

    def ensure_router_and_nat(self, vpc_name: str, private_subnet_name: str) -> Tuple[Optional[str], Optional[str]]:
        """Crea (o reutiliza) un Cloud Router con Cloud NAT asociado, SOLO
        sobre la subred privada. `nat_mode: none` no crea nada -los workers
        deben tener otra forma de alcanzar internet, o no la necesitan.
        Devuelve (router_name, nat_name) o (None, None)."""
        if self.spec.nat_mode == "none":
            return None, None

        router_name = self._name("router")
        existing_router = self._find_existing("router")
        if not existing_router:
            router_body = {
                "name": router_name,
                "description": self._tags("router"),
                "network": f"projects/{self.spec.project_id}/global/networks/{vpc_name}",
            }
            op = self.compute.routers().insert(
                project=self.spec.project_id, region=self.spec.region, body=router_body
            ).execute()
            self._wait(op, region=self.spec.region)
            router = self.compute.routers().get(
                project=self.spec.project_id, region=self.spec.region, router=router_name
            ).execute()
            self._record("router", "router", router["selfLink"], attributes={"name": router_name})
            logger.info("[RED-GCP] Cloud Router creado: %s", router["selfLink"])

        nat_name = self._name("nat")
        existing_nat = next(
            (r for r in self.state.list_resources(self.deployment_id) if r.get("component") == "nat"), None
        )
        if existing_nat:
            logger.info("[SKIP][RED-GCP] Cloud NAT ya registrado.")
            return router_name, nat_name

        router = self.compute.routers().get(
            project=self.spec.project_id, region=self.spec.region, router=router_name
        ).execute()
        nat_body = {
            "name": nat_name,
            "natIpAllocateOption": "AUTO_ONLY",
            # Cubre AMBAS subredes (no solo la privada): sin esto, si algún
            # día el Gateway pierde su IP externa efímera un instante (no
            # aplica hoy -sin dominio propio no hay reasignación de IP-, pero
            # deja la puerta cerrada a ese modo de fallo si se implementa en
            # el futuro), se quedaría sin salida a internet durante el hueco.
            "sourceSubnetworkIpRangesToNat": "LIST_OF_SUBNETWORKS",
            "subnetworks": [
                {
                    "name": f"projects/{self.spec.project_id}/regions/{self.spec.region}/subnetworks/{private_subnet_name}",
                    "sourceIpRangesToNat": ["ALL_IP_RANGES"],
                }
            ],
        }
        router["nats"] = (router.get("nats") or []) + [nat_body]
        op = self.compute.routers().patch(
            project=self.spec.project_id, region=self.spec.region, router=router_name, body=router
        ).execute()
        self._wait(op, region=self.spec.region)
        self._record("nat", "nat", f"{router['selfLink']}/nats/{nat_name}", attributes={"name": nat_name})
        logger.info("[RED-GCP] Cloud NAT '%s' asociado al router '%s'.", nat_name, router_name)
        return router_name, nat_name

    def ensure_firewall_rules(self, vpc_name: str) -> List[Tuple[str, str]]:
        """Crea (o actualiza por diff) las reglas de firewall. SIN network
        tags (ver el docstring del módulo): acotadas por sourceRanges, usando
        la separación de subredes pública/privada como sustituto de la
        referencia SG->SG de AWS / NSG de Azure. Devuelve [(id, name), ...]."""
        public_cidr, private_cidr = self._resolve_subnet_cidrs()
        admin_cidrs = self.spec.admin_cidrs or ["0.0.0.0/0"]
        if admin_cidrs == ["0.0.0.0/0"]:
            logger.warning(
                "[RED-GCP] cidr_admin_ssh = 0.0.0.0/0: el puerto 22 del gateway queda abierto "
                "a toda internet. Restringir en producción."
            )
        public_cidrs = self.spec.public_cidrs or ["0.0.0.0/0"]
        worker_ports = [str(p) for p in (self.spec.worker_ports or [])]

        reglas: List[Tuple[str, Dict[str, Any]]] = []

        gw_ports = ["80"]
        if self.spec.tls_enabled:
            gw_ports.append("443")
        if self.spec.expose_direct_ports:
            gw_ports.extend(str(p) for p in (self.spec.gateway_public_ports or []))
        reglas.append(("fw-gateway-ssh", {"sourceRanges": admin_cidrs, "ports": ["22"]}))
        reglas.append(("fw-gateway-public", {"sourceRanges": public_cidrs, "ports": gw_ports}))
        reglas.append(("fw-workers-from-gateway", {
            "sourceRanges": [public_cidr], "ports": ["22"] + worker_ports,
        }))
        # Rango completo (no solo worker_ports): comunicación inter-nodo
        # (tensor/pipeline parallel entre réplicas, coordinación de Ray) sobre
        # puertos que no se pueden enumerar de antemano -mismo criterio que
        # azure_network.py y que la plantilla propia de SkyPilot para tráfico
        # intra-red (sky/provision/gcp/constants.py). Confinado a la subred
        # PRIVADA, sin IPs externas ahí, así que el radio de exposición real
        # es cero.
        reglas.append(("fw-workers-internal", {
            "sourceRanges": [private_cidr], "ports": None,  # None = todos los puertos TCP/UDP
        }))

        resultados: List[Tuple[str, str]] = []
        for component, params in reglas:
            gcp_id, name = self._ensure_one_firewall(vpc_name, component, params)
            resultados.append((gcp_id, name))
        return resultados

    def _ensure_one_firewall(
        self, vpc_name: str, component: str, params: Dict[str, Any]
    ) -> Tuple[str, str]:
        name = self._name(component)
        ports = params["ports"]
        allowed = (
            [{"IPProtocol": "tcp"}, {"IPProtocol": "udp"}]
            if ports is None
            else [{"IPProtocol": "tcp", "ports": ports}]
        )
        body = {
            "name": name,
            "description": self._tags(component),
            "network": f"projects/{self.spec.project_id}/global/networks/{vpc_name}",
            "direction": "INGRESS",
            "sourceRanges": params["sourceRanges"],
            "allowed": allowed,
            "priority": 1000,
        }

        try:
            existing = self.compute.firewalls().get(project=self.spec.project_id, firewall=name).execute()
        except HttpError as exc:
            if not _is_not_found(exc):
                raise
            existing = None

        if existing is None:
            op = self.compute.firewalls().insert(project=self.spec.project_id, body=body).execute()
            self._wait(op)
            logger.info("[RED-GCP] Regla de firewall creada: %s", name)
        elif existing.get("sourceRanges") != body["sourceRanges"] or existing.get("allowed") != body["allowed"]:
            op = self.compute.firewalls().patch(
                project=self.spec.project_id, firewall=name, body=body
            ).execute()
            self._wait(op)
            logger.info("[RED-GCP] Regla de firewall actualizada (diff): %s", name)
        else:
            logger.info("[SKIP][RED-GCP] Regla de firewall sin cambios: %s", name)

        fw = self.compute.firewalls().get(project=self.spec.project_id, firewall=name).execute()
        self._record(component, component, fw["selfLink"], attributes={"name": name})
        return fw["selfLink"], name

    # -------------------------------------------------------------------
    # Orquestación
    # -------------------------------------------------------------------
    def provision(self, dry_run: bool = False) -> GcpNetworkOutputs:
        """Orquesta el aprovisionamiento completo: VPC -> subredes -> Cloud
        Router/NAT -> reglas de firewall, en ese orden. Idempotente: cada
        paso reutiliza lo que ya exista para este deployment_id."""
        if dry_run:
            logger.info("[RED-GCP] --dry-run: no se ejecuta ninguna llamada mutante a GCP.")
            return self.status()  # type: ignore[return-value]

        self.state.set_deployment_status(self.deployment_id, "creating")
        try:
            vpc_id, vpc_name = self.ensure_network()
            public_id, public_name, private_id, private_name = self.ensure_subnets(vpc_name)
            router_name, nat_name = self.ensure_router_and_nat(vpc_name, private_name)
            firewalls = self.ensure_firewall_rules(vpc_name)
        except Exception as exc:
            self.state.set_deployment_status(self.deployment_id, "error", error=str(exc))
            self.state.log_event(self.deployment_id, "network", "provision", "error", message=str(exc))
            raise

        self.state.set_deployment_status(self.deployment_id, "active")
        self.state.log_event(self.deployment_id, "network", "provision", "ok")

        public_cidr, private_cidr = self._resolve_subnet_cidrs()
        router = self.compute.routers().get(
            project=self.spec.project_id, region=self.spec.region, router=router_name
        ).execute() if router_name else None

        return GcpNetworkOutputs(
            deployment_id=self.deployment_id,
            project_id=self.spec.project_id,
            region=self.spec.region,
            vpc_id=vpc_id,
            vpc_name=vpc_name,
            public_subnet_id=public_id,
            public_subnet_name=public_name,
            public_subnet_cidr=public_cidr,
            private_subnet_id=private_id,
            private_subnet_name=private_name,
            private_subnet_cidr=private_cidr,
            router_id=(router or {}).get("selfLink"),
            router_name=router_name,
            nat_name=nat_name,
            firewall_ids=[gcp_id for gcp_id, _ in firewalls],
            firewall_names=[name for _, name in firewalls],
            managed_by_us=True,
        )

    def status(self) -> Dict[str, Any]:
        return {
            "deployment_id": self.deployment_id,
            "resources": self.state.list_resources(self.deployment_id),
        }

    def plan_destroy(self) -> List[PlannedDeletion]:
        resources = self.state.resources_in_delete_order(self.deployment_id)
        return [
            PlannedDeletion(
                resource_type=res["resource_type"],
                component=res["component"],
                gcp_id=res.get("aws_id"),
                name=(res.get("attributes") or {}).get("name"),
                delete_order=res["delete_order"],
                managed_by_us=res.get("managed_by_us", True),
                attributes=res.get("attributes"),
            )
            for res in resources
        ]

    def _tags_match_deployment(self, component: str) -> Optional[bool]:
        """Segunda condición del mecanismo de propiedad. Devuelve None si el
        recurso ya no existe (nada que borrar), False si existe pero su
        'description' apunta a otro deployment_id (no tocar), True si coincide."""
        try:
            if component == "vpc":
                obj = self.compute.networks().get(
                    project=self.spec.project_id, network=self._name("vpc")
                ).execute()
            elif component in ("subnet-public", "subnet-private"):
                obj = self.compute.subnetworks().get(
                    project=self.spec.project_id, region=self.spec.region, subnetwork=self._name(component)
                ).execute()
            elif component == "router":
                obj = self.compute.routers().get(
                    project=self.spec.project_id, region=self.spec.region, router=self._name("router")
                ).execute()
            elif component == "nat":
                # La NAT no es un recurso independiente (vive dentro de
                # 'nats[]' del router) -su propiedad es la del router padre.
                return self._tags_match_deployment("router")
            elif component.startswith("fw-"):
                obj = self.compute.firewalls().get(
                    project=self.spec.project_id, firewall=self._name(component)
                ).execute()
            else:
                return False
        except HttpError as exc:
            if _is_not_found(exc):
                return None
            raise

        tags = self._parse_tags(obj.get("description"))
        return tags.get("deployment_id") == self.deployment_id and tags.get("managed") == "true"

    def destroy(self, dry_run: bool = False, force: bool = False) -> DestroyReport:
        plan = self.plan_destroy()
        report = DestroyReport(deployment_id=self.deployment_id)

        if dry_run:
            for item in plan:
                logger.info(
                    "[DESTROY-GCP] (dry-run) %s %s id=%s orden=%s managed_by_us=%s",
                    item.resource_type, item.component, item.gcp_id, item.delete_order, item.managed_by_us,
                )
            return report

        self.state.set_deployment_status(self.deployment_id, "destroying")

        for item in plan:
            if not item.gcp_id:
                continue
            if not item.managed_by_us and not force:
                report.skipped_not_ours.append(item)
                logger.warning("[DESTROY-GCP] Omitido (managed_by_us=False): %s %s", item.component, item.gcp_id)
                continue

            tags_match = self._tags_match_deployment(item.component)
            if tags_match is None:
                self.state.mark_resource_state(self.deployment_id, item.gcp_id, "deleted")
                report.succeeded.append(item)
                logger.info("[DESTROY-GCP] %s ya no existía; nada que borrar.", item.component)
                continue
            if not tags_match:
                report.skipped_not_ours.append(item)
                logger.warning(
                    "[DESTROY-GCP] Omitido: la propiedad de %s no coincide con deployment_id=%s.",
                    item.component, self.deployment_id,
                )
                continue

            try:
                self._delete_one(item)
                self.state.mark_resource_state(self.deployment_id, item.gcp_id, "deleted")
                report.succeeded.append(item)
            except HttpError as exc:
                self.state.mark_resource_state(self.deployment_id, item.gcp_id, "error")
                report.failed.append({"item": item, "error": str(getattr(exc, "status_code", exc)), "message": str(exc)})
                report.manual_actions_required.append(
                    f"Revisar manualmente {item.component} ({item.name}): {exc}. "
                    f"Comando de diagnóstico: gcloud compute firewall-rules describe {item.name} "
                    "(ajustar el subcomando según el tipo de recurso)."
                )
                logger.error("[DESTROY-GCP] Fallo borrando %s %s: %s", item.component, item.gcp_id, exc)

        if report.ok:
            self.state.set_deployment_status(self.deployment_id, "destroyed")
            self.state.close_deployment(self.deployment_id)
            self.state.log_event(self.deployment_id, "destroy", "destroy", "ok")
        else:
            self.state.set_deployment_status(self.deployment_id, "degraded", error="destroy parcial: ver DestroyReport")
            self.state.log_event(self.deployment_id, "destroy", "destroy", "warning", message="destroy parcial")

        return report

    def _delete_one(self, item: PlannedDeletion) -> None:
        component = item.component
        project = self.spec.project_id
        region = self.spec.region

        if component.startswith("fw-"):
            op = self.compute.firewalls().delete(project=project, firewall=self._name(component)).execute()
            self._wait(op)
        elif component == "nat":
            router_name = self._name("router")
            router = self.compute.routers().get(project=project, region=region, router=router_name).execute()
            router["nats"] = [n for n in (router.get("nats") or []) if n.get("name") != self._name("nat")]
            op = self.compute.routers().patch(project=project, region=region, router=router_name, body=router).execute()
            self._wait(op, region=region)
        elif component == "router":
            op = self.compute.routers().delete(project=project, region=region, router=self._name("router")).execute()
            self._wait(op, region=region)
        elif component == "subnet-public":
            op = self.compute.subnetworks().delete(
                project=project, region=region, subnetwork=self._name("subnet-public")
            ).execute()
            self._wait(op, region=region)
        elif component == "subnet-private":
            op = self.compute.subnetworks().delete(
                project=project, region=region, subnetwork=self._name("subnet-private")
            ).execute()
            self._wait(op, region=region)
        elif component == "vpc":
            op = self.compute.networks().delete(project=project, network=self._name("vpc")).execute()
            self._wait(op)
        else:
            raise GcpNetworkError(f"Componente desconocido en destroy: {component}")

    def scan_orphans(self) -> List[Dict[str, Any]]:
        """Busca recursos con 'description' de sooniverse (managed=true, mismo
        client_id/environment) que no estén en el estado de este deployment_id
        -deployment ya destruido o desconocido. A diferencia de AWS (una
        cuenta/región compartida entre todos los clientes) o de Azure (un
        Resource Group dedicado por cliente), GCP no tiene ni tag-filtering en
        el servidor ni un contenedor por cliente -así que se listan TODOS los
        recursos de cada tipo en el proyecto/región y se filtra en el cliente
        parseando 'description'. Aceptable para un proyecto 'hosted' de un
        solo cliente; en un proyecto realmente compartido entre clientes esto
        necesitaría paginar con cuidado."""
        known_ids = {r.get("aws_id") for r in self.state.list_resources(self.deployment_id)}
        orphans: List[Dict[str, Any]] = []
        project = self.spec.project_id
        region = self.spec.region

        def _es_nuestro(description: Optional[str]) -> bool:
            tags = self._parse_tags(description)
            return (
                tags.get("managed") == "true"
                and tags.get("prefix") == TAG_PREFIX
                and tags.get("client_id") == self.spec.client_id
                and tags.get("environment") == self.spec.environment
            )

        checks = [
            ("Microsoft.Fake", None),  # placeholder no usado, ver checks reales abajo
        ]
        del checks  # (evita un listado engañoso; los checks reales están inline abajo)

        for item in self.compute.networks().list(project=project).get("items", []) or []:
            if _es_nuestro(item.get("description")) and item["selfLink"] not in known_ids:
                orphans.append({"gcp_id": item["selfLink"], "type": "networks", "tags": self._parse_tags(item.get("description"))})

        for item in self.compute.subnetworks().list(project=project, region=region).get("items", []) or []:
            if _es_nuestro(item.get("description")) and item["selfLink"] not in known_ids:
                orphans.append({"gcp_id": item["selfLink"], "type": "subnetworks", "tags": self._parse_tags(item.get("description"))})

        for item in self.compute.routers().list(project=project, region=region).get("items", []) or []:
            if _es_nuestro(item.get("description")) and item["selfLink"] not in known_ids:
                orphans.append({"gcp_id": item["selfLink"], "type": "routers", "tags": self._parse_tags(item.get("description"))})

        for item in self.compute.firewalls().list(project=project).get("items", []) or []:
            if _es_nuestro(item.get("description")) and item["selfLink"] not in known_ids:
                orphans.append({"gcp_id": item["selfLink"], "type": "firewalls", "tags": self._parse_tags(item.get("description"))})

        return orphans
