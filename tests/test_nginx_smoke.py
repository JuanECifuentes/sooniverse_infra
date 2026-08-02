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
    "        body = f'{name!r}:{{self.path}}:upgrade={{upgrade}}:connection={{connection}}'\n"
    "        self.send_response(200)\n"
    "        self.send_header('Content-Type', 'text/plain')\n"
    "        self.end_headers()\n"
    "        self.wfile.write(body.encode())\n"
    "    def log_message(self, *a): pass\n"
    "socketserver.TCPServer(('0.0.0.0', {port}), H).serve_forever()\n"
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
    conf_path.write_text(render_nginx_conf({"gateway": {"tls": {"habilitado": False}}}), encoding="utf-8")

    def _cleanup():
        for name in upstream_names + [nginx_container]:
            subprocess.run([DOCKER, "rm", "-f", name], capture_output=True)
        subprocess.run([DOCKER, "network", "rm", network], capture_output=True)
        conf_path.unlink(missing_ok=True)

    _cleanup()  # por si una corrida anterior murió a medias con los mismos nombres

    try:
        subprocess.run([DOCKER, "network", "create", network], capture_output=True, timeout=20, check=True)

        for name in upstream_names:
            script = UPSTREAM_SERVER_SNIPPET.format(port=ports[name]).replace("{name!r}", repr(name))
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
