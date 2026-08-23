"""
Smoke test de nginx (Fase 7): levanta SOLO el stack de nginx con upstreams
simulados (contenedores ligeros que responden con su propio nombre) y
verifica el ruteo real: /, /v1/, /panel/, /healthz, y que la cabecera de
upgrade de WebSocket llega íntegra al upstream de Open WebUI.

Requiere Docker. Cualquier fallo de orquestación (Docker no disponible, redes,
timeouts) se trata como SKIP -no como fallo- para no romper `pytest` en un
entorno sin Docker; solo las aserciones sobre el comportamiento de nginx
cuentan como fallo real.
"""

import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from render_gateway_stack import render_nginx_conf  # noqa: E402

DOCKER = shutil.which("docker")
SCRATCH_DIR = REPO_ROOT / "tests" / "_tmp_nginx_smoke"


def _docker_ready() -> bool:
    if DOCKER is None:
        return False
    try:
        subprocess.run([DOCKER, "info"], capture_output=True, timeout=10, check=True)
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _docker_ready(), reason="Docker no disponible o no responde")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_http(port: int, path: str = "/healthz", timeout: int = 20) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(1)
    return False


UPSTREAM_SERVER_SNIPPET = (
    "import http.server,socketserver\n"
    "class H(http.server.BaseHTTPRequestHandler):\n"
    "    def do_GET(self):\n"
    "        upgrade = self.headers.get('Upgrade', '')\n"
    "        connection = self.headers.get('Connection', '')\n"
    "        body = f'__NAME__:{self.path}:upgrade={upgrade}:connection={connection}'\n"
    "        self.send_response(200)\n"
    "        self.send_header('Content-Type', 'text/plain')\n"
    "        self.end_headers()\n"
    "        self.wfile.write(body.encode())\n"
    "    def log_message(self, *a): pass\n"
    "socketserver.TCPServer(('0.0.0.0', __PORT__), H).serve_forever()\n"
)


