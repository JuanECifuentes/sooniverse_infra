#!/usr/bin/env python3
"""Usuario y raíz remota del Gateway/Workers, según la nube (red_y_aislamiento.cloud).

SkyPilot usa un usuario SSH DISTINTO por proveedor -confirmado contra las
plantillas Ray instaladas de SkyPilot 0.13.0:
  - AWS:   'ubuntu'    (sky/templates/aws.yml.j2)
  - Azure: 'azureuser' (sky/templates/azure-ray.yml.j2:58 -> ssh_user: azureuser)
  - GCP:   'gcpuser'   (sky/templates/gcp-ray.yml.j2:87   -> ssh_user: gcpuser)

Antes de este módulo, el repo asumía 'ubuntu' hardcodeado en 4 scripts distintos
(generate_infra.py, sync_endpoints.py, benchmark_capacity.py,
sync_openwebui_models.py) con un comentario que afirmaba que SkyPilot
"normaliza el usuario remoto a 'ubuntu' sin importar la nube" -verificado
como FALSO contra el código fuente instalado de SkyPilot. En Azure/GCP eso
rompe en silencio: file_mounts apuntando a un $HOME que no existe, el
ssh_proxy_command del bastion, y usermod -aG docker ubuntu sobre un usuario
que nunca se creó.

Módulo deliberadamente sin dependencias (solo stdlib) para que los scripts
"ligeros" (sync_endpoints.py, benchmark_capacity.py, sync_openwebui_models.py)
puedan importarlo sin arrastrar boto3/azure-sdk/googleapiclient -mismo
principio que aws_network.py/azure_network.py, que solo se importan
perezosamente donde hace falta.
"""

from __future__ import annotations

# Único punto de verdad: si SkyPilot cambia de usuario en una versión futura,
# se corrige aquí y se propaga a los 4 scripts que lo consumen.
REMOTE_USER_BY_CLOUD = {
    "aws": "ubuntu",
    "azure": "azureuser",
    "gcp": "gcpuser",
}

DEFAULT_CLOUD = "aws"  # mismo default retrocompatible que ConfigValidator.ALLOWED_CLOUDS


def remote_user_for(cloud: str | None) -> str:
    """Usuario SSH remoto que SkyPilot crea en el Gateway/Workers para `cloud`.

    `cloud` ausente/None se trata como 'aws' (retrocompatible: ningún
    config_global.yaml existente antes de esta función traía 'cloud')."""
    key = (cloud or DEFAULT_CLOUD).lower()
    try:
        return REMOTE_USER_BY_CLOUD[key]
    except KeyError:
        raise ValueError(
            f"cloud_remote.remote_user_for: cloud '{cloud}' desconocido. "
            f"Permitidos: {sorted(REMOTE_USER_BY_CLOUD)}"
        ) from None


def remote_root_for(cloud: str | None) -> str:
    """Ruta absoluta del workspace remoto (raíz del repo montado por SkyPilot)
    para `cloud`. Usar SIEMPRE esta función en vez de un '/home/ubuntu/...'
    literal -ver el docstring del módulo."""
    return f"/home/{remote_user_for(cloud)}/sooniverse_infra"
