#!/usr/bin/env python3
"""
==============================================================================
Sooniverse Infra - SkyPilot Multi-Node Generator & Provisioner (FASE 1)
==============================================================================
Lee 'config_global.yaml', valida el contrato y genera la topología distribuida:

  ┌───────────────────────────── VPC ──────────────────────────────┐
  │  Subred pública                    Subred privada               │
  │  ┌──────────────────────┐          ┌────────────────────────┐   │
  │  │ NODO GATEWAY (1)     │          │ WORKERS vLLM (N)       │   │
  │  │  LiteLLM  :4000      │─────────▶│  vllm :8007  (GPU)     │   │
  │  │  OpenWebUI:8080/80   │  interno │  sin IP pública        │   │
  │  │  Django   :8000      │          │  SSH vía bastion       │   │
  │  └──────────▲───────────┘          └────────────────────────┘   │
  └─────────────┼──────────────────────────────────────────────────┘
                │ público (80 / 4000 / 8000 / 8080)

Artefactos generados:
  .sky_generated.gateway.yaml          -> tarea SkyPilot del Nodo Gateway
  .sky_generated.worker-<id>.yaml      -> una tarea por workload (num_nodes = replicas)
  .sky_config_workers.yaml             -> config de cliente SkyPilot (VPC + IPs internas + bastion)
"""

import argparse
import hashlib
import ipaddress
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import yaml
except ImportError:
    print("[ERROR] La librería 'pyyaml' no está instalada. Ejecuta: pip install pyyaml")
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parent.parent

GATEWAY_MANIFEST = ".sky_generated.gateway.yaml"
WORKER_MANIFEST_FMT = ".sky_generated.worker-{wl_id}.yaml"
SKY_GATEWAY_CONFIG = ".sky_config_gateway.yaml"
SKY_WORKERS_CONFIG = ".sky_config_workers.yaml"
ENDPOINTS_CACHE = ".sooniverse_endpoints.json"

REMOTE_ROOT = "/home/ubuntu/sooniverse_infra"


class ConfigValidationError(Exception):
    """Excepción personalizada para errores de validación en config_global.yaml."""


