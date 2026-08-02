#!/usr/bin/env python3
"""
==============================================================================
Sooniverse Infra - Gestor de red AWS (Fase 1: VPC/subredes/NAT/SGs por boto3)
==============================================================================
Reemplaza la creación manual de VPC, subredes, Internet Gateway, NAT Gateway,
route tables y Security Groups (ver Manual_VPC_SecurityGroup.md, ahora anexo
histórico) por un ciclo de vida gestionado íntegramente en Python vía boto3.

Alcance de este módulo: SOLO la capa de red (recursos EC2/VPC). El ciclo de
vida completo (VPC -> gateway SkyPilot -> workers SkyPilot -> endpoints) se
orquesta desde `scripts/generate_infra.py` / `scripts/destroy_infra.py`
(Fase 3), que llaman a `AwsNetworkManager` como un paso más.

Mecanismo de propiedad (ver PROMPT_CLAUDE_CODE_sooniverse_red.md, sección 1):
un recurso solo se borra si (a) está registrado en `InfraStateStore` con el
`deployment_id` correspondiente Y (b) sus tags AWS coinciden con ese mismo
`deployment_id`. Si cualquiera de las dos condiciones falla, no se borra.
"""

from __future__ import annotations

import ipaddress
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    import boto3
    from botocore.config import Config as BotoConfig
    from botocore.exceptions import ClientError, WaiterError
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "Falta 'boto3' (viene con 'skypilot[aws]'). Instala con: pip install boto3"
    ) from exc

from infra_state import InfraStateStore, InMemoryInfraStateStore  # type: ignore

logger = logging.getLogger("sooniverse.aws_network")

TAG_PREFIX = "sooniverse"
TAG_MANAGED = f"{TAG_PREFIX}:managed"
TAG_CLIENT = f"{TAG_PREFIX}:client-id"
TAG_ENV = f"{TAG_PREFIX}:environment"
TAG_DEPLOYMENT = f"{TAG_PREFIX}:deployment-id"
TAG_COMPONENT = f"{TAG_PREFIX}:component"
TAG_CREATED_AT = f"{TAG_PREFIX}:created-at"

# Orden de borrado (inverso de la creación). Números más bajos se borran antes.
DELETE_ORDER = {
    "sg-workers": 10,
    "sg-gateway": 11,
    "vpce-s3": 20,
    "nat": 30,
    "eip": 40,
    "rtb-private": 50,
    "rtb-public": 51,
    "igw": 60,
    "subnet-private": 70,
    "subnet-public": 71,
    "vpc": 80,
}

# Orden de creación (usado por provision(); es el inverso de DELETE_ORDER).
_CREATE_ORDER_COMPONENTS = [
    "vpc",
    "subnet-public",
    "subnet-private",
    "igw",
    "eip",
    "nat",
    "rtb-public",
    "rtb-private",
    "vpce-s3",
    "sg-gateway",
    "sg-workers",
]


class NetworkError(Exception):
    """Error de aprovisionamiento o destrucción de red."""


class DefaultVpcGuardError(NetworkError):
    """Se intentó operar sobre la VPC por defecto de la cuenta. Abortado."""


@dataclass(frozen=True)
class NetworkSpec:
    """Entrada declarativa de `AwsNetworkManager`: todo lo que necesita saber para
    aprovisionar (o planear la destrucción de) la capa de red de un despliegue.
    Se construye típicamente con `generate_infra.build_network_spec_from_config()`."""

    client_id: str
    environment: str
    region: str
    vpc_cidr: str
    az_count: int
    public_subnet_cidrs: Optional[List[str]] = None
    private_subnet_cidrs: Optional[List[str]] = None
    nat_mode: str = "single"  # "single" | "per-az" | "none"
    enable_s3_endpoint: bool = True
    admin_cidrs: Optional[List[str]] = None      # SSH al gateway
    public_cidrs: Optional[List[str]] = None      # HTTP/HTTPS al gateway
    gateway_public_ports: Optional[List[int]] = None
    worker_ports: Optional[List[int]] = None
    expose_direct_ports: bool = False
    tls_enabled: bool = False
    nat_timeout_seconds: int = 300
    extra_tags: Optional[Dict[str, str]] = None
    aws_profile: Optional[str] = None  # perfil de credenciales (~/.aws/credentials) por cliente

    def __post_init__(self) -> None:
        if self.nat_mode not in ("single", "per-az", "none"):
            raise NetworkError(f"nat_mode inválido: {self.nat_mode!r}")
        if self.az_count < 1:
            raise NetworkError("az_count debe ser >= 1")
        # Normaliza cliente.id (minúsculas, [a-z0-9-], máx 20 chars) según convención multi-cliente.
        normalized = self.client_id.lower()
        if normalized != self.client_id or len(self.client_id) > 20:
            raise NetworkError(
                f"client_id '{self.client_id}' inválido: debe ser minúsculas, [a-z0-9-], máx 20 caracteres."
            )


@dataclass(frozen=True)
class NetworkOutputs:
    """Resultado de `AwsNetworkManager.provision()`/`adopt_existing()`: los IDs y
    nombres reales que `TopologyBuilder` necesita para construir las configs de
    cliente de SkyPilot (`aws.vpc_name`, `aws.security_group_name`)."""

    deployment_id: str
    vpc_id: str
    vpc_name: str
    availability_zones: List[str]
    public_subnet_ids: List[str]
    private_subnet_ids: List[str]
    internet_gateway_id: Optional[str]
    nat_gateway_ids: List[str]
    elastic_ip_allocation_ids: List[str]
    public_route_table_id: Optional[str]
    private_route_table_ids: List[str]
    sg_gateway_id: str
    sg_gateway_name: str
    sg_workers_id: str
    sg_workers_name: str
    managed_by_us: bool = True


@dataclass
class PlannedDeletion:
    """Un recurso que `destroy()`/`plan_destroy()` va a intentar borrar (o ya intentó),
    en el orden dado por `delete_order`."""

    resource_type: str
    component: str
    aws_id: Optional[str]
    name: Optional[str]
    delete_order: int
    managed_by_us: bool


