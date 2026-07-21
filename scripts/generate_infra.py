#!/usr/bin/env python3
"""
==============================================================================
Sooniverse Infra - SkyPilot Infrastructure Generator & Provisioner
==============================================================================
Script principal para leer 'config_global.yaml', validar las reglas de negocio,
generar dinámicamente '.sky_generated.yaml' para SkyPilot en AWS, y
opcionalmente ejecutar la infraestructura de manera transparente.
"""

import argparse
import os
import shutil
import subprocess
import sys
from typing import Any, Dict, List

try:
    import yaml
except ImportError:
    print("[ERROR] La librería 'pyyaml' no está instalada. Ejecuta: pip install pyyaml")
    sys.exit(1)


class ConfigValidationError(Exception):
    """Excepción personalizada para errores de validación en config_global.yaml."""
    pass


class ConfigValidator:
    """Validador de esquema y reglas de negocio para la configuración global."""

    ALLOWED_ENTORNOS = {"prod", "dev"}
    ALLOWED_MODOS = {"byoc", "hosted"}
    ALLOWED_TAREAS = {"llm-texto", "embeddings"}

    @classmethod
    def validate(cls, config: Dict[str, Any]) -> None:
        """
        Valida que la estructura del diccionario cumpla con el contrato de Sooniverse.
        """
        if not isinstance(config, dict):
            raise ConfigValidationError("El archivo de configuración debe ser un mapa YAML válido.")

        # 1. Validar sección cliente
        cliente = config.get("cliente")
        if not cliente or not isinstance(cliente, dict):
            raise ConfigValidationError("Falta la sección obligatoria 'cliente'.")

        cliente_id = cliente.get("id")
        if not cliente_id or not isinstance(cliente_id, str):
            raise ConfigValidationError("Falta 'cliente.id' o no es una cadena válida.")

        entorno = cliente.get("entorno")
        if entorno not in cls.ALLOWED_ENTORNOS:
            raise ConfigValidationError(
                f"'cliente.entorno' inválido: '{entorno}'. Permitidos: {cls.ALLOWED_ENTORNOS}"
            )

        modo = cliente.get("modo")
        if modo not in cls.ALLOWED_MODOS:
            raise ConfigValidationError(
                f"'cliente.modo' inválido: '{modo}'. Permitidos: {cls.ALLOWED_MODOS}"
            )

        # 2. Validar sección red_y_aislamiento
        red = config.get("red_y_aislamiento")
        if not red or not isinstance(red, dict):
            raise ConfigValidationError("Falta la sección obligatoria 'red_y_aislamiento'.")

        if not red.get("region"):
            raise ConfigValidationError("Falta 'red_y_aislamiento.region'.")

        tags = red.get("tags_obligatorios")
        if not tags or not isinstance(tags, dict):
            raise ConfigValidationError("Falta 'red_y_aislamiento.tags_obligatorios'.")

        # 3. Validar sección workloads
        workloads = config.get("workloads")
        if not workloads or not isinstance(workloads, list):
            raise ConfigValidationError("Falta la sección 'workloads' o no contiene elementos.")

        for idx, wl in enumerate(workloads):
            if not isinstance(wl, dict):
                raise ConfigValidationError(f"El workload #{idx + 1} no es un objeto válido.")

            wl_id = wl.get("id")
            if not wl_id:
                raise ConfigValidationError(f"El workload #{idx + 1} requiere un 'id'.")

            tipo_tarea = wl.get("tipo_tarea")
            if tipo_tarea not in cls.ALLOWED_TAREAS:
                raise ConfigValidationError(
                    f"Workload '{wl_id}': 'tipo_tarea' inválido '{tipo_tarea}'. Permitidos: {cls.ALLOWED_TAREAS}"
                )

            accelerator = wl.get("accelerator")
            if not accelerator:
                raise ConfigValidationError(f"Workload '{wl_id}': Requiere el campo 'accelerator'.")

            cantidad_gpus = wl.get("cantidad_gpus", 0)
            if not isinstance(cantidad_gpus, int) or cantidad_gpus <= 0:
                raise ConfigValidationError(
                    f"Workload '{wl_id}': 'cantidad_gpus' debe ser un entero positivo (> 0)."
                )

            puerto = wl.get("puerto")
            if not puerto or not isinstance(puerto, int):
                raise ConfigValidationError(f"Workload '{wl_id}': Debe especificar un 'puerto' entero.")


