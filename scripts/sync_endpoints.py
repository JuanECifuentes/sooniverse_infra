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
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import yaml
except ImportError:
    print("[ERROR] Falta 'pyyaml'. Ejecuta: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

ENDPOINTS_CACHE = REPO_ROOT / ".sooniverse_endpoints.json"
LITELLM_CONFIG = REPO_ROOT / "docker_images" / "gateway" / "litellm_config.yaml"
SKY_WORKERS_CONFIG = REPO_ROOT / ".sky_config_workers.yaml"
REMOTE_ROOT = "/home/ubuntu/sooniverse_infra"

READY_RE = re.compile(r"SOONIVERSE_WORKER_READY=([^|\s]+)\|([0-9.]+)\|(\d+)")
NODE_IPS_RE = re.compile(r"SOONIVERSE_NODE_IPS=([0-9.,]+)")
IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


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


def discover_worker_ips(cluster: str, env: Dict[str, str]) -> List[str]:
    for label, fn in (
        ("python-api", lambda: discover_via_python_api(cluster)),
        ("run-logs", lambda: discover_via_logs(cluster, env)),
        ("status--ip", lambda: discover_via_status_ip(cluster, env)),
    ):
        ips = fn()
        if ips:
            print(f"   [{cluster}] {len(ips)} IP(s) privada(s) vía {label}: {', '.join(ips)}")
            return ips
    print(f"   [{cluster}] sin IPs descubiertas (¿clúster no aprovisionado?)")
    return []


def build_endpoints(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Construye el pool completo de deployments a partir del contrato + SkyPilot."""
    names = cluster_names(config)
    env = worker_env(config)
    endpoints: List[Dict[str, Any]] = []

    for wl in config["workloads"]:
        cluster = names[wl["id"]]
        frac = wl.get("asignacion_fraccional", {})
        for ip in discover_worker_ips(cluster, env):
            endpoints.append({
                "workload_id": wl["id"],
                "cluster": cluster,
                "model_public_name": wl.get("nombre_publico", wl["id"]),
                "hf_repo": wl.get("hf_repo", "unknown"),
                "accelerator": wl.get("accelerator"),
                "ip": ip,
                "port": wl["puerto"],
                "weight": wl.get("peso_balanceo", 1),
                "max_model_len": frac.get("max_model_len"),
            })

    return endpoints


# =============================================================================
# APLICACIÓN DE CAMBIOS
# =============================================================================
def render_config(endpoints: List[Dict[str, Any]], config: Dict[str, Any]) -> None:
    strategy = config.get("gateway", {}).get("load_balancing_strategy", "latency-based-routing")
    ENDPOINTS_CACHE.write_text(json.dumps(endpoints, indent=2), encoding="utf-8")

    subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "render_litellm_config.py"),
            "--endpoints-json", str(ENDPOINTS_CACHE),
            "--strategy", strategy,
            "--config", str(REPO_ROOT / "config_global.yaml"),
            "--output", str(LITELLM_CONFIG),
        ],
        check=True,
    )


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

    reload_cmd = (
        f"cd {REMOTE_ROOT}/docker_images/gateway && "
        f"sudo docker compose --env-file {REMOTE_ROOT}/.env up -d --no-deps litellm && "
        f"sleep 6 && curl -sf http://localhost:4000/health/readiness || true"
    )
    print("[EXEC] Recargando el contenedor LiteLLM en el Gateway...")
    try:
        subprocess.run([sky, "exec", gateway_cluster, reload_cmd], check=True)
    except subprocess.CalledProcessError as exc:
        print(f"[WARNING] La recarga de LiteLLM devolvió código {exc.returncode}. Revisa 'sky logs'.")
        return False

    return True


def register_in_db(endpoints: List[Dict[str, Any]], cluster_of: Dict[str, str]) -> None:
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

    try:
        with conn.cursor() as cur:
            clusters = sorted({ep["cluster"] for ep in endpoints})
            if clusters:
                # Los nodos que ya no aparecen quedan marcados como no saludables.
                cur.execute(
                    "UPDATE sooniverse.worker_node SET is_healthy = FALSE "
                    "WHERE cluster_name = ANY(%s)",
                    (clusters,),
                )

            for rank, ep in enumerate(endpoints):
                cur.execute(
                    """
                    INSERT INTO sooniverse.worker_node
                        (cluster_name, node_rank, private_ip, port, model_name, accelerator,
                         is_healthy, last_seen_at)
                    VALUES (%s, %s, %s, %s, %s, %s, TRUE, NOW())
                    ON CONFLICT (cluster_name, private_ip, port) DO UPDATE SET
                        node_rank    = EXCLUDED.node_rank,
                        model_name   = EXCLUDED.model_name,
                        accelerator  = EXCLUDED.accelerator,
                        is_healthy   = TRUE,
                        last_seen_at = NOW()
                    """,
                    (ep["cluster"], rank, ep["ip"], ep["port"],
                     ep["model_public_name"], ep.get("accelerator")),
                )
        conn.commit()
        print(f"[OK] Inventario sincronizado en sooniverse.worker_node ({len(endpoints)} nodo/s)")
    except Exception as exc:  # noqa: BLE001 - el registro es informativo, no bloqueante
        conn.rollback()
        print(f"[WARNING] Falló el registro en BD: {exc}")
    finally:
        conn.close()


# =============================================================================
# CLI
# =============================================================================
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
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"[ERROR] No existe {config_path}", file=sys.stderr)
        return 1

    with config_path.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

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
        print(f"   - {ep['model_public_name']:<26} http://{ep['ip']}:{ep['port']}/v1  "
              f"(peso {ep.get('weight', 1)}, workload {ep['workload_id']})")

    if not args.apply:
        print("\n[INFO] Dry-run. Añade --apply para escribir el config y recargar LiteLLM.")
        return 0

    render_config(endpoints, config)

    if not args.skip_db:
        register_in_db(endpoints, cluster_names(config))

    if not args.skip_push:
        gw = cluster_names(config)["__gateway__"]
        push_and_reload(gw)

    print("\n[SUCCESS] Balanceador LiteLLM sincronizado con el pool de workers.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
