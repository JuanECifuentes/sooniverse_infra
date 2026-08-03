#!/usr/bin/env python3
"""
==============================================================================
Sooniverse Infra - Sincronización de Endpoints Worker -> LiteLLM Gateway
==============================================================================
Descubre las IPs PRIVADAS de los nodos worker vLLM aprovisionados por SkyPilot,
regenera el `litellm_config.yaml` del Nodo Gateway y recarga LiteLLM sin
downtime del resto del stack.

Además registra el inventario en `sooniverse.worker_node` para que el panel de
métricas de Django sepa qué nodos componen el pool.

Uso:
    python scripts/sync_endpoints.py                 # dry-run: solo muestra el pool descubierto
    python scripts/sync_endpoints.py --apply         # render + push al Gateway + reload LiteLLM
    python scripts/sync_endpoints.py --apply --skip-db
    python scripts/sync_endpoints.py --endpoints-file endpoints.json --apply   # pool manual
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import yaml
except ImportError:
    print("[ERROR] Falta 'pyyaml'. Ejecuta: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

DEFAULT_CONFIG_PATH = REPO_ROOT / "config_global.yaml"
LITELLM_CONFIG = REPO_ROOT / "docker_images" / "gateway" / "litellm_config.yaml"
REMOTE_ROOT = "/home/ubuntu/sooniverse_infra"

# Reasignados por configure_paths_for() según --config (multi-cliente, Fase 6):
# aíslan el caché de endpoints y el bastion entre clientes que comparten cuenta
# AWS. Los valores de abajo son solo el fallback de compatibilidad (config
# raíz, comportamiento anterior a la Fase 6).
ENDPOINTS_CACHE = REPO_ROOT / ".sooniverse_endpoints.json"
SKY_WORKERS_CONFIG = REPO_ROOT / ".sky_config_workers.yaml"


def configure_paths_for(config_path: Path, config: Dict[str, Any]) -> None:
    """Ver `generate_infra.artifacts_dir_for` (misma regla, duplicada a propósito
    para no acoplar este script a generate_infra.py): si --config es el
    config_global.yaml de la raíz, se mantienen las rutas legadas en la raíz;
    cualquier otro --config obtiene su propio `.artifacts/<cliente>-<entorno>/`.
    """
    global ENDPOINTS_CACHE, SKY_WORKERS_CONFIG

    try:
        is_default_root_config = config_path.resolve() == DEFAULT_CONFIG_PATH.resolve()
    except OSError:
        is_default_root_config = False

    if is_default_root_config:
        base_dir = REPO_ROOT
    else:
        cliente = config["cliente"]
        base_dir = REPO_ROOT / ".artifacts" / f"{cliente['id']}-{cliente['entorno']}"
        base_dir.mkdir(parents=True, exist_ok=True)

    ENDPOINTS_CACHE = base_dir / ".sooniverse_endpoints.json"
    SKY_WORKERS_CONFIG = base_dir / ".sky_config_workers.yaml"

READY_RE = re.compile(r"SOONIVERSE_WORKER_READY=([^|\s]+)\|([0-9.]+)\|(\d+)")
NODE_IPS_RE = re.compile(r"SOONIVERSE_NODE_IPS=([0-9.,]+)")
IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
HEALTH_CHECK_TIMEOUT_SECONDS = 5
# LiteLLM tarda 1-4 min en aceptar conexiones tras 'docker compose up -d --no-deps
# litellm' (Prisma + init del proxy); confirmado en corridas reales que el rango
# varía bastante (una vez ~90s, otra vez ~4min). Antes, un sleep fijo de 6s + un
# único curl (con el resultado descartado) hacía que esta función reportara
# éxito siempre, aunque LiteLLM todavía devolviera connection refused durante
# minutos -causando 502 en verify_deployment.py y en cualquier cliente que
# pegara justo después de una resincronización de endpoints.
LITELLM_READY_TIMEOUT_SECONDS = 300
LITELLM_READY_POLL_INTERVAL_SECONDS = 5

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_REMOTE_LINE_RE = re.compile(r"^\([^)]*pid=\d+\)\s?(.*)$")


def _strip_sky_exec_echo(raw_output: str) -> str:
    """'sky exec' imprime 'Command to run: <comando>' como eco ANTES de ejecutar
    y cada línea remota real llega con un prefijo '(nombre, pid=N)'. Sin filtrar
    ambas capas, un marcador de texto (p.ej. SOONIVERSE_LITELLM_READY) que
    aparece dentro del propio comando enviado (el bucle de espera lo contiene
    literalmente) se detectaría como éxito incluso si el comando remoto nunca
    llegó a imprimirlo de verdad."""
    clean = _ANSI_RE.sub("", raw_output or "")
    lines = [m.group(1) for line in clean.splitlines() if (m := _REMOTE_LINE_RE.match(line.strip()))]
    return "\n".join(lines)


# =============================================================================
# HELPERS
# =============================================================================
def sky_bin() -> Optional[str]:
    return shutil.which("sky")


def _sky_out(args: List[str], env: Optional[Dict[str, str]] = None, timeout: int = 180) -> str:
    """Ejecuta 'sky ...' capturando stdout+stderr; devuelve '' si falla."""
    sky = sky_bin()
    if not sky:
        return ""
    try:
        proc = subprocess.run(
            [sky] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, **(env or {})},
        )
        return (proc.stdout or "") + (proc.stderr or "")
    except (subprocess.SubprocessError, OSError):
        return ""


def cluster_names(config: Dict[str, Any]) -> Dict[str, str]:
    """Reproduce el naming del generador sin importarlo (evita acoplamiento circular)."""
    cliente = config["cliente"]
    base = f"sooniverse-{cliente['id']}-{cliente['entorno']}"
    names = {"__gateway__": f"{base}-gw"}
    for wl in config["workloads"]:
        names[wl["id"]] = f"{base}-{wl['id']}".lower().replace("_", "-").replace(".", "-")
    return names


def worker_env(config: Dict[str, Any]) -> Dict[str, str]:
    """Los clústeres worker viven bajo la config de cliente con use_internal_ips."""
    if SKY_WORKERS_CONFIG.exists():
        return {"SKYPILOT_CONFIG": str(SKY_WORKERS_CONFIG)}
    return {}


def _gateway_public_ip(gateway_cluster: str) -> Optional[str]:
    out = _sky_out(["status", "--ip", gateway_cluster])
    lines = [l.strip() for l in out.strip().splitlines() if IPV4_RE.match(l.strip())]
    return lines[-1] if lines else None


def refresh_bastion_config(config: Dict[str, Any], gateway_cluster: str) -> None:
    """Reescribe `.sky_config_workers.yaml` con la IP pública ACTUAL del gateway.

    Necesario porque el bastion puede quedar desactualizado entre corridas de
    `generate_infra.py --run` (p.ej. tras un `sky stop`/`sky start` manual que
    cambia la IP pública del gateway): sin esto, `sync_endpoints.py` intentaría
    descubrir workers vía un bastion muerto.
    """
    red = config["red_y_aislamiento"]
    if not red.get("workers_en_subred_privada", True):
        return  # sin subred privada no hay bastion que mantener al día

    gateway_ip = _gateway_public_ip(gateway_cluster)
    if not gateway_ip:
        print(f"[WARNING] No se pudo obtener la IP pública de '{gateway_cluster}'; "
              "se usará el bastion existente (si lo hay) sin refrescar.")
        return

    current: Dict[str, Any] = {}
    if SKY_WORKERS_CONFIG.exists():
        current = yaml.safe_load(SKY_WORKERS_CONFIG.read_text(encoding="utf-8")) or {}
    aws_cfg = current.get("aws", {})

    if aws_cfg.get("ssh_proxy_command") and gateway_ip in aws_cfg["ssh_proxy_command"]:
        return  # ya apunta a la IP correcta, no reescribir innecesariamente

    gateway_ssh_key = Path.home() / ".sky" / "generated" / "ssh-keys" / f"{gateway_cluster}.key"
    if not gateway_ssh_key.exists():
        print(f"[WARNING] No se encontró la clave SSH del gateway ({gateway_ssh_key}); "
              "no se puede refrescar el bastion todavía.")
        return
    os.chmod(gateway_ssh_key, 0o600)

    aws_cfg["ssh_proxy_command"] = (
        f"ssh -W %h:%p -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        f"-o ConnectTimeout=10 -i {gateway_ssh_key} ubuntu@{gateway_ip}"
    )
    aws_cfg.setdefault("vpc_name", red.get("vpc_name"))
    aws_cfg.setdefault("use_internal_ips", True)
    if red.get("security_group_workers"):
        aws_cfg.setdefault("security_group_name", red["security_group_workers"])
    current["aws"] = {k: v for k, v in aws_cfg.items() if v is not None}

    SKY_WORKERS_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    SKY_WORKERS_CONFIG.write_text(yaml.dump(current, default_flow_style=False, sort_keys=False), encoding="utf-8")
    print(f"[INFO] Bastion actualizado -> {gateway_ip} ({SKY_WORKERS_CONFIG.name})")


# =============================================================================
# DESCUBRIMIENTO DE IPS PRIVADAS
# =============================================================================
def discover_via_python_api(cluster: str) -> List[str]:
    """Vía preferente: handle de SkyPilot -> internal_ips() de todos los nodos."""
    try:
        import sky  # noqa: PLC0415 - import perezoso: SkyPilot es opcional en local
    except ImportError:
        return []

    try:
        records = sky.status(cluster_names=[cluster])
        # En versiones con API asíncrona, status() devuelve un request id.
        if not isinstance(records, list):
            records = sky.get(records)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - cualquier incompatibilidad cae al siguiente método
        return []

    for rec in records or []:
        handle = rec.get("handle") if isinstance(rec, dict) else None
        if handle is None:
            continue
        for attr in ("internal_ips", "external_ips"):
            getter = getattr(handle, attr, None)
            if callable(getter):
                try:
                    ips = [ip for ip in (getter() or []) if ip and IPV4_RE.match(str(ip))]
                except Exception:  # noqa: BLE001
                    ips = []
                if ips:
                    return list(dict.fromkeys(ips))
    return []


# Tags que SkyPilot 0.13.0 escribe en cada instancia EC2 (confirmado leyendo
# sky/provision/constants.py y sky/provision/aws/instance.py del paquete
# instalado, no asumido): el nombre de clúster va en AMBAS.
_SKYPILOT_CLUSTER_TAG_KEYS = ("ray-cluster-name", "skypilot-cluster-name")


def discover_via_describe_instances(cluster: str, region: str) -> List[Dict[str, str]]:
    """4º método (el más fiable: no depende del estado interno de SkyPilot).

    Busca instancias EC2 en ejecución con tag de clúster = `cluster` y toma su
    `PrivateIpAddress` + `SubnetId` directamente de la API de AWS.
    """
    try:
        import boto3
    except ImportError:
        return []

    try:
        ec2 = boto3.client("ec2", region_name=region)
        for tag_key in _SKYPILOT_CLUSTER_TAG_KEYS:
            resp = ec2.describe_instances(
                Filters=[
                    {"Name": f"tag:{tag_key}", "Values": [cluster]},
                    {"Name": "instance-state-name", "Values": ["running"]},
                ]
            )
            found = []
            for reservation in resp.get("Reservations", []):
                for instance in reservation.get("Instances", []):
                    ip = instance.get("PrivateIpAddress")
                    if ip:
                        found.append({"ip": ip, "subnet_id": instance.get("SubnetId")})
            if found:
                return found
    except Exception:  # noqa: BLE001 - boto3 no configurado o sin permisos -> cae al siguiente método
        return []
    return []


def discover_via_logs(cluster: str, env: Dict[str, str]) -> List[str]:
    """Parsea los marcadores emitidos por el run script del worker."""
    out = _sky_out(["logs", cluster, "--no-follow"], env=env)
    if not out:
        return []

    ips: List[str] = []
    for match in NODE_IPS_RE.finditer(out):
        ips.extend(ip.strip() for ip in match.group(1).split(",") if ip.strip())
    for match in READY_RE.finditer(out):
        ips.append(match.group(2))

    return [ip for ip in dict.fromkeys(ips) if IPV4_RE.match(ip)]


def discover_via_status_ip(cluster: str, env: Dict[str, str]) -> List[str]:
    """Último recurso: IP del nodo head (single-node o cluster degradado)."""
    out = _sky_out(["status", "--ip", cluster], env=env)
    return [line.strip() for line in out.splitlines() if IPV4_RE.match(line.strip())]


def discover_worker_ips(cluster: str, env: Dict[str, str], region: str) -> List[Dict[str, str]]:
    """Devuelve una lista de {"ip": ..., "subnet_id": ... | None}. `subnet_id` solo
    se conoce vía el método describe_instances (los otros tres solo dan la IP)."""
    for label, fn in (
        ("python-api", lambda: [{"ip": ip, "subnet_id": None} for ip in discover_via_python_api(cluster)]),
        ("describe-instances", lambda: discover_via_describe_instances(cluster, region)),
        ("run-logs", lambda: [{"ip": ip, "subnet_id": None} for ip in discover_via_logs(cluster, env)]),
        ("status--ip", lambda: [{"ip": ip, "subnet_id": None} for ip in discover_via_status_ip(cluster, env)]),
    ):
        found = fn()
        if found:
            ips = [f["ip"] for f in found]
            print(f"   [{cluster}] {len(found)} IP(s) privada(s) vía {label}: {', '.join(ips)}")
            return found
    print(f"   [{cluster}] sin IPs descubiertas (¿clúster no aprovisionado?)")
    return []


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_REMOTE_LINE_RE = re.compile(r"^\([^)]*pid=\d+\)\s?(.*)$")


def check_worker_health(ip: str, port: int, gateway_cluster: str) -> bool:
    """GET /health con timeout corto, ejecutado VÍA el gateway (`sky exec`).

    El worker vive en la subred privada (sin IP pública): un intento directo
    desde donde corre este script -normalmente la máquina del operador, fuera
    de la VPC- nunca puede alcanzar esa IP y siempre reportaría "no sano" sin
    importar el estado real del worker (bug real encontrado en una corrida de
    despliegue real: el pool quedaba vacío aunque vLLM ya respondía /health).
    El gateway, que sí vive dentro de la VPC, es quien puede comprobarlo de
    verdad -el mismo patrón que ya usa verify_deployment.py::check_gateway_reaches_workers.

    OJO: 'sky exec' imprime "Command to run: <comando>" como eco ANTES de
    ejecutar, así que buscar el marcador contra TODO el stdout siempre lo
    encuentra -incluso si el comando remoto falló- porque el texto del
    marcador ya aparece literalmente en esa línea de eco (segundo bug real,
    encontrado en la misma corrida: se reportaba "sano" siempre, sin importar
    el estado real). Por eso se filtra a solo las líneas con el prefijo
    "(cluster, pid=NNN)" que sky antepone a la salida real del comando.
    """
    cmd = [
        "sky", "exec", gateway_cluster,
        f"curl -sf --max-time {HEALTH_CHECK_TIMEOUT_SECONDS} http://{ip}:{port}/health "
        ">/dev/null 2>&1 && echo SOONIVERSE_HEALTH_OK || echo SOONIVERSE_HEALTH_FAIL",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=HEALTH_CHECK_TIMEOUT_SECONDS + 25)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False

    clean = _ANSI_RE.sub("", result.stdout or "")
    remote_lines = [m.group(1) for line in clean.splitlines() if (m := _REMOTE_LINE_RE.match(line.strip()))]
    return "SOONIVERSE_HEALTH_OK" in "\n".join(remote_lines)


def build_endpoints(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Construye el pool completo de deployments a partir del contrato + SkyPilot.

    Antes de incluir un endpoint en el resultado, verifica su salud (`/health`):
    los que no responden se registran como no sanos (ver `register_in_db`) y no
    entran al `litellm_config.yaml`, pero no bloquean el resto del despliegue.
    """
    names = cluster_names(config)
    env = worker_env(config)
    region = config["red_y_aislamiento"]["region"]
    endpoints: List[Dict[str, Any]] = []

    for wl in config["workloads"]:
        cluster = names[wl["id"]]
        frac = wl.get("asignacion_fraccional", {})
        for found in discover_worker_ips(cluster, env, region):
            ip = found["ip"]
            healthy = check_worker_health(ip, wl["puerto"], names["__gateway__"])
            if not healthy:
                print(f"   [WARNING] {ip}:{wl['puerto']} no respondió /health; "
                      "queda fuera del pool de LiteLLM (se reintentará en la próxima sincronización)")
            endpoints.append({
                "workload_id": wl["id"],
                "cluster": cluster,
                "model_public_name": wl.get("nombre_publico", wl["id"]),
                "hf_repo": wl.get("hf_repo", "unknown"),
                "accelerator": wl.get("accelerator"),
                "ip": ip,
                "subnet_id": found.get("subnet_id"),
                "port": wl["puerto"],
                "weight": wl.get("peso_balanceo", 1),
                "max_model_len": frac.get("max_model_len"),
                "healthy": healthy,
                "capacidades": wl.get("capacidades", {}),
            })

    return endpoints