# =============================================================================
# VALIDACIÓN DEL CONTRATO
# =============================================================================
class ConfigValidator:
    """Validador de esquema y reglas de negocio para la configuración global."""

    ALLOWED_ENTORNOS = {"prod", "dev"}
    ALLOWED_MODOS = {"byoc", "hosted"}
    ALLOWED_TAREAS = {"llm-texto", "embeddings"}
    ALLOWED_LB_STRATEGIES = {
        "latency-based-routing",
        "simple-shuffle",
        "least-busy",
        "usage-based-routing",
        "usage-based-routing-v2",
    }
    ALLOWED_GESTION_RED = {"auto", "existente"}
    ALLOWED_NAT_MODOS = {"single", "per-az", "none"}

    @classmethod
    def validate(cls, config: Dict[str, Any]) -> None:
        if not isinstance(config, dict):
            raise ConfigValidationError("El archivo de configuración debe ser un mapa YAML válido.")

        cls._validate_cliente(config)
        cls._validate_red(config)
        cls._validate_gateway(config)
        cls._validate_base_de_datos(config)
        cls._validate_workloads(config)

    # -- secciones -------------------------------------------------------------
    @classmethod
    def _validate_cliente(cls, config: Dict[str, Any]) -> None:
        cliente = config.get("cliente")
        if not cliente or not isinstance(cliente, dict):
            raise ConfigValidationError("Falta la sección obligatoria 'cliente'.")

        if not cliente.get("id") or not isinstance(cliente["id"], str):
            raise ConfigValidationError("Falta 'cliente.id' o no es una cadena válida.")

        if cliente.get("entorno") not in cls.ALLOWED_ENTORNOS:
            raise ConfigValidationError(
                f"'cliente.entorno' inválido: '{cliente.get('entorno')}'. Permitidos: {cls.ALLOWED_ENTORNOS}"
            )

        if cliente.get("modo") not in cls.ALLOWED_MODOS:
            raise ConfigValidationError(
                f"'cliente.modo' inválido: '{cliente.get('modo')}'. Permitidos: {cls.ALLOWED_MODOS}"
            )

    @classmethod
    def _validate_red(cls, config: Dict[str, Any]) -> None:
        red = config.get("red_y_aislamiento")
        if not red or not isinstance(red, dict):
            raise ConfigValidationError("Falta la sección obligatoria 'red_y_aislamiento'.")

        if not red.get("region"):
            raise ConfigValidationError("Falta 'red_y_aislamiento.region'.")

        tags = red.get("tags_obligatorios")
        if not tags or not isinstance(tags, dict):
            raise ConfigValidationError("Falta 'red_y_aislamiento.tags_obligatorios'.")

        privada = red.get("workers_en_subred_privada", True)
        if not isinstance(privada, bool):
            raise ConfigValidationError("'red_y_aislamiento.workers_en_subred_privada' debe ser booleano.")

        gestion_red = red.get("gestion_red", "auto")
        if gestion_red not in cls.ALLOWED_GESTION_RED:
            raise ConfigValidationError(
                f"'red_y_aislamiento.gestion_red' inválido: '{gestion_red}'. Permitidos: {cls.ALLOWED_GESTION_RED}"
            )

        if gestion_red == "existente":
            # Modo legado: la VPC/SGs ya existen y se referencian por nombre.
            if privada and not red.get("vpc_name"):
                print(
                    "[WARNING] 'workers_en_subred_privada: true' sin 'vpc_name'. SkyPilot usará la VPC por "
                    "defecto y sus subredes; verifica que exista una subred sin ruta directa a Internet "
                    "Gateway y con NAT, o los workers no podrán descargar el modelo."
                )
            return

        # gestion_red == "auto": AwsNetworkManager crea la VPC; validar el resto del contrato.
        cls._validate_red_auto(red)

    @classmethod
    def _validate_red_auto(cls, red: Dict[str, Any]) -> None:
        vpc_cidr_raw = red.get("vpc_cidr")
        if not vpc_cidr_raw:
            raise ConfigValidationError("Falta 'red_y_aislamiento.vpc_cidr' (requerido en modo 'auto').")
        try:
            vpc_net = ipaddress.ip_network(vpc_cidr_raw, strict=True)
        except ValueError as exc:
            raise ConfigValidationError(f"'red_y_aislamiento.vpc_cidr' inválido: {exc}") from exc

        azs = red.get("azs", 1)
        if not isinstance(azs, int) or azs < 1:
            raise ConfigValidationError("'red_y_aislamiento.azs' debe ser un entero >= 1.")

        nat = red.get("nat_gateway") or {}
        if not isinstance(nat, dict):
            raise ConfigValidationError("'red_y_aislamiento.nat_gateway' debe ser un mapa.")
        nat_modo = nat.get("modo", "single")
        if nat_modo not in cls.ALLOWED_NAT_MODOS:
            raise ConfigValidationError(
                f"'red_y_aislamiento.nat_gateway.modo' inválido: '{nat_modo}'. Permitidos: {cls.ALLOWED_NAT_MODOS}"
            )

        privada = red.get("workers_en_subred_privada", True)
        endpoints = red.get("vpc_endpoints") or {}
        if privada and nat_modo == "none" and not endpoints.get("s3"):
            raise ConfigValidationError(
                "'workers_en_subred_privada: true' con 'nat_gateway.modo: none' requiere al menos "
                "'vpc_endpoints.s3: true'; de lo contrario los workers no tendrán salida a internet "
                "para descargar el modelo ni acceder a otros servicios AWS."
            )

        # Validar CIDRs explícitos de subredes (si el operador los fija a mano en vez de dejar
        # el cálculo automático determinista).
        subredes = red.get("subredes") or {}
        for clave in ("publicas", "privadas"):
            cidrs = subredes.get(clave)
            if cidrs is None:
                continue
            if not isinstance(cidrs, list) or not cidrs:
                raise ConfigValidationError(f"'red_y_aislamiento.subredes.{clave}' debe ser una lista de CIDR.")
            for cidr in cidrs:
                try:
                    subnet = ipaddress.ip_network(cidr, strict=True)
                except ValueError as exc:
                    raise ConfigValidationError(
                        f"'red_y_aislamiento.subredes.{clave}' contiene un CIDR inválido '{cidr}': {exc}"
                    ) from exc
                if not subnet.subnet_of(vpc_net):
                    raise ConfigValidationError(
                        f"'red_y_aislamiento.subredes.{clave}': el CIDR '{cidr}' no está contenido en "
                        f"'vpc_cidr' ({vpc_cidr_raw})."
                    )

        all_subnet_cidrs = list(subredes.get("publicas") or []) + list(subredes.get("privadas") or [])
        seen_networks = []
        for cidr in all_subnet_cidrs:
            net = ipaddress.ip_network(cidr, strict=True)
            for other in seen_networks:
                if net.overlaps(other):
                    raise ConfigValidationError(
                        f"'red_y_aislamiento.subredes': los CIDR '{cidr}' y '{other}' se solapan."
                    )
            seen_networks.append(net)

    @classmethod
    def _validate_gateway(cls, config: Dict[str, Any]) -> None:
        gw = config.get("gateway")
        if not gw or not isinstance(gw, dict):
            raise ConfigValidationError("Falta la sección obligatoria 'gateway' (Fase 1).")

        if not isinstance(gw.get("habilitado", True), bool):
            raise ConfigValidationError("'gateway.habilitado' debe ser booleano.")

        puertos = gw.get("puertos_publicos")
        if not puertos or not isinstance(puertos, list) or not all(isinstance(p, int) for p in puertos):
            raise ConfigValidationError("'gateway.puertos_publicos' debe ser una lista de enteros.")

        strategy = gw.get("load_balancing_strategy", "latency-based-routing")
        if strategy not in cls.ALLOWED_LB_STRATEGIES:
            raise ConfigValidationError(
                f"'gateway.load_balancing_strategy' inválida: '{strategy}'. "
                f"Permitidas: {cls.ALLOWED_LB_STRATEGIES}"
            )

    @classmethod
    def _validate_base_de_datos(cls, config: Dict[str, Any]) -> None:
        db = config.get("base_de_datos")
        if not db or not isinstance(db, dict):
            raise ConfigValidationError("Falta la sección obligatoria 'base_de_datos'.")

        if "AUTO_INIT_DB" not in db:
            raise ConfigValidationError("Falta el flag 'base_de_datos.AUTO_INIT_DB' (true | false).")

        if not isinstance(db["AUTO_INIT_DB"], bool):
            raise ConfigValidationError("'base_de_datos.AUTO_INIT_DB' debe ser booleano (true | false).")

        schema_dir = db.get("schema_dir", "database")
        schema_path = REPO_ROOT / schema_dir
        if not schema_path.is_dir() or not any(schema_path.glob("*.sql")):
            raise ConfigValidationError(
                f"'base_de_datos.schema_dir' no existe o no contiene .sql: {schema_dir}"
            )

    @classmethod
    def _validate_workloads(cls, config: Dict[str, Any]) -> None:
        workloads = config.get("workloads")
        if not workloads or not isinstance(workloads, list):
            raise ConfigValidationError("Falta la sección 'workloads' o no contiene elementos.")

        vistos = set()
        for idx, wl in enumerate(workloads):
            if not isinstance(wl, dict):
                raise ConfigValidationError(f"El workload #{idx + 1} no es un objeto válido.")

            wl_id = wl.get("id")
            if not wl_id:
                raise ConfigValidationError(f"El workload #{idx + 1} requiere un 'id'.")
            if wl_id in vistos:
                raise ConfigValidationError(f"'workloads[].id' duplicado: '{wl_id}'.")
            vistos.add(wl_id)

            if wl.get("tipo_tarea") not in cls.ALLOWED_TAREAS:
                raise ConfigValidationError(
                    f"Workload '{wl_id}': 'tipo_tarea' inválido '{wl.get('tipo_tarea')}'. "
                    f"Permitidos: {cls.ALLOWED_TAREAS}"
                )

            if not wl.get("accelerator"):
                raise ConfigValidationError(f"Workload '{wl_id}': Requiere el campo 'accelerator'.")

            cantidad_gpus = wl.get("cantidad_gpus", 0)
            if not isinstance(cantidad_gpus, int) or cantidad_gpus <= 0:
                raise ConfigValidationError(
                    f"Workload '{wl_id}': 'cantidad_gpus' debe ser un entero positivo (> 0)."
                )

            replicas = wl.get("replicas", 1)
            if not isinstance(replicas, int) or replicas <= 0:
                raise ConfigValidationError(
                    f"Workload '{wl_id}': 'replicas' debe ser un entero positivo (> 0)."
                )

            puerto = wl.get("puerto")
            if not puerto or not isinstance(puerto, int):
                raise ConfigValidationError(f"Workload '{wl_id}': Debe especificar un 'puerto' entero.")


