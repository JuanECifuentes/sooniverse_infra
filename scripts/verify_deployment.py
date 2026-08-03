#!/usr/bin/env python3
"""
==============================================================================
Sooniverse Infra - Verificación automática de despliegue (Fase 3, FASE 8)
==============================================================================
Ejecuta las comprobaciones de la sección 5.5 del proyecto: red, aislamiento
de workers, salud de LiteLLM/nginx y registro en BD. Cada comprobación es
independiente -una que falla no detiene a las demás- y el resultado se
registra en `sooniverse.infra_event` (fase 'verify').

Salida: tabla con OK/FAIL/N-A por comprobación. Código de salida != 0 si
alguna comprobación CRÍTICA falló (no si quedó en N/A por falta de un
despliegue activo o de 'sky' instalado).

Uso:
    python scripts/verify_deployment.py
    python scripts/verify_deployment.py --config clients/acme/config_global.yaml
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")

try:
    import yaml
except ImportError:
    print("[ERROR] Falta 'pyyaml'. Ejecuta: pip install pyyaml")
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
DEFAULT_CONFIG_PATH = REPO_ROOT / "config_global.yaml"


def artifacts_dir_for(config_path: Path, config: Dict[str, Any]) -> Path:
    """Misma regla que generate_infra.artifacts_dir_for (duplicada a propósito,
    ver ese docstring): raíz del repo si --config es el config_global.yaml
    raíz, `.artifacts/<cliente>-<entorno>/` para cualquier otro (Fase 6)."""
    try:
        is_default_root_config = config_path.resolve() == DEFAULT_CONFIG_PATH.resolve()
    except OSError:
        is_default_root_config = False
    if is_default_root_config:
        return REPO_ROOT
    cliente = config["cliente"]
    return REPO_ROOT / ".artifacts" / f"{cliente['id']}-{cliente['entorno']}"


@dataclass
class CheckResult:
    name: str
    status: str  # "OK" | "FAIL" | "N/A"
    detail: str = ""
    critical: bool = True


@dataclass
class VerificationContext:
    config: Dict[str, Any]
    artifacts_dir: Path = REPO_ROOT
    deployment: Optional[Dict[str, Any]] = None
    resources: List[Dict[str, Any]] = field(default_factory=list)
    gateway_ip: Optional[str] = None
    sky_available: bool = False


def _sky_binary() -> Optional[str]:
    return shutil.which("sky")


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_REMOTE_LINE_RE = re.compile(r"^\([^)]*pid=\d+\)\s?(.*)$")


def _sky_exec_remote_output(cluster: str, remote_cmd: str, timeout: int, retries: int = 2) -> str:
    """Ejecuta `remote_cmd` en `cluster` vía 'sky exec' y devuelve SOLO la salida
    real del comando remoto -no el "Command to run: <remote_cmd>" que 'sky exec'
    imprime como eco ANTES de ejecutar. Sin este filtro, buscar un marcador de
    texto (p.ej. "SOONIVERSE_CURL_FAIL") contra todo el stdout siempre lo
    encuentra, porque ese texto ya aparece literalmente en la línea de eco del
    comando -independientemente de si el comando remoto de verdad falló (bug
    real encontrado en una corrida de despliegue real: dos comprobaciones
    reportaban FAIL siempre, incluso cuando el curl remoto funcionaba).

    Reintenta unas pocas veces: `sky exec` back-to-back sobre el mismo clúster
    (p.ej. varias comprobaciones seguidas en la misma corrida de verify) puede
    fallar de forma transitoria por contención de la sesión SSH/ControlMaster
    -confirmado en una corrida real: la misma comprobación fallaba dentro de
    verify_deployment.py pero funcionaba perfectamente al repetirla aislada
    segundos después."""
    last_output = ""
    for attempt in range(retries):
        try:
            result = subprocess.run(["sky", "exec", cluster, remote_cmd], capture_output=True, text=True, timeout=timeout)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            result = None

        if result is not None:
            clean = _ANSI_RE.sub("", result.stdout or "")
            lines = [m.group(1) for line in clean.splitlines() if (m := _REMOTE_LINE_RE.match(line.strip()))]
            last_output = "\n".join(lines)
            if last_output:
                return last_output

        if attempt < retries - 1:
            time.sleep(3)

    return last_output


def _cluster_is_up(cluster: str) -> bool:
    """True si 'sky status <cluster>' lo encuentra. OJO: a diferencia de
    'sky status --ip', esta variante sale con código 0 INCLUSO si el clúster no
    existe (solo imprime "not found" en stdout) -por eso se valida el texto, no
    el returncode. Evita reportar FAIL crítico cuando el clúster en realidad
    todavía no se aprovisionó."""
    try:
        result = subprocess.run(["sky", "status", cluster], capture_output=True, text=True, timeout=15)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    if result.returncode != 0:
        return False
    return "not found" not in result.stdout.lower()


def _http_get(url: str, headers: Optional[Dict[str, str]] = None, timeout: int = 5) -> Optional[Dict[str, Any]]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                return {"status": resp.status, "json": json.loads(body)}
            except json.JSONDecodeError:
                return {"status": resp.status, "text": body}
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        return {"error": str(exc)}


def _http_post(url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None, timeout: int = 20) -> Optional[Dict[str, Any]]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return {"status": resp.status, "json": json.loads(body)}
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        return {"error": str(exc)}


# =============================================================================
# Comprobaciones (cada una: ctx -> CheckResult)
# =============================================================================
def check_private_route_to_nat(ctx: VerificationContext) -> CheckResult:
    name = "Subred privada rutea a NAT"
    private_rt_ids = [r["aws_id"] for r in ctx.resources if r["component"] == "rtb-private" and r["aws_id"]]
    if not private_rt_ids:
        return CheckResult(name, "N/A", "No hay route tables privadas registradas", critical=False)

    import boto3
    ec2 = boto3.client("ec2", region_name=ctx.config["red_y_aislamiento"]["region"])
    ok = True
    for rt_id in private_rt_ids:
        try:
            rt = ec2.describe_route_tables(RouteTableIds=[rt_id])["RouteTables"][0]
        except Exception as exc:  # noqa: BLE001
            return CheckResult(name, "FAIL", f"{rt_id}: {exc}")
        has_nat_route = any(
            route.get("DestinationCidrBlock") == "0.0.0.0/0" and route.get("NatGatewayId")
            for route in rt.get("Routes", [])
        )
        ok = ok and has_nat_route
    return CheckResult(name, "OK" if ok else "FAIL", f"{len(private_rt_ids)} route table(s) verificadas")


def check_public_route_to_igw(ctx: VerificationContext) -> CheckResult:
    name = "Subred pública rutea a IGW"
    public_rt_ids = [r["aws_id"] for r in ctx.resources if r["component"] == "rtb-public" and r["aws_id"]]
    if not public_rt_ids:
        return CheckResult(name, "N/A", "No hay route table pública registrada", critical=False)

    import boto3
    ec2 = boto3.client("ec2", region_name=ctx.config["red_y_aislamiento"]["region"])
    ok = True
    for rt_id in public_rt_ids:
        rt = ec2.describe_route_tables(RouteTableIds=[rt_id])["RouteTables"][0]
        has_igw_route = any(
            route.get("DestinationCidrBlock") == "0.0.0.0/0" and route.get("GatewayId", "").startswith("igw-")
            for route in rt.get("Routes", [])
        )
        ok = ok and has_igw_route
    return CheckResult(name, "OK" if ok else "FAIL", f"{len(public_rt_ids)} route table(s) verificadas")


def check_workers_no_public_ip(ctx: VerificationContext) -> CheckResult:
    name = "Los workers no tienen IP pública"
    private_subnet_ids = [r["aws_id"] for r in ctx.resources if r["component"] == "subnet-private" and r["aws_id"]]
    if not private_subnet_ids:
        return CheckResult(name, "N/A", "No hay subredes privadas registradas", critical=False)

    import boto3
    ec2 = boto3.client("ec2", region_name=ctx.config["red_y_aislamiento"]["region"])
    resp = ec2.describe_instances(
        Filters=[
            {"Name": "subnet-id", "Values": private_subnet_ids},
            {"Name": "instance-state-name", "Values": ["running", "pending"]},
        ]
    )
    instances = [i for r in resp.get("Reservations", []) for i in r.get("Instances", [])]
    if not instances:
        return CheckResult(name, "N/A", "No hay instancias corriendo en subredes privadas todavía", critical=False)

    with_public_ip = [i["InstanceId"] for i in instances if i.get("PublicIpAddress")]
    if with_public_ip:
        return CheckResult(name, "FAIL", f"Instancias con IP pública: {with_public_ip}")
    return CheckResult(name, "OK", f"{len(instances)} instancia(s) verificadas sin IP pública")


def check_workers_sg_no_open_cidr(ctx: VerificationContext) -> CheckResult:
    name = "SG de workers no acepta 0.0.0.0/0 en el puerto vLLM"
    sg_id = next((r["aws_id"] for r in ctx.resources if r["component"] == "sg-workers" and r["aws_id"]), None)
    if not sg_id:
        return CheckResult(name, "N/A", "No hay SG de workers registrado", critical=False)

    import boto3
    ec2 = boto3.client("ec2", region_name=ctx.config["red_y_aislamiento"]["region"])
    sg = ec2.describe_security_groups(GroupIds=[sg_id])["SecurityGroups"][0]
    worker_ports = {wl["puerto"] for wl in ctx.config["workloads"]}

    for perm in sg.get("IpPermissions", []):
        if perm.get("FromPort") in worker_ports:
            for ip_range in perm.get("IpRanges", []):
                if ip_range.get("CidrIp") == "0.0.0.0/0":
                    return CheckResult(name, "FAIL", f"Puerto {perm.get('FromPort')} abierto a 0.0.0.0/0")
    return CheckResult(name, "OK", f"{len(worker_ports)} puerto(s) vLLM revisados, solo SG->SG")


def check_gateway_reaches_workers(ctx: VerificationContext) -> CheckResult:
    name = "El gateway alcanza cada worker (curl /health)"
    if not ctx.sky_available or not ctx.gateway_ip:
        return CheckResult(name, "N/A", "Requiere 'sky' y una IP de gateway activa", critical=False)

    endpoints_file = ctx.artifacts_dir / ".sooniverse_endpoints.json"
    if not endpoints_file.exists():
        return CheckResult(name, "N/A", "No existe .sooniverse_endpoints.json (correr sync_endpoints.py --apply)", critical=False)

    endpoints = json.loads(endpoints_file.read_text(encoding="utf-8"))
    if not endpoints:
        return CheckResult(name, "N/A", "El pool de endpoints está vacío", critical=False)

    gateway_cluster = f"sooniverse-{ctx.config['cliente']['id']}-{ctx.config['cliente']['entorno']}-gw"
    failures = []
    for ep in endpoints:
        remote_cmd = (
            f"curl -sf --max-time 5 http://{ep['ip']}:{ep['port']}/health >/dev/null 2>&1 "
            "&& echo SOONIVERSE_CURL_OK || echo SOONIVERSE_CURL_FAIL"
        )
        output = _sky_exec_remote_output(gateway_cluster, remote_cmd, timeout=30)
        if "SOONIVERSE_CURL_OK" not in output:
            failures.append(f"{ep['ip']}:{ep['port']}")

    if failures:
        return CheckResult(name, "FAIL", f"Sin respuesta: {failures}")
    return CheckResult(name, "OK", f"{len(endpoints)} worker(s) respondieron /health")


def check_worker_has_internet_egress(ctx: VerificationContext) -> CheckResult:
    name = "El worker tiene salida a Internet (NAT vivo)"
    if not ctx.sky_available:
        return CheckResult(name, "N/A", "Requiere 'sky' instalado", critical=False)

    workloads = ctx.config.get("workloads", [])
    if not workloads:
        return CheckResult(name, "N/A", "No hay workloads en el contrato", critical=False)

    base = f"sooniverse-{ctx.config['cliente']['id']}-{ctx.config['cliente']['entorno']}"
    cluster = f"{base}-{workloads[0]['id']}".lower().replace("_", "-").replace(".", "-")

    if not _cluster_is_up(cluster):
        return CheckResult(name, "N/A", f"Clúster '{cluster}' no aprovisionado todavía", critical=False)

    remote_cmd = (
        "curl -sfI --max-time 5 https://huggingface.co >/dev/null 2>&1 "
        "&& echo SOONIVERSE_CURL_OK || echo SOONIVERSE_CURL_FAIL"
    )
    output = _sky_exec_remote_output(cluster, remote_cmd, timeout=30)
    if "SOONIVERSE_CURL_OK" not in output:
        return CheckResult(name, "FAIL", "El worker no alcanzó huggingface.co vía NAT")
    return CheckResult(name, "OK", f"{cluster} tiene salida a Internet")


def check_litellm_lists_models(ctx: VerificationContext) -> CheckResult:
    name = "LiteLLM lista los modelos esperados (/v1/models)"
    if not ctx.gateway_ip:
        return CheckResult(name, "N/A", "No hay IP de gateway", critical=False)

    master_key = _read_env_var("LITELLM_MASTER_KEY")
    headers = {"Authorization": f"Bearer {master_key}"} if master_key else {}
    # A través de nginx (puerto 80), no del :4000 directo -ese puerto ya no está
    # publicado al host desde la Fase 5 salvo 'exponer_puertos_directos: true'.
    resp = _http_get(f"http://{ctx.gateway_ip}/v1/models", headers=headers)
    if resp is None or "error" in resp:
        return CheckResult(name, "FAIL", str(resp.get("error") if resp else "sin respuesta"))

    models = {m.get("id") for m in resp.get("json", {}).get("data", [])}
    expected = {wl.get("nombre_publico", wl["id"]) for wl in ctx.config.get("workloads", [])}
    missing = expected - models
    if missing:
        return CheckResult(name, "FAIL", f"Faltan en /v1/models: {missing}")
    return CheckResult(name, "OK", f"{len(models)} modelo(s) listados")


def check_litellm_pool_health(ctx: VerificationContext) -> CheckResult:
    name = "El pool tiene tantos endpoints sanos como réplicas"
    if not ctx.gateway_ip:
        return CheckResult(name, "N/A", "No hay IP de gateway", critical=False)

    master_key = _read_env_var("LITELLM_MASTER_KEY")
    headers = {"Authorization": f"Bearer {master_key}"} if master_key else {}
    resp = _http_get(f"http://{ctx.gateway_ip}/health", headers=headers, timeout=15)
    if resp is None or "error" in resp:
        return CheckResult(name, "FAIL", str(resp.get("error") if resp else "sin respuesta"))

    body = resp.get("json", {})
    healthy = len(body.get("healthy_endpoints", []))
    expected_replicas = sum(wl.get("replicas", 1) for wl in ctx.config.get("workloads", []))
    if healthy < expected_replicas:
        return CheckResult(name, "FAIL", f"{healthy}/{expected_replicas} endpoints sanos")
    return CheckResult(name, "OK", f"{healthy}/{expected_replicas} endpoints sanos")


def check_end_to_end_completion(ctx: VerificationContext) -> CheckResult:
    name = "Petición end-to-end responde (/v1/chat/completions)"
    if not ctx.gateway_ip or not ctx.config.get("workloads"):
        return CheckResult(name, "N/A", "No hay IP de gateway o workloads", critical=False)

    master_key = _read_env_var("LITELLM_MASTER_KEY")
    headers = {"Authorization": f"Bearer {master_key}"} if master_key else {}
    model = ctx.config["workloads"][0].get("nombre_publico", ctx.config["workloads"][0]["id"])
    payload = {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 16}
    resp = _http_post(f"http://{ctx.gateway_ip}/v1/chat/completions", payload, headers=headers)
    if resp is None or "error" in resp:
        return CheckResult(name, "FAIL", str(resp.get("error") if resp else "sin respuesta"))
    if "choices" not in resp.get("json", {}):
        return CheckResult(name, "FAIL", f"Respuesta inesperada: {resp.get('json')}")
    return CheckResult(name, "OK", "Respuesta con 'choices' recibida")


def check_nginx_routes(ctx: VerificationContext) -> CheckResult:
    name = "nginx sirve /, /v1/, /panel/ y /healthz en el puerto 80"
    if not ctx.gateway_ip:
        return CheckResult(name, "N/A", "No hay IP de gateway", critical=False)

    # /v1/models exige autenticación: sin la master key, un 401 es la prueba de
    # que nginx SÍ enrutó a LiteLLM (que respondió), no un fallo de ruteo.
    master_key = _read_env_var("LITELLM_MASTER_KEY")
    auth_headers = {"Authorization": f"Bearer {master_key}"} if master_key else {}

    routes = ["/", "/v1/models", "/panel/", "/healthz"]
    failures = []
    for route in routes:
        headers = auth_headers if route == "/v1/models" else None
        resp = _http_get(f"http://{ctx.gateway_ip}{route}", headers=headers, timeout=5)
        if resp is None or "error" in resp:
            failures.append(route)
    if failures:
        return CheckResult(name, "FAIL", f"Rutas sin respuesta: {failures}")
    return CheckResult(name, "OK", f"{len(routes)} ruta(s) verificadas")


# LiteLLM tarda 1-4 min en aceptar conexiones cada vez que sync_endpoints.py lo
# recarga (Prisma + init del proxy); el rango varía bastante entre corridas
# reales. Confirmado: las 4 comprobaciones de abajo fallaban con
# 502/connection-refused justo después de una resincronización de endpoints, y
# pasaban a OK sin tocar nada minutos después -no era un problema de red ni de
# config, sino de no esperar. Mismo timeout que sync_endpoints.LITELLM_READY_TIMEOUT_SECONDS
# para no reportar FAIL en verify por algo que sync ya está esperando activamente.
LITELLM_CHECK_TIMEOUT_SECONDS = 300
LITELLM_CHECK_POLL_INTERVAL_SECONDS = 10


def _retry_until_ok(
    check_fn: Callable[[VerificationContext], CheckResult],
    ctx: VerificationContext,
    timeout: int = LITELLM_CHECK_TIMEOUT_SECONDS,
    interval: int = LITELLM_CHECK_POLL_INTERVAL_SECONDS,
) -> CheckResult:
    """Reintenta `check_fn(ctx)` con espera fija hasta que deje de fallar o se
    agote `timeout`. N/A se devuelve tal cual (no hay nada que esperar: falta
    'sky', no hay deployment activo, etc.)."""
    deadline = time.monotonic() + timeout
    result = check_fn(ctx)
    waited = 0
    while result.status == "FAIL" and time.monotonic() < deadline:
        print(f"  [ESPERA] '{result.name}' aún falla ({waited}s/{timeout}s): {result.detail}")
        time.sleep(interval)
        waited += interval
        result = check_fn(ctx)
    return result


def check_litellm_lists_models_retry(ctx: VerificationContext) -> CheckResult:
    return _retry_until_ok(check_litellm_lists_models, ctx)


def check_litellm_pool_health_retry(ctx: VerificationContext) -> CheckResult:
    return _retry_until_ok(check_litellm_pool_health, ctx)


def check_end_to_end_completion_retry(ctx: VerificationContext) -> CheckResult:
    return _retry_until_ok(check_end_to_end_completion, ctx)


def check_nginx_routes_retry(ctx: VerificationContext) -> CheckResult:
    return _retry_until_ok(check_nginx_routes, ctx)


def check_db_registers_workers(ctx: VerificationContext) -> CheckResult:
    name = "La BD registra los workers (sooniverse.worker_node)"
    from db_setup import connect, resolve_db_config

    try:
        conn = connect(resolve_db_config(REPO_ROOT / ".env"))
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, "FAIL", str(exc))

    try:
        expected_replicas = sum(wl.get("replicas", 1) for wl in ctx.config.get("workloads", []))
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM sooniverse.worker_node WHERE cluster_name LIKE %s AND is_healthy",
                (f"sooniverse-{ctx.config['cliente']['id']}-{ctx.config['cliente']['entorno']}-%",),
            )
            count = cur.fetchone()[0]
    finally:
        conn.close()

    if expected_replicas and count < expected_replicas:
        return CheckResult(name, "FAIL", f"{count}/{expected_replicas} workers registrados y sanos")
    return CheckResult(name, "OK", f"{count} worker(s) registrados y sanos")


def _read_env_var(key: str) -> Optional[str]:
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith(f"{key}=") :
            return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    return None


CHECKS: List[Callable[[VerificationContext], CheckResult]] = [
    check_private_route_to_nat,
    check_public_route_to_igw,
    check_workers_no_public_ip,
    check_workers_sg_no_open_cidr,
    check_gateway_reaches_workers,
    check_worker_has_internet_egress,
    check_litellm_lists_models_retry,
    check_litellm_pool_health_retry,
    check_end_to_end_completion_retry,
    check_nginx_routes_retry,
    check_db_registers_workers,
]


def build_context(config: Dict[str, Any], config_path: Path) -> VerificationContext:
    ctx = VerificationContext(config=config, artifacts_dir=artifacts_dir_for(config_path, config))
    ctx.sky_available = _sky_binary() is not None

    red = config["red_y_aislamiento"]
    cliente = config["cliente"]

    if red.get("gestion_red", "auto") == "auto":
        try:
            from infra_state import PostgresInfraStateStore

            store = PostgresInfraStateStore()
            store.ping()
            deployment = store.get_active_deployment(cliente["id"], cliente["entorno"], red["region"])
            if deployment:
                ctx.deployment = deployment
                ctx.resources = store.list_resources(deployment["deployment_id"])
        except Exception as exc:  # noqa: BLE001 - la verificación de red sigue siendo best-effort
            print(f"[WARNING] No se pudo leer el estado de PostgreSQL: {exc}")

    if ctx.sky_available:
        gateway_cluster = f"sooniverse-{cliente['id']}-{cliente['entorno']}-gw"
        try:
            out = subprocess.run(["sky", "status", "--ip", gateway_cluster], capture_output=True, text=True, timeout=20)
            # 'sky status --ip' sobre un clúster inexistente sale con código != 0 e
            # imprime "Cluster(s) not found: ..." en STDOUT (no en stderr) -sin
            # validar el formato de IP, esa frase se colaba como "gateway_ip" y
            # rompía cualquier URL construida con ella ("Invalid IPv6 URL").
            if out.returncode == 0:
                lines = [l.strip() for l in out.stdout.strip().splitlines() if IPV4_RE.match(l.strip())]
                ctx.gateway_ip = lines[-1] if lines else None
            else:
                ctx.gateway_ip = None
        except (subprocess.TimeoutExpired, FileNotFoundError):
            ctx.gateway_ip = None

    return ctx


def main() -> int:
    parser = argparse.ArgumentParser(description="Verificación automática del despliegue Sooniverse.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    args = parser.parse_args()

    config_path = Path(args.config)
    with config_path.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    ctx = build_context(config, config_path)

    results: List[CheckResult] = []
    for check_fn in CHECKS:
        try:
            results.append(check_fn(ctx))
        except Exception as exc:  # noqa: BLE001 - una comprobación no debe tumbar a las demás
            results.append(CheckResult(check_fn.__name__, "FAIL", f"Excepción: {exc}"))

    print(f"\n{'COMPROBACIÓN':<55} ESTADO  DETALLE")
    print("-" * 100)
    for r in results:
        icon = {"OK": "[OK]  ", "FAIL": "[FAIL]", "N/A": "[N/A] "}[r.status]
        print(f"{r.name:<55} {icon} {r.detail}")

    if ctx.deployment:
        try:
            from infra_state import PostgresInfraStateStore

            store = PostgresInfraStateStore()
            for r in results:
                store.log_event(
                    ctx.deployment["deployment_id"], "verify", r.name,
                    "ok" if r.status == "OK" else ("warning" if r.status == "N/A" else "error"),
                    message=r.detail,
                )
        except Exception:  # noqa: BLE001 - el registro de auditoría no debe romper el reporte
            pass

    critical_failures = [r for r in results if r.status == "FAIL" and r.critical]
    print(f"\n{len(results) - len(critical_failures)}/{len(results)} comprobaciones OK/N-A "
          f"({len(critical_failures)} fallo(s) crítico(s))")
    return 1 if critical_failures else 0


if __name__ == "__main__":
    sys.exit(main())
