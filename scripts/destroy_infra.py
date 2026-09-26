#!/usr/bin/env python3
"""
==============================================================================
Sooniverse Infra - Destrucción completa del despliegue (Fase 3)
==============================================================================
Reemplaza el flujo manual ("sky down" + borrado a mano de la VPC en la
consola) por un ciclo de destrucción único, en el orden estricto que evita
dejar NAT Gateways/Elastic IPs huérfanos cobrando:

  1. Clústeres SkyPilot worker (sky down)   -- primero: sin el gateway como
                                                bastion, SkyPilot pierde el SSH
                                                a instancias sin IP pública.
  2. Clúster SkyPilot gateway (sky down)
  3. Capa de red (AwsNetworkManager.destroy): SGs -> VPC endpoints -> NAT ->
     EIPs -> route tables -> IGW -> subredes -> VPC.

Nunca borra: la base de datos PostgreSQL / el esquema `sooniverse`, recursos
de otros clientes, recursos sin nuestros tags, o la VPC por defecto de la
cuenta (ver DefaultVpcGuardError en aws_network.py).

Uso:
    python scripts/destroy_infra.py --dry-run
    python scripts/destroy_infra.py --yes
    python scripts/destroy_infra.py --only network --yes
    python scripts/destroy_infra.py --scan-orphans
    python scripts/destroy_infra.py --scan-orphans --purge-orphans --yes
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml
except ImportError:
    print("[ERROR] Falta 'pyyaml'. Ejecuta: pip install pyyaml")
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from generate_infra import (  # noqa: E402
    ConfigValidationError,
    ConfigValidator,
    TopologyBuilder,
    build_network_spec_from_config,
)


def _sky_binary() -> Optional[str]:
    return shutil.which("sky")


def _sky_down(cluster: str, aws_profile: Optional[str] = None) -> bool:
    """`sky down -y <cluster>`. Devuelve True si tuvo éxito o el clúster ya no existía.

    `aws_profile` (BYOC): SkyPilot resuelve credenciales AWS con la cadena estándar
    de boto3, así que sin AWS_PROFILE en el entorno terminaría intentando destruir
    el clúster con las credenciales por defecto (la cuenta de Sooniverse), no las
    del cliente dueño del clúster."""
    sky = _sky_binary()
    if not sky:
        print("[ERROR] El comando 'sky' no está en el PATH.")
        return False
    env = {**os.environ, "AWS_PROFILE": aws_profile} if aws_profile else None
    print(f"[EXEC] {sky} down -y {cluster}" + (f" (AWS_PROFILE={aws_profile})" if aws_profile else ""))
    result = subprocess.run([sky, "down", "-y", cluster], capture_output=True, text=True, env=env)
    if result.returncode != 0:
        stderr = (result.stderr or "").lower()
        if "does not exist" in stderr or "no existing cluster" in stderr:
            print(f"[SKIP] El clúster '{cluster}' ya no existe.")
            return True
        print(f"[ERROR] 'sky down {cluster}' falló:\n{result.stderr}")
        return False
    return True


# Política de reintento para apagar el cómputo (workers GPU + Gateway): los
# nodos GPU pueden tardar hasta ~15 min en terminar del todo (drivers NVIDIA +
# limpieza del ENI), así que "sky down" + una espera corta no basta -pero
# tampoco hay que reintentar para siempre si algo está genuinamente roto.
# 1 intento/min durante 20 min y, si para entonces sigue sin terminar,
# mostrar el error y parar -no seguir intentando por cuenta propia.
DESTROY_MAX_WAIT_SECONDS = 1200
DESTROY_RETRY_INTERVAL_SECONDS = 60


def _instances_pending(
    clusters: List[str], region: str, aws_profile: Optional[str] = None, cloud: str = "aws"
) -> List[str]:
    """IDs de instancia de estos clústeres SkyPilot que NO llegaron todavía a
    'terminated' (incluye 'shutting-down', 'stopping', 'pending', 'running':
    cualquier estado donde el ENI sigue potencialmente 'in-use', que es lo
    que de verdad bloquea la capa de red con DependencyViolation).

    BUG CONFIRMADO en un despliegue real: esta función llamaba a la API de
    EC2 SIN IMPORTAR LA NUBE, usando la región de Azure/GCP como si fuera una
    región AWS ("Could not connect to the endpoint URL:
    https://ec2.westus3.amazonaws.com/"), abortando la destrucción completa
    antes de llegar siquiera a la capa de red. Azure no tiene el mismo
    patrón de terminación asíncrona de EC2 (el 'begin_delete().result()' de
    SkyPilot ya espera a que la VM desaparezca de verdad), así que para
    cualquier nube que no sea AWS basta con confiar en que 'sky down'
    terminó con éxito -sin este chequeo AWS-específico, que no aplica."""
    if cloud != "aws":
        return []
    try:
        import boto3
    except ImportError:
        return []

    ec2 = boto3.Session(profile_name=aws_profile, region_name=region).client("ec2")
    filtro_valores = [c for cluster in clusters for c in (cluster, f"{cluster}-*")]
    pendientes: List[str] = []
    for tag_key in ("ray-cluster-name", "skypilot-cluster-name"):
        resp = ec2.describe_instances(
            Filters=[
                {"Name": f"tag:{tag_key}", "Values": filtro_valores},
                {"Name": "instance-state-name",
                 "Values": ["pending", "running", "stopping", "shutting-down"]},
            ]
        )
        for reservation in resp.get("Reservations", []):
            pendientes.extend(i["InstanceId"] for i in reservation.get("Instances", []))
    return pendientes


def _teardown_clusters_with_budget(
    clusters: List[str], region: str, deadline: float, aws_profile: Optional[str] = None,
    cloud: str = "aws",
) -> bool:
    """`sky down` de cada clúster de la lista (en orden) y reintenta hasta que
    TODAS sus instancias EC2 confirmen 'terminated', a razón de un intento por
    minuto, sin pasar de `deadline` (un `time.monotonic()` absoluto,
    compartido entre fases por el llamador -ver `destroy()`- para que el
    presupuesto total de 20 min sea del proceso de destrucción completo, no
    20 min por cada fase). Devuelve False (sin lanzar) si se agota el
    presupuesto: quien llama decide si aborta la capa de red o no."""
    attempt = 0
    while True:
        attempt += 1
        for cluster in clusters:
            _sky_down(cluster, aws_profile=aws_profile)

        pendientes = _instances_pending(clusters, region, aws_profile=aws_profile, cloud=cloud)
        if not pendientes:
            if attempt > 1:
                print(f"[OK] {', '.join(clusters)}: instancia(s) terminada(s) tras {attempt} intento(s).")
            return True

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(
                f"[ERROR] Tras {DESTROY_MAX_WAIT_SECONDS // 60} min ({attempt} intento(s)), sigue(n) "
                f"activa(s) {len(pendientes)} instancia(s) de {clusters}: {pendientes}. Los nodos GPU "
                "pueden tardar hasta ~15 min en apagarse del todo; no se sigue reintentando "
                "automáticamente para no perder más tiempo. Verifica en la consola de EC2 y vuelve a "
                "correr 'python scripts/destroy_infra.py --yes' cuando confirmes que ya terminaron."
            )
            return False

        wait = min(DESTROY_RETRY_INTERVAL_SECONDS, max(1, int(remaining)))
        print(f"[ESPERA] {len(pendientes)} instancia(s) todavía no termina(n) (intento {attempt}); "
              f"reintentando en {wait}s (presupuesto restante: {int(remaining)}s)...")
        time.sleep(wait)


def load_config(config_path: Path) -> Dict[str, Any]:
    with config_path.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    ConfigValidator.validate(config)
    return config


def confirm_destructive_action(client_id: str, environment: str, args: argparse.Namespace) -> bool:
    if args.dry_run:
        return True
    if args.yes:
        return True
    try:
        typed = input(
            f"Escribe '{client_id}' para confirmar la destrucción de {client_id}/{environment} "
            f"(o Ctrl+C para abortar): "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        return False
    return typed == client_id


# =============================================================================
# --scan-orphans / --purge-orphans (barrido de toda la región, no de un solo
# deployment_id): compara los recursos tag:sooniverse:managed=true en AWS
# contra TODAS las filas no borradas de sooniverse.infra_resource, sin
# importar a qué deployment_id pertenezcan.
# =============================================================================
def _region_known_aws_ids(region: str) -> Dict[str, Tuple[str, str]]:
    """Devuelve {aws_id: (status_del_deployment, state_del_recurso)} para todo lo
    registrado en la BD en esa región y que no esté marcado 'deleted'."""
    from db_setup import connect, resolve_db_config

    conn = connect(resolve_db_config(REPO_ROOT / ".env"))
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.aws_id, d.status, r.state
                FROM sooniverse.infra_resource r
                JOIN sooniverse.infra_deployment d ON d.deployment_id = r.deployment_id
                WHERE r.region = %s AND r.state != 'deleted' AND r.aws_id IS NOT NULL
                """,
                (region,),
            )
            return {row[0]: (row[1], row[2]) for row in cur.fetchall()}
    finally:
        conn.close()