# =============================================================================
# SCRIPTS REMOTOS
# =============================================================================
GPU_SETUP_SCRIPT = """
set -euo pipefail

# A. Dependencias esenciales, driver estable y utilidad modprobe
sudo apt-get update && sudo apt-get install -y ubuntu-drivers-common build-essential \
  nvidia-driver-550-server nvidia-utils-550-server nvidia-modprobe

# B. Carga manual de módulos en caliente (evita reinicio de la instancia)
sudo modprobe nvidia
sudo modprobe nvidia-uvm
sudo nvidia-modprobe -u -c=0

# C. Docker Engine
if ! command -v docker &> /dev/null; then
    curl -fsSL https://get.docker.com -o get-docker.sh
    sudo sh get-docker.sh
    sudo usermod -aG docker ubuntu
fi

# D. NVIDIA Container Toolkit (expone la GPU a Docker)
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --yes --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit

# E. Runtime NVIDIA por defecto en Docker
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
sudo chmod 666 /var/run/docker.sock

# F. Verificación de salud
nvidia-smi

# G. Persistencia del cache de modelos
mkdir -p {remote_root}/docker_images/{modelo}/hf_cache
"""

WORKER_RUN_SCRIPT = """
set -euo pipefail
echo "===> [WORKER rank ${{SKYPILOT_NODE_RANK:-0}}] Desplegando vLLM ({wl_id})"
cd {remote_root}/docker_images/{modelo}

export MODEL_NAME="${{MODEL_NAME}}"
export GPU_MEMORY_UTILIZATION="${{GPU_MEMORY_UTILIZATION}}"
export MAX_MODEL_LEN="${{MAX_MODEL_LEN}}"

sudo docker compose up -d
sudo docker compose ps

# El worker solo escucha en la red interna de la VPC; LiteLLM en el Gateway lo consume.
SELF_IP=$(hostname -I | awk '{{print $1}}')

# Marcadores parseables por scripts/sync_endpoints.py para descubrir el pool.
echo "SOONIVERSE_WORKER_READY={wl_id}|${{SELF_IP}}|{puerto}"
if [ "${{SKYPILOT_NODE_RANK:-0}}" = "0" ]; then
    echo "SOONIVERSE_NODE_IPS=$(echo "${{SKYPILOT_NODE_IPS:-$SELF_IP}}" | tr '\\n' ',' | sed 's/,$//')"
fi
echo "===> Worker listo en ${{SELF_IP}}:{puerto}"
"""

GATEWAY_SETUP_SCRIPT = """
set -euo pipefail

# A. Dependencias base (sin GPU: el Gateway es CPU-only)
sudo apt-get update
sudo apt-get install -y curl jq python3-pip postgresql-client

# B. Docker Engine + Compose plugin
if ! command -v docker &> /dev/null; then
    curl -fsSL https://get.docker.com -o get-docker.sh
    sudo sh get-docker.sh
    sudo usermod -aG docker ubuntu
fi
sudo systemctl enable --now docker
sudo chmod 666 /var/run/docker.sock

# C. Dependencias Python del orquestador local (db_setup / render de config)
pip3 install --break-system-packages --quiet "psycopg2-binary>=2.9" "pyyaml>=6.0" || \
  pip3 install --quiet "psycopg2-binary>=2.9" "pyyaml>=6.0"

# D. El Gateway actúa como bastion SSH hacia los workers privados
sudo sed -i 's/^#*AllowTcpForwarding.*/AllowTcpForwarding yes/' /etc/ssh/sshd_config
sudo systemctl reload ssh || sudo systemctl reload sshd || true

mkdir -p {remote_root}/docker_images/gateway/data
echo "===> Gateway aprovisionado."
"""

