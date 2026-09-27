#!/usr/bin/env python3
"""
Adjunta una Public IP Standard a la NIC del worker de un clúster SkyPilot en
Azure apenas aparece, en paralelo a 'sky launch' -que no podemos esperar,
porque el bootstrap de runtime de SkyPilot (miniconda/uv/ray, dentro del
propio 'sky launch') necesita salida a internet ANTES de devolvernos el
control.

Por qué existe: confirmado con Azure Network Watcher (connectionStatus=
Unreachable, 316/316 sondas fallidas, sin problemas de NSG/ruteo reportados)
que el NAT Gateway Standard de esta suscripción/región no funciona en el
plano de datos pese a que la API de control lo reporta 'Succeeded' -ver el
docstring de AzureNetworkManager.ensure_nat_gateway(). Confirmado también que
una Public IP asociada directamente a la NIC restaura la salida al instante
(Azure prefiere la IP pública de la propia NIC sobre el NAT Gateway del
subnet para SNAT).

Por qué NO se hace con 'use_internal_ips: false' en el manifiesto de
SkyPilot: eso cambiaría también la IP que SkyPilot usa para SSH/gestión del
clúster, lo que exigiría abrir el puerto 22 al 0.0.0.0/0 en el NSG de
workers para que 'sky launch' (corriendo fuera de la VNet) pudiera
conectarse -bajando el nivel de seguridad actual (hoy solo acepta SSH desde
dentro de la VNet, vía bastion). Este script deja 'use_internal_ips: true' y
el túnel por el Gateway intactos: la Public IP que adjunta es SOLO para
tráfico saliente (SNAT), nunca se usa para entrar. El NSG de workers sigue
bloqueando toda entrada que no venga de la VNet.
"""

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from azure_check_gpu_quota import _credential_and_subscription  # noqa: E402
from azure.mgmt.compute import ComputeManagementClient  # noqa: E402
from azure.mgmt.network import NetworkManagementClient  # noqa: E402
from azure.mgmt.network.models import PublicIPAddress, PublicIPAddressSku  # noqa: E402

NIC_ID_RE = re.compile(r"/networkInterfaces/([^/]+)", re.IGNORECASE)


def _matches_workload(tags: dict, workload_id: str) -> bool:
    # NO usar 'ray-cluster-name'/'skypilot-cluster-name': en Azure esos tags
    # llevan el nombre ABREVIADO/hasheado que SkyPilot genera internamente
    # (p.ej. 'sooniverse-cliente-test-azure-f3-97e585e4'), no el nombre
    # lógico del clúster -confirmado en un despliegue real (el matching por
    # esos tags nunca encontraba la VM, timeout silencioso). 'workload' es
    # nuestro propio tag (ver generate_infra.py TopologyBuilder.build_worker,
    # 'workload': wl['id']), con el valor exacto que ya conocemos.
    return tags.get("rol") == "worker" and tags.get("workload") == workload_id


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resource-group", required=True)
    parser.add_argument("--workload-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--poll-interval", type=int, default=5)
    # BYOC (Lighthouse): la suscripción del cliente, cuando difiere de la de
    # '.env' -ver generate_infra.py::_spawn_egress_ip_watcher, que la propaga
    # desde 'red_y_aislamiento.azure_subscription_id'.
    parser.add_argument("--subscription-id", default=None)
    args = parser.parse_args()

    credential, subscription_id = _credential_and_subscription(args.subscription_id)
    compute = ComputeManagementClient(credential, subscription_id)
    net = NetworkManagementClient(credential, subscription_id)

    deadline = time.time() + args.timeout
    attached_vms: set = set()

    while time.time() < deadline:
        for vm in compute.virtual_machines.list(args.resource_group):
            if vm.name in attached_vms or not _matches_workload(vm.tags or {}, args.workload_id):
                continue
            nics = vm.network_profile.network_interfaces if vm.network_profile else []
            if not nics:
                continue
            match = NIC_ID_RE.search(nics[0].id)
            if not match:
                continue
            nic_name = match.group(1)
            nic = net.network_interfaces.get(args.resource_group, nic_name)
            if not nic.ip_configurations:
                continue
            if nic.ip_configurations[0].public_ip_address:
                attached_vms.add(vm.name)
                continue

            pip_name = f"{nic_name}-egress-ip"
            print(f"[EGRESS-IP] Creando Public IP de salida para '{nic_name}'...", flush=True)
            pip = net.public_ip_addresses.begin_create_or_update(
                args.resource_group,
                pip_name,
                PublicIPAddress(
                    location=args.region,
                    sku=PublicIPAddressSku(name="Standard"),
                    public_ip_allocation_method="Static",
                ),
            ).result()
            nic.ip_configurations[0].public_ip_address = pip
            net.network_interfaces.begin_create_or_update(args.resource_group, nic_name, nic).result()
            print(f"[EGRESS-IP] '{nic_name}' -> {pip.ip_address} (solo salida, SSH sigue solo por el bastion)", flush=True)
            attached_vms.add(vm.name)

        if attached_vms:
            return 0
        time.sleep(args.poll_interval)

    print(f"[EGRESS-IP] timeout ({args.timeout}s) esperando la VM del workload '{args.workload_id}'.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
