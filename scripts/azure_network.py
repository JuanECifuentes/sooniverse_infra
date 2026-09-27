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
`AZURE_CLIENT_SECRET`) -ver `.env.example`. A diferencia de AWS (donde BYOC
es un perfil/rol *distinto* por cliente, `aws_profile` + AssumeRole) el BYOC
de Azure NO cambia de identidad: usa **Azure Lighthouse**. El cliente delega
su suscripción (rol Contributor) al mismo Service Principal de Sooniverse
-ver `onboarding/azure-byoc-terraform/`- y lo único que cambia por cliente es
LA SUSCRIPCIÓN sobre la que operan las mismas credenciales, vía
`red_y_aislamiento.azure_subscription_id` (`AzureNetworkSpec.subscription_id`,
ver `_default_credential`). Ausente => `AZURE_SUBSCRIPTION_ID` de `.env`
(modo 'hosted', comportamiento histórico). `ConfigValidator._validate_red_azure`
exige el campo cuando `cliente.modo: byoc`.

Limitación conocida: el servidor local de la API de SkyPilot resuelve la
suscripción desde el perfil por defecto de `az` CLI (`az account show`), no
desde estas variables de entorno -por eso `generate_infra.py`,
`verify_deployment.py` y `destroy_infra.py` invocan
`ensure_azure_cli_subscription()` (definida en este módulo) antes de
cualquier `sky launch/exec/down/status` en Azure, y los despliegues BYOC de
clientes distintos van en serie, no en paralelo (cambiar de suscripción
reinicia ese servidor).

Mecanismo de propiedad: idéntico al de AWS (ver PROMPT_CLAUDE_CODE_sooniverse_red.md
y `aws_network.py`) -un recurso solo se borra si (a) está registrado en
`InfraStateStore` con el `deployment_id` correspondiente Y (b) sus tags Azure
reales siguen coincidiendo con ese mismo `deployment_id`.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from azure.core.exceptions import ResourceNotFoundError, HttpResponseError
    from azure.identity import ClientSecretCredential
    from azure.mgmt.network import NetworkManagementClient
    from azure.mgmt.network.models import (
        AddressSpace,
        NatGateway,
        NatGatewaySku,
        NetworkSecurityGroup,
        PublicIPAddress,
        PublicIPAddressSku,
        SecurityRule,
        SubResource,
        Subnet,
        VirtualNetwork,
    )
    from azure.mgmt.resource.resources import ResourceManagementClient
    from azure.mgmt.resource.resources.models import ResourceGroup, Tags, TagsPatchResource
    from azure.mgmt.msi import ManagedServiceIdentityClient
    from azure.mgmt.msi.models import Identity
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "Falta el SDK de Azure. Instala con: "
        "pip install azure-identity azure-mgmt-network azure-mgmt-resource azure-mgmt-msi"
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
    # Sin rol asignado (ver ensure_remote_identity()); el borrado del
    # Resource Group completo ya la arrastraría igual, este orden es solo
    # por consistencia con el resto del plan.
    "remote-identity": 41,
    # pip-gateway (dominio propio, ver AzureNetworkSpec.gateway_eip) se
    # intenta destruir aquí en el orden normal SI 'persistente' es false o
    # force=True; con el default (persistente), destroy() la conserva y de
    # paso NO ejecuta el borrado de "resource-group" para no arrastrarla en
    # la cascada del Resource Group completo (ver destroy()).
    "pip-gateway": 45,
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
    # Dominio propio (gateway.dominio.*): reserva una Public IP Standard/Static
    # DEDICADA para el Gateway -mismos nombres de campo que aws_network.NetworkSpec
    # a propósito (ver AzureNetworkOutputs). A diferencia de la VPC/EIP de AWS
    # (que SÍ necesita buscarse por tag porque create_vpc()/allocate_address()
    # no son idempotentes por nombre), esta Public IP usa el mismo nombre
    # determinista '<cliente>-<entorno>-pip-gateway' que el resto de este
    # módulo -sobrevive a un destroy porque el Resource Group que la contiene
    # tampoco se borra mientras haya algo persistente dentro (ver destroy()).
    gateway_eip: bool = False
    gateway_eip_persistent: bool = True
    gateway_domain: Optional[str] = None

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
    # Public IP DEDICADA y persistente del Gateway (gateway.dominio.*).
    # Nombrados igual que sus equivalentes en aws_network.NetworkOutputs
    # ('eip', no 'pip') a propósito: generate_infra.py los lee con
    # getattr(net_outputs, "gateway_eip_allocation_id", None) sin bifurcar
    # por nube -mismo patrón que el campo 'aws_id' genérico de InfraStateStore.
    gateway_eip_allocation_id: Optional[str] = None
    gateway_eip_public_ip: Optional[str] = None
    # Nombre de la User-Assigned Managed Identity que 'remote_identity' del
    # bloque 'azure' de SkyPilot debe usar -ver ensure_remote_identity() y el
    # docstring del módulo sobre por qué es obligatoria en esta suscripción.
    remote_identity_name: Optional[str] = None


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
    # Public IP del Gateway conservada a propósito
    # (gateway.dominio.eip_persistente=true) -no es un fallo ni un "no es
    # nuestro", igual que aws_network.DestroyReport.kept_persistent. Antes
    # este campo no existía y destroy_infra.py lo leía con
    # getattr(report, "kept_persistent", []) porque nunca se implementó.
    kept_persistent: List[PlannedDeletion] = field(default_factory=list)

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