GATEWAY_RUN_SCRIPT = """
set -euo pipefail
cd {remote_root}

echo "===> [GATEWAY] Cliente ${{CLIENTE_ID}} (${{ENTORNO}})"

# ---------------------------------------------------------------------------
# 1. Inicialización opcional de la base de datos (flag AUTO_INIT_DB del contrato)
# ---------------------------------------------------------------------------
if [ "${{AUTO_INIT_DB}}" = "true" ]; then
    echo "===> AUTO_INIT_DB=true -> ingestando {schema_dir}/*.sql (orden lexicográfico)"
    REFRESH_FLAG=""
    if [ "${{AUTO_REFRESH_METRICS}}" = "true" ]; then REFRESH_FLAG="--refresh"; fi
    python3 scripts/db_setup.py --env-file .env --sql-dir {schema_dir} ${{REFRESH_FLAG}}
else
    echo "===> AUTO_INIT_DB=false -> se omite la inicialización automática de la BD."
    echo "     Ejecuta manualmente: python scripts/db_setup.py"
fi

# ---------------------------------------------------------------------------
# 2. Render del config.yaml de LiteLLM con las IPs privadas de los workers
#    WORKER_ENDPOINTS es inyectado por SkyPilot (JSON). Vacío en el primer
#    arranque: `scripts/sync_endpoints.py` lo rellena al levantar los workers.
# ---------------------------------------------------------------------------
python3 scripts/render_litellm_config.py \
    --endpoints-json "${{WORKER_ENDPOINTS}}" \
    --strategy "${{LB_STRATEGY}}" \
    --output docker_images/gateway/litellm_config.yaml

# ---------------------------------------------------------------------------
# 3. Levantar el stack del Gateway
# ---------------------------------------------------------------------------
cd {remote_root}/docker_images/gateway
sudo -E docker compose --env-file {remote_root}/.env up -d --build
sudo docker compose ps

echo "===> Gateway operativo:"
echo "     LiteLLM      -> http://$(curl -s ifconfig.me):4000"
echo "     Open WebUI   -> http://$(curl -s ifconfig.me):8080"
echo "     Panel Django -> http://$(curl -s ifconfig.me):8000/metrics/"
"""