class SkyYamlBuilder:
    """Construye la especificación YAML compatible con SkyPilot AWS."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.cliente = config["cliente"]
        self.red = config["red_y_aislamiento"]
        self.workloads = config["workloads"]

    def build(self) -> Dict[str, Any]:
        """Genera el diccionario de configuración final para SkyPilot."""
        cluster_name = f"sooniverse-{self.cliente['id']}-{self.cliente['entorno']}"

        # Consolidar aceleradores y puertos de todas las cargas de trabajo
        primary_wl = self.workloads[0]
        accelerators = f"{primary_wl['accelerator']}:{primary_wl['cantidad_gpus']}"
        ports = [wl["puerto"] for wl in self.workloads]

        resources: Dict[str, Any] = {
            "cloud": "aws",
            "region": self.red["region"],
            "accelerators": accelerators,
            "ports": ports,
            "labels": self.red.get("tags_obligatorios", {}),
        }

        # Override de image_id si está especificado en config_global.yaml
        if self.red.get("image_id"):
            resources["image_id"] = self.red["image_id"]

        # Override de tipo de instancia si está especificado
        if primary_wl.get("tipo_instancia"):
            resources["instance_type"] = primary_wl["tipo_instancia"]

        # Variables de entorno inyectadas al pod/host remoto
        frac = primary_wl.get("asignacion_fraccional", {})
        envs = {
            "CLIENTE_ID": self.cliente["id"],
            "ENTORNO": self.cliente["entorno"],
            "MODO": self.cliente["modo"],
            "MODEL_NAME": primary_wl.get("hf_repo", "cyankiwi/Qwen3.5-2B-AWQ-4bit"),
            "GPU_MEMORY_UTILIZATION": str(frac.get("gpu_memory_utilization", 0.95)),
            "MAX_MODEL_LEN": str(frac.get("max_model_len", 16384)),
        }

        # Configuración del setup remoto (instalación robusta de GPU y Docker)
        setup_script = """
set -euo pipefail

# A. Instalar dependencias esenciales, el driver estable y la utilidad modprobe faltante
sudo apt-get update && sudo apt-get install -y ubuntu-drivers-common build-essential nvidia-driver-550-server nvidia-utils-550-server nvidia-modprobe

# B. Carga manual de módulos en caliente (Corrige el error de comunicación de la GPU sin reiniciar)
sudo modprobe nvidia
sudo modprobe nvidia-uvm
sudo nvidia-modprobe -u -c=0

# D. Instalar Docker Engine (si no está presente)
if ! command -v docker &> /dev/null; then
    curl -fsSL https://get.docker.com -o get-docker.sh
    sudo sh get-docker.sh
    sudo usermod -aG docker ubuntu
fi

# E. Instalar NVIDIA Container Toolkit (Permite a Docker ver la GPU)
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit

# F. Configurar Docker para usar el runtime de NVIDIA y reiniciar el servicio
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
sudo chmod 666 /var/run/docker.sock

# G. Verificación final de salud del entorno
nvidia-smi

# H. Preparar persistencia del cache de modelos (hf_cache)
mkdir -p /home/ubuntu/sooniverse_infra/docker_images/qwen3.5/hf_cache
"""

        # Script de ejecución remota
        run_script = """
set -euo pipefail
echo "===> Desplegando servicios Sooniverse vía Docker Compose..."
cd /home/ubuntu/sooniverse_infra/docker_images/qwen3.5