_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_env_azure_vars(env_path: Path = _REPO_ROOT / ".env") -> None:
    """Carga AZURE_* de .env en os.environ si no están ya seteadas (mismo
    mecanismo/prioridad que scripts/azure_check_gpu_quota.py::_load_env y
    scripts/db_setup.py::resolve_db_config: variables ya presentes en el
    proceso ganan sobre el archivo).

    CORREGIDO: generate_infra.py (a diferencia de azure_check_gpu_quota.py)
    nunca cargaba .env en os.environ -boto3 (AWS) resuelve credenciales solo
    con el perfil de la CLI sin necesitar esto, pero ClientSecretCredential
    (Azure) exige las 4 variables directamente en el entorno. Cualquier
    invocación de 'generate_infra.py --config <cliente-azure> --run' desde
    una terminal limpia (sin exportar las AZURE_* a mano primero) fallaba con
    'Faltan credenciales de Azure' pese a que .env las tenía -confirmado en
    un despliegue real."""
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.startswith("AZURE_") and value and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def _default_credential(subscription_id: Optional[str] = None) -> Tuple["ClientSecretCredential", str]:
    _load_env_azure_vars()
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


def ensure_azure_cli_subscription(subscription_id: Optional[str]) -> None:
    """Azure BYOC (Lighthouse): antes de CUALQUIER 'sky launch/exec/down/
    status', el perfil por defecto del CLI 'az' debe apuntar a la suscripción
    del cliente -SkyPilot resuelve su suscripción efectiva SOLO desde ahí
    (sky/adaptors/azure.py::get_subscription_id ->
    credentials.get_cli_profile().get_subscription_id()), nunca desde las
    variables de entorno AZURE_* (eso sí basta para `AzureNetworkManager`,
    que usa el SDK directamente vía `_default_credential`, pero no para 'sky').

    Llamada compartida por generate_infra.py, verify_deployment.py y
    destroy_infra.py -los tres invocan 'sky' contra clústeres Azure.

    'subscription_id' None => modo 'hosted' sin override: no se toca el
    perfil de 'az' vigente (comportamiento histórico intacto).

    Ese resultado además queda cacheado con 'lru_cache(scope="global")' DENTRO
    del servidor local persistente de la API de SkyPilot (sky.server.server),
    que sigue vivo entre invocaciones de 'sky' -si el servidor ya arrancó con
    la suscripción anterior, cambiar el perfil de 'az' no alcanza: hay que
    'sky api stop' para que el próximo comando lo relance y lo relea. Esto
    implica que despliegues BYOC de clientes Azure distintos deben ir en
    SERIE, nunca en paralelo, mientras exista un único servidor local.
    """
    if not subscription_id:
        return
    if shutil.which("az") is None:
        raise RuntimeError(
            "Se requiere 'red_y_aislamiento.azure_subscription_id' (BYOC) pero el CLI 'az' "
            "no está en PATH -SkyPilot lo necesita para resolver la suscripción activa "
            "(ver README 7.1)."
        )
    current = subprocess.run(
        ["az", "account", "show", "--query", "id", "-o", "tsv"],
        capture_output=True,
        text=True,
    )
    current_id = current.stdout.strip() if current.returncode == 0 else None
    if current_id == subscription_id:
        return

    print(f"[AZURE-CLI] Activando la suscripción {subscription_id} en 'az' (perfil por defecto)...")
    result = subprocess.run(
        ["az", "account", "set", "--subscription", subscription_id],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"No se pudo activar la suscripción '{subscription_id}' en 'az' "
            f"(¿la delegación de Azure Lighthouse sigue vigente? revisa "
            f"'az account list --refresh' y el portal -> Proveedores de servicios). "
            f"Detalle: {result.stderr.strip()}"
        )

    sky = shutil.which("sky")
    if sky:
        print("[AZURE-CLI] Reiniciando el servidor local de la API de SkyPilot "
              "(cambió la suscripción activa)...")
        subprocess.run([sky, "api", "stop"], capture_output=True, text=True)


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
        self.msi_client = ManagedServiceIdentityClient(self._credential, self._subscription_id)

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
        # BUG CONFIRMADO en un despliegue real: _record() solo actualiza el
        # ESTADO (Postgres) -el tag real 'sooniverse:deployment-id' del
        # recurso en Azure se queda con el deployment_id VIEJO. destroy()
        # decide qué borrar leyendo el tag real (_tags_match_deployment), no
        # el estado, así que sin este re-etiquetado un recurso legítimamente
        # nuestro (sobrevivió a varias corridas de 'ensure_*' bajo distintos
        # deployment_id) se reportaba "Omitido: los tags no coinciden" y
        # quedaba huérfano para siempre tras cada destroy.
        self._retag_for_current_deployment(azure_id)
        return {"aws_id": azure_id, "component": component, "attributes": {"name": name}}

    def _retag_for_current_deployment(self, azure_id: str) -> None:
        """Actualiza (merge, sin tocar el resto) los tags 'sooniverse:deployment-id'
        Y 'sooniverse:managed' del recurso Azure real -ver el comentario en el
        único llamador, _find_existing(). API genérica de tags de Azure
        Resource Manager: funciona por resource ID sin importar el tipo de
        recurso, así que no hace falta lógica por tipo (VNet/NSG/NAT/PIP/MSI/
        Resource Group).

        BUG CONFIRMADO en un despliegue real: la primera versión de este
        método solo actualizaba TAG_DEPLOYMENT -_tags_match_deployment()
        exige TAMBIÉN TAG_MANAGED=='true', así que un recurso cuyo tag
        'managed' se hubiera perdido en algún momento (p.ej. un PUT externo
        sin ese tag) seguía reportando "no coinciden" en destroy() aunque
        deployment-id ya coincidiera -confirmado leyendo los tags reales del
        VNet: {'sooniverse:deployment-id': '<id-actual>'} SIN
        'sooniverse:managed' en absoluto."""
        try:
            self.resource_client.tags.begin_update_at_scope(
                azure_id,
                TagsPatchResource(
                    operation="Merge",
                    properties=Tags(tags={TAG_DEPLOYMENT: self.deployment_id, TAG_MANAGED: "true"}),
                ),
            ).result()
        except Exception:  # noqa: BLE001 - best-effort, no debe romper el 'ensure_*' que lo llama
            logger.warning("[RED-AZURE] No se pudo re-etiquetar %s con el deployment_id actual.", azure_id)

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
            if component == "remote-identity":
                name = self._name("msi")
                identity = self.msi_client.user_assigned_identities.get(rg_name, name)
                return identity.id, name
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

        # CORREGIDO: azure-mgmt-network/resource >=33 (API 2026-01-01) ya no
        # acepta un dict plano en estas llamadas -el servidor lo deserializa
        # como un 'ResourceDefinition' genérico y rechaza propiedades como
        # 'address_space' con InvalidRequestContent. Confirmado en un
        # despliegue real; se usa el modelo tipado explícito en todas las
        # llamadas de este módulo.
        self.resource_client.resource_groups.create_or_update(
            name, ResourceGroup(location=self.spec.region, tags=self._tags("resource-group"))
        )
        self._record("resource-group", "resource-group", name, attributes={"name": name})
        logger.info("[RED-AZURE] Resource Group creado: %s", name)
        return name

    def ensure_remote_identity(self, rg_name: str) -> str:
        """Crea (o reutiliza) una User-Assigned Managed Identity SIN ningún
        rol asignado, y devuelve su nombre corto (para 'azure.remote_identity'
        del bloque de cliente de SkyPilot -ver build_sky_gateway_config()/
        build_sky_workers_config() en generate_infra.py).

        Por qué es obligatoria en esta suscripción: sin 'remote_identity'
        explícito, SkyPilot crea SU PROPIA Managed Identity por cada cluster
        Y le asigna el rol Contributor sobre el Resource Group (plantilla ARM
        embebida, ver sky/provision/azure/config.py). Eso exige el permiso
        'Microsoft.Authorization/roleAssignments/write' sobre el Resource
        Group -que el Service Principal de este despliegue NO tiene (solo
        Contributor "puro", sin permisos de IAM)-, y CADA 'sky launch' fallaba
        con 'InvalidTemplateDeployment: Authorization failed for template
        resource ... roleAssignments'. El mensaje de SkyPilot en ese caso es
        el genérico "Failed to acquire resources in all zones" -confirmado en
        un despliegue real que el problema NUNCA fue capacidad de GPU/VM: se
        reprodujo igual con CPU-only SKUs, y el operador SÍ pudo aprovisionar
        la misma SKU T4 a mano con sus propios permisos de Portal.

        Crear una identidad YA EXISTENTE (sin rol) y pasarla como
        'remote_identity' hace que SkyPilot elimine los recursos de MSI/
        roleAssignment de su plantilla ARM por completo
        (sky/provision/azure/config.py::_remove_msi_resources_from_template),
        evitando ese permiso. Crear la identidad en sí solo exige
        'Microsoft.ManagedIdentity/userAssignedIdentities/write' -parte del
        rol Contributor normal, confirmado que el Service Principal SÍ lo
        tiene. Sin ningún rol asignado, la identidad no le da a la VM ningún
        permiso adicional sobre la API de Azure -aceptable porque el Gateway/
        los workers no llaman a esa API desde dentro de la VM."""
        name = self._name("msi")
        existing = self._find_existing("remote-identity", rg_name=rg_name)
        if existing:
            logger.info("[SKIP][RED-AZURE] Managed Identity ya registrada: %s", name)
            return name

        # A diferencia de begin_create_or_update() (async, devuelve un
        # poller), create_or_update() de MSI es síncrono y devuelve el
        # objeto Identity ya resuelto -sin necesitar un GET adicional.
        identity = self.msi_client.user_assigned_identities.create_or_update(
            rg_name, name, Identity(location=self.spec.region, tags=self._tags("remote-identity"))
        )
        self._record("remote-identity", "remote-identity", identity.id, parent_aws_id=rg_name, attributes={"name": name})
        logger.info("[RED-AZURE] Managed Identity lista: %s", identity.id)
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
            VirtualNetwork(
                location=self.spec.region,
                address_space=AddressSpace(address_prefixes=[self.spec.vnet_cidr]),
                tags=self._tags("vnet"),
            ),
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
            rg_name, name, NetworkSecurityGroup(location=self.spec.region, tags=self._tags(component))
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

        # CORREGIDO: se borra ANTES de crear/actualizar. Los nombres de regla
        # incluyen el índice POSICIONAL de 'rules' (allow-{port}-{idx}-{cidr_idx}):
        # si el conjunto de workloads cambia (se agrega/quita uno), el índice
        # de los puertos que SÍ siguen existiendo se recorre, así que un mismo
        # puerto puede terminar con un nombre de regla NUEVO en esta corrida.
        # Con el orden anterior (crear primero, borrar después), la regla
        # vieja -todavía viva bajo su nombre/prioridad anteriores, ya que
        # 'wanted' no la vuelve a mencionar con ese nombre- seguía ocupando su
        # prioridad numérica cuando la regla nueva pedía esa MISMA prioridad
        # (el contador es monotónico desde 100 en cada corrida), y Azure
        # rechazaba el create con 'SecurityRuleConflict' -confirmado en un
        # despliegue real al quitar un workload (embeddings) de la config.
        for rule_name in list(current_by_name):
            if rule_name not in wanted:
                self.network_client.security_rules.begin_delete(rg_name, nsg_name, rule_name).result()
                del current_by_name[rule_name]

        for rule_name, body in wanted.items():
            existing_rule = current_by_name.get(rule_name)
            if existing_rule and (
                existing_rule.source_address_prefix == body["source_address_prefix"]
                and existing_rule.destination_port_range == body["destination_port_range"]
            ):
                continue
            self.network_client.security_rules.begin_create_or_update(
                rg_name, nsg_name, rule_name, SecurityRule(**body)
            ).result()

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
            PublicIPAddress(
                location=self.spec.region,
                sku=PublicIPAddressSku(name="Standard"),
                public_ip_allocation_method="Static",
                tags=self._tags("pip-nat"),
            ),
        )
        pip = pip_poller.result()
        self._record("pip-nat", "pip-nat", pip.id, parent_aws_id=rg_name, attributes={"name": pip_name})

        nat_name = self._name("natgw")
        t0 = time.monotonic()
        nat_poller = self.network_client.nat_gateways.begin_create_or_update(
            rg_name,
            nat_name,
            NatGateway(
                location=self.spec.region,
                sku=NatGatewaySku(name="Standard"),
                public_ip_addresses=[SubResource(id=pip.id)],
                tags=self._tags("natgw"),
            ),
        )
        nat = nat_poller.result()
        self._record("natgw", "natgw", nat.id, parent_aws_id=rg_name, attributes={"name": nat_name})
        logger.info("[RED-AZURE] NAT Gateway disponible: %s (%.1fs)", nat.id, time.monotonic() - t0)
        return nat.id

    def ensure_gateway_public_ip(self, rg_name: str) -> Tuple[str, str]:
        """Reserva (o reutiliza) una Public IP Standard/Static DEDICADA al
        Gateway (equivalente de `aws_network.ensure_gateway_eip()`), para que
        un registro DNS A no se rompa entre 'sky launch's (SkyPilot asigna una
        IP efímera por defecto) ni entre un destroy_infra.py y el siguiente
        despliegue.

        A diferencia de AWS -donde `allocate_address()` no es idempotente por
        nombre y hace falta buscar por tag (client_id, environment)-, esta
        Public IP usa el mismo nombre determinista que el resto de este
        módulo: un PUT repetido a '<cliente>-<entorno>-pip-gateway' reutiliza
        la MISMA dirección IP ya asignada (no cambia en un re-PUT) y de paso
        re-etiqueta el recurso al deployment_id ACTUAL -necesario porque
        sobrevive a un destroy con un deployment_id nuevo en el siguiente
        despliegue (ver destroy(), que preserva el Resource Group completo
        mientras esta IP siga viva dentro)."""
        name = self._name("pip-gateway")
        poller = self.network_client.public_ip_addresses.begin_create_or_update(
            rg_name,
            name,
            PublicIPAddress(
                location=self.spec.region,
                sku=PublicIPAddressSku(name="Standard"),
                public_ip_allocation_method="Static",
                tags=self._tags("pip-gateway"),
            ),
        )
        pip = poller.result()
        self._record(
            "pip-gateway", "pip-gateway", pip.id,
            parent_aws_id=rg_name,
            attributes={
                "name": name,
                "public_ip": pip.ip_address,
                "persistente": self.spec.gateway_eip_persistent,
            },
        )
        logger.info("[RED-AZURE] Public IP del Gateway lista: %s (%s)", pip.ip_address, pip.id)
        return pip.id, pip.ip_address

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

        subnet_kwargs: Dict[str, Any] = {
            "address_prefix": cidr,
            "network_security_group": SubResource(id=nsg_id),
        }
        if nat_gateway_id:
            subnet_kwargs["nat_gateway"] = SubResource(id=nat_gateway_id)

        poller = self.network_client.subnets.begin_create_or_update(
            rg_name, vnet_name, name, Subnet(**subnet_kwargs)
        )
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
            remote_identity_name = self.ensure_remote_identity(rg_name)
            vnet_id, vnet_name = self.ensure_vnet(rg_name)
            nsg_gateway_id, nsg_workers_id = self.ensure_security_groups(rg_name)
            nat_gateway_id = self.ensure_nat_gateway(rg_name)
            public_subnet_id, private_subnet_id = self.ensure_subnets(
                rg_name, vnet_name, nsg_gateway_id, nsg_workers_id, nat_gateway_id
            )
            gateway_eip_alloc_id: Optional[str] = None
            gateway_eip_public_ip: Optional[str] = None
            if self.spec.gateway_eip:
                gateway_eip_alloc_id, gateway_eip_public_ip = self.ensure_gateway_public_ip(rg_name)
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
            gateway_eip_allocation_id=gateway_eip_alloc_id,
            gateway_eip_public_ip=gateway_eip_public_ip,
            remote_identity_name=remote_identity_name,
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
            elif component == "pip-gateway":
                obj = self.network_client.public_ip_addresses.get(rg_name, self._name("pip-gateway"))
            elif component == "remote-identity":
                obj = self.msi_client.user_assigned_identities.get(rg_name, self._name("msi"))
            else:
                return False
        except ResourceNotFoundError:
            return None

        tags = obj.tags or {}
        return tags.get(TAG_DEPLOYMENT) == self.deployment_id and tags.get(TAG_MANAGED) == "true"

    def destroy(self, dry_run: bool = False, force: bool = False) -> DestroyReport:
        """Nota sobre 'pip-gateway' (dominio propio, gateway.dominio.eip_persistente):
        a diferencia de AWS -donde cada recurso se borra por su propio ID y
        "conservar la EIP" es tan simple como saltarse ESE borrado-, en Azure
        el ÚLTIMO paso del plan es siempre borrar el Resource Group COMPLETO
        (component == "resource-group"), que arrastra en cascada CUALQUIER
        cosa que quede dentro -incluida una Public IP que hasta ese punto se
        había "conservado" individualmente. Por eso, cuando se conserva la
        Public IP del Gateway, este método SALTA TAMBIÉN el borrado del
        Resource Group (queda vivo, vacío salvo por esa IP) en vez de solo
        omitir el ítem 'pip-gateway'."""
        plan = self.plan_destroy()
        report = DestroyReport(deployment_id=self.deployment_id)

        def _es_pip_gateway_persistente(item: PlannedDeletion) -> bool:
            return (
                item.component == "pip-gateway"
                and (item.attributes or {}).get("persistente", True)
                and not force
            )

        conserva_pip_gateway = any(_es_pip_gateway_persistente(item) for item in plan)

        if dry_run:
            for item in plan:
                if _es_pip_gateway_persistente(item):
                    report.kept_persistent.append(item)
                    logger.info(
                        "[DESTROY-AZURE] (dry-run) Public IP del Gateway se conservaría "
                        "(gateway.dominio.eip_persistente=true): %s", item.azure_id,
                    )
                    continue
                if item.component == "resource-group" and conserva_pip_gateway:
                    logger.info(
                        "[DESTROY-AZURE] (dry-run) Resource Group NO se borraría "
                        "(contiene la Public IP persistente del Gateway): %s", item.azure_id,
                    )
                    continue
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

            if _es_pip_gateway_persistente(item):
                report.kept_persistent.append(item)
                # state='adopted' (no 'deleted'): sigue existiendo en Azure a
                # propósito -mismo patrón que aws_network.py para 'eip-gateway'.
                self.state.mark_resource_state(self.deployment_id, item.azure_id, "adopted")
                logger.info(
                    "[DESTROY-AZURE] Public IP del Gateway conservada "
                    "(gateway.dominio.eip_persistente=true): %s", item.azure_id,
                )
                continue

            if item.component == "resource-group" and conserva_pip_gateway:
                report.manual_actions_required.append(
                    f"Resource Group '{rg_name}' no se borró: contiene la Public IP "
                    "persistente del Gateway (gateway.dominio.eip_persistente=true). "
                    f"Bórralo a mano (az group delete --name {rg_name}) cuando el "
                    "dominio ya no se vaya a reutilizar."
                )
                logger.info(
                    "[DESTROY-AZURE] Resource Group NO se borra: contiene la Public IP "
                    "persistente del Gateway: %s", rg_name,
                )
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
        elif component == "pip-gateway":
            # Solo se llega aquí con force=True o gateway_eip_persistent=false
            # (el caso normal la conserva, ver destroy()).
            self.network_client.public_ip_addresses.begin_delete(rg_name, self._name("pip-gateway")).result()
        elif component == "nsg-gateway":
            self.network_client.network_security_groups.begin_delete(rg_name, self._name("gateway")).result()
        elif component == "nsg-workers":
            self.network_client.network_security_groups.begin_delete(rg_name, self._name("workers")).result()
        elif component == "vnet":
            self.network_client.virtual_networks.begin_delete(rg_name, self._name("vnet")).result()
        elif component == "remote-identity":
            self.msi_client.user_assigned_identities.delete(rg_name, self._name("msi"))
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
