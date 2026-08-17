#!/usr/bin/env python3
"""
==============================================================================
Sooniverse Infra - Sincronización de Open WebUI con las capacidades reales
==============================================================================
Aplica al Gateway ya desplegado lo que acaba de sondear/escribir
scripts/test_model_capabilities.py --write-db en sooniverse.model_capability:

  1. Re-renderiza docker_images/gateway/docker-compose.yml (los flags
     ENABLE_TITLE_GENERATION/ENABLE_TAGS_GENERATION/... de Open WebUI se
     derivan de .sooniverse_capabilities.json, que test_model_capabilities.py
     acaba de escribir -ver scripts/render_gateway_stack.py).
  2. Empuja el compose regenerado al Gateway (mismo transporte que
     sync_endpoints.py: 'sky rsync', con 'sky exec' + heredoc como fallback
     para el archivo suelto; una sincronización de todo el directorio de la
     imagen SOLO se hace si 'sky rsync' está disponible, un heredoc no escala
     a un árbol de archivos).
  3. Recrea el contenedor 'open-webui' (los ENABLE_* son variables de entorno
     del propio servicio, no un archivo montado: 'docker compose up -d' SÍ
     detecta el cambio y recrea el contenedor -a diferencia del caso de
     litellm_config.yaml documentado en sync_endpoints.py-).
  4. Corre el servicio one-shot 'openwebui-bootstrap' (perfil 'bootstrap') para
     que los modelos en Open WebUI reflejen sooniverse.model_capability.

Uso:
    python scripts/sync_openwebui_models.py                 # dry-run: solo re-renderiza local
    python scripts/sync_openwebui_models.py --apply         # push + recrea + bootstrap
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional

try:
    import yaml
except ImportError:
    print("[ERROR] Falta 'pyyaml'. Ejecuta: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

DEFAULT_CONFIG_PATH = REPO_ROOT / "config_global.yaml"
REMOTE_ROOT = "/home/ubuntu/sooniverse_infra"

GATEWAY_COMPOSE = REPO_ROOT / "docker_images" / "gateway" / "docker-compose.yml"

OPENWEBUI_READY_TIMEOUT_SECONDS = 180
OPENWEBUI_READY_POLL_INTERVAL_SECONDS = 5


def sky_bin() -> Optional[str]:
    return shutil.which("sky")


def _gateway_ssh_target(gateway_cluster: str) -> Optional[Dict[str, str]]:
    """IP pública + clave SSH que SkyPilot ya generó para este clúster (ver
    ~/.sky/generated/ssh-keys/<cluster>.key). Base para el fallback por scp:
    'sky rsync' no existe en todas las versiones de SkyPilot (confirmado en
    despliegue real contra 0.13.0: 'Error: No such command 'rsync'' -no un
    fallo de red, el comando no existe-), así que no basta con reintentar."""
    sky = sky_bin()
    if not sky:
        return None
    try:
        out = subprocess.run([sky, "status", "--ip", gateway_cluster], capture_output=True, text=True, timeout=20)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if out.returncode != 0:
        return None
    lines = [l.strip() for l in out.stdout.strip().splitlines() if l.strip()]
    ip = lines[-1] if lines else None
    key_path = Path.home() / ".sky" / "generated" / "ssh-keys" / f"{gateway_cluster}.key"
    if not ip or not key_path.exists():
        return None
    return {"ip": ip, "key": str(key_path)}


def _scp_push(local_path: Path, remote_path: str, target: Dict[str, str], recursive: bool = False) -> bool:
    cmd = [
        "scp", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
        "-i", target["key"],
    ]
    if recursive:
        cmd.append("-r")
    cmd += [str(local_path), f"ubuntu@{target['ip']}:{remote_path}"]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=120)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        detail = exc.stderr if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        print(f"[WARNING] scp falló para {local_path.name}: {detail}")
        return False


def artifacts_dir_for(config_path: Path, config) -> Path:
    """Misma regla que generate_infra.artifacts_dir_for (duplicada a propósito,
    ver ese docstring): dónde busca render_gateway_stack.py el
    .sooniverse_capabilities.json de ESTE cliente."""
    try:
        is_default_root_config = config_path.resolve() == DEFAULT_CONFIG_PATH.resolve()
    except OSError:
        is_default_root_config = False
    if is_default_root_config:
        return REPO_ROOT
    cliente = config["cliente"]
    return REPO_ROOT / ".artifacts" / f"{cliente['id']}-{cliente['entorno']}"


def gateway_cluster_for(config) -> str:
    cliente = config["cliente"]
    return f"sooniverse-{cliente['id']}-{cliente['entorno']}-gw"


def push_compose(gateway_cluster: str) -> bool:
    """Empuja docker-compose.yml regenerado (los ENABLE_* de open-webui)."""
    sky = sky_bin()
    if not sky:
        print("[WARNING] 'sky' no está en el PATH; no se puede empujar el compose al Gateway.")
        return False

    remote_path = f"{REMOTE_ROOT}/docker_images/gateway/docker-compose.yml"

    target = _gateway_ssh_target(gateway_cluster)
    if target:
        print(f"[EXEC] scp {GATEWAY_COMPOSE.name} -> {gateway_cluster}:{remote_path}")
        if _scp_push(GATEWAY_COMPOSE, remote_path, target):
            return True

    print("[INFO] scp no disponible; usando 'sky exec' + heredoc como transporte.")
    payload = GATEWAY_COMPOSE.read_text(encoding="utf-8")
    script = f"cat > {remote_path} <<'SOONIVERSE_EOF'\n{payload}\nSOONIVERSE_EOF\n"
    try:
        subprocess.run([sky, "exec", gateway_cluster, script], check=True)
        return True
    except subprocess.CalledProcessError as exc:
        print(f"[ERROR] No se pudo escribir el compose remoto (código {exc.returncode}).")
        return False


def push_openwebui_image_dir(gateway_cluster: str) -> None:
    """Sincroniza docker_images/openwebui/ (Dockerfile, overlay, patches) por si
    cambió desde el último 'sky launch'. Best-effort vía scp -r con la clave
    SSH que SkyPilot ya generó para el clúster (ver _gateway_ssh_target);
    'sky rsync' no existe en todas las versiones de SkyPilot."""
    remote_dir = f"{REMOTE_ROOT}/docker_images/openwebui"
    local_dir = REPO_ROOT / "docker_images" / "openwebui"

    target = _gateway_ssh_target(gateway_cluster)
    if not target:
        print("[INFO] No se pudo resolver IP/clave SSH del Gateway; se usa la copia ya presente ahí.")
        return

    print(f"[EXEC] scp -r {local_dir} -> {gateway_cluster}:{remote_dir}")
    if not _scp_push(local_dir, f"{REMOTE_ROOT}/docker_images/", target, recursive=True):
        print("[INFO] No se pudo sincronizar docker_images/openwebui/; se usa la copia ya presente en el Gateway.")


def recreate_and_bootstrap(gateway_cluster: str) -> bool:
    sky = sky_bin()
    if not sky:
        print("[WARNING] 'sky' no está en el PATH; no se puede operar el Gateway.")
        return False

    attempts = OPENWEBUI_READY_TIMEOUT_SECONDS // OPENWEBUI_READY_POLL_INTERVAL_SECONDS
    remote_cmd = (
        f"cd {REMOTE_ROOT}/docker_images/gateway && "
        f"sudo docker compose --env-file {REMOTE_ROOT}/.env up -d --build open-webui && "
        f"for i in $(seq 1 {attempts}); do "
        f"status=$(sudo docker inspect --format '{{{{.State.Health.Status}}}}' sooniverse-webui 2>/dev/null); "
        f"if [ \"$status\" = healthy ]; then echo SOONIVERSE_WEBUI_READY; break; fi; "
        f"echo \"[ESPERA] open-webui aun no responde ($i/{attempts}, estado=$status)\"; "
        f"sleep {OPENWEBUI_READY_POLL_INTERVAL_SECONDS}; "
        f"done && "
        f"sudo docker compose --env-file {REMOTE_ROOT}/.env --profile bootstrap "
        f"run --rm openwebui-bootstrap"
    )
    print("[EXEC] Recreando open-webui (nuevos ENABLE_*) y corriendo el bootstrap de modelos...")
    try:
        proc = subprocess.run(
            [sky, "exec", gateway_cluster, remote_cmd],
            capture_output=True, text=True,
            timeout=OPENWEBUI_READY_TIMEOUT_SECONDS + 120,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        print(f"[WARNING] La operación no respondió: {exc}")
        return False

    output = (proc.stdout or "") + (proc.stderr or "")
    for line in output.splitlines():
        if line.startswith("[ESPERA]") or line.startswith("[bootstrap]") or line.startswith("[OK]") \
                or line.startswith("[WARNING]") or line.startswith("[ERROR]"):
            print(f"  {line}")

    if proc.returncode != 0:
        print(f"[WARNING] El bootstrap remoto terminó con código {proc.returncode}. "
              f"Revisa 'sky logs {gateway_cluster}' / 'docker logs sooniverse-webui-bootstrap'.")
        return False
    print("[OK] Open WebUI sincronizado con las capacidades efectivas.")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Aplica las capacidades efectivas (sooniverse.model_capability) a Open WebUI."
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--apply", action="store_true",
                        help="Empuja el compose regenerado y ejecuta el bootstrap en el Gateway")
    args = parser.parse_args()

    config_path = Path(args.config)
    with config_path.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    from render_gateway_stack import render as render_gateway_stack  # type: ignore[import-not-found]

    render_gateway_stack(config, capabilities_dir=artifacts_dir_for(config_path, config))

    if not args.apply:
        print("\n[DRY-RUN] Compose regenerado localmente. Usa --apply para empujarlo al Gateway "
              "y sincronizar los modelos.")
        return 0

    gateway_cluster = gateway_cluster_for(config)
    if not push_compose(gateway_cluster):
        return 1
    push_openwebui_image_dir(gateway_cluster)
    return 0 if recreate_and_bootstrap(gateway_cluster) else 1


if __name__ == "__main__":
    sys.exit(main())
