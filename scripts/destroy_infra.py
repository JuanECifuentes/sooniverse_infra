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
import shutil
import subprocess
import sys
import time
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


def _sky_down(cluster: str) -> bool:
    """`sky down -y <cluster>`. Devuelve True si tuvo éxito o el clúster ya no existía."""
    sky = _sky_binary()
    if not sky:
        print("[ERROR] El comando 'sky' no está en el PATH.")
        return False
    print(f"[EXEC] {sky} down -y {cluster}")
    result = subprocess.run([sky, "down", "-y", cluster], capture_output=True, text=True)
    if result.returncode != 0:
        stderr = (result.stderr or "").lower()
        if "does not exist" in stderr or "no existing cluster" in stderr:
            print(f"[SKIP] El clúster '{cluster}' ya no existe.")
            return True
        print(f"[ERROR] 'sky down {cluster}' falló:\n{result.stderr}")
        return False
    return True


def _wait_for_instances_terminated(clusters: List[str], region: str, timeout: int = 180) -> None:
    """Espera a que las instancias EC2 de estos clústeres SkyPilot lleguen a
    'terminated' antes de tocar la capa de red.

    'sky down' vuelve en cuanto AWS ACEPTA la petición de terminación, no
    cuando la instancia realmente desaparece -de 'shutting-down' a
    'terminated' pasan según AWS varios segundos-. Sin esta espera, el ENI de
    la instancia sigue "in-use" cuando el paso [3/3] intenta borrar los
    Security Groups/subredes/VPC, y falla con DependencyViolation
    (confirmado en una destrucción real: los 4 fallos fueron por un ENI que
    tardó en soltarse, no un problema real de tags/propiedad)."""
    try:
        import boto3
    except ImportError:
        return

    ec2 = boto3.client("ec2", region_name=region)
    filtro_valores = [c for cluster in clusters for c in (cluster, f"{cluster}-*")]
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        pendientes = []
        for tag_key in ("ray-cluster-name", "skypilot-cluster-name"):
            resp = ec2.describe_instances(
                Filters=[
                    {"Name": f"tag:{tag_key}", "Values": filtro_valores},
                    {"Name": "instance-state-name", "Values": ["shutting-down"]},
                ]
            )
            for reservation in resp.get("Reservations", []):
                pendientes.extend(i["InstanceId"] for i in reservation.get("Instances", []))
        if not pendientes:
            return
        print(f"[ESPERA] {len(pendientes)} instancia(s) todavía terminando ({pendientes}); "
              f"esperando antes de tocar la red...")
        time.sleep(10)
    print(f"[WARNING] Timeout ({timeout}s) esperando a que las instancias terminen; "
          "la limpieza de red podría fallar por dependencias -reintenta 'destroy_infra.py --yes'.")


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


def scan_orphans(region: str) -> List[Dict[str, Any]]:
    import boto3
    from aws_network import TAG_MANAGED

    ec2 = boto3.client("ec2", region_name=region)
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


def purge_orphans(orphans: List[Dict[str, Any]], region: str) -> None:
    import boto3
    from aws_network import DELETE_ORDER

    ec2 = boto3.client("ec2", region_name=region)
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

    if only in ("all",) and not args.dry_run:
        print("\n--- [1/3] Workers vLLM (sky down) ---")
        for wl in config["workloads"]:
            cluster = builder.worker_cluster(wl["id"])
            _sky_down(cluster)

        print("\n--- [2/3] Nodo Gateway (sky down) ---")
        _sky_down(builder.gateway_cluster)

        clusters = [builder.worker_cluster(wl["id"]) for wl in config["workloads"]] + [builder.gateway_cluster]
        _wait_for_instances_terminated(clusters, red["region"])
    elif only in ("all",):
        print("\n--- (dry-run) Se ejecutaría 'sky down' de workers y gateway ---")
        for wl in config["workloads"]:
            print(f"       sky down -y {builder.worker_cluster(wl['id'])}")
        print(f"       sky down -y {builder.gateway_cluster}")

    if red.get("gestion_red", "auto") != "auto":
        print("\n[SKIP] 'gestion_red: existente' -> la VPC/SGs no los gestiona este sistema; nada que destruir.")
        return 0

    print("\n--- [3/3] Capa de red AWS ---")
    from aws_network import AwsNetworkManager
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
    mgr = AwsNetworkManager(spec, state=state, deployment_id=deployment_id)

    report = mgr.destroy(dry_run=args.dry_run, force=args.force)

    if args.dry_run:
        kept_ids = {item.aws_id for item in report.kept_persistent}
        for item in mgr.plan_destroy():
            if item.aws_id in kept_ids:
                print(f"  [{item.delete_order:>3}] {item.component:<14} {item.aws_id or '(sin id)'} "
                      f"[CONSERVADO] gateway.dominio.eip_persistente=true")
                continue
            print(f"  [{item.delete_order:>3}] {item.component:<14} {item.aws_id or '(sin id)'} "
                  f"managed_by_us={item.managed_by_us}")
        return 0

    print(f"\n[REPORTE] Éxitos: {len(report.succeeded)} | Fallos: {len(report.failed)} | "
          f"Omitidos (no nuestros): {len(report.skipped_not_ours)} | "
          f"Conservados (dominio.eip_persistente): {len(report.kept_persistent)}")
    for item in report.kept_persistent:
        print(f"  [CONSERVADO] {item.component} {item.aws_id} (gateway.dominio.eip_persistente=true)")
    for failure in report.failed:
        item = failure["item"]
        print(f"  [FALLO] {item.component} {item.aws_id}: {failure['error']}")
    for action in report.manual_actions_required:
        print(f"  [MANUAL] {action}")

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
        region = config["red_y_aislamiento"]["region"]
        orphans = scan_orphans(region)
        print_orphans(orphans)
        if args.purge_orphans:
            if not args.yes:
                print("[ABORTADO] --purge-orphans requiere --yes.")
                return 1
            purge_orphans(orphans, region)
        return 0

    try:
        return destroy(config, args)
    except Exception as exc:  # noqa: BLE001 - frontera del CLI
        print(f"\n[ERROR INESPERADO] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