# =============================================================================
# APLICACIÓN DE CAMBIOS
# =============================================================================
def render_config(endpoints: List[Dict[str, Any]], config: Dict[str, Any]) -> None:
    """Escribe el litellm_config.yaml SOLO con los endpoints que pasaron el
    health check (ver `check_worker_health`). El caché completo (sanos y no
    sanos) queda en `.sooniverse_endpoints.json` para diagnóstico/BD."""
    strategy = config.get("gateway", {}).get("load_balancing_strategy", "latency-based-routing")
    ENDPOINTS_CACHE.write_text(json.dumps(endpoints, indent=2), encoding="utf-8")

    healthy_endpoints = [ep for ep in endpoints if ep.get("healthy", True)]
    if len(healthy_endpoints) < len(endpoints):
        print(f"[INFO] {len(endpoints) - len(healthy_endpoints)} endpoint(s) no sano(s) excluido(s) "
              f"del litellm_config.yaml (siguen en {ENDPOINTS_CACHE.name} para diagnóstico).")

    healthy_json = ENDPOINTS_CACHE.parent / ".sooniverse_endpoints.healthy.json"
    healthy_json.write_text(json.dumps(healthy_endpoints, indent=2), encoding="utf-8")

    subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "render_litellm_config.py"),
            "--endpoints-json", str(healthy_json),
            "--strategy", strategy,
            "--config", str(REPO_ROOT / "config_global.yaml"),
            "--output", str(LITELLM_CONFIG),
        ],
        check=True,
    )
    healthy_json.unlink(missing_ok=True)