def _is_orphan(known: Dict[str, Tuple[str, str]], aws_id: str) -> Tuple[bool, Optional[str]]:
    """True si `aws_id` debe reportarse como huérfano, y el status a mostrar.

    Un recurso con `state='adopted'` (p.ej. la Elastic IP del Gateway con
    `gateway.dominio.eip_persistente: true`) NUNCA es huérfano aunque su
    deployment padre ya esté 'destroyed'/'error': quedó vivo en AWS A PROPÓSITO,
    no porque el destroy se lo haya saltado. Ver AwsNetworkManager.destroy()."""
    info = known.get(aws_id)
    if info is None:
        return True, None
    status, state = info
    if state == "adopted":
        return False, status
    return status in ("destroyed", "error"), status


def scan_orphans(region: str, aws_profile: Optional[str] = None) -> List[Dict[str, Any]]:
    import boto3
    from aws_network import TAG_MANAGED

    ec2 = boto3.Session(profile_name=aws_profile, region_name=region).client("ec2")
    known = _region_known_aws_ids(region)

    # 'describe_nat_gateways' es distinto del resto: AWS sigue devolviendo un
    # NAT ya destruido durante un buen rato con state='deleted' -a diferencia
    # de una VPC/subred/IGW ya borrada, que simplemente deja de aparecer-.
    # Sin este filtro, un NAT correctamente destruido por
    # AwsNetworkManager.destroy() se reportaba como huérfano para siempre
    # hasta que AWS lo purgara de su API (confirmado en una destrucción real).
    nat_filters = [
        {"Name": f"tag:{TAG_MANAGED}", "Values": ["true"]},
        {"Name": "state", "Values": ["pending", "failed", "available", "deleting"]},
    ]
    checks = [
        (ec2.describe_vpcs, "Vpcs", "VpcId", None),
        (ec2.describe_subnets, "Subnets", "SubnetId", None),
        (ec2.describe_internet_gateways, "InternetGateways", "InternetGatewayId", None),
        (ec2.describe_nat_gateways, "NatGateways", "NatGatewayId", nat_filters),
        (ec2.describe_security_groups, "SecurityGroups", "GroupId", None),
        (ec2.describe_route_tables, "RouteTables", "RouteTableId", None),
        (ec2.describe_vpc_endpoints, "VpcEndpoints", "VpcEndpointId", None),
    ]

    orphans: List[Dict[str, Any]] = []
    for fn, list_key, id_key, extra_filters in checks:
        resp = fn(Filters=extra_filters or [{"Name": f"tag:{TAG_MANAGED}", "Values": ["true"]}])
        for item in resp.get(list_key, []):
            aws_id = item.get(id_key)
            if not aws_id:
                continue
            is_orphan, status = _is_orphan(known, aws_id)
            if is_orphan:
                tags = {t["Key"]: t["Value"] for t in item.get("Tags", [])}
                orphans.append({
                    "aws_id": aws_id, "type": list_key, "name": tags.get("Name", ""),
                    "deployment_status": status or "no-registrado", "tags": tags,
                })

    addresses = ec2.describe_addresses(Filters=[{"Name": f"tag:{TAG_MANAGED}", "Values": ["true"]}])
    for addr in addresses.get("Addresses", []):
        alloc_id = addr.get("AllocationId")
        if not alloc_id:
            continue
        is_orphan, status = _is_orphan(known, alloc_id)
        if is_orphan:
            tags = {t["Key"]: t["Value"] for t in addr.get("Tags", [])}
            orphans.append({
                "aws_id": alloc_id, "type": "Addresses", "name": tags.get("Name", ""),
                "deployment_status": status or "no-registrado", "tags": tags,
            })

    return orphans