# Exportar variables de entorno para docker-compose
export MODEL_NAME="${MODEL_NAME}"
export GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION}"
export MAX_MODEL_LEN="${MAX_MODEL_LEN}"

echo "Iniciando contenedores para el cliente ${CLIENTE_ID} (${ENTORNO})..."
sudo docker compose up -d

echo "===> Despliegue completado con éxito. Estado de contenedores:"
sudo docker compose ps
"""

        sky_spec = {
            "name": cluster_name,
            "resources": resources,
            "file_mounts": {
                "/home/ubuntu/sooniverse_infra/docker_images/qwen3.5": "./docker_images/qwen3.5",
            },
            "envs": envs,
            "setup": setup_script.strip(),
            "run": run_script.strip(),
        }

        return sky_spec


def load_config(config_path: str) -> Dict[str, Any]:
    """Carga y valida el archivo de configuración YAML."""
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"No se encontró el archivo de configuración: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    ConfigValidator.validate(config)
    return config


def generate_sky_yaml(config: Dict[str, Any], output_path: str) -> None:
    """Genera e imprime el archivo YAML para SkyPilot."""
    builder = SkyYamlBuilder(config)
    sky_dict = builder.build()

    header = (
        "# ==============================================================================\n"
        "# ARCHIVO GENERADO AUTOMÁTICAMENTE POR SOONIVERSE INFRA GENERATOR\n"
        "# NO EDITAR MANUALMENTE. MODIFICAR config_global.yaml EN SU LUGAR.\n"
        "# ==============================================================================\n\n"
    )

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(header)
        yaml.dump(sky_dict, f, default_flow_style=False, sort_keys=False)

    print(f"[SUCCESS] Archivo de infraestructura generado correctamente en: {output_path}")


def run_skypilot(output_path: str) -> None:
    """Invoca internamente a SkyPilot para aprovisionar el clúster."""
    sky_binary = shutil.which("sky")

    print("\n" + "=" * 70)
    print(" INVOCANDO SKYPILOT TRANSPARENTEMENTE")
    print("=" * 70)

    if not sky_binary:
        print("[WARNING] El comando 'sky' (SkyPilot) no está instalado o no se encuentra en el PATH.")
        print(f"[INFO] Se ha generado el archivo '{output_path}'.")
        print("[INFO] Para instalar SkyPilot: pip install skypilot[aws]")
        print(f"[INFO] Para ejecutar manualmente: sky launch -y {output_path}")
        return

    cmd = [sky_binary, "launch", "-y", output_path]
    print(f"[EXEC] Ejecutando: {' '.join(cmd)}\n")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"\n[ERROR] Falló la ejecución de SkyPilot (código de salida {e.returncode}).")
        sys.exit(e.returncode)


def main():
    parser = argparse.ArgumentParser(
        description="Generador y aprovisionador dinámico de infraestructura Sooniverse con SkyPilot."
    )
    parser.add_argument(
        "--config",
        default="config_global.yaml",
        help="Ruta al archivo central de configuración (por defecto: config_global.yaml)",
    )
    parser.add_argument(
        "--output",
        default=".sky_generated.yaml",
        help="Ruta donde se guardará el YAML generado para SkyPilot (por defecto: .sky_generated.yaml)",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Si se especifica, invoca inmediatamente a SkyPilot ('sky launch -y .sky_generated.yaml')",
    )

    args = parser.parse_args()

    print("[SOONIVERSE INFRA] Leyendo configuración central...")
    try:
        config = load_config(args.config)
        generate_sky_yaml(config, args.output)

        if args.run:
            run_skypilot(args.output)
        else:
            print(f"\n[INFO] Para aprovisionar en AWS con SkyPilot, ejecuta:")
            print(f"       python scripts/generate_infra.py --run")

    except ConfigValidationError as e:
        print(f"\n[ERROR DE CONFIGURACIÓN] {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n[ERROR INESPERADO] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