@pytest.fixture
def nginx_stack():
    suffix = uuid.uuid4().hex[:8]
    network = f"sooniverse-smoke-{suffix}"
    upstream_names = ["open-webui", "litellm", "metrics"]
    ports = {"open-webui": 8080, "litellm": 4000, "metrics": 8000}
    nginx_container = f"sooniverse-smoke-nginx-{suffix}"
    started_ok = False

    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    conf_path = SCRATCH_DIR / f"default-{suffix}.conf"

    def _cleanup():
        for name in upstream_names + [nginx_container]:
            subprocess.run([DOCKER, "rm", "-f", name], capture_output=True)
        subprocess.run([DOCKER, "network", "rm", network], capture_output=True)
        conf_path.unlink(missing_ok=True)

    # Limpia contenedores/red de una corrida anterior que murió a medias (los
    # nombres de los upstreams son fijos). OJO: esto va ANTES de escribir
    # conf_path -si se escribiera antes, este mismo _cleanup() lo borraría
    # inmediatamente (unlink), dejando el path inexistente; Docker entonces
    # bind-montaría ahí un DIRECTORIO vacío en vez del archivo (comportamiento
    # de Docker al montar un origen que no existe), y nginx serviría un
    # directorio en vez del config -bug real encontrado reproduciendo esta
    # fixture a mano paso a paso.
    _cleanup()
    conf_path.write_text(render_nginx_conf({"gateway": {"tls": {"habilitado": False}}}), encoding="utf-8")

    try:
        subprocess.run([DOCKER, "network", "create", network], capture_output=True, timeout=20, check=True)

        for name in upstream_names:
            script = UPSTREAM_SERVER_SNIPPET.replace("__NAME__", name).replace("__PORT__", str(ports[name]))
            result = subprocess.run(
                [DOCKER, "run", "-d", "--rm", "--network", network, "--name", name,
                 "python:3.12-alpine", "python3", "-c", script],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                pytest.skip(f"No se pudo levantar el upstream simulado '{name}': {result.stderr}")

        host_port = _free_port()
        result = subprocess.run(
            [DOCKER, "run", "-d", "--rm", "--network", network, "--name", nginx_container,
             "-p", f"{host_port}:80",
             "-v", f"{conf_path}:/etc/nginx/conf.d/default.conf:ro",
             "nginx:1.27-alpine"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            pytest.skip(f"No se pudo levantar nginx: {result.stderr}")

        if not _wait_for_http(host_port):
            logs = subprocess.run([DOCKER, "logs", nginx_container], capture_output=True, text=True)
            pytest.skip(f"nginx no respondió a tiempo. Logs:\n{logs.stdout}\n{logs.stderr}")

        started_ok = True
        yield host_port
    finally:
        if not started_ok:
            pass
        _cleanup()


def _get(port: int, path: str, headers=None) -> str:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.read().decode("utf-8")


def test_healthz_is_served_directly_by_nginx(nginx_stack):
    body = _get(nginx_stack, "/healthz")
    assert body.strip() == "ok"


def test_root_routes_to_open_webui(nginx_stack):
    body = _get(nginx_stack, "/")
    assert "open-webui" in body


def test_v1_routes_to_litellm(nginx_stack):
    body = _get(nginx_stack, "/v1/models")
    assert "litellm" in body


def test_panel_routes_to_metrics(nginx_stack):
    body = _get(nginx_stack, "/panel/dashboard")
    assert "metrics" in body


def test_websocket_upgrade_headers_reach_open_webui(nginx_stack):
    body = _get(nginx_stack, "/", headers={"Upgrade": "websocket", "Connection": "Upgrade"})
    assert "upgrade=websocket" in body
    assert "connection=upgrade" in body.lower()


# =============================================================================
# Variante 'gateway.dominio' (modo letsencrypt): 3 server{} -80/dominio-redirect,
# 80/catch-all, 443/dominio-. nginx exige que el certificado exista para poder
# arrancar aunque no probemos tráfico HTTPS real, así que se genera uno
# autofirmado desechable con la MISMA ruta que usaría certbot en producción
# (/etc/letsencrypt/live/<dominio>/{fullchain,privkey}.pem).
# =============================================================================
LETSENCRYPT_DOMAIN = "test.sooniverse.local"


@pytest.fixture
def nginx_stack_letsencrypt():
    suffix = uuid.uuid4().hex[:8]
    network = f"sooniverse-smoke-le-{suffix}"
    upstream_names = ["open-webui", "litellm", "metrics"]
    ports = {"open-webui": 8080, "litellm": 4000, "metrics": 8000}
    nginx_container = f"sooniverse-smoke-le-nginx-{suffix}"
    started_ok = False

    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    conf_path = SCRATCH_DIR / f"default-le-{suffix}.conf"
    certs_dir = SCRATCH_DIR / f"letsencrypt-{suffix}"
    webroot_dir = SCRATCH_DIR / f"certbot-www-{suffix}"
    live_dir = certs_dir / "live" / LETSENCRYPT_DOMAIN

    def _cleanup():
        for name in upstream_names + [nginx_container]:
            subprocess.run([DOCKER, "rm", "-f", name], capture_output=True)
        subprocess.run([DOCKER, "network", "rm", network], capture_output=True)
        conf_path.unlink(missing_ok=True)
        shutil.rmtree(certs_dir, ignore_errors=True)
        shutil.rmtree(webroot_dir, ignore_errors=True)

    # Mismo orden que nginx_stack: limpiar ANTES de escribir, nunca después
    # (ver el comentario en esa fixture -unlink justo después de escribir borra
    # el archivo recién creado y Docker monta un directorio vacío en su lugar).
    _cleanup()

    live_dir.mkdir(parents=True, exist_ok=True)
    webroot_dir.mkdir(parents=True, exist_ok=True)
    (webroot_dir / ".well-known" / "acme-challenge").mkdir(parents=True, exist_ok=True)
    (webroot_dir / ".well-known" / "acme-challenge" / "smoke-token").write_text("smoke-ok", encoding="utf-8")

    subprocess.run(
        [
            "openssl", "req", "-x509", "-nodes", "-days", "1", "-newkey", "rsa:2048",
            "-keyout", str(live_dir / "privkey.pem"),
            "-out", str(live_dir / "fullchain.pem"),
            "-subj", f"/CN={LETSENCRYPT_DOMAIN}",
        ],
        capture_output=True, timeout=30,
    )

    conf_path.write_text(
        render_nginx_conf({
            "gateway": {
                "tls": {"habilitado": True, "modo": "letsencrypt", "dominio": LETSENCRYPT_DOMAIN},
                "dominio": {"redirigir_http": True},
            }
        }),
        encoding="utf-8",
    )

    try:
        subprocess.run([DOCKER, "network", "create", network], capture_output=True, timeout=20, check=True)

        for name in upstream_names:
            script = UPSTREAM_SERVER_SNIPPET.replace("__NAME__", name).replace("__PORT__", str(ports[name]))
            result = subprocess.run(
                [DOCKER, "run", "-d", "--rm", "--network", network, "--name", name,
                 "python:3.12-alpine", "python3", "-c", script],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                pytest.skip(f"No se pudo levantar el upstream simulado '{name}': {result.stderr}")

        host_port = _free_port()
        result = subprocess.run(
            [DOCKER, "run", "-d", "--rm", "--network", network, "--name", nginx_container,
             "-p", f"{host_port}:80",
             "-v", f"{conf_path}:/etc/nginx/conf.d/default.conf:ro",
             "-v", f"{certs_dir}:/etc/letsencrypt:ro",
             "-v", f"{webroot_dir}:/var/www/certbot:ro",
             "nginx:1.27-alpine"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            pytest.skip(f"No se pudo levantar nginx (modo letsencrypt): {result.stderr}")

        if not _wait_for_http(host_port):
            logs = subprocess.run([DOCKER, "logs", nginx_container], capture_output=True, text=True)
            pytest.skip(f"nginx no respondió a tiempo. Logs:\n{logs.stdout}\n{logs.stderr}")

        started_ok = True
        yield host_port
    finally:
        if not started_ok:
            pass
        _cleanup()


def _get_with_host(port: int, path: str, host: str, follow_redirects: bool = True):
    """GET contra 127.0.0.1:<port> con un Host: header específico, sin que
    urllib intente resolver DNS para 'host' -es exactamente lo que hace un
    balanceador/nginx real cuando decide el server{} por Host, no por IP."""
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):
            return None

    opener = urllib.request.build_opener() if follow_redirects else urllib.request.build_opener(_NoRedirect())
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"Host": host})
    try:
        with opener.open(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8"), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="ignore"), dict(exc.headers)


def test_letsencrypt_mode_nginx_starts_with_backup_cert(nginx_stack_letsencrypt):
    """Si nginx arrancó y respondió (ver fixture), el certificado -real o de
    respaldo- ya se cargó sin error: 'listen 443 ssl' con un archivo
    inexistente hace que nginx falle por completo al iniciar. Se consulta por
    el server{} catch-all (Host ajeno al dominio) para no seguir el redirect a
    https, que en este entorno de prueba no tiene puerto 443 publicado."""
    status, body, _ = _get_with_host(nginx_stack_letsencrypt, "/healthz", "203.0.113.10")
    assert status == 200
    assert body.strip() == "ok"


def test_letsencrypt_mode_redirects_domain_http_to_https(nginx_stack_letsencrypt):
    status, _, headers = _get_with_host(
        nginx_stack_letsencrypt, "/panel/dashboard", LETSENCRYPT_DOMAIN, follow_redirects=False
    )
    assert status == 301
    assert headers.get("Location") == f"https://{LETSENCRYPT_DOMAIN}/panel/dashboard"


def test_letsencrypt_mode_serves_acme_challenge_over_http_even_with_redirect(nginx_stack_letsencrypt):
    """El reto HTTP-01 de Let's Encrypt debe responder en claro incluso cuando
    'redirigir_http: true' -si no, certbot nunca podría validar el dominio."""
    status, body, _ = _get_with_host(
        nginx_stack_letsencrypt, "/.well-known/acme-challenge/smoke-token", LETSENCRYPT_DOMAIN
    )
    assert status == 200
    assert body.strip() == "smoke-ok"


def test_letsencrypt_mode_bare_ip_access_still_serves_app_over_http(nginx_stack_letsencrypt):
    """El server{} catch-all (server_name _) sigue sirviendo la app en claro
    -acceso por IP desnuda, o mientras el DNS del dominio no resuelve todavía-,
    sin redirigir, aunque el dominio real sí lo haga."""
    status, body, _ = _get_with_host(nginx_stack_letsencrypt, "/v1/models", "203.0.113.10")
    assert status == 200
    assert "litellm" in body