def print_orphans(orphans: List[Dict[str, Any]]) -> None:
    if not orphans:
        print("[OK] No se encontraron recursos huérfanos.")
        return
    print(f"\n{'TIPO':<20} {'AWS ID':<24} {'NOMBRE':<40} ESTADO DESPLIEGUE")
    print("-" * 100)
    for o in orphans:
        print(f"{o['type']:<20} {o['aws_id']:<24} {o['name']:<40} {o['deployment_status']}")


def scan_orphans_azure(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Equivalente Azure de `scan_orphans()`. A diferencia de AWS (una cuenta/
    región compartida entre todos los clientes, de ahí el escaneo por tag en
    TODA la región), cada cliente Azure vive en su propio Resource Group
    dedicado (ver azure_network.py) -así que alcanza con mirar DENTRO de ese
    Resource Group, no hace falta escanear la suscripción entera."""
    from azure_network import AzureNetworkManager
    from infra_state import PostgresInfraStateStore

    cliente = config["cliente"]
    red = config["red_y_aislamiento"]
    state = PostgresInfraStateStore()
    state.ping()
    existing = state.get_active_deployment(cliente["id"], cliente["entorno"], red["region"])
    # Sin despliegue activo: se usa un deployment_id "de un solo uso", nunca
    # persistido (no se llama a state.open_deployment), así que
    # list_resources() para él siempre está vacío -y CUALQUIER recurso
    # encontrado en el Resource Group aparece como huérfano, que es justo lo
    # correcto cuando no hay un despliegue registrado dueño de ese RG.
    deployment_id = existing["deployment_id"] if existing else str(uuid.uuid4())

    spec = build_network_spec_from_config(config)
    mgr = AzureNetworkManager(spec, state=state, deployment_id=deployment_id)
    orphans = mgr.scan_orphans()
    status = "activo" if existing else "sin-despliegue-registrado"
    for o in orphans:
        o["deployment_status"] = status
        o["name"] = (o.get("azure_id") or "").rsplit("/", 1)[-1]
    return orphans


def print_orphans_azure(orphans: List[Dict[str, Any]]) -> None:
    if not orphans:
        print("[OK] No se encontraron recursos huérfanos en el Resource Group de este cliente.")
        return
    print(f"\n{'TIPO':<45} {'NOMBRE':<30} ESTADO DESPLIEGUE")
    print("-" * 110)
    for o in orphans:
        print(f"{o['type']:<45} {o['name']:<30} {o['deployment_status']}")
        print(f"    {o['azure_id']}")


def purge_orphans(orphans: List[Dict[str, Any]], region: str, aws_profile: Optional[str] = None) -> None:
    import boto3
    from aws_network import DELETE_ORDER

    ec2 = boto3.Session(profile_name=aws_profile, region_name=region).client("ec2")
    ordered = sorted(orphans, key=lambda o: DELETE_ORDER.get(_component_of(o), 999))

    for o in ordered:
        aws_id = o["aws_id"]
        resource_type = o["type"]
        try:
            if resource_type == "SecurityGroups":
                sg = ec2.describe_security_groups(GroupIds=[aws_id])["SecurityGroups"][0]
                if sg.get("IpPermissions"):
                    ec2.revoke_security_group_ingress(GroupId=aws_id, IpPermissions=sg["IpPermissions"])
                if sg.get("IpPermissionsEgress"):
                    ec2.revoke_security_group_egress(GroupId=aws_id, IpPermissions=sg["IpPermissionsEgress"])
                ec2.delete_security_group(GroupId=aws_id)
            elif resource_type == "VpcEndpoints":
                ec2.delete_vpc_endpoints(VpcEndpointIds=[aws_id])
            elif resource_type == "NatGateways":
                ec2.delete_nat_gateway(NatGatewayId=aws_id)
            elif resource_type == "Addresses":
                ec2.release_address(AllocationId=aws_id)
            elif resource_type == "RouteTables":
                rt = ec2.describe_route_tables(RouteTableIds=[aws_id])["RouteTables"][0]
                for assoc in rt.get("Associations", []):
                    if not assoc.get("Main") and assoc.get("RouteTableAssociationId"):
                        ec2.disassociate_route_table(AssociationId=assoc["RouteTableAssociationId"])
                ec2.delete_route_table(RouteTableId=aws_id)
            elif resource_type == "InternetGateways":
                igw = ec2.describe_internet_gateways(InternetGatewayIds=[aws_id])["InternetGateways"][0]
                for attachment in igw.get("Attachments", []):
                    ec2.detach_internet_gateway(InternetGatewayId=aws_id, VpcId=attachment["VpcId"])
                ec2.delete_internet_gateway(InternetGatewayId=aws_id)
            elif resource_type == "Subnets":
                ec2.delete_subnet(SubnetId=aws_id)
            elif resource_type == "Vpcs":
                vpc = ec2.describe_vpcs(VpcIds=[aws_id])["Vpcs"][0]
                if vpc.get("IsDefault"):
                    print(f"[ABORTADO] {aws_id} es la VPC por defecto; nunca se borra.")
                    continue
                ec2.delete_vpc(VpcId=aws_id)
            print(f"[OK] Purgado: {resource_type} {aws_id}")
        except Exception as exc:  # noqa: BLE001 - reporte por recurso, no debe abortar el resto
            print(f"[ERROR] No se pudo purgar {resource_type} {aws_id}: {exc}")


def _component_of(orphan: Dict[str, Any]) -> str:
    return {
        "Vpcs": "vpc", "Subnets": "subnet-public", "InternetGateways": "igw",
        "NatGateways": "nat", "Addresses": "eip", "RouteTables": "rtb-public",
        "SecurityGroups": "sg-workers", "VpcEndpoints": "vpce-s3",
    }.get(orphan["type"], "")


# Orden de borrado para purge_orphans_azure(), por TIPO ARM (minúsculas, ver
# _azure_delete_order). Mismo criterio que aws_network.DELETE_ORDER: los
# recursos "hoja" primero (NAT/Public IP/NSG), la VNet al final -a diferencia
# de AWS, aquí no hace falta desasociar nada antes: Azure rechaza con un error
# claro (no una excepción de purga silenciosa) si un recurso todavía tiene
# dependientes, así que el peor caso es un [ERROR] legible, no una purga a
# medias no detectada.
_AZURE_ORPHAN_DELETE_ORDER = {
    "microsoft.network/virtualnetworks/subnets": 10,
    "microsoft.network/natgateways": 20,
    "microsoft.network/publicipaddresses": 21,
    "microsoft.network/networksecuritygroups": 30,
    "microsoft.network/virtualnetworks": 40,
}


def _azure_delete_order(orphan: Dict[str, Any]) -> int:
    return _AZURE_ORPHAN_DELETE_ORDER.get((orphan.get("type") or "").lower(), 999)


def purge_orphans_azure(orphans: List[Dict[str, Any]]) -> None:
    """Borra los recursos huérfanos reportados por scan_orphans_azure(), por
    su resourceId completo. A diferencia de AWS (una llamada boto3 tipada por
    cada tipo de recurso, ver purge_orphans()), Azure ofrece un único método
    genérico -resources.begin_delete_by_id()- que funciona para CUALQUIER
    tipo de recurso sin necesitar un cliente tipado por servicio (network,
    compute, ...). Solo hace falta el 'api_version' correcto para cada tipo,
    que se resuelve EN VIVO contra el Resource Provider (resources.providers)
    en vez de hardcodearlo -así no queda desactualizado si Azure publica una
    versión nueva del API."""
    from azure_network import _default_credential
    from azure.mgmt.resource.resources import ResourceManagementClient

    credential, sub_id = _default_credential()
    resource_client = ResourceManagementClient(credential, sub_id)

    api_version_cache: Dict[str, Optional[str]] = {}

    def _api_version_for(resource_type: str) -> Optional[str]:
        if resource_type in api_version_cache:
            return api_version_cache[resource_type]
        version = None
        try:
            namespace, tipo = resource_type.split("/", 1)
            provider = resource_client.providers.get(namespace)
            for rt in provider.resource_types or []:
                if rt.resource_type.lower() == tipo.lower():
                    versiones = rt.api_versions or []
                    # Prioriza versiones estables (sin 'preview') sobre las de
                    # vista previa; ambas listas vienen ordenadas más reciente
                    # primero.
                    estables = [v for v in versiones if "preview" not in v.lower()]
                    version = (estables or versiones or [None])[0]
                    break
        except Exception as exc:  # noqa: BLE001 - se reporta por recurso, no debe abortar el resto
            print(f"[WARNING] No se pudo resolver el api_version de '{resource_type}': {exc}")
        api_version_cache[resource_type] = version
        return version

    ordered = sorted(orphans, key=_azure_delete_order)
    for o in ordered:
        resource_id = o["azure_id"]
        resource_type = o["type"]
        api_version = _api_version_for(resource_type)
        if not api_version:
            print(f"[ERROR] No se pudo borrar {resource_id}: sin api_version resuelto para '{resource_type}'.")
            continue
        try:
            resource_client.resources.begin_delete_by_id(resource_id, api_version).result()
            print(f"[OK] Purgado: {resource_type} {resource_id}")
        except Exception as exc:  # noqa: BLE001 - reporte por recurso, no debe abortar el resto
            print(f"[ERROR] No se pudo purgar {resource_type} {resource_id}: {exc}")


# =============================================================================
# Destrucción normal (sky down workers -> sky down gateway -> red)
# =============================================================================
def destroy(config: Dict[str, Any], args: argparse.Namespace) -> int:
    cliente = config["cliente"]
    red = config["red_y_aislamiento"]
    builder = TopologyBuilder(config)

    print("\n" + "=" * 74)
    print(f" DESTRUCCIÓN: {cliente['id']}/{cliente['entorno']} ({red['region']})")
    print("=" * 74)

    if not confirm_destructive_action(cliente["id"], cliente["entorno"], args):
        print("[ABORTADO] Confirmación no recibida.")
        return 1

    only = args.only
    cloud = red.get("cloud", "aws")

    if only in ("all",) and not args.dry_run:
        worker_clusters = [builder.worker_cluster(wl["id"]) for wl in config["workloads"]]
        aws_profile = red.get("aws_profile")
        # Presupuesto ÚNICO para todo el apagado de cómputo (workers + Gateway),
        # no uno por fase: 20 min en total, 1 intento/min (ver
        # _teardown_clusters_with_budget). Los workers van primero -sin el
        # Gateway como bastion, SkyPilot pierde el SSH a instancias sin IP
        # pública- así que si se agota el presupuesto ahí, el Gateway ni se toca.
        deadline = time.monotonic() + DESTROY_MAX_WAIT_SECONDS

        print("\n--- [1/3] Workers vLLM (sky down, hasta 20 min de reintentos) ---")
        workers_ok = True
        if worker_clusters:
            workers_ok = _teardown_clusters_with_budget(
                worker_clusters, red["region"], deadline, aws_profile=aws_profile, cloud=cloud,
            )

        if not workers_ok:
            print("\n[ABORTADO] La capa de red no se toca con workers todavía activos "
                  "-fallaría por dependencias (ENI en uso) y solo generaría ruido.")
            return 2

        print("\n--- [2/3] Nodo Gateway (sky down, mismo presupuesto) ---")
        gateway_ok = _teardown_clusters_with_budget(
            [builder.gateway_cluster], red["region"], deadline, aws_profile=aws_profile, cloud=cloud,
        )
        if not gateway_ok:
            print("\n[ABORTADO] La capa de red no se toca con el Gateway todavía activo.")
            return 2
    elif only in ("all",):
        print("\n--- (dry-run) Se ejecutaría 'sky down' de workers y gateway ---")
        for wl in config["workloads"]:
            print(f"       sky down -y {builder.worker_cluster(wl['id'])}")
        print(f"       sky down -y {builder.gateway_cluster}")

    if red.get("gestion_red", "auto") != "auto":
        print("\n[SKIP] 'gestion_red: existente' -> la VPC/SGs no los gestiona este sistema; nada que destruir.")
        return 0

    print(f"\n--- [3/3] Capa de red {cloud.upper()} ---")
    from infra_state import PostgresInfraStateStore

    state = PostgresInfraStateStore()
    state.ping()
    existing = state.get_active_deployment(cliente["id"], cliente["entorno"], red["region"])
    if not existing:
        print(f"[INFO] No hay un despliegue activo registrado para "
              f"{cliente['id']}/{cliente['entorno']}/{red['region']}. Nada que destruir en la capa de red.")
        return 0

    deployment_id = existing["deployment_id"]
    spec = build_network_spec_from_config(config)
    if cloud == "azure":
        from azure_network import AzureNetworkManager

        mgr = AzureNetworkManager(spec, state=state, deployment_id=deployment_id)
    elif cloud == "gcp":
        # ⚠️ Teórico, no probado en ejecución (ver scripts/gcp_network.py).
        from gcp_network import GcpNetworkManager

        mgr = GcpNetworkManager(spec, state=state, deployment_id=deployment_id)
    else:
        from aws_network import AwsNetworkManager

        mgr = AwsNetworkManager(spec, state=state, deployment_id=deployment_id)

    report = mgr.destroy(dry_run=args.dry_run, force=args.force)
    # AzureNetworkManager.PlannedDeletion usa 'azure_id'; GcpNetworkManager
    # usa 'gcp_id' (mismo campo conceptual, nombre distinto -ver los
    # docstrings de azure_network.py/gcp_network.py).
    id_attr = {"aws": "aws_id", "azure": "azure_id", "gcp": "gcp_id"}.get(cloud, "aws_id")

    if args.dry_run:
        kept_ids = {getattr(item, id_attr) for item in getattr(report, "kept_persistent", [])}
        for item in mgr.plan_destroy():
            item_id = getattr(item, id_attr)
            if item_id in kept_ids:
                print(f"  [{item.delete_order:>3}] {item.component:<14} {item_id or '(sin id)'} "
                      f"[CONSERVADO] gateway.dominio.eip_persistente=true")
                continue
            print(f"  [{item.delete_order:>3}] {item.component:<14} {item_id or '(sin id)'} "
                  f"managed_by_us={item.managed_by_us}")
        return 0

    print(f"\n[REPORTE] Éxitos: {len(report.succeeded)} | Fallos: {len(report.failed)} | "
          f"Omitidos (no nuestros): {len(report.skipped_not_ours)} | "
          f"Conservados (dominio.eip_persistente): {len(getattr(report, 'kept_persistent', []))}")
    for item in getattr(report, "kept_persistent", []):
        print(f"  [CONSERVADO] {item.component} {getattr(item, id_attr)} (gateway.dominio.eip_persistente=true)")
    for failure in report.failed:
        item = failure["item"]
        print(f"  [FALLO] {item.component} {getattr(item, id_attr)}: {failure['error']}")
    for action in report.manual_actions_required:
        print(f"  [MANUAL] {action}")

    # Usuario IAM dedicado a apagar/arrancar workers (aws_iam_worker_control.py,
    # ver generate_infra.py fase 'network'): best-effort, nunca bloquea el
    # resto de la destrucción. Si nunca se creó (credenciales del despliegue
    # sin permiso IAM), esto simplemente no encuentra nada que borrar.
    # Concepto EXCLUSIVO de AWS (IAM) -confirmado en un despliegue real: para
    # Azure 'mgr' es un AzureNetworkManager sin atributo 'session', así que
    # esto emitía un WARNING falso en cada destroy que no era AWS.
    if cloud == "aws":
        try:
            from aws_iam_worker_control import delete_worker_control_user

            tags_ob = red.get("tags_obligatorios", {}) or {}
            delete_worker_control_user(
                mgr.session,
                cliente_id=tags_ob.get("cliente_id", cliente["id"]),
                entorno=tags_ob.get("entorno", cliente["entorno"]),
            )
        except Exception as exc:  # noqa: BLE001 - best-effort
            print(f"[WARNING] No se pudo limpiar el usuario IAM de control de workers: {exc}")

    return 0 if report.ok else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="Destrucción completa del despliegue Sooniverse.")
    parser.add_argument("--config", default=str(REPO_ROOT / "config_global.yaml"))
    parser.add_argument("--dry-run", action="store_true", help="Imprime el plan sin tocar nada")
    parser.add_argument("--yes", action="store_true", help="Confirma la destrucción sin preguntar")
    parser.add_argument("--only", choices=["all", "network"], default="all")
    parser.add_argument("--force", action="store_true",
                         help="Ignora managed_by_us=False (solo para depuración; usar con cuidado)")
    parser.add_argument("--scan-orphans", action="store_true",
                         help="Busca recursos tag:sooniverse:managed=true en la región no registrados o de despliegues ya destruidos")
    parser.add_argument("--purge-orphans", action="store_true",
                         help="Con --scan-orphans y --yes: borra los huérfanos encontrados")
    args = parser.parse_args()

    try:
        config = load_config(Path(args.config))
    except (ConfigValidationError, FileNotFoundError) as exc:
        print(f"[ERROR DE CONFIGURACIÓN] {exc}", file=sys.stderr)
        return 1

    if args.scan_orphans:
        cloud = config["red_y_aislamiento"].get("cloud", "aws")
        if cloud == "azure":
            orphans = scan_orphans_azure(config)
            print_orphans_azure(orphans)
            if args.purge_orphans:
                if not args.yes:
                    print("[ABORTADO] --purge-orphans requiere --yes.")
                    return 1
                purge_orphans_azure(orphans)
            return 0

        if cloud == "gcp":
            # ⚠️ Teórico, no probado en ejecución. GcpNetworkManager.scan_orphans()
            # ya lista huérfanos reales (ver scripts/gcp_network.py), pero
            # --purge-orphans NO está implementado todavía para GCP -bórralos a
            # mano con 'gcloud compute <tipo> delete <nombre>' hasta que se
            # implemente y verifique contra un proyecto real.
            from gcp_network import GcpNetworkManager

            spec = build_network_spec_from_config(config)
            mgr = GcpNetworkManager(spec, deployment_id="scan-orphans-temporal")
            orphans = mgr.scan_orphans()
            if not orphans:
                print("[OK] No se encontraron recursos huérfanos de sooniverse en el proyecto/región.")
            else:
                print(f"\n{'TIPO':<20} ID")
                print("-" * 100)
                for o in orphans:
                    print(f"{o['type']:<20} {o['gcp_id']}")
            if args.purge_orphans:
                print("[ABORTADO] --purge-orphans no está implementado para GCP en esta versión "
                      "(implementación teórica, no probada). Bórralos manualmente con 'gcloud'.")
                return 1
            return 0

        region = config["red_y_aislamiento"]["region"]
        aws_profile = config["red_y_aislamiento"].get("aws_profile")
        orphans = scan_orphans(region, aws_profile=aws_profile)
        print_orphans(orphans)
        if args.purge_orphans:
            if not args.yes:
                print("[ABORTADO] --purge-orphans requiere --yes.")
                return 1
            purge_orphans(orphans, region, aws_profile=aws_profile)
        return 0

    try:
        return destroy(config, args)
    except Exception as exc:  # noqa: BLE001 - frontera del CLI
        print(f"\n[ERROR INESPERADO] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
