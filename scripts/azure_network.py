#!/usr/bin/env python3
"""
==============================================================================
Sooniverse Infra - Gestor de red Azure (modo "hosted", cuenta propia)
==============================================================================
Equivalente Azure de `scripts/aws_network.py::AwsNetworkManager`: mismo
contrato público (`provision()`, `destroy()`, `plan_destroy()`, `scan_orphans()`),
misma máquina de fases en `scripts/generate_infra.py` (`deploy()`), mismos
comandos de CLI -la nube se elige con `red_y_aislamiento.cloud: "azure"` en el
contrato, no con un flag nuevo. AwsNetworkManager NO se modifica ni se importa
desde aquí (este módulo debe poder cargarse sin tener boto3 instalado).

Mapeo de recursos AWS -> Azure:
    VPC                     -> Virtual Network (VNet)
    (sin equivalente AWS)   -> Resource Group (contenedor nuevo de todo lo demás)
    Subred pública/privada  -> Subnet dentro del VNet
    Internet Gateway        -> ruta de salida implícita (system route), sin
                               recurso explícito -Azure la da gratis a
                               cualquier subred sin NAT Gateway asociado.
    NAT Gateway + EIP       -> NAT Gateway + Public IP (SKU Standard),
                               asociado DIRECTAMENTE a la subred privada (no
                               hace falta una Route Table separada: a
                               diferencia de AWS, el NAT Gateway de Azure se
                               asocia a nivel de subred y esa asociación ES la
                               ruta 0.0.0.0/0 -> NAT).
    Security Group          -> Network Security Group (NSG)
    EIP del Gateway (dominio)-> Public IP persistente (NO implementado en esta
                               fase: dominio propio + TLS real sigue "solo AWS
                               por ahora", ver README.md/ConfigValidator).

Diferencia real de arquitectura frente a AWS -SG->SG no tiene equivalente 1:1
en NSG-: AWS permite que el SG de los workers solo acepte tráfico "desde el
SG del gateway" (referencia por grupo, sin importar la IP). Azure NSG no
soporta referenciar OTRO NSG como origen de una regla -el mecanismo real
equivalente serían Application Security Groups (ASG) asociados al NIC de cada
VM, pero eso requiere que SkyPilot adjunte el NIC del Gateway a un ASG al
crearlo, y no hay forma de confirmar hoy (sin una suscripción real) si el
backend Azure de SkyPilot expone esa opción. Para no fabricar un
comportamiento no verificado, esta primera versión restringe el ingreso del
NSG de workers por CIDR de la SUBRED PÚBLICA (donde SOLO vive el Gateway, un
único nodo) en vez de por ASG -mismo efecto práctico para esta topología
(nadie más que el Gateway está en esa subred), pero técnicamente es una regla
por rango de IP, no una referencia de grupo. Documentar como candidato a
reforzar con ASG una vez validado contra una suscripción real.

Autenticación: Service Principal (`ClientSecretCredential`) leído de las
variables de entorno estándar de Azure (`AZURE_TENANT_ID`, `AZURE_CLIENT_ID`,
`AZURE_CLIENT_SECRET`, `AZURE_SUBSCRIPTION_ID`) -ver `.env.example`. A
diferencia de AWS no existe hoy un "azure_profile" nombrado (aws_profile es
Fase 6 de aislamiento multi-cliente); para modo 'hosted' con una sola
suscripción de Sooniverse esto basta. Diseño diferido a cuando se implemente
BYOC en Azure: un Service Principal/tenant por cliente, análogo a
`red_y_aislamiento.aws_profile`.

Mecanismo de propiedad: idéntico al de AWS (ver PROMPT_CLAUDE_CODE_sooniverse_red.md
y `aws_network.py`) -un recurso solo se borra si (a) está registrado en
`InfraStateStore` con el `deployment_id` correspondiente Y (b) sus tags Azure
reales siguen coincidiendo con ese mismo `deployment_id`.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    from azure.core.exceptions import ResourceNotFoundError, HttpResponseError
    from azure.identity import ClientSecretCredential
    from azure.mgmt.network import NetworkManagementClient
    from azure.mgmt.resource.resources import ResourceManagementClient
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "Falta el SDK de Azure. Instala con: "
        "pip install azure-identity azure-mgmt-network azure-mgmt-resource"
    ) from exc

from infra_state import InfraStateStore, InMemoryInfraStateStore  # type: ignore

logger = logging.getLogger("sooniverse.azure_network")

# Mismo esquema de tags que aws_network.py (no se importa de ahí para no forzar
# boto3 en un entorno solo-Azure; mantener ambas listas de constantes en sync
# si se cambia el prefijo).
TAG_PREFIX = "sooniverse"
TAG_MANAGED = f"{TAG_PREFIX}:managed"
TAG_CLIENT = f"{TAG_PREFIX}:client-id"
TAG_ENV = f"{TAG_PREFIX}:environment"
TAG_DEPLOYMENT = f"{TAG_PREFIX}:deployment-id"
TAG_COMPONENT = f"{TAG_PREFIX}:component"
TAG_CREATED_AT = f"{TAG_PREFIX}:created-at"

# Orden de borrado (inverso de la creación). Números más bajos se borran antes.
# A diferencia de AWS no hay route tables/IGW explícitos que borrar (ver
# docstring del módulo).
DELETE_ORDER = {
    "subnet-private": 10,
    "subnet-public": 11,
    "natgw": 20,
    "pip-nat": 21,
    "nsg-workers": 30,
    "nsg-gateway": 31,
    "vnet": 40,
    "resource-group": 90,
}

_CREATE_ORDER_COMPONENTS = [
    "resource-group",
    "vnet",
    "nsg-gateway",
    "nsg-workers",
    "pip-nat",
    "natgw",
    "subnet-public",
    "subnet-private",
]


class AzureNetworkError(Exception):
    """Error de aprovisionamiento o destrucción de red en Azure."""


@dataclass(frozen=True)
class AzureNetworkSpec:
    """Entrada declarativa de `AzureNetworkManager`, análoga a `aws_network.NetworkSpec`.
    Se construye típicamente con `generate_infra.build_network_spec_from_config()`."""

    client_id: str
    environment: str
    region: str  # slug de Azure, ej. "eastus" -no confundir con regiones AWS.
    vnet_cidr: str
    az_count: int = 1  # Azure no tiene subredes zonales; >1 solo genera un WARNING (ver ensure_subnets).
    public_subnet_cidrs: Optional[List[str]] = None
    private_subnet_cidrs: Optional[List[str]] = None
    nat_mode: str = "single"  # "single" | "none" -Azure no tiene un equivalente limpio a "per-az" (ver ConfigValidator)
    admin_cidrs: Optional[List[str]] = None
    public_cidrs: Optional[List[str]] = None
    gateway_public_ports: Optional[List[int]] = None
    worker_ports: Optional[List[int]] = None
    expose_direct_ports: bool = False
    tls_enabled: bool = False
    extra_tags: Optional[Dict[str, str]] = None
    subscription_id: Optional[str] = None  # None => AZURE_SUBSCRIPTION_ID del entorno

    def __post_init__(self) -> None:
        if self.nat_mode not in ("single", "none"):
            raise AzureNetworkError(
                f"nat_mode inválido para Azure: {self.nat_mode!r}. Permitidos: 'single', 'none' "
                "('per-az' no tiene mapeo directo a NAT Gateway de Azure en esta versión)."
            )
        normalized = self.client_id.lower()
        if normalized != self.client_id or len(self.client_id) > 20:
            raise AzureNetworkError(
                f"client_id '{self.client_id}' inválido: debe ser minúsculas, [a-z0-9-], máx 20 caracteres."
            )


@dataclass(frozen=True)
class AzureNetworkOutputs:
    """Resultado de `AzureNetworkManager.provision()`: los IDs/nombres reales que
    `TopologyBuilder` necesita para construir la config de cliente SkyPilot
    (`azure.resource_group`, equivalentes a `aws.vpc_name`/`security_group_name`)."""

    deployment_id: str
    resource_group_name: str
    vnet_id: str
    vnet_name: str
    public_subnet_id: str
    private_subnet_id: str
    nat_gateway_id: Optional[str]
    nsg_gateway_id: str
    nsg_gateway_name: str
    nsg_workers_id: str
    nsg_workers_name: str
    managed_by_us: bool = True


@dataclass
class PlannedDeletion:
    resource_type: str
    component: str
    azure_id: Optional[str]
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
    subred de cada tipo -Azure no tiene el concepto de "subred por AZ" que sí
    tiene AWS (ver AzureNetworkSpec.az_count)."""
    vnet_net = ipaddress.ip_network(vnet_cidr, strict=True)
    subnet_prefix = max(vnet_net.prefixlen + 4, 20)
    all_subnets = list(vnet_net.subnets(new_prefix=subnet_prefix))
    if len(all_subnets) < 2:
        raise AzureNetworkError(
            f"'vnet_cidr' {vnet_cidr} no tiene espacio para 2 subredes /{subnet_prefix}."
        )
    half = len(all_subnets) // 2
    return str(all_subnets[0]), str(all_subnets[half])