def push_and_reload(gateway_cluster: str) -> bool:
    """Envía el config al Gateway y recarga únicamente el contenedor de LiteLLM."""
    sky = sky_bin()
    if not sky:
        print("[WARNING] 'sky' no está en el PATH; no se puede empujar el config al Gateway.")
        return False

    remote_cfg = f"{REMOTE_ROOT}/docker_images/gateway/litellm_config.yaml"
    print(f"[EXEC] sky rsync {LITELLM_CONFIG.name} -> {gateway_cluster}:{remote_cfg}")
    try:
        subprocess.run(
            [sky, "rsync", str(LITELLM_CONFIG), f"{gateway_cluster}:{remote_cfg}"],
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        # `sky rsync` no existe en todas las versiones: fallback vía sky exec + heredoc.
        print("[INFO] 'sky rsync' no disponible; usando 'sky exec' como transporte.")
        payload = LITELLM_CONFIG.read_text(encoding="utf-8")
        script = f"cat > {remote_cfg} <<'SOONIVERSE_EOF'\n{payload}\nSOONIVERSE_EOF\n"
        try:
            subprocess.run([sky, "exec", gateway_cluster, script], check=True)
        except subprocess.CalledProcessError as exc:
            print(f"[ERROR] No se pudo escribir el config remoto (código {exc.returncode}).")
            return False

    attempts = LITELLM_READY_TIMEOUT_SECONDS // LITELLM_READY_POLL_INTERVAL_SECONDS
    # OJO #1: litellm_config.yaml se monta con un bind mount normal
    # ('volumes:', no 'configs:' de Compose) -Compose NO seguimiento el
    # contenido de ese archivo para decidir si recrear el contenedor, solo la
    # definición del servicio (imagen, environment, command...). Por eso 'up -d
    # --no-deps litellm' es un no-op cuando lo único que cambió es el YAML: el
    # proceso de LiteLLM sigue vivo con la config vieja en memoria
    # indefinidamente (confirmado en una corrida real: el contenedor llevaba 37
    # minutos arriba tras un 'reload' que reportó éxito, y el modelo nuevo
    # nunca apareció en /v1/models). Se usa 'restart' para forzar que el
    # proceso relea el archivo sí o sí.
    #
    # OJO #2: el puerto 4000 de litellm NO se publica al host salvo
    # 'exponer_puertos_directos: true' (nginx es la única puerta pública). Un
    # curl a http://localhost:4000/... desde el HOST del Gateway siempre da
    # "Connection refused" -no porque LiteLLM no esté listo, sino porque el
    # puerto no existe ahí. Se usa el propio healthcheck de Docker (accesible
    # vía `docker inspect` desde el host sin publicar el puerto) en vez de
    # reinventar la comprobación HTTP.
    reload_cmd = (
        f"cd {REMOTE_ROOT}/docker_images/gateway && "
        f"sudo docker compose --env-file {REMOTE_ROOT}/.env restart litellm && "
        f"for i in $(seq 1 {attempts}); do "
        f"status=$(sudo docker inspect --format '{{{{.State.Health.Status}}}}' sooniverse-litellm 2>/dev/null); "
        f"if [ \"$status\" = healthy ]; then echo SOONIVERSE_LITELLM_READY; exit 0; fi; "
        f"echo \"[ESPERA] litellm aun no responde ($i/{attempts}, estado=$status)\"; "
        f"sleep {LITELLM_READY_POLL_INTERVAL_SECONDS}; "
        f"done; echo SOONIVERSE_LITELLM_TIMEOUT; exit 1"
    )
    print("[EXEC] Recargando el contenedor LiteLLM en el Gateway (esperando healthcheck)...")
    try:
        proc = subprocess.run(
            [sky, "exec", gateway_cluster, reload_cmd],
            capture_output=True, text=True,
            timeout=LITELLM_READY_TIMEOUT_SECONDS + 60,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        print(f"[WARNING] La recarga de LiteLLM no respondió: {exc}")
        return False

    output = _strip_sky_exec_echo((proc.stdout or "") + (proc.stderr or ""))
    for line in output.splitlines():
        if line.startswith("[ESPERA]"):
            print(f"  {line}")

    if "SOONIVERSE_LITELLM_READY" in output:
        return True

    print(
        f"[WARNING] LiteLLM no pasó su healthcheck tras {LITELLM_READY_TIMEOUT_SECONDS}s de "
        f"recargarse. Revisa 'sky logs {gateway_cluster}' y "
        "'docker logs sooniverse-litellm' en el Gateway."
    )
    return False


def _current_network_context(config: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """deployment_id + security_group_id del despliegue activo (modo 'auto'),
    para enriquecer el inventario de worker_node. Best-effort: si la capa de
    red no está gestionada por este sistema, devuelve todo en None."""
    red = config["red_y_aislamiento"]
    if red.get("gestion_red", "auto") != "auto":
        return {"deployment_id": None, "security_group_id": None}

    try:
        from infra_state import PostgresInfraStateStore

        store = PostgresInfraStateStore()
        store.ping()
        cliente = config["cliente"]
        deployment = store.get_active_deployment(cliente["id"], cliente["entorno"], red["region"])
        if not deployment:
            return {"deployment_id": None, "security_group_id": None}

        sg_id = None
        for res in store.list_resources(deployment["deployment_id"]):
            if res["component"] == "sg-workers":
                sg_id = res.get("aws_id")
                break
        return {"deployment_id": deployment["deployment_id"], "security_group_id": sg_id}
    except Exception as exc:  # noqa: BLE001 - enriquecimiento best-effort
        print(f"[WARNING] No se pudo leer el contexto de red desde PostgreSQL: {exc}")
        return {"deployment_id": None, "security_group_id": None}


def register_in_db(endpoints: List[Dict[str, Any]], cluster_of: Dict[str, str], config: Dict[str, Any]) -> None:
    """Actualiza el inventario `sooniverse.worker_node` (best-effort)."""
    try:
        from db_setup import DbSetupError, connect, resolve_db_config  # type: ignore[import-not-found]
    except ImportError as exc:
        print(f"[WARNING] No se pudo importar db_setup ({exc}); se omite el registro en BD.")
        return

    try:
        conn = connect(resolve_db_config(REPO_ROOT / ".env"))
    except DbSetupError as exc:
        print(f"[WARNING] Sin acceso a PostgreSQL ({exc}); se omite el registro en BD.")
        return

    net_ctx = _current_network_context(config)

    try:
        with conn.cursor() as cur:
            clusters = sorted({ep["cluster"] for ep in endpoints})
            if clusters:
                # Los nodos que ya no aparecen quedan marcados como no saludables.
                cur.execute(
                    "UPDATE sooniverse.worker_node SET is_healthy = FALSE, health_status = 'unknown' "
                    "WHERE cluster_name = ANY(%s)",
                    (clusters,),
                )

            for rank, ep in enumerate(endpoints):
                healthy = ep.get("healthy", True)
                cur.execute(
                    """
                    INSERT INTO sooniverse.worker_node
                        (cluster_name, node_rank, private_ip, port, model_name, accelerator,
                         is_healthy, last_seen_at, deployment_id, subnet_id, security_group_id,
                         last_health_check, health_status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), %s, %s, %s, NOW(), %s)
                    ON CONFLICT (cluster_name, private_ip, port) DO UPDATE SET
                        node_rank          = EXCLUDED.node_rank,
                        model_name         = EXCLUDED.model_name,
                        accelerator        = EXCLUDED.accelerator,
                        is_healthy         = EXCLUDED.is_healthy,
                        last_seen_at       = NOW(),
                        deployment_id      = COALESCE(EXCLUDED.deployment_id, sooniverse.worker_node.deployment_id),
                        subnet_id          = COALESCE(EXCLUDED.subnet_id, sooniverse.worker_node.subnet_id),
                        security_group_id  = COALESCE(EXCLUDED.security_group_id, sooniverse.worker_node.security_group_id),
                        last_health_check  = NOW(),
                        health_status      = EXCLUDED.health_status
                    """,
                    (ep["cluster"], rank, ep["ip"], ep["port"], ep["model_public_name"], ep.get("accelerator"),
                     healthy, net_ctx["deployment_id"], ep.get("subnet_id"), net_ctx["security_group_id"],
                     "healthy" if healthy else "unhealthy"),
                )
        conn.commit()
        healthy_count = sum(1 for ep in endpoints if ep.get("healthy", True))
        print(f"[OK] Inventario sincronizado en sooniverse.worker_node "
              f"({healthy_count}/{len(endpoints)} sano(s))")
    except Exception as exc:  # noqa: BLE001 - el registro es informativo, no bloqueante
        conn.rollback()
        print(f"[WARNING] Falló el registro en BD: {exc}")
    finally:
        conn.close()


# =============================================================================
# CLI
# =============================================================================
def run_once(config: Dict[str, Any], args: argparse.Namespace) -> int:
    names = cluster_names(config)

    if not args.endpoints_file and config["red_y_aislamiento"].get("workers_en_subred_privada", True):
        refresh_bastion_config(config, names["__gateway__"])

    print("[SYNC] Descubriendo el pool de workers vLLM...")
    if args.endpoints_file:
        endpoints = json.loads(Path(args.endpoints_file).read_text(encoding="utf-8"))
        print(f"[SYNC] Pool manual cargado desde {args.endpoints_file}: {len(endpoints)} endpoint(s)")
    else:
        endpoints = build_endpoints(config)

    if not endpoints:
        print("[WARNING] Pool vacío: no se encontró ningún worker vLLM alcanzable.")
        print("          Verifica 'sky status' y que los clústeres worker estén UP.")

    print(f"\n[SYNC] Pool resultante ({len(endpoints)} deployment/s):")
    for ep in endpoints:
        salud = "sano" if ep.get("healthy", True) else "NO SANO"
        print(f"   - {ep['model_public_name']:<26} http://{ep['ip']}:{ep['port']}/v1  "
              f"(peso {ep.get('weight', 1)}, workload {ep['workload_id']}, {salud})")

    if not args.apply:
        print("\n[INFO] Dry-run. Añade --apply para escribir el config y recargar LiteLLM.")
        return 0

    render_config(endpoints, config)

    if not args.skip_db:
        register_in_db(endpoints, names, config)

    if not args.skip_push:
        if not push_and_reload(names["__gateway__"]):
            print("\n[ERROR] LiteLLM no quedó sano tras la recarga; el pool NO quedó sincronizado.")
            return 1

    print("\n[SUCCESS] Balanceador LiteLLM sincronizado con el pool de workers.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sincroniza las IPs privadas de los workers vLLM con el balanceador LiteLLM."
    )
    parser.add_argument("--config", default=str(REPO_ROOT / "config_global.yaml"))
    parser.add_argument("--apply", action="store_true",
                        help="Aplica: render + push al Gateway + reload de LiteLLM")
    parser.add_argument("--endpoints-file", help="Usa un pool manual en JSON en vez de consultar SkyPilot")
    parser.add_argument("--skip-db", action="store_true", help="No registrar el inventario en PostgreSQL")
    parser.add_argument("--skip-push", action="store_true", help="Solo render local, sin tocar el Gateway")
    parser.add_argument(
        "--watch", action="store_true",
        help="Reconciliación periódica: repite la sincronización cada --interval segundos hasta Ctrl+C. "
             "Implica --apply. Para dejarlo corriendo en el Gateway, instálalo como unidad systemd "
             "(ver comentario junto a este flag más abajo).",
    )
    parser.add_argument("--interval", type=int, default=60, help="Segundos entre corridas con --watch (default: 60)")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"[ERROR] No existe {config_path}", file=sys.stderr)
        return 1

    with config_path.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    configure_paths_for(config_path, config)

    if not args.watch:
        return run_once(config, args)

    # --watch implica --apply: sin esto, el "modo reconciliación" no reconciliaría nada.
    args.apply = True
    print(f"[SYNC] Modo --watch: sincronizando cada {args.interval}s. Ctrl+C para detener.")
    # Para producción, en vez de dejar esto corriendo en primer plano, instálalo como unidad
    # systemd en el Gateway, p.ej.:
    #   [Unit]
    #   Description=Sooniverse sync_endpoints watch
    #   [Service]
    #   ExecStart=/usr/bin/python3 /home/ubuntu/sooniverse_infra/scripts/sync_endpoints.py --watch
    #   Restart=always
    #   [Install]
    #   WantedBy=multi-user.target
    previous_ips: Optional[set] = None
    try:
        while True:
            try:
                endpoints = build_endpoints(config)
                current_ips = {(ep["ip"], ep["port"]) for ep in endpoints if ep.get("healthy", True)}
                if previous_ips is not None and current_ips != previous_ips:
                    added = current_ips - previous_ips
                    removed = previous_ips - current_ips
                    if added:
                        print(f"[WATCH] Nuevos endpoints sanos: {sorted(added)}")
                    if removed:
                        print(f"[WATCH] Endpoints que salieron del pool: {sorted(removed)}")
                previous_ips = current_ips

                render_config(endpoints, config)
                if not args.skip_db:
                    register_in_db(endpoints, cluster_names(config), config)
                if not args.skip_push:
                    push_and_reload(cluster_names(config)["__gateway__"])
            except Exception as exc:  # noqa: BLE001 - una corrida fallida no debe tumbar el watch
                print(f"[WARNING] Corrida de --watch falló: {exc}")

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[SYNC] --watch detenido por el usuario.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