@dataclass
class DestroyReport:
    """Resultado de `destroy()`: qué se borró, qué falló (con el código de error de
    AWS), qué se omitió por el mecanismo de propiedad, y comandos de diagnóstico
    para lo que requiere intervención manual."""

    deployment_id: str
    succeeded: List[PlannedDeletion] = field(default_factory=list)
    failed: List[Dict[str, Any]] = field(default_factory=list)
    skipped_not_ours: List[PlannedDeletion] = field(default_factory=list)
    manual_actions_required: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True si no hubo ningún fallo (puede haber omitidos por managed_by_us=False)."""
        return not self.failed


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_subnet_cidrs(vpc_cidr: str, az_count: int) -> Tuple[List[str], List[str]]:
    """Subdivide `vpc_cidr` (típicamente /16) en bloques /20 deterministas:
    las primeras `az_count` subredes /20 son públicas (empezando en el bloque 0),
    y las siguientes `az_count` (empezando en la mitad alta del espacio /16,
    offset 128 de bloques /20) son privadas. Reproducible entre corridas.
    """
    vpc_net = ipaddress.ip_network(vpc_cidr, strict=True)
    subnet_prefix = max(vpc_net.prefixlen + 4, 20)
    all_subnets = list(vpc_net.subnets(new_prefix=subnet_prefix))
    total_blocks = len(all_subnets)
    half = total_blocks // 2

    if az_count > half:
        raise NetworkError(
            f"'vpc_cidr' {vpc_cidr} no tiene espacio para {az_count} AZ públicas + "
            f"{az_count} privadas con subredes /{subnet_prefix} (máximo {half} por grupo)."
        )

    public = [str(net) for net in all_subnets[:az_count]]
    private = [str(net) for net in all_subnets[half : half + az_count]]
    return public, private


class AwsNetworkManager:
    """Ciclo de vida completo de la capa de red AWS para un despliegue Sooniverse."""

    def __init__(
        self,
        spec: NetworkSpec,
        state: Optional[InfraStateStore] = None,
        session: Optional["boto3.Session"] = None,
        deployment_id: Optional[str] = None,
    ) -> None:
        self.spec = spec
        self.state = state if state is not None else InMemoryInfraStateStore()
        boto_config = BotoConfig(retries={"max_attempts": 10, "mode": "adaptive"})
        # Aislamiento de credenciales por cliente (Fase 6): red_y_aislamiento.aws_profile
        # selecciona un perfil de ~/.aws/credentials o ~/.aws/config. Si se pasa una
        # `session` explícita (tests con moto, o el futuro modo BYOC con AssumeRole de
        # abajo), esta gana sobre `aws_profile`.
        #
        # Hook futuro (NO implementado): modo BYOC vía AssumeRole + External ID, para que
        # el cliente final apruebe el acceso desde SU cuenta AWS sin compartir credenciales
        # permanentes con nosotros:
        #   sts = boto3.client("sts")
        #   creds = sts.assume_role(
        #       RoleArn=f"arn:aws:iam::{cliente_account_id}:role/SooniverseDeployRole",
        #       RoleSessionName=f"sooniverse-{spec.client_id}-{spec.environment}",
        #       ExternalId=cliente_external_id,  # mitiga el "confused deputy problem"
        #   )["Credentials"]
        #   session = boto3.Session(aws_access_key_id=creds["AccessKeyId"], ...)
        self._session = session or boto3.Session(profile_name=spec.aws_profile, region_name=spec.region)
        self.ec2 = self._session.client("ec2", region_name=spec.region, config=boto_config)

        if deployment_id:
            self.deployment_id = deployment_id
        else:
            self.deployment_id = self.state.open_deployment(
                client_id=spec.client_id,
                environment=spec.environment,
                region=spec.region,
            )

        self._log_prefix = "[RED]"

    # -------------------------------------------------------------------
    # Utilidades internas
    # -------------------------------------------------------------------

    def _name(self, component: str, suffix: str = "") -> str:
        base = f"sooniverse-{self.spec.client_id}-{self.spec.environment}-{component}"
        return f"{base}-{suffix}" if suffix else base

    def _tags(self, component: str, name: str) -> List[Dict[str, str]]:
        tags = {
            TAG_MANAGED: "true",
            TAG_CLIENT: self.spec.client_id,
            TAG_ENV: self.spec.environment,
            TAG_DEPLOYMENT: self.deployment_id,
            TAG_COMPONENT: component,
            TAG_CREATED_AT: _now_iso(),
            "Name": name,
        }
        for key, value in (self.spec.extra_tags or {}).items():
            if key.startswith(f"{TAG_PREFIX}:"):
                raise NetworkError(
                    f"'extra_tags' no puede usar el prefijo reservado '{TAG_PREFIX}:' (clave: {key})"
                )
            tags.setdefault(key, value)
        return [{"Key": k, "Value": v} for k, v in tags.items()]

    def _tag_specs(self, resource_type: str, component: str, name: str) -> List[Dict[str, Any]]:
        return [{"ResourceType": resource_type, "Tags": self._tags(component, name)}]

    def _find_by_component(self, describe_fn, id_field: str, component: str) -> Optional[Dict[str, Any]]:
        """Busca un recurso existente por (deployment-id, component). Usado por cada
        `ensure_*` antes de crear, para que `provision()` sea idempotente."""
        resp = describe_fn(
            Filters=[
                {"Name": f"tag:{TAG_DEPLOYMENT}", "Values": [self.deployment_id]},
                {"Name": f"tag:{TAG_COMPONENT}", "Values": [component]},
            ]
        )
        items = next(v for k, v in resp.items() if isinstance(v, list))

        def _state_of(item: Dict[str, Any]) -> Optional[str]:
            state = item.get("State")
            if isinstance(state, dict):
                return state.get("Name")
            return state

        healthy = [i for i in items if _state_of(i) not in ("deleted", "terminated", "failed")]
        return healthy[0] if healthy else (items[0] if items else None)

    def _record(self, resource_type: str, component: str, aws_id: str, **extra: Any) -> None:
        fields = {
            "resource_type": resource_type,
            "component": component,
            "aws_id": aws_id,
            "region": self.spec.region,
            "delete_order": DELETE_ORDER.get(component, 999),
            "managed_by_us": True,
            "state": "active",
        }
        fields.update(extra)
        self.state.record_resource(self.deployment_id, **fields)

    def _guard_not_default_vpc(self, vpc_id: str) -> None:
        resp = self.ec2.describe_vpcs(VpcIds=[vpc_id])
        vpcs = resp.get("Vpcs", [])
        if vpcs and vpcs[0].get("IsDefault"):
            raise DefaultVpcGuardError(
                f"[RED] Abortado: '{vpc_id}' es la VPC por defecto de la cuenta. "
                "Este sistema nunca crea, modifica ni destruye la VPC por defecto."
            )

    # -------------------------------------------------------------------
    # ensure_* (idempotentes)
    # -------------------------------------------------------------------

    def ensure_vpc(self) -> str:
        """Reutiliza la VPC de este deployment_id si ya existe; si no, la crea con
        DNS support/hostnames habilitados y espera a que quede 'available'."""
        name = self._name("vpc")
        existing = self._find_by_component(self.ec2.describe_vpcs, "VpcId", "vpc")
        if existing:
            vpc_id = existing["VpcId"]
            logger.info("[SKIP][RED:VPC] Reutilizando VPC existente %s", vpc_id)
            self._record("vpc", "vpc", vpc_id, attributes={"name": name})
            return vpc_id

        t0 = time.monotonic()
        resp = self.ec2.create_vpc(
            CidrBlock=self.spec.vpc_cidr,
            TagSpecifications=self._tag_specs("vpc", "vpc", name),
        )
        vpc_id = resp["Vpc"]["VpcId"]
        self._record("vpc", "vpc", vpc_id, state="creating", attributes={"name": name})
        self._guard_not_default_vpc(vpc_id)

        self.ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})
        self.ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})

        self._wait("vpc_available", VpcIds=[vpc_id])
        self._record("vpc", "vpc", vpc_id, state="active", attributes={"name": name})
        logger.info("[RED:VPC] VPC %s creada en %.1fs", vpc_id, time.monotonic() - t0)
        return vpc_id

    def _available_azs(self) -> List[str]:
        resp = self.ec2.describe_availability_zones(
            Filters=[{"Name": "state", "Values": ["available"]}]
        )
        names = sorted(z["ZoneName"] for z in resp["AvailabilityZones"])
        if len(names) < self.spec.az_count:
            raise NetworkError(
                f"La región {self.spec.region} solo tiene {len(names)} AZ disponibles, "
                f"se pidieron {self.spec.az_count}."
            )
        return names[: self.spec.az_count]

    def ensure_subnets(self, vpc_id: str) -> Tuple[List[str], List[str]]:
        """Crea (o reutiliza) una subred pública y una privada por AZ, usando los
        CIDR explícitos del spec o los calculados por `compute_subnet_cidrs()`.
        Devuelve (ids_publicas, ids_privadas)."""
        azs = self._available_azs()
        public_cidrs = self.spec.public_subnet_cidrs
        private_cidrs = self.spec.private_subnet_cidrs
        if not public_cidrs or not private_cidrs:
            public_cidrs, private_cidrs = compute_subnet_cidrs(self.spec.vpc_cidr, self.spec.az_count)

        public_ids = [self._ensure_one_subnet(vpc_id, cidr, az, "subnet-public", public=True) for cidr, az in zip(public_cidrs, azs)]
        private_ids = [self._ensure_one_subnet(vpc_id, cidr, az, "subnet-private", public=False) for cidr, az in zip(private_cidrs, azs)]
        return public_ids, private_ids

    def _ensure_one_subnet(self, vpc_id: str, cidr: str, az: str, component: str, public: bool) -> str:
        resp = self.ec2.describe_subnets(
            Filters=[
                {"Name": f"tag:{TAG_DEPLOYMENT}", "Values": [self.deployment_id]},
                {"Name": f"tag:{TAG_COMPONENT}", "Values": [component]},
                {"Name": "availabilityZone", "Values": [az]},
            ]
        )
        existing = resp.get("Subnets", [])
        if existing:
            subnet_id = existing[0]["SubnetId"]
            self._record(component, component, subnet_id, availability_zone=az, parent_aws_id=vpc_id)
            return subnet_id

        name = self._name(component.replace("subnet-", "subred-"), az)
        resp = self.ec2.create_subnet(
            VpcId=vpc_id,
            CidrBlock=cidr,
            AvailabilityZone=az,
            TagSpecifications=self._tag_specs("subnet", component, name),
        )
        subnet_id = resp["Subnet"]["SubnetId"]
        self._record(component, component, subnet_id, availability_zone=az, parent_aws_id=vpc_id)

        if public:
            self.ec2.modify_subnet_attribute(SubnetId=subnet_id, MapPublicIpOnLaunch={"Value": True})

        self._wait("subnet_available", SubnetIds=[subnet_id])
        logger.info("[RED] Subred %s (%s, %s) creada: %s", component, cidr, az, subnet_id)
        return subnet_id

    def ensure_internet_gateway(self, vpc_id: str) -> str:
        """Crea (o reutiliza) el Internet Gateway y lo adjunta a `vpc_id`."""
        existing = self._find_by_component(self.ec2.describe_internet_gateways, "InternetGatewayId", "igw")
        if existing:
            igw_id = existing["InternetGatewayId"]
            self._record("igw", "igw", igw_id, parent_aws_id=vpc_id)
            logger.info("[SKIP][RED] IGW ya existe: %s", igw_id)
            return igw_id

        name = self._name("igw")
        resp = self.ec2.create_internet_gateway(TagSpecifications=self._tag_specs("internet-gateway", "igw", name))
        igw_id = resp["InternetGateway"]["InternetGatewayId"]
        self.ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
        self._record("igw", "igw", igw_id, parent_aws_id=vpc_id)
        logger.info("[RED] Internet Gateway creado y adjunto: %s", igw_id)
        return igw_id

    def ensure_nat_gateways(self, public_subnet_ids: List[str]) -> Tuple[List[str], List[str]]:
        """Asigna una EIP y crea un NAT Gateway por `nat_mode` ('single': uno solo en
        la primera subred pública; 'per-az': uno por AZ; 'none': ninguno). Espera a
        que cada uno quede 'available' (puede tardar minutos). Devuelve
        (ids_nat, ids_eip_allocation)."""
        if self.spec.nat_mode == "none":
            return [], []

        existing = self.ec2.describe_nat_gateways(
            Filters=[
                {"Name": f"tag:{TAG_DEPLOYMENT}", "Values": [self.deployment_id]},
                {"Name": "state", "Values": ["pending", "available"]},
            ]
        ).get("NatGateways", [])
        if existing:
            nat_ids = [n["NatGatewayId"] for n in existing]
            eip_ids = [a["AllocationId"] for n in existing for a in n.get("NatGatewayAddresses", []) if a.get("AllocationId")]
            for nat_id in nat_ids:
                self._record("nat", "nat", nat_id)
            logger.info("[SKIP][RED:NAT] NAT Gateway(s) ya existen: %s", nat_ids)
            return nat_ids, eip_ids

        subnets_for_nat = public_subnet_ids if self.spec.nat_mode == "per-az" else public_subnet_ids[:1]

        nat_ids: List[str] = []
        eip_ids: List[str] = []
        for idx, subnet_id in enumerate(subnets_for_nat):
            eip_name = self._name("eip", str(idx))
            eip_resp = self.ec2.allocate_address(
                Domain="vpc",
                TagSpecifications=self._tag_specs("elastic-ip", "eip", eip_name),
            )
            alloc_id = eip_resp["AllocationId"]
            self._record("eip", "eip", alloc_id)
            eip_ids.append(alloc_id)

            nat_name = self._name("nat", str(idx))
            nat_resp = self.ec2.create_nat_gateway(
                SubnetId=subnet_id,
                AllocationId=alloc_id,
                TagSpecifications=self._tag_specs("natgateway", "nat", nat_name),
            )
            nat_id = nat_resp["NatGateway"]["NatGatewayId"]
            self._record("nat", "nat", nat_id, state="creating", parent_aws_id=subnet_id)
            nat_ids.append(nat_id)

        for nat_id in nat_ids:
            self._wait("nat_gateway_available", NatGatewayIds=[nat_id], timeout=self.spec.nat_timeout_seconds)
            self._record("nat", "nat", nat_id, state="active")
            logger.info("[RED:NAT] NAT Gateway disponible: %s", nat_id)

        return nat_ids, eip_ids

    def ensure_route_tables(
        self,
        vpc_id: str,
        igw_id: Optional[str],
        public_subnet_ids: List[str],
        private_subnet_ids: List[str],
        nat_gateway_ids: List[str],
    ) -> Tuple[Optional[str], List[str]]:
        """Crea la route table pública (0.0.0.0/0 -> IGW) y una privada por subred
        privada (0.0.0.0/0 -> NAT correspondiente), asociándolas a sus subredes.
        Devuelve (id_rt_publica, ids_rt_privadas)."""
        public_rt_id = None
        if igw_id and public_subnet_ids:
            public_rt_id = self._ensure_route_table(vpc_id, "rtb-public")
            self._ensure_route(public_rt_id, "0.0.0.0/0", gateway_id=igw_id)
            for subnet_id in public_subnet_ids:
                self._ensure_association(public_rt_id, subnet_id)

        private_rt_ids: List[str] = []
        if private_subnet_ids:
            for idx, subnet_id in enumerate(private_subnet_ids):
                rt_id = self._ensure_route_table(vpc_id, "rtb-private", suffix=str(idx))
                if nat_gateway_ids:
                    nat_id = nat_gateway_ids[idx] if self.spec.nat_mode == "per-az" and idx < len(nat_gateway_ids) else nat_gateway_ids[0]
                    self._ensure_route(rt_id, "0.0.0.0/0", nat_gateway_id=nat_id)
                self._ensure_association(rt_id, subnet_id)
                private_rt_ids.append(rt_id)

        return public_rt_id, private_rt_ids

    def _ensure_route_table(self, vpc_id: str, component: str, suffix: str = "") -> str:
        resp = self.ec2.describe_route_tables(
            Filters=[
                {"Name": f"tag:{TAG_DEPLOYMENT}", "Values": [self.deployment_id]},
                {"Name": f"tag:{TAG_COMPONENT}", "Values": [component]},
                {"Name": "tag:Name", "Values": [self._name(component, suffix)]},
            ]
        )
        existing = resp.get("RouteTables", [])
        if existing:
            rt_id = existing[0]["RouteTableId"]
            self._record(component, component, rt_id, parent_aws_id=vpc_id)
            return rt_id

        name = self._name(component, suffix)
        resp = self.ec2.create_route_table(VpcId=vpc_id, TagSpecifications=self._tag_specs("route-table", component, name))
        rt_id = resp["RouteTable"]["RouteTableId"]
        self._record(component, component, rt_id, parent_aws_id=vpc_id)
        return rt_id

    def _ensure_route(self, rt_id: str, cidr: str, gateway_id: Optional[str] = None, nat_gateway_id: Optional[str] = None) -> None:
        try:
            kwargs: Dict[str, Any] = {"RouteTableId": rt_id, "DestinationCidrBlock": cidr}
            if gateway_id:
                kwargs["GatewayId"] = gateway_id
            if nat_gateway_id:
                kwargs["NatGatewayId"] = nat_gateway_id
            self.ec2.create_route(**kwargs)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "RouteAlreadyExists":
                raise

    def _ensure_association(self, rt_id: str, subnet_id: str) -> None:
        rt = self.ec2.describe_route_tables(RouteTableIds=[rt_id])["RouteTables"][0]
        already = any(a.get("SubnetId") == subnet_id for a in rt.get("Associations", []))
        if not already:
            self.ec2.associate_route_table(RouteTableId=rt_id, SubnetId=subnet_id)

    def ensure_vpc_endpoints(self, vpc_id: str, private_route_table_ids: List[str]) -> List[str]:
        """Crea el VPC Endpoint de tipo Gateway hacia S3 (gratis) si
        `enable_s3_endpoint`, asociado a las route tables privadas. No crea
        interface endpoints (ECR/logs) -esos tienen coste y no están implementados."""
        if not self.spec.enable_s3_endpoint:
            return []

        existing = self._find_by_component(self.ec2.describe_vpc_endpoints, "VpcEndpointId", "vpce-s3")
        if existing:
            vpce_id = existing["VpcEndpointId"]
            self._record("vpce-s3", "vpce-s3", vpce_id)
            return [vpce_id]

        service_name = f"com.amazonaws.{self.spec.region}.s3"
        name = self._name("vpce-s3")
        resp = self.ec2.create_vpc_endpoint(
            VpcId=vpc_id,
            ServiceName=service_name,
            VpcEndpointType="Gateway",
            RouteTableIds=private_route_table_ids,
            TagSpecifications=self._tag_specs("vpc-endpoint", "vpce-s3", name),
        )
        vpce_id = resp["VpcEndpoint"]["VpcEndpointId"]
        self._record("vpce-s3", "vpce-s3", vpce_id)
        logger.info("[RED] VPC Endpoint S3 (gateway, gratis) creado: %s", vpce_id)
        return [vpce_id]

    def ensure_security_groups(self, vpc_id: str) -> Tuple[str, str]:
        """Crea (o reutiliza) los SG de gateway y workers, y sincroniza sus reglas
        de ingreso por diff (añade las que faltan, revoca las que sobran) según el
        spec actual. El de workers solo acepta tráfico por referencia al SG del
        gateway (SG->SG), nunca por CIDR. Devuelve (sg_gateway_id, sg_workers_id)."""
        gw_name = self._name("gateway")
        workers_name = self._name("workers")

        sg_gateway_id = self._ensure_security_group(vpc_id, "sg-gateway", gw_name)
        sg_workers_id = self._ensure_security_group(vpc_id, "sg-workers", workers_name)

        admin_cidrs = self.spec.admin_cidrs or ["0.0.0.0/0"]
        if admin_cidrs == ["0.0.0.0/0"]:
            logger.warning(
                "[RED] cidr_admin_ssh = 0.0.0.0/0: el puerto 22 del gateway queda abierto a "
                "toda internet. Restringir en producción."
            )
        public_cidrs = self.spec.public_cidrs or ["0.0.0.0/0"]

        gw_ingress = [{"proto": "tcp", "port": 22, "cidrs": admin_cidrs}]
        gw_ingress.append({"proto": "tcp", "port": 80, "cidrs": public_cidrs})
        if self.spec.tls_enabled:
            gw_ingress.append({"proto": "tcp", "port": 443, "cidrs": public_cidrs})
        if self.spec.expose_direct_ports:
            for port in self.spec.gateway_public_ports or []:
                gw_ingress.append({"proto": "tcp", "port": port, "cidrs": public_cidrs})
        self._sync_ingress_cidr_rules(sg_gateway_id, gw_ingress)
        self._sync_egress_open(sg_gateway_id)

        worker_ports = self.spec.worker_ports or []
        workers_sg_refs: List[Dict[str, Any]] = [{"proto": "tcp", "port": 22, "source_sg": sg_gateway_id}]
        for port in worker_ports:
            workers_sg_refs.append({"proto": "tcp", "port": port, "source_sg": sg_gateway_id})
            # Comunicación inter-nodo (tensor/pipeline parallel) para réplicas > 1: se
            # permite siempre por seguridad operativa; es un no-op si nunca se usa.
            workers_sg_refs.append({"proto": "tcp", "port": port, "source_sg": sg_workers_id})
        self._sync_ingress_sg_rules(sg_workers_id, workers_sg_refs)
        self._sync_egress_open(sg_workers_id)

        return sg_gateway_id, sg_workers_id

    def _ensure_security_group(self, vpc_id: str, component: str, name: str) -> str:
        existing = self._find_by_component(self.ec2.describe_security_groups, "GroupId", component)
        if existing:
            sg_id = existing["GroupId"]
            self._record(component, component, sg_id, parent_aws_id=vpc_id, attributes={"name": name})
            logger.info("[SKIP][RED] Security Group %s ya existe: %s", component, sg_id)
            return sg_id

        resp = self.ec2.create_security_group(
            GroupName=name,
            Description=f"Sooniverse {component} para {self.spec.client_id}/{self.spec.environment}",
            VpcId=vpc_id,
            TagSpecifications=self._tag_specs("security-group", component, name),
        )
        sg_id = resp["GroupId"]
        self._record(component, component, sg_id, parent_aws_id=vpc_id, attributes={"name": name})
        return sg_id

    def _sync_ingress_cidr_rules(self, sg_id: str, rules: List[Dict[str, Any]]) -> None:
        current = self.ec2.describe_security_groups(GroupIds=[sg_id])["SecurityGroups"][0]
        wanted = set()
        for rule in rules:
            for cidr in rule["cidrs"]:
                wanted.add((rule["proto"], rule["port"], "cidr", cidr))

        existing = set()
        for perm in current.get("IpPermissions", []):
            proto = perm.get("IpProtocol")
            port = perm.get("FromPort")
            for ip_range in perm.get("IpRanges", []):
                existing.add((proto, port, "cidr", ip_range["CidrIp"]))

        to_add = wanted - existing
        to_remove = existing - wanted
        for proto, port, _, cidr in to_add:
            self.ec2.authorize_security_group_ingress(
                GroupId=sg_id,
                IpPermissions=[{"IpProtocol": proto, "FromPort": port, "ToPort": port, "IpRanges": [{"CidrIp": cidr}]}],
            )
        for proto, port, _, cidr in to_remove:
            self.ec2.revoke_security_group_ingress(
                GroupId=sg_id,
                IpPermissions=[{"IpProtocol": proto, "FromPort": port, "ToPort": port, "IpRanges": [{"CidrIp": cidr}]}],
            )

    def _sync_ingress_sg_rules(self, sg_id: str, rules: List[Dict[str, Any]]) -> None:
        current = self.ec2.describe_security_groups(GroupIds=[sg_id])["SecurityGroups"][0]
        wanted = {(rule["proto"], rule["port"], rule["source_sg"]) for rule in rules}

        existing = set()
        for perm in current.get("IpPermissions", []):
            proto = perm.get("IpProtocol")
            port = perm.get("FromPort")
            for pair in perm.get("UserIdGroupPairs", []):
                existing.add((proto, port, pair["GroupId"]))

        to_add = wanted - existing
        to_remove = existing - wanted
        for proto, port, source_sg in to_add:
            self.ec2.authorize_security_group_ingress(
                GroupId=sg_id,
                IpPermissions=[{"IpProtocol": proto, "FromPort": port, "ToPort": port, "UserIdGroupPairs": [{"GroupId": source_sg}]}],
            )
        for proto, port, source_sg in to_remove:
            self.ec2.revoke_security_group_ingress(
                GroupId=sg_id,
                IpPermissions=[{"IpProtocol": proto, "FromPort": port, "ToPort": port, "UserIdGroupPairs": [{"GroupId": source_sg}]}],
            )

    def _sync_egress_open(self, sg_id: str) -> None:
        current = self.ec2.describe_security_groups(GroupIds=[sg_id])["SecurityGroups"][0]
        has_open_egress = any(
            perm.get("IpProtocol") == "-1" and any(r.get("CidrIp") == "0.0.0.0/0" for r in perm.get("IpRanges", []))
            for perm in current.get("IpPermissionsEgress", [])
        )
        if not has_open_egress:
            # Los SG en VPC nacen con egress "all/0.0.0.0/0" por defecto en AWS real;
            # esto es un no-op salvo en backends (p.ej. moto) que no lo precreen.
            try:
                self.ec2.authorize_security_group_egress(
                    GroupId=sg_id,
                    IpPermissions=[{"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}],
                )
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "InvalidPermission.Duplicate":
                    raise

    def _wait(self, waiter_name: str, timeout: int = 300, **kwargs: Any) -> None:
        waiter = self.ec2.get_waiter(waiter_name)
        max_attempts = max(1, timeout // 5)
        try:
            waiter.wait(**kwargs, WaiterConfig={"Delay": 5, "MaxAttempts": max_attempts})
        except WaiterError as exc:
            raise NetworkError(f"Timeout esperando '{waiter_name}' ({timeout}s): {exc}") from exc

    # -------------------------------------------------------------------
    # Orquestación
    # -------------------------------------------------------------------

    def provision(self, dry_run: bool = False) -> NetworkOutputs:
        """Orquesta el aprovisionamiento completo: VPC -> subredes -> IGW -> NAT ->
        route tables -> VPC endpoints -> Security Groups, en ese orden. Idempotente:
        cada paso reutiliza lo que ya exista para este deployment_id. Actualiza
        `deployment.status` a 'active' al terminar, o 'error' si algo falla."""
        if dry_run:
            logger.info("[RED] --dry-run: no se ejecuta ninguna llamada mutante a AWS.")
            return self.status()  # type: ignore[return-value]

        self.state.set_deployment_status(self.deployment_id, "creating")
        try:
            vpc_id = self.ensure_vpc()
            public_subnet_ids, private_subnet_ids = self.ensure_subnets(vpc_id)
            igw_id = self.ensure_internet_gateway(vpc_id)
            nat_ids, eip_ids = self.ensure_nat_gateways(public_subnet_ids)
            public_rt_id, private_rt_ids = self.ensure_route_tables(
                vpc_id, igw_id, public_subnet_ids, private_subnet_ids, nat_ids
            )
            self.ensure_vpc_endpoints(vpc_id, private_rt_ids)
            sg_gateway_id, sg_workers_id = self.ensure_security_groups(vpc_id)
        except Exception as exc:
            self.state.set_deployment_status(self.deployment_id, "error", error=str(exc))
            self.state.log_event(self.deployment_id, "network", "provision", "error", message=str(exc))
            raise

        self.state.set_deployment_status(self.deployment_id, "active")
        self.state.log_event(self.deployment_id, "network", "provision", "ok")

        azs = self._available_azs()
        return NetworkOutputs(
            deployment_id=self.deployment_id,
            vpc_id=vpc_id,
            vpc_name=self._name("vpc"),
            availability_zones=azs,
            public_subnet_ids=public_subnet_ids,
            private_subnet_ids=private_subnet_ids,
            internet_gateway_id=igw_id,
            nat_gateway_ids=nat_ids,
            elastic_ip_allocation_ids=eip_ids,
            public_route_table_id=public_rt_id,
            private_route_table_ids=private_rt_ids,
            sg_gateway_id=sg_gateway_id,
            sg_gateway_name=self._name("gateway"),
            sg_workers_id=sg_workers_id,
            sg_workers_name=self._name("workers"),
            managed_by_us=True,
        )

    def adopt_existing(self, vpc_name_or_id: str) -> NetworkOutputs:
        """Modo 'existente': adopta una VPC ya creada a mano. `managed_by_us=False`,
        el destroy jamás la tocará."""
        if vpc_name_or_id.startswith("vpc-"):
            vpcs = self.ec2.describe_vpcs(VpcIds=[vpc_name_or_id])["Vpcs"]
        else:
            vpcs = self.ec2.describe_vpcs(Filters=[{"Name": "tag:Name", "Values": [vpc_name_or_id]}])["Vpcs"]
        if not vpcs:
            raise NetworkError(f"No se encontró la VPC '{vpc_name_or_id}' para adoptar.")
        vpc = vpcs[0]
        vpc_id = vpc["VpcId"]

        subnets = self.ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]
        public_ids = [s["SubnetId"] for s in subnets if s.get("MapPublicIpOnLaunch")]
        private_ids = [s["SubnetId"] for s in subnets if not s.get("MapPublicIpOnLaunch")]

        return NetworkOutputs(
            deployment_id=self.deployment_id,
            vpc_id=vpc_id,
            vpc_name=vpc_name_or_id,
            availability_zones=sorted({s["AvailabilityZone"] for s in subnets}),
            public_subnet_ids=public_ids,
            private_subnet_ids=private_ids,
            internet_gateway_id=None,
            nat_gateway_ids=[],
            elastic_ip_allocation_ids=[],
            public_route_table_id=None,
            private_route_table_ids=[],
            sg_gateway_id="",
            sg_gateway_name=self.spec.extra_tags.get("security_group_gateway", "") if self.spec.extra_tags else "",
            sg_workers_id="",
            sg_workers_name=self.spec.extra_tags.get("security_group_workers", "") if self.spec.extra_tags else "",
            managed_by_us=False,
        )

    def status(self) -> Dict[str, Any]:
        """Snapshot de solo lectura: deployment_id + recursos registrados en el estado."""
        return {
            "deployment_id": self.deployment_id,
            "resources": self.state.list_resources(self.deployment_id),
        }

    # -------------------------------------------------------------------
    # Destrucción
    # -------------------------------------------------------------------

    def plan_destroy(self) -> List[PlannedDeletion]:
        """Lee el estado y devuelve la lista de recursos a borrar en orden (no
        muta nada; es lo que imprime `destroy(dry_run=True)`)."""
        resources = self.state.resources_in_delete_order(self.deployment_id)
        plan = []
        for res in resources:
            plan.append(
                PlannedDeletion(
                    resource_type=res["resource_type"],
                    component=res["component"],
                    aws_id=res.get("aws_id"),
                    name=(res.get("attributes") or {}).get("name"),
                    delete_order=res["delete_order"],
                    managed_by_us=res.get("managed_by_us", True),
                )
            )
        return plan

    def _tags_match_deployment(self, resource_type: str, aws_id: str) -> bool:
        """Segunda condición del mecanismo de propiedad: los tags AWS reales deben
        seguir apuntando a este deployment_id antes de borrar."""
        describers = {
            "vpc": (self.ec2.describe_vpcs, "VpcIds", "Vpcs"),
            "subnet-public": (self.ec2.describe_subnets, "SubnetIds", "Subnets"),
            "subnet-private": (self.ec2.describe_subnets, "SubnetIds", "Subnets"),
            "igw": (self.ec2.describe_internet_gateways, "InternetGatewayIds", "InternetGateways"),
            "nat": (self.ec2.describe_nat_gateways, "NatGatewayIds", "NatGateways"),
            "rtb-public": (self.ec2.describe_route_tables, "RouteTableIds", "RouteTables"),
            "rtb-private": (self.ec2.describe_route_tables, "RouteTableIds", "RouteTables"),
            "sg-gateway": (self.ec2.describe_security_groups, "GroupIds", "SecurityGroups"),
            "sg-workers": (self.ec2.describe_security_groups, "GroupIds", "SecurityGroups"),
            "vpce-s3": (self.ec2.describe_vpc_endpoints, "VpcEndpointIds", "VpcEndpoints"),
        }
        if resource_type == "eip":
            resp = self.ec2.describe_addresses(AllocationIds=[aws_id])
            items = resp.get("Addresses", [])
        else:
            fn, id_kwarg, list_key = describers.get(resource_type, (None, None, None))
            if fn is None:
                return False
            try:
                resp = fn(**{id_kwarg: [aws_id]})
            except ClientError:
                return False
            items = resp.get(list_key, [])

        if not items:
            return False
        tags = {t["Key"]: t["Value"] for t in items[0].get("Tags", [])}
        return tags.get(TAG_DEPLOYMENT) == self.deployment_id and tags.get(TAG_MANAGED) == "true"

    def destroy(self, dry_run: bool = False, force: bool = False) -> DestroyReport:
        """Borra cada recurso de `plan_destroy()` en orden, verificando ANTES de cada
        borrado que los tags AWS reales siguen coincidiendo con `deployment_id`
        (mecanismo de propiedad). Continúa aunque un recurso falle; nunca borra si
        `managed_by_us=False` salvo `force=True`. `dry_run=True` no muta nada."""
        plan = self.plan_destroy()
        report = DestroyReport(deployment_id=self.deployment_id)

        if dry_run:
            for item in plan:
                logger.info(
                    "[DESTROY] (dry-run) %s %s id=%s orden=%s managed_by_us=%s",
                    item.resource_type, item.component, item.aws_id, item.delete_order, item.managed_by_us,
                )
            return report

        self.state.set_deployment_status(self.deployment_id, "destroying")

        for item in plan:
            if not item.aws_id:
                continue
            if not item.managed_by_us and not force:
                report.skipped_not_ours.append(item)
                logger.warning("[DESTROY] Omitido (managed_by_us=False): %s %s", item.component, item.aws_id)
                continue
            if not self._tags_match_deployment(item.resource_type, item.aws_id):
                report.skipped_not_ours.append(item)
                logger.warning(
                    "[DESTROY] Omitido: los tags AWS de %s (%s) no coinciden con deployment_id=%s. "
                    "No se borra.", item.component, item.aws_id, self.deployment_id,
                )
                continue

            try:
                self._delete_one(item)
                self.state.mark_resource_state(self.deployment_id, item.aws_id, "deleted")
                report.succeeded.append(item)
            except ClientError as exc:
                code = exc.response["Error"]["Code"]
                self.state.mark_resource_state(self.deployment_id, item.aws_id, "error")
                report.failed.append({"item": item, "error": code, "message": str(exc)})
                vpc_id_hint = self._vpc_id_for_diagnostics(item)
                report.manual_actions_required.append(
                    f"Revisar manualmente {item.component} ({item.aws_id}): {code}. "
                    f"Comando de diagnóstico: aws ec2 describe-network-interfaces "
                    f"--filters Name=vpc-id,Values={vpc_id_hint} --region {self.spec.region}"
                )
                logger.error("[DESTROY] Fallo borrando %s %s: %s", item.component, item.aws_id, exc)

        if report.ok:
            self.state.set_deployment_status(self.deployment_id, "destroyed")
            self.state.close_deployment(self.deployment_id)
            self.state.log_event(self.deployment_id, "destroy", "destroy", "ok")
        else:
            self.state.set_deployment_status(self.deployment_id, "degraded", error="destroy parcial: ver DestroyReport")
            self.state.log_event(self.deployment_id, "destroy", "destroy", "warning", message="destroy parcial")

        return report

    def _delete_one(self, item: PlannedDeletion) -> None:
        aws_id = item.aws_id
        component = item.component

        if component in ("sg-gateway", "sg-workers"):
            self._revoke_all_sg_rules(aws_id)
            self.ec2.delete_security_group(GroupId=aws_id)
        elif component == "vpce-s3":
            self.ec2.delete_vpc_endpoints(VpcEndpointIds=[aws_id])
        elif component == "nat":
            self.ec2.delete_nat_gateway(NatGatewayId=aws_id)
            self._wait("nat_gateway_deleted", NatGatewayIds=[aws_id], timeout=self.spec.nat_timeout_seconds)
        elif component == "eip":
            self.ec2.release_address(AllocationId=aws_id)
        elif component in ("rtb-public", "rtb-private"):
            rt = self.ec2.describe_route_tables(RouteTableIds=[aws_id])["RouteTables"][0]
            for assoc in rt.get("Associations", []):
                if not assoc.get("Main") and assoc.get("RouteTableAssociationId"):
                    self.ec2.disassociate_route_table(AssociationId=assoc["RouteTableAssociationId"])
            self.ec2.delete_route_table(RouteTableId=aws_id)
        elif component == "igw":
            vpc_id = self._resource_vpc_id(item)
            if vpc_id:
                self.ec2.detach_internet_gateway(InternetGatewayId=aws_id, VpcId=vpc_id)
            self.ec2.delete_internet_gateway(InternetGatewayId=aws_id)
        elif component in ("subnet-public", "subnet-private"):
            self.ec2.delete_subnet(SubnetId=aws_id)
        elif component == "vpc":
            self._sweep_untracked_security_groups(aws_id)
            self.ec2.delete_vpc(VpcId=aws_id)
        else:
            raise NetworkError(f"Componente desconocido en destroy: {component}")

    def _resource_vpc_id(self, item: PlannedDeletion) -> Optional[str]:
        for res in self.state.list_resources(self.deployment_id):
            if res.get("aws_id") == item.aws_id:
                return res.get("parent_aws_id")
        return None

    def _vpc_id_for_diagnostics(self, item: PlannedDeletion) -> str:
        if item.component == "vpc":
            return item.aws_id
        return self._resource_vpc_id(item) or "<vpc-id-desconocida>"

    def _sweep_untracked_security_groups(self, vpc_id: str) -> None:
        """SkyPilot crea su propio Security Group ('sky-sg-*') por clúster además
        de los sooniverse-<cliente>-<entorno>-{gateway,workers} que gestionamos
        nosotros. Ese SG nunca se registra en infra_resource, así que el bucle
        principal de destroy() nunca lo intenta borrar -y bloquea DeleteVpc con
        DependencyViolation aunque los SG propios y todas las instancias ya estén
        fuera (confirmado en una corrida real: 'sky-sg-ifu-be3e' seguía vivo
        después de que sg-gateway, sg-workers y ambos clústeres SkyPilot se
        hubieran borrado sin error). El SG 'default' se deja: AWS lo borra solo
        al borrar la VPC y no se puede eliminar explícitamente."""
        try:
            sgs = self.ec2.describe_security_groups(
                Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
            )["SecurityGroups"]
        except ClientError as exc:
            logger.warning("[DESTROY] No se pudo listar Security Groups de %s: %s", vpc_id, exc)
            return

        for sg in sgs:
            if sg["GroupName"] == "default":
                continue
            sg_id = sg["GroupId"]
            try:
                self._revoke_all_sg_rules(sg_id)
                self.ec2.delete_security_group(GroupId=sg_id)
                logger.info(
                    "[DESTROY] Security Group no rastreado %s (%s) eliminado antes de borrar la VPC.",
                    sg_id, sg["GroupName"],
                )
            except ClientError as exc:
                logger.warning(
                    "[DESTROY] No se pudo eliminar el Security Group no rastreado %s (%s): %s",
                    sg_id, sg["GroupName"], exc,
                )

    def _revoke_all_sg_rules(self, sg_id: str) -> None:
        sg = self.ec2.describe_security_groups(GroupIds=[sg_id])["SecurityGroups"][0]
        if sg.get("IpPermissions"):
            self.ec2.revoke_security_group_ingress(GroupId=sg_id, IpPermissions=sg["IpPermissions"])
        if sg.get("IpPermissionsEgress"):
            self.ec2.revoke_security_group_egress(GroupId=sg_id, IpPermissions=sg["IpPermissionsEgress"])

    def scan_orphans(self) -> List[Dict[str, Any]]:
        """Busca recursos con tag sooniverse:managed=true en la región que NO estén
        en el estado (o cuyo deployment_id ya no exista/esté destruido)."""
        known_ids = {r.get("aws_id") for r in self.state.list_resources(self.deployment_id)}
        orphans: List[Dict[str, Any]] = []

        checks = [
            (self.ec2.describe_vpcs, "Vpcs", "VpcId"),
            (self.ec2.describe_subnets, "Subnets", "SubnetId"),
            (self.ec2.describe_internet_gateways, "InternetGateways", "InternetGatewayId"),
            (self.ec2.describe_nat_gateways, "NatGateways", "NatGatewayId"),
            (self.ec2.describe_security_groups, "SecurityGroups", "GroupId"),
        ]
        for fn, list_key, id_key in checks:
            resp = fn(Filters=[{"Name": f"tag:{TAG_MANAGED}", "Values": ["true"]}])
            for item in resp.get(list_key, []):
                aws_id = item.get(id_key)
                if aws_id and aws_id not in known_ids:
                    tags = {t["Key"]: t["Value"] for t in item.get("Tags", [])}
                    orphans.append({"aws_id": aws_id, "type": list_key, "tags": tags})

        addresses = self.ec2.describe_addresses(Filters=[{"Name": f"tag:{TAG_MANAGED}", "Values": ["true"]}])
        for addr in addresses.get("Addresses", []):
            alloc_id = addr.get("AllocationId")
            if alloc_id and alloc_id not in known_ids:
                tags = {t["Key"]: t["Value"] for t in addr.get("Tags", [])}
                orphans.append({"aws_id": alloc_id, "type": "Addresses", "tags": tags})

        return orphans