def _default_credential(subscription_id: Optional[str] = None) -> Tuple["ClientSecretCredential", str]:
    tenant_id = os.environ.get("AZURE_TENANT_ID")
    client_id = os.environ.get("AZURE_CLIENT_ID")
    client_secret = os.environ.get("AZURE_CLIENT_SECRET")
    sub_id = subscription_id or os.environ.get("AZURE_SUBSCRIPTION_ID")
    if not (tenant_id and client_id and client_secret and sub_id):
        raise AzureNetworkError(
            "Faltan credenciales de Azure. Define AZURE_TENANT_ID, AZURE_CLIENT_ID, "
            "AZURE_CLIENT_SECRET y AZURE_SUBSCRIPTION_ID en .env (ver .env.example)."
        )
    return ClientSecretCredential(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret), sub_id


class AzureNetworkManager:
    """Ciclo de vida completo de la capa de red Azure para un despliegue Sooniverse.
    Interfaz pública espejo de `aws_network.AwsNetworkManager` (ver docstring del módulo)."""

    def __init__(
        self,
        spec: AzureNetworkSpec,
        state: Optional[InfraStateStore] = None,
        credential: Optional[Any] = None,
        subscription_id: Optional[str] = None,
        deployment_id: Optional[str] = None,
    ) -> None:
        self.spec = spec
        self.state = state if state is not None else InMemoryInfraStateStore()

        if credential is not None and subscription_id:
            self._credential = credential
            self._subscription_id = subscription_id
        else:
            self._credential, self._subscription_id = _default_credential(spec.subscription_id)

        self.resource_client = ResourceManagementClient(self._credential, self._subscription_id)
        self.network_client = NetworkManagementClient(self._credential, self._subscription_id)

        if deployment_id:
            self.deployment_id = deployment_id
        else:
            self.deployment_id = self.state.open_deployment(
                client_id=spec.client_id,
                environment=spec.environment,
                region=spec.region,
                cloud="azure",
            )

        self._log_prefix = "[RED-AZURE]"

    # -------------------------------------------------------------------
    # Utilidades internas
    # -------------------------------------------------------------------

    def _name(self, component: str, suffix: str = "") -> str:
        base = f"sooniverse-{self.spec.client_id}-{self.spec.environment}-{component}"
        return f"{base}-{suffix}" if suffix else base

    @property
    def resource_group_name(self) -> str:
        return self._name("rg")

    def _tags(self, component: str) -> Dict[str, str]:
        tags = {
            TAG_MANAGED: "true",
            TAG_CLIENT: self.spec.client_id,
            TAG_ENV: self.spec.environment,
            TAG_DEPLOYMENT: self.deployment_id,
            TAG_COMPONENT: component,
            TAG_CREATED_AT: _now_iso(),
        }
        for key, value in (self.spec.extra_tags or {}).items():
            if key.startswith(f"{TAG_PREFIX}:"):
                raise AzureNetworkError(
                    f"'extra_tags' no puede usar el prefijo reservado '{TAG_PREFIX}:' (clave: {key})"
                )
            tags.setdefault(key, value)
        return tags

    def _record(self, resource_type: str, component: str, azure_id: str, **extra: Any) -> None:
        fields = {
            "resource_type": resource_type,
            "component": component,
            # Campo reutilizado del protocolo InfraStateStore (nombrado 'aws_id'
            # por historia, pero genérico: guarda el resource ID de CUALQUIER nube).
            "aws_id": azure_id,
            "region": self.spec.region,
            "delete_order": DELETE_ORDER.get(component, 999),
            "managed_by_us": True,
            "state": "active",
        }
        fields.update(extra)
        self.state.record_resource(self.deployment_id, **fields)

    def _find_existing(
        self,
        component: str,
        rg_name: Optional[str] = None,
        vnet_name: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Busca un recurso ya registrado para este deployment_id + component.

        Primero mira el ESTADO (Postgres) -rápido, y es la fuente de verdad
        para la relación 1:1 con deployment_id (la que necesitan
        destroy()/scan_orphans() para decidir qué es "nuestro"). CORREGIDO: si
        el estado no tiene el recurso (BD perdida/reiniciada, o esta es una
        corrida de recuperación), se consulta Azure DIRECTAMENTE por el
        nombre determinista -todos los nombres de este módulo son
        'sooniverse-<cliente>-<entorno>-<componente>', sin deployment_id- y,
        si existe, se auto-registra en el estado bajo el deployment_id ACTUAL
        antes de devolverlo. Sin esto, un estado perdido no duplicaba el
        recurso Azure en sí (el PUT de ARM ya es idempotente por nombre),
        pero sí dejaba una fila de estado huérfana del deployment_id viejo y
        forzaba un create_or_update evitable en cada `ensure_*` -equivalente
        funcional del `_find_by_component()` de aws_network.py, que sí
        consulta AWS en vivo porque `create_vpc()`/etc. NO son idempotentes
        por nombre."""
        for res in self.state.list_resources(self.deployment_id):
            if res.get("component") == component:
                return res

        azure_id, name = self._lookup_live(component, rg_name, vnet_name)
        if azure_id is None:
            return None
        logger.info(
            "[RED-AZURE] %s no estaba en el estado pero SÍ existe en Azure (%s); "
            "se adopta bajo deployment_id=%s.",
            component, name, self.deployment_id,
        )
        self._record(component, component, azure_id, attributes={"name": name})
        return {"aws_id": azure_id, "component": component, "attributes": {"name": name}}

    def _lookup_live(
        self,
        component: str,
        rg_name: Optional[str],
        vnet_name: Optional[str],
    ) -> Tuple[Optional[str], Optional[str]]:
        """GET directo a Azure por el nombre determinista del componente (ver
        `_find_existing`). Devuelve (azure_id, name) o (None, None) si no
        existe o si falta el `rg_name`/`vnet_name` necesario para el GET."""
        try:
            if component == "resource-group":
                # Convención de este módulo: el 'azure_id' registrado para
                # resource-group es el NOMBRE desnudo (ver ensure_resource_group,
                # que devuelve 'name' y no el resourceId completo de ARM) -aquí
                # se respeta esa convención en vez del rg.id que devolvería el SDK.
                name = self.resource_group_name
                self.resource_client.resource_groups.get(name)
                return name, name
            if not rg_name:
                return None, None
            if component == "vnet":
                name = self._name("vnet")
                vnet = self.network_client.virtual_networks.get(rg_name, name)
                return vnet.id, name
            if component in ("nsg-gateway", "nsg-workers"):
                name = self._name(component.replace("nsg-", ""))
                nsg = self.network_client.network_security_groups.get(rg_name, name)
                return nsg.id, name
            if component == "pip-nat":
                name = self._name("pip-nat")
                pip = self.network_client.public_ip_addresses.get(rg_name, name)
                return pip.id, name
            if component == "natgw":
                name = self._name("natgw")
                nat = self.network_client.nat_gateways.get(rg_name, name)
                return nat.id, name
            if component in ("subnet-public", "subnet-private") and vnet_name:
                name = component.replace("subnet-", "subred-")
                subnet = self.network_client.subnets.get(rg_name, vnet_name, name)
                return subnet.id, name
        except ResourceNotFoundError:
            return None, None
        return None, None

    # -------------------------------------------------------------------
    # ensure_* (idempotentes)
    # -------------------------------------------------------------------

    def ensure_resource_group(self) -> str:
        name = self.resource_group_name
        existing = self._find_existing("resource-group")
        if existing:
            logger.info("[SKIP][RED-AZURE] Resource Group ya registrado: %s", name)
            return name

        self.resource_client.resource_groups.create_or_update(
            name, {"location": self.spec.region, "tags": self._tags("resource-group")}
        )
        self._record("resource-group", "resource-group", name, attributes={"name": name})
        logger.info("[RED-AZURE] Resource Group creado: %s", name)
        return name

    def ensure_vnet(self, rg_name: str) -> Tuple[str, str]:
        """Devuelve (vnet_id, vnet_name)."""
        name = self._name("vnet")
        existing = self._find_existing("vnet", rg_name=rg_name)
        if existing:
            logger.info("[SKIP][RED-AZURE] VNet ya registrada: %s", name)
            return existing["aws_id"], name

        poller = self.network_client.virtual_networks.begin_create_or_update(
            rg_name,
            name,
            {
                "location": self.spec.region,
                "address_space": {"address_prefixes": [self.spec.vnet_cidr]},
                "tags": self._tags("vnet"),
            },
        )
        vnet = poller.result()
        self._record("vnet", "vnet", vnet.id, parent_aws_id=rg_name, attributes={"name": name})
        logger.info("[RED-AZURE] VNet creada: %s", vnet.id)
        return vnet.id, name

    def _resolve_subnet_cidrs(self) -> Tuple[str, str]:
        """(cidr_publico, cidr_privado) reales de ESTE despliegue: honra los
        CIDR explícitos de 'red_y_aislamiento.subredes.publicas/privadas' si
        se declararon, igual que `ensure_subnets()`. CORREGIDO: antes
        `ensure_security_groups()` recalculaba SIEMPRE con
        `compute_subnet_cidrs(vnet_cidr)` sin mirar los CIDR explícitos -si el
        operador los fijaba a mano, las reglas NSG de los workers acababan
        permitiendo el CIDR equivocado (el calculado, no el real de la subred
        pública donde vive el Gateway), bloqueando en silencio el tráfico
        gateway -> worker. Único punto de verdad para que ambos métodos no
        puedan volver a divergir."""
        if self.spec.public_subnet_cidrs and self.spec.private_subnet_cidrs:
            return self.spec.public_subnet_cidrs[0], self.spec.private_subnet_cidrs[0]
        return compute_subnet_cidrs(self.spec.vnet_cidr)

    def ensure_security_groups(self, rg_name: str) -> Tuple[str, str]:
        """Crea (o reutiliza) los NSG de gateway y workers, y sincroniza sus reglas
        de ingreso por diff. El de workers restringe el origen al CIDR de la
        subred pública (ver docstring del módulo sobre por qué no es una
        referencia de grupo como en AWS). Devuelve (nsg_gateway_id, nsg_workers_id)."""
        nsg_gateway_id, gw_name = self._ensure_nsg(rg_name, "nsg-gateway")
        nsg_workers_id, workers_name = self._ensure_nsg(rg_name, "nsg-workers")

        admin_cidrs = self.spec.admin_cidrs or ["0.0.0.0/0"]
        if admin_cidrs == ["0.0.0.0/0"]:
            logger.warning(
                "[RED-AZURE] cidr_admin_ssh = 0.0.0.0/0: el puerto 22 del gateway queda abierto "
                "a toda internet. Restringir en producción."
            )
        public_cidrs = self.spec.public_cidrs or ["0.0.0.0/0"]

        gw_rules: List[Dict[str, Any]] = [{"port": 22, "cidrs": admin_cidrs}]
        gw_rules.append({"port": 80, "cidrs": public_cidrs})
        if self.spec.tls_enabled:
            gw_rules.append({"port": 443, "cidrs": public_cidrs})
        if self.spec.expose_direct_ports:
            for port in self.spec.gateway_public_ports or []:
                gw_rules.append({"port": port, "cidrs": public_cidrs})
        self._sync_nsg_rules(rg_name, gw_name, gw_rules)

        public_cidr, private_cidr = self._resolve_subnet_cidrs()
        worker_ports = self.spec.worker_ports or []
        worker_rules: List[Dict[str, Any]] = [{"port": 22, "cidrs": [public_cidr]}]
        for port in worker_ports:
            worker_rules.append({"port": port, "cidrs": [public_cidr]})
            # Comunicación inter-nodo (tensor/pipeline parallel) para réplicas > 1,
            # equivalente al SG-workers -> SG-workers de AWS.
            worker_rules.append({"port": port, "cidrs": [private_cidr]})
        self._sync_nsg_rules(rg_name, workers_name, worker_rules)

        return nsg_gateway_id, nsg_workers_id

    def _ensure_nsg(self, rg_name: str, component: str) -> Tuple[str, str]:
        name = self._name(component.replace("nsg-", ""))
        existing = self._find_existing(component, rg_name=rg_name)
        if existing:
            logger.info("[SKIP][RED-AZURE] NSG %s ya registrado: %s", component, name)
            return existing["aws_id"], name

        poller = self.network_client.network_security_groups.begin_create_or_update(
            rg_name, name, {"location": self.spec.region, "tags": self._tags(component)}
        )
        nsg = poller.result()
        self._record(component, component, nsg.id, parent_aws_id=rg_name, attributes={"name": name})
        return nsg.id, name

    def _sync_nsg_rules(self, rg_name: str, nsg_name: str, rules: List[Dict[str, Any]]) -> None:
        """Diff idempotente de security_rules custom del NSG (las reglas 'Default*'
        del sistema no aparecen en security_rules.list, así que no se tocan)."""
        current = list(self.network_client.security_rules.list(rg_name, nsg_name))
        current_by_name = {r.name: r for r in current}

        # CORREGIDO: la prioridad se calculaba como 100 + idx*10 + cidr_idx, que
        # colisiona en cuanto una regla trae 10 o más CIDR (cidr_idx=10 en la
        # regla idx=0 da la misma prioridad -110- que cidr_idx=0 en idx=1);
        # Azure exige prioridad ÚNICA por NSG y rechaza el create/update con un
        # error de "prioridad duplicada". Un contador monotónico sobre el par
        # (idx, cidr_idx) aplanado es única por construcción sin importar
        # cuántos CIDR traiga cada regla.
        wanted: Dict[str, Dict[str, Any]] = {}
        priority = 100
        for idx, rule in enumerate(rules):
            for cidr_idx, cidr in enumerate(rule["cidrs"]):
                rule_name = f"allow-{rule['port']}-{idx}-{cidr_idx}"
                wanted[rule_name] = {
                    "protocol": "Tcp",
                    "source_port_range": "*",
                    "destination_port_range": str(rule["port"]),
                    "source_address_prefix": cidr,
                    "destination_address_prefix": "*",
                    "access": "Allow",
                    "direction": "Inbound",
                    "priority": priority,
                }
                priority += 1

        for rule_name, body in wanted.items():
            existing_rule = current_by_name.get(rule_name)
            if existing_rule and (
                existing_rule.source_address_prefix == body["source_address_prefix"]
                and existing_rule.destination_port_range == body["destination_port_range"]
            ):
                continue
            self.network_client.security_rules.begin_create_or_update(
                rg_name, nsg_name, rule_name, body
            ).result()

        for rule_name in current_by_name:
            if rule_name not in wanted:
                self.network_client.security_rules.begin_delete(rg_name, nsg_name, rule_name).result()

    def ensure_nat_gateway(self, rg_name: str) -> Optional[str]:
        """Crea (o reutiliza) una Public IP Standard + NAT Gateway. `nat_mode: none`
        no crea nada (los workers deben usar `vpc_endpoints`-equivalente o no
        necesitan salida a internet). Devuelve el ID del NAT Gateway o None."""
        if self.spec.nat_mode == "none":
            return None

        existing_nat = self._find_existing("natgw", rg_name=rg_name)
        if existing_nat:
            logger.info("[SKIP][RED-AZURE] NAT Gateway ya registrado.")
            return existing_nat["aws_id"]

        pip_name = self._name("pip-nat")
        pip_poller = self.network_client.public_ip_addresses.begin_create_or_update(
            rg_name,
            pip_name,
            {
                "location": self.spec.region,
                "sku": {"name": "Standard"},
                "public_ip_allocation_method": "Static",
                "tags": self._tags("pip-nat"),
            },
        )
        pip = pip_poller.result()
        self._record("pip-nat", "pip-nat", pip.id, parent_aws_id=rg_name, attributes={"name": pip_name})

        nat_name = self._name("natgw")
        t0 = time.monotonic()
        nat_poller = self.network_client.nat_gateways.begin_create_or_update(
            rg_name,
            nat_name,
            {
                "location": self.spec.region,
                "sku": {"name": "Standard"},
                "public_ip_addresses": [{"id": pip.id}],
                "tags": self._tags("natgw"),
            },
        )
        nat = nat_poller.result()
        self._record("natgw", "natgw", nat.id, parent_aws_id=rg_name, attributes={"name": nat_name})
        logger.info("[RED-AZURE] NAT Gateway disponible: %s (%.1fs)", nat.id, time.monotonic() - t0)
        return nat.id

    def ensure_subnets(
        self, rg_name: str, vnet_name: str, nsg_gateway_id: str, nsg_workers_id: str, nat_gateway_id: Optional[str]
    ) -> Tuple[str, str]:
        """Crea (o reutiliza) UNA subred pública y UNA privada (Azure no tiene
        subredes zonales; ver AzureNetworkSpec.az_count). La privada asocia el
        NAT Gateway directamente -esa asociación ES la ruta 0.0.0.0/0 -> NAT, no
        hace falta una Route Table separada como en AWS."""
        if self.spec.az_count > 1:
            logger.warning(
                "[RED-AZURE] azs=%s ignorado: las subredes de Azure no son zonales, "
                "se crea una única subred pública y una privada.",
                self.spec.az_count,
            )

        public_cidr, private_cidr = self._resolve_subnet_cidrs()

        public_id = self._ensure_one_subnet(
            rg_name, vnet_name, "subnet-public", public_cidr, nsg_gateway_id, nat_gateway_id=None
        )
        private_id = self._ensure_one_subnet(
            rg_name, vnet_name, "subnet-private", private_cidr, nsg_workers_id, nat_gateway_id=nat_gateway_id
        )
        return public_id, private_id

    def _ensure_one_subnet(
        self,
        rg_name: str,
        vnet_name: str,
        component: str,
        cidr: str,
        nsg_id: str,
        nat_gateway_id: Optional[str],
    ) -> str:
        name = component.replace("subnet-", "subred-")
        existing = self._find_existing(component, rg_name=rg_name, vnet_name=vnet_name)
        if existing:
            logger.info("[SKIP][RED-AZURE] Subred %s ya registrada.", component)
            return existing["aws_id"]

        body: Dict[str, Any] = {
            "address_prefix": cidr,
            "network_security_group": {"id": nsg_id},
        }
        if nat_gateway_id:
            body["nat_gateway"] = {"id": nat_gateway_id}

        poller = self.network_client.subnets.begin_create_or_update(rg_name, vnet_name, name, body)
        subnet = poller.result()
        self._record(component, component, subnet.id, parent_aws_id=vnet_name, attributes={"name": name})
        logger.info("[RED-AZURE] Subred %s (%s) creada: %s", component, cidr, subnet.id)
        return subnet.id

    # -------------------------------------------------------------------
    # Orquestación
    # -------------------------------------------------------------------

    def provision(self, dry_run: bool = False) -> AzureNetworkOutputs:
        """Orquesta el aprovisionamiento completo: Resource Group -> VNet ->
        Security Groups -> NAT Gateway -> subredes, en ese orden. Idempotente:
        cada paso reutiliza lo que ya exista para este deployment_id."""
        if dry_run:
            logger.info("[RED-AZURE] --dry-run: no se ejecuta ninguna llamada mutante a Azure.")
            return self.status()  # type: ignore[return-value]

        self.state.set_deployment_status(self.deployment_id, "creating")
        try:
            rg_name = self.ensure_resource_group()
            vnet_id, vnet_name = self.ensure_vnet(rg_name)
            nsg_gateway_id, nsg_workers_id = self.ensure_security_groups(rg_name)
            nat_gateway_id = self.ensure_nat_gateway(rg_name)
            public_subnet_id, private_subnet_id = self.ensure_subnets(
                rg_name, vnet_name, nsg_gateway_id, nsg_workers_id, nat_gateway_id
            )
        except Exception as exc:
            self.state.set_deployment_status(self.deployment_id, "error", error=str(exc))
            self.state.log_event(self.deployment_id, "network", "provision", "error", message=str(exc))
            raise

        self.state.set_deployment_status(self.deployment_id, "active")
        self.state.log_event(self.deployment_id, "network", "provision", "ok")

        return AzureNetworkOutputs(
            deployment_id=self.deployment_id,
            resource_group_name=rg_name,
            vnet_id=vnet_id,
            vnet_name=vnet_name,
            public_subnet_id=public_subnet_id,
            private_subnet_id=private_subnet_id,
            nat_gateway_id=nat_gateway_id,
            nsg_gateway_id=nsg_gateway_id,
            nsg_gateway_name=self._name("gateway"),
            nsg_workers_id=nsg_workers_id,
            nsg_workers_name=self._name("workers"),
            managed_by_us=True,
        )

    def status(self) -> Dict[str, Any]:
        return {
            "deployment_id": self.deployment_id,
            "resources": self.state.list_resources(self.deployment_id),
        }

    # -------------------------------------------------------------------
    # Destrucción
    # -------------------------------------------------------------------

    def plan_destroy(self) -> List[PlannedDeletion]:
        resources = self.state.resources_in_delete_order(self.deployment_id)
        return [
            PlannedDeletion(
                resource_type=res["resource_type"],
                component=res["component"],
                azure_id=res.get("aws_id"),
                name=(res.get("attributes") or {}).get("name"),
                delete_order=res["delete_order"],
                managed_by_us=res.get("managed_by_us", True),
                attributes=res.get("attributes"),
            )
            for res in resources
        ]

    def _tags_match_deployment(self, component: str, azure_id: str, parent_rg: Optional[str]) -> Optional[bool]:
        """Segunda condición del mecanismo de propiedad. Devuelve None si el
        recurso ya no existe (nada que borrar), False si existe pero sus tags
        apuntan a otro deployment_id (no tocar), True si coincide."""
        rg_name = self.resource_group_name
        try:
            if component == "resource-group":
                obj = self.resource_client.resource_groups.get(rg_name)
            elif component == "vnet":
                obj = self.network_client.virtual_networks.get(rg_name, self._name("vnet"))
            elif component in ("subnet-public", "subnet-private"):
                # Las subredes de Azure no tienen tags propios -heredan la
                # propiedad de su VNet padre. Si el VNet sigue siendo nuestro,
                # la subred también lo es.
                return self._tags_match_deployment("vnet", azure_id, parent_rg)
            elif component == "nsg-gateway":
                obj = self.network_client.network_security_groups.get(rg_name, self._name("gateway"))
            elif component == "nsg-workers":
                obj = self.network_client.network_security_groups.get(rg_name, self._name("workers"))
            elif component == "natgw":
                obj = self.network_client.nat_gateways.get(rg_name, self._name("natgw"))
            elif component == "pip-nat":
                obj = self.network_client.public_ip_addresses.get(rg_name, self._name("pip-nat"))
            else:
                return False
        except ResourceNotFoundError:
            return None

        tags = obj.tags or {}
        return tags.get(TAG_DEPLOYMENT) == self.deployment_id and tags.get(TAG_MANAGED) == "true"

    def destroy(self, dry_run: bool = False, force: bool = False) -> DestroyReport:
        plan = self.plan_destroy()
        report = DestroyReport(deployment_id=self.deployment_id)

        if dry_run:
            for item in plan:
                logger.info(
                    "[DESTROY-AZURE] (dry-run) %s %s id=%s orden=%s managed_by_us=%s",
                    item.resource_type, item.component, item.azure_id, item.delete_order, item.managed_by_us,
                )
            return report

        self.state.set_deployment_status(self.deployment_id, "destroying")
        rg_name = self.resource_group_name

        for item in plan:
            if not item.azure_id:
                continue
            if not item.managed_by_us and not force:
                report.skipped_not_ours.append(item)
                logger.warning("[DESTROY-AZURE] Omitido (managed_by_us=False): %s %s", item.component, item.azure_id)
                continue

            tags_match = self._tags_match_deployment(item.component, item.azure_id, rg_name)
            if tags_match is None:
                self.state.mark_resource_state(self.deployment_id, item.azure_id, "deleted")
                report.succeeded.append(item)
                logger.info("[DESTROY-AZURE] %s ya no existía; nada que borrar.", item.component)
                continue
            if not tags_match:
                report.skipped_not_ours.append(item)
                logger.warning(
                    "[DESTROY-AZURE] Omitido: los tags de %s no coinciden con deployment_id=%s.",
                    item.component, self.deployment_id,
                )
                continue

            try:
                self._delete_one(rg_name, item)
                self.state.mark_resource_state(self.deployment_id, item.azure_id, "deleted")
                report.succeeded.append(item)
            except HttpResponseError as exc:
                self.state.mark_resource_state(self.deployment_id, item.azure_id, "error")
                report.failed.append({"item": item, "error": str(exc.error.code if exc.error else exc), "message": str(exc)})
                report.manual_actions_required.append(
                    f"Revisar manualmente {item.component} ({item.azure_id}): {exc}. "
                    f"Comando de diagnóstico: az resource show --ids {item.azure_id}"
                )
                logger.error("[DESTROY-AZURE] Fallo borrando %s %s: %s", item.component, item.azure_id, exc)

        if report.ok:
            self.state.set_deployment_status(self.deployment_id, "destroyed")
            self.state.close_deployment(self.deployment_id)
            self.state.log_event(self.deployment_id, "destroy", "destroy", "ok")
        else:
            self.state.set_deployment_status(self.deployment_id, "degraded", error="destroy parcial: ver DestroyReport")
            self.state.log_event(self.deployment_id, "destroy", "destroy", "warning", message="destroy parcial")

        return report

    def _delete_one(self, rg_name: str, item: PlannedDeletion) -> None:
        component = item.component
        if component == "subnet-public":
            self.network_client.subnets.begin_delete(rg_name, self._name("vnet"), "subred-public").result()
        elif component == "subnet-private":
            self.network_client.subnets.begin_delete(rg_name, self._name("vnet"), "subred-private").result()
        elif component == "natgw":
            self.network_client.nat_gateways.begin_delete(rg_name, self._name("natgw")).result()
        elif component == "pip-nat":
            self.network_client.public_ip_addresses.begin_delete(rg_name, self._name("pip-nat")).result()
        elif component == "nsg-gateway":
            self.network_client.network_security_groups.begin_delete(rg_name, self._name("gateway")).result()
        elif component == "nsg-workers":
            self.network_client.network_security_groups.begin_delete(rg_name, self._name("workers")).result()
        elif component == "vnet":
            self.network_client.virtual_networks.begin_delete(rg_name, self._name("vnet")).result()
        elif component == "resource-group":
            # Última red de seguridad: borra en cascada cualquier recurso no
            # rastreado que haya quedado dentro (equivalente a
            # _sweep_untracked_security_groups en aws_network.py, pero a nivel
            # de Resource Group completo -es el contenedor exclusivo de este
            # despliegue, nada más debería vivir ahí).
            self.resource_client.resource_groups.begin_delete(rg_name).result()
        else:
            raise AzureNetworkError(f"Componente desconocido en destroy: {component}")

    def scan_orphans(self) -> List[Dict[str, Any]]:
        """Busca recursos con tag sooniverse:managed=true en el Resource Group de
        este cliente/entorno que no estén en el estado (deployment_id ya
        destruido o desconocido)."""
        known_ids = {r.get("aws_id") for r in self.state.list_resources(self.deployment_id)}
        orphans: List[Dict[str, Any]] = []
        rg_name = self.resource_group_name

        try:
            resources = self.resource_client.resources.list_by_resource_group(
                rg_name, filter=f"tagName eq '{TAG_MANAGED}' and tagValue eq 'true'"
            )
        except ResourceNotFoundError:
            return []

        for res in resources:
            if res.id and res.id not in known_ids:
                orphans.append({"azure_id": res.id, "type": res.type, "tags": res.tags or {}})
        return orphans