# =============================================================================
# BUILDERS DE MANIFIESTOS SKYPILOT
# =============================================================================
class TopologyBuilder:
    """Construye las especificaciones SkyPilot del Gateway y de los Workers."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.cliente = config["cliente"]
        self.red = config["red_y_aislamiento"]
        self.gateway = config.get("gateway", {})
        self.db = config.get("base_de_datos", {})
        self.workloads = config["workloads"]
        self._network_outputs = None  # poblado por apply_network_outputs() en modo 'auto'

    def apply_network_outputs(self, outputs: Any) -> None:
        """Inyecta los IDs/nombres reales creados por AwsNetworkManager (modo
        'gestion_red: auto'), para que build_sky_gateway_config/
        build_sky_workers_config referencien la VPC/SGs recién creados en vez
        de los nombres estáticos del contrato."""
        self._network_outputs = outputs

    # -- naming ---------------------------------------------------------------
    @property
    def base_name(self) -> str:
        return f"sooniverse-{self.cliente['id']}-{self.cliente['entorno']}"

    @property
    def gateway_cluster(self) -> str:
        return f"{self.base_name}-gw"

    def worker_cluster(self, wl_id: str) -> str:
        # SkyPilot exige nombres cortos y en minúsculas con guiones.
        return f"{self.base_name}-{wl_id}".lower().replace("_", "-").replace(".", "-")

    # -- comunes --------------------------------------------------------------
    def _base_envs(self) -> Dict[str, str]:
        return {
            "CLIENTE_ID": self.cliente["id"],
            "ENTORNO": self.cliente["entorno"],
            "MODO": self.cliente["modo"],
        }

    # -- gateway --------------------------------------------------------------
    def build_gateway(self, worker_endpoints: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        gw = self.gateway
        resources: Dict[str, Any] = {
            "cloud": "aws",
            "region": self.red["region"],
            "instance_type": gw.get("tipo_instancia", "t3.large"),
            "disk_size": gw.get("disk_size", 100),
            # Única superficie pública del clúster.
            "ports": gw.get("puertos_publicos", [80, 4000, 8000, 8080]),
            "labels": {**self.red.get("tags_obligatorios", {}), "rol": "gateway"},
        }

        envs = {
            **self._base_envs(),
            "ROL_NODO": "gateway",
            "AUTO_INIT_DB": str(self.db.get("AUTO_INIT_DB", True)).lower(),
            "AUTO_REFRESH_METRICS": str(self.db.get("auto_refresh_metrics", True)).lower(),
            "LB_STRATEGY": gw.get("load_balancing_strategy", "latency-based-routing"),
            "WEBUI_SIGNUP": str(gw.get("open_webui", {}).get("signup_habilitado", False)).lower(),
            "METRICS_REFRESH_INTERVAL": str(
                gw.get("django_metrics", {}).get("metrics_refresh_interval", 300)
            ),
            # Lista de endpoints vLLM en JSON; se rellena tras aprovisionar los workers.
            "WORKER_ENDPOINTS": json.dumps(worker_endpoints or []),
        }

        file_mounts = {
            f"{REMOTE_ROOT}/docker_images/gateway": "./docker_images/gateway",
            f"{REMOTE_ROOT}/database": "./database",
            f"{REMOTE_ROOT}/scripts": "./scripts",
            f"{REMOTE_ROOT}/django_metrics": "./django_metrics",
            f"{REMOTE_ROOT}/config_global.yaml": "./config_global.yaml",
            f"{REMOTE_ROOT}/.env": "./.env",
        }

        schema_dir = self.db.get("schema_dir", "database")

        return {
            "name": self.gateway_cluster,
            "resources": resources,
            "num_nodes": 1,
            "file_mounts": file_mounts,
            "envs": envs,
            "setup": GATEWAY_SETUP_SCRIPT.format(remote_root=REMOTE_ROOT).strip(),
            "run": GATEWAY_RUN_SCRIPT.format(remote_root=REMOTE_ROOT, schema_dir=schema_dir).strip(),
        }

    # -- workers --------------------------------------------------------------
    def build_worker(self, wl: Dict[str, Any]) -> Dict[str, Any]:
        modelo = wl.get("modelo", wl["id"])
        frac = wl.get("asignacion_fraccional", {})

        resources: Dict[str, Any] = {
            "cloud": "aws",
            "region": self.red["region"],
            "accelerators": f"{wl['accelerator']}:{wl['cantidad_gpus']}",
            "labels": {**self.red.get("tags_obligatorios", {}), "rol": "worker", "workload": wl["id"]},
            # El puerto se declara para que SkyPilot abra la regla en el Security
            # Group; sin ella el Gateway tampoco podría alcanzar al worker DENTRO
            # de la VPC. La privacidad no la da esta lista, la da
            # `use_internal_ips: true`: el worker no recibe IP pública, así que la
            # regla solo es alcanzable desde dentro de la VPC.
            # Para restringir el origen a nivel de CIDR, define
            # `red_y_aislamiento.security_group_workers` con un SG propio.
            "ports": [wl["puerto"]],
        }

        if self.red.get("image_id"):
            resources["image_id"] = self.red["image_id"]
        if wl.get("tipo_instancia"):
            resources["instance_type"] = wl["tipo_instancia"]

        envs = {
            **self._base_envs(),
            "ROL_NODO": "worker",
            "WORKLOAD_ID": wl["id"],
            "MODEL_NAME": wl.get("hf_repo", "cyankiwi/Qwen3.5-2B-AWQ-4bit"),
            "MODEL_PUBLIC_NAME": wl.get("nombre_publico", wl["id"]),
            "GPU_MEMORY_UTILIZATION": str(frac.get("gpu_memory_utilization", 0.95)),
            "MAX_MODEL_LEN": str(frac.get("max_model_len", 16384)),
            "VLLM_PORT": str(wl["puerto"]),
        }

        return {
            "name": self.worker_cluster(wl["id"]),
            "resources": resources,
            "num_nodes": wl.get("replicas", 1),
            "file_mounts": {
                f"{REMOTE_ROOT}/docker_images/{modelo}": f"./docker_images/{modelo}",
            },
            "envs": envs,
            "setup": GPU_SETUP_SCRIPT.format(remote_root=REMOTE_ROOT, modelo=modelo).strip(),
            "run": WORKER_RUN_SCRIPT.format(
                remote_root=REMOTE_ROOT, modelo=modelo, wl_id=wl["id"], puerto=wl["puerto"]
            ).strip(),
        }

    # -- config de cliente SkyPilot para el Nodo Gateway ------------------------
    def build_sky_gateway_config(self) -> Dict[str, Any]:
        """
        Fuerza al Nodo Gateway a nacer en la misma VPC que los workers (con su
        propio Security Group reservado), para que el túnel SSH bastion y las
        rutas internas a la subred privada funcionen. Sin esto, SkyPilot puede
        elegir la VPC por defecto de la cuenta, aislando al Gateway de los
        workers aunque ambos estén "arriba".
        """
        aws_cfg: Dict[str, Any] = {}
        net = self._network_outputs

        vpc_name = net.vpc_name if net else self.red.get("vpc_name")
        sg_gateway = net.sg_gateway_name if net else self.red.get("security_group_gateway")

        if vpc_name:
            aws_cfg["vpc_name"] = vpc_name
        if sg_gateway:
            aws_cfg["security_group_name"] = sg_gateway

        return {"aws": aws_cfg} if aws_cfg else {}

    # -- config de cliente SkyPilot para los workers privados ------------------
    def build_sky_workers_config(self, gateway_ip: Optional[str] = None) -> Dict[str, Any]:
        """
        Genera la configuración de cliente de SkyPilot que fuerza a los workers a
        vivir dentro de la VPC sin IP pública, tunelizando SSH por el Gateway.
        """
        aws_cfg: Dict[str, Any] = {}
        net = self._network_outputs

        vpc_name = net.vpc_name if net else self.red.get("vpc_name")
        if vpc_name:
            aws_cfg["vpc_name"] = vpc_name

        # Security Group: en modo 'auto' es el que crea AwsNetworkManager (SG->SG
        # con el gateway); en modo 'existente' es el pre-creado por el operador.
        sg_workers = net.sg_workers_name if net else self.red.get("security_group_workers")
        if sg_workers:
            aws_cfg["security_group_name"] = sg_workers

        if self.red.get("workers_en_subred_privada", True):
            aws_cfg["use_internal_ips"] = True
            if gateway_ip:
                gateway_ssh_key = (
                    Path.home() / ".sky" / "generated" / "ssh-keys" / f"{self.gateway_cluster}.key"
                )
                if gateway_ssh_key.exists():
                    os.chmod(gateway_ssh_key, 0o600)
                aws_cfg["ssh_proxy_command"] = (
                    f"ssh -W %h:%p -o StrictHostKeyChecking=no "
                    f"-o UserKnownHostsFile=/dev/null -i {gateway_ssh_key} ubuntu@{gateway_ip}"
                )

        return {"aws": aws_cfg} if aws_cfg else {}


# =============================================================================
# IO
# =============================================================================
HEADER = (
    "# ==============================================================================\n"
    "# ARCHIVO GENERADO AUTOMÁTICAMENTE POR SOONIVERSE INFRA GENERATOR\n"
    "# NO EDITAR MANUALMENTE. MODIFICAR config_global.yaml EN SU LUGAR.\n"
    "# ==============================================================================\n\n"
)


def load_config(config_path: str) -> Dict[str, Any]:
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"No se encontró el archivo de configuración: {config_path}")

    with path.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    ConfigValidator.validate(config)
    return config


def dump_yaml(data: Dict[str, Any], out_path: Path, header: bool = True) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        if header:
            f.write(HEADER)
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


def build_network_spec_from_config(config: Dict[str, Any]) -> "Any":
    """Traduce `red_y_aislamiento` + `gateway` + `workloads[].puerto` del contrato
    a un `aws_network.NetworkSpec`. Solo tiene sentido en modo 'gestion_red: auto'."""
    from aws_network import NetworkSpec  # import perezoso: boto3 solo hace falta aquí

    cliente = config["cliente"]
    red = config["red_y_aislamiento"]
    gw = config.get("gateway", {})
    nat = red.get("nat_gateway") or {}
    endpoints = red.get("vpc_endpoints") or {}
    subredes = red.get("subredes") or {}
    tls = gw.get("tls") or {}

    worker_ports = sorted({wl["puerto"] for wl in config["workloads"]})

    return NetworkSpec(
        client_id=cliente["id"],
        environment=cliente["entorno"],
        region=red["region"],
        vpc_cidr=red.get("vpc_cidr", "10.0.0.0/16"),
        az_count=red.get("azs", 1),
        public_subnet_cidrs=subredes.get("publicas"),
        private_subnet_cidrs=subredes.get("privadas"),
        nat_mode=nat.get("modo", "single"),
        enable_s3_endpoint=bool(endpoints.get("s3", True)),
        admin_cidrs=[red.get("cidr_admin_ssh", "0.0.0.0/0")],
        public_cidrs=[red.get("cidr_permitido_gateway", "0.0.0.0/0")],
        gateway_public_ports=gw.get("puertos_publicos", [80, 4000, 8000, 8080]),
        worker_ports=worker_ports,
        expose_direct_ports=bool(gw.get("exponer_puertos_directos", False)),
        tls_enabled=bool(tls.get("habilitado", False)),
        nat_timeout_seconds=nat.get("timeout_segundos", 300),
        extra_tags=red.get("tags_obligatorios") or {},
    )


def config_hash_of(config: Dict[str, Any]) -> str:
    """sha256 determinista del contrato completo (para detectar cambios entre corridas)."""
    canonical = json.dumps(config, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def generate_manifests(config: Dict[str, Any], out_dir: Path, builder: Optional["TopologyBuilder"] = None) -> Dict[str, Any]:
    """Escribe todos los manifiestos de la topología y devuelve sus rutas."""
    builder = builder or TopologyBuilder(config)
    artefactos: Dict[str, Any] = {"gateway": None, "workers": {}, "sky_config": None}

    if builder.gateway.get("habilitado", True):
        gw_path = out_dir / GATEWAY_MANIFEST
        dump_yaml(builder.build_gateway(), gw_path)
        artefactos["gateway"] = gw_path
        print(f"[OK] Gateway     -> {gw_path.name}  (cluster: {builder.gateway_cluster})")

    for wl in config["workloads"]:
        wk_path = out_dir / WORKER_MANIFEST_FMT.format(wl_id=wl["id"])
        dump_yaml(builder.build_worker(wl), wk_path)
        artefactos["workers"][wl["id"]] = wk_path
        print(
            f"[OK] Worker '{wl['id']}' -> {wk_path.name}  "
            f"(cluster: {builder.worker_cluster(wl['id'])}, nodos: {wl.get('replicas', 1)})"
        )

    sky_cfg = builder.build_sky_workers_config()
    if sky_cfg:
        cfg_path = out_dir / SKY_WORKERS_CONFIG
        dump_yaml(sky_cfg, cfg_path)
        artefactos["sky_config"] = cfg_path
        print(f"[OK] SkyPilot cfg -> {cfg_path.name}  (VPC / IPs internas / bastion)")

    return artefactos


# =============================================================================
# ORQUESTACIÓN DE DESPLIEGUE
# =============================================================================
def _sky_binary() -> Optional[str]:
    return shutil.which("sky")


def _run_sky(args: List[str], env: Optional[Dict[str, str]] = None) -> None:
    sky = _sky_binary()
    if not sky:
        raise RuntimeError(
            "El comando 'sky' (SkyPilot) no está en el PATH. Instala con: pip install \"skypilot[aws]\""
        )
    cmd = [sky] + args
    print(f"[EXEC] {' '.join(cmd)}")
    merged_env = {**os.environ, **(env or {})}
    subprocess.run(cmd, check=True, env=merged_env)


def _gateway_public_ip(cluster: str) -> Optional[str]:
    sky = _sky_binary()
    if not sky:
        return None
    try:
        out = subprocess.run(
            [sky, "status", "--ip", cluster], check=True, capture_output=True, text=True
        )
        ip = out.stdout.strip().splitlines()[-1].strip()
        return ip or None
    except (subprocess.CalledProcessError, IndexError):
        return None


PHASE_ORDER = ["network", "gateway", "workers", "endpoints", "verify"]

# Compatibilidad con los valores antiguos de --only (antes de la Fase 3).
_ONLY_LEGACY_ALIASES = {"all": set(PHASE_ORDER), "gateway": {"gateway"}, "workers": {"workers"}}


def _phases_for(only: str) -> set:
    if only in _ONLY_LEGACY_ALIASES:
        return _ONLY_LEGACY_ALIASES[only]
    if only in PHASE_ORDER:
        return {only}
    raise ValueError(f"--only inválido: {only}")


def _open_state_store(config: Dict[str, Any]):
    """Abre (o recupera) el despliegue activo en PostgreSQL. Si la BD no es
    alcanzable, lanza ANTES de que se cree nada en AWS (guardia de la Fase 2)."""
    from infra_state import PostgresInfraStateStore

    cliente = config["cliente"]
    red = config["red_y_aislamiento"]
    store = PostgresInfraStateStore()
    store.ping()  # aborta aquí si PostgreSQL no responde
    deployment_id = store.open_deployment(
        client_id=cliente["id"],
        environment=cliente["entorno"],
        region=red["region"],
        config_hash=config_hash_of(config),
        config_snapshot=config,
    )
    return store, deployment_id


def deploy(
    config: Dict[str, Any],
    artefactos: Dict[str, Any],
    only: str = "all",
    out_dir: Optional[Path] = None,
    config_path: Optional[Path] = None,
    dry_run: bool = False,
) -> None:
    """
    Máquina de fases (ver docs/01_FLUJO_DESPLIEGUE.md):
      network  -> AwsNetworkManager.provision() (VPC/subredes/NAT/SGs). Se omite
                  si 'gestion_red: existente'.
      gateway  -> sky launch del Gateway en la subred pública; captura su IP.
      workers  -> regenera el bastion con esa IP y lanza los workers en la
                  subred privada.
      endpoints-> sync_endpoints.py --apply (descubre IPs, recarga LiteLLM).
      verify   -> scripts/verify_deployment.py (best-effort, no aborta 'all').
    Cada fase es reanudable: los `ensure_*` de AwsNetworkManager y el propio
    `sky launch` son idempotentes, así que repetir una fase ya aplicada es
    seguro y rápido.
    """
    out_dir = out_dir or REPO_ROOT
    config_path = config_path or (REPO_ROOT / "config_global.yaml")
    red = config["red_y_aislamiento"]
    phases = _phases_for(only)

    print("\n" + "=" * 74)
    print(" DESPLIEGUE SOONIVERSE - MÁQUINA DE FASES (FASE 3)")
    print("=" * 74)

    builder = TopologyBuilder(config)
    gateway_ip: Optional[str] = None
    state = None
    deployment_id = None

    # --- FASE: state (siempre, si gestion_red == auto) ----------------------
    if red.get("gestion_red", "auto") == "auto":
        if dry_run:
            # --dry-run no debe escribir en PostgreSQL: solo lee un despliegue
            # activo si ya existe, nunca abre uno nuevo.
            from infra_state import PostgresInfraStateStore

            state = PostgresInfraStateStore()
            state.ping()
            existing = state.get_active_deployment(
                config["cliente"]["id"], config["cliente"]["entorno"], red["region"]
            )
            deployment_id = existing["deployment_id"] if existing else None
            print(f"[ESTADO] (dry-run, solo lectura) deployment_id={deployment_id or '(ninguno todavía)'}")
        else:
            t0 = time.monotonic()
            state, deployment_id = _open_state_store(config)
            print(f"[ESTADO] deployment_id={deployment_id} ({time.monotonic() - t0:.1f}s)")

    # --- FASE: network --------------------------------------------------------
    if "network" in phases:
        print("\n--- [RED] Red AWS (VPC/subredes/NAT/Security Groups) ---")
        if red.get("gestion_red", "auto") == "auto" and dry_run and not deployment_id:
            # Sin despliegue previo: no hay nada que leer y, para no escribir en
            # PostgreSQL durante un dry-run, no se instancia AwsNetworkManager
            # (su constructor abriría un deployment_id nuevo si no se le pasa uno).
            print("[RED] --dry-run: no existe un despliegue previo para "
                  f"{config['cliente']['id']}/{config['cliente']['entorno']}/{red['region']}. "
                  "Se crearía una VPC, subredes, NAT, route tables y Security Groups nuevos.")
        elif red.get("gestion_red", "auto") == "auto":
            from aws_network import AwsNetworkManager

            spec = build_network_spec_from_config(config)
            mgr = AwsNetworkManager(spec, state=state, deployment_id=deployment_id)
            if dry_run:
                print("[RED] --dry-run: no se ejecuta ninguna llamada mutante a AWS.")
                for item in mgr.plan_destroy():
                    print(f"       (existente) {item.component} {item.aws_id}")
            else:
                t0 = time.monotonic()
                network_outputs = mgr.provision()
                print(f"[RED] VPC={network_outputs.vpc_id} ({network_outputs.vpc_name}) "
                      f"SG-gateway={network_outputs.sg_gateway_id} SG-workers={network_outputs.sg_workers_id} "
                      f"({time.monotonic() - t0:.1f}s)")
                builder.apply_network_outputs(network_outputs)

                # El render de manifiestos depende de los IDs reales de red: regenerarlos ahora.
                artefactos = generate_manifests(config, out_dir, builder=builder)
        else:
            print("[SKIP] 'gestion_red: existente' -> se omite AwsNetworkManager (VPC/SGs manuales).")

    # --- FASE: gateway ----------------------------------------------------------
    if "gateway" in phases and artefactos.get("gateway") and dry_run:
        print("\n--- [GATEWAY] --dry-run: se lanzaría "
              f"'sky launch -y -c {builder.gateway_cluster} {artefactos['gateway']}' ---")
    elif "gateway" in phases and artefactos.get("gateway"):
        print("\n--- [GATEWAY] Nodo Gateway (público) ---")

        gw_cfg = builder.build_sky_gateway_config()
        gateway_env: Dict[str, str] = {}
        if gw_cfg:
            gw_cfg_path = REPO_ROOT / SKY_GATEWAY_CONFIG
            dump_yaml(gw_cfg, gw_cfg_path)
            gateway_env["SKYPILOT_CONFIG"] = str(gw_cfg_path)
            print(f"[INFO] SkyPilot usará {gw_cfg_path.name} (misma VPC que los workers)")

        t0 = time.monotonic()
        _run_sky(
            ["launch", "-y", "-c", builder.gateway_cluster, str(artefactos["gateway"])],
            env=gateway_env,
        )
        if state and deployment_id:
            state.log_event(deployment_id, "gateway", "sky_launch", "ok",
                             message=builder.gateway_cluster, duration_ms=int((time.monotonic() - t0) * 1000))

    if artefactos.get("gateway") and not dry_run:
        gateway_ip = _gateway_public_ip(builder.gateway_cluster)
        print(f"[INFO] IP pública del Gateway: {gateway_ip or 'no disponible'}")

    # --- FASE: workers (regenera el bastion con la IP real del gateway) --------
    if "workers" in phases and dry_run:
        clusters = ", ".join(builder.worker_cluster(wl["id"]) for wl in config["workloads"])
        print(f"\n--- [WORKERS] --dry-run: se lanzarían: {clusters} ---")
    elif "workers" in phases:
        print("\n--- [WORKERS] Workers vLLM (subred privada) ---")

        sky_cfg = builder.build_sky_workers_config(gateway_ip=gateway_ip)
        worker_env: Dict[str, str] = {}
        if sky_cfg:
            cfg_path = REPO_ROOT / SKY_WORKERS_CONFIG
            dump_yaml(sky_cfg, cfg_path)
            worker_env["SKYPILOT_CONFIG"] = str(cfg_path)
            print(f"[INFO] SkyPilot usará {cfg_path.name} (use_internal_ips + bastion)")

        for wl in config["workloads"]:
            cluster = builder.worker_cluster(wl["id"])
            manifest = artefactos["workers"][wl["id"]]
            print(f"\n> Workload '{wl['id']}' ({wl.get('replicas', 1)} nodo/s)")
            t0 = time.monotonic()
            _run_sky(["launch", "-y", "-c", cluster, str(manifest)], env=worker_env)
            if state and deployment_id:
                state.log_event(deployment_id, "workers", "sky_launch", "ok",
                                 message=cluster, duration_ms=int((time.monotonic() - t0) * 1000))

    # --- FASE: endpoints --------------------------------------------------------
    if "endpoints" in phases and dry_run:
        print("\n--- [ENDPOINTS] --dry-run: se ejecutaría sync_endpoints.py --apply ---")
    elif "endpoints" in phases:
        print("\n--- [ENDPOINTS] Sincronización de endpoints en LiteLLM ---")
        sync_script = REPO_ROOT / "scripts" / "sync_endpoints.py"
        cmd = [sys.executable, str(sync_script), "--config", str(config_path), "--apply"]
        print(f"[EXEC] {' '.join(cmd)}")
        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as exc:
            print(f"[WARNING] La sincronización automática falló (código {exc.returncode}).")
            print("          Reintenta manualmente: python scripts/sync_endpoints.py --apply")

    # --- FASE: verify (best-effort; no aborta el resto del pipeline) -----------
    if "verify" in phases and dry_run:
        print("\n--- [VERIFY] --dry-run: se ejecutaría verify_deployment.py ---")
    elif "verify" in phases:
        print("\n--- [VERIFY] Verificación de despliegue ---")
        verify_script = REPO_ROOT / "scripts" / "verify_deployment.py"
        if verify_script.exists():
            result = subprocess.run(
                [sys.executable, str(verify_script), "--config", str(config_path)]
            )
            if result.returncode != 0:
                print(f"[WARNING] verify_deployment.py reportó fallos (código {result.returncode}).")
        else:
            print("[SKIP] scripts/verify_deployment.py no existe todavía.")

    if state and deployment_id and not dry_run:
        try:
            resources = state.list_resources(deployment_id)
            healthy = all(r.get("state") in ("active", "creating") for r in resources)
            state.set_deployment_status(deployment_id, "active" if healthy else "degraded")
        except Exception as exc:  # noqa: BLE001 - no debe tumbar el reporte final por un fallo de estado
            print(f"[WARNING] No se pudo actualizar el estado final del despliegue: {exc}")

    if gateway_ip:
        print("\n" + "=" * 74)
        print(f" LiteLLM      : http://{gateway_ip}:4000")
        print(f" Open WebUI   : http://{gateway_ip}:8080")
        print(f" Panel Django : http://{gateway_ip}:8000/metrics/")
        if deployment_id:
            print(f" deployment_id: {deployment_id}")
        print("=" * 74)


# =============================================================================
# CLI
# =============================================================================
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generador y aprovisionador multi-nodo de infraestructura Sooniverse (SkyPilot)."
    )
    parser.add_argument("--config", default=str(REPO_ROOT / "config_global.yaml"),
                        help="Ruta al contrato central de configuración")
    parser.add_argument("--out-dir", default=str(REPO_ROOT),
                        help="Directorio donde se escriben los manifiestos generados")
    parser.add_argument("--run", action="store_true",
                        help="Aprovisiona la topología completa en AWS tras generar los manifiestos")
    parser.add_argument(
        "--only", choices=["all", "network", "gateway", "workers", "endpoints", "verify"], default="all",
        help="Limita el aprovisionamiento a una fase de la topología",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Con --run: solo genera manifiestos e imprime lo que se haría, sin llamadas mutantes a AWS/SkyPilot",
    )
    parser.add_argument("--init-db", action="store_true",
                        help="Ejecuta scripts/db_setup.py localmente (ignora el flag AUTO_INIT_DB)")
    parser.add_argument("--no-auto-init-db", action="store_true",
                        help="Fuerza AUTO_INIT_DB=false en esta ejecución sin editar el YAML")

    args = parser.parse_args()

    print("[SOONIVERSE INFRA] Leyendo contrato central...")
    try:
        config = load_config(args.config)

        if args.no_auto_init_db:
            config.setdefault("base_de_datos", {})["AUTO_INIT_DB"] = False
            print("[INFO] Override de CLI: AUTO_INIT_DB=false")

        artefactos = generate_manifests(config, Path(args.out_dir))

        auto_init = config.get("base_de_datos", {}).get("AUTO_INIT_DB", True)
        print(f"[INFO] AUTO_INIT_DB = {str(auto_init).lower()} "
              f"({'la BD se inicializa en el despliegue' if auto_init else 'inicialización manual'})")

        if args.init_db:
            print("\n--- Inicialización local de la base de datos ---")
            subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "db_setup.py"), "--refresh"], check=True)

        if args.run:
            deploy(
                config, artefactos, only=args.only,
                out_dir=Path(args.out_dir), config_path=Path(args.config),
                dry_run=args.dry_run,
            )
        else:
            print("\n[INFO] Para aprovisionar la topología en AWS:")
            print("       python scripts/generate_infra.py --run")
            print("       python scripts/generate_infra.py --run --dry-run          # plan, sin tocar AWS")
            print("       python scripts/generate_infra.py --run --only network     # solo la capa de red")
            print("       python scripts/generate_infra.py --run --only gateway     # solo el gateway")

    except ConfigValidationError as e:
        print(f"\n[ERROR DE CONFIGURACIÓN] {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - frontera del CLI
        print(f"\n[ERROR INESPERADO] {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
