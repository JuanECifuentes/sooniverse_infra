#!/usr/bin/env python3
"""
==============================================================================
Sooniverse Infra - Render de la pila del Gateway (nginx + docker-compose)
==============================================================================
Genera `docker_images/gateway/nginx/default.conf` y
`docker_images/gateway/docker-compose.yml` a partir de `config_global.yaml`,
para que nginx sea la ÚNICA puerta de entrada pública por defecto:

  - `gateway.exponer_puertos_directos: false` (default, recomendado): litellm,
    open-webui y metrics NO publican puertos al host (solo `expose:` en la red
    interna de Docker). Únicamente `proxy` (nginx) publica 80/443.
  - `gateway.tls.habilitado: true` (modo 'self-signed'): nginx también escucha
    443 con un certificado autofirmado generado en el setup del gateway (ver
    GATEWAY_SETUP_SCRIPT en scripts/generate_infra.py). Los modos 'letsencrypt'
    y 'acm' quedan documentados como hook futuro, no implementados aquí.

Este archivo SE REGENERA en cada `generate_infra.py --run` (y se puede
invocar suelto): no lo edites a mano si esperas que sobreviva al próximo
despliegue.

Uso:
    python scripts/render_gateway_stack.py --config config_global.yaml
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

try:
    import yaml
except ImportError:
    print("[ERROR] Falta 'pyyaml'. Ejecuta: pip install pyyaml")
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parent.parent
GATEWAY_DIR = REPO_ROOT / "docker_images" / "gateway"
NGINX_CONF_PATH = GATEWAY_DIR / "nginx" / "default.conf"
COMPOSE_PATH = GATEWAY_DIR / "docker-compose.yml"

CAPABILITIES_FILENAME = ".sooniverse_capabilities.json"

# Nombres EXACTOS de variables de entorno de Open WebUI (backend/open_webui/config.py,
# verificados contra el tag fijado en docker_images/openwebui/Dockerfile). Todas
# se comparan con `.lower() == 'true'` en el propio Open WebUI: se emiten en
# minúsculas para no depender de esa normalización.
OPEN_WEBUI_TASK_ENV_KEYS = (
    "ENABLE_TITLE_GENERATION",
    "ENABLE_TAGS_GENERATION",
    "ENABLE_AUTOCOMPLETE_GENERATION",
    "ENABLE_FOLLOW_UP_GENERATION",
    "ENABLE_RETRIEVAL_QUERY_GENERATION",
    "ENABLE_SEARCH_QUERY_GENERATION",
)

GENERATED_HEADER = (
    "# ==============================================================================\n"
    "# ARCHIVO GENERADO POR scripts/render_gateway_stack.py A PARTIR DE config_global.yaml\n"
    "# Se sobrescribe en cada despliegue. Cambia el contrato, no este archivo.\n"
    "# ==============================================================================\n"
)


# =============================================================================
# Capacidades efectivas -> flags globales de Open WebUI
# =============================================================================
def _load_effective_capabilities(capabilities_dir: Optional[Path]) -> Dict[str, bool]:
    """Lee `.sooniverse_capabilities.json` (lo escribe
    scripts/test_model_capabilities.py --json en la fase 'capabilities' de
    generate_infra.py::deploy(), ver docs/01_FLUJO_DESPLIEGUE.md). Devuelve la
    UNIÓN de capacidades efectivas entre todos los modelos desplegados: los
    flags de tareas automáticas de Open WebUI (título, tags...) son globales
    de la instancia, no por modelo, así que se activan si AL MENOS un modelo
    los soporta de verdad.

    Fail-closed por diseño: si el archivo no existe todavía (primer render,
    antes de que exista ningún despliegue con workers sondeados), todo queda
    en False -nunca se activa una tarea automática sin haber confirmado que
    algún modelo la soporta."""
    default = {"any_vision": False, "any_tool_calling": False, "any_json_object": False}
    if not capabilities_dir:
        return default

    path = Path(capabilities_dir) / CAPABILITIES_FILENAME
    if not path.exists():
        return default

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        models = payload.get("models", [])
        return {
            "any_vision": any(m.get("effective_vision") for m in models),
            "any_tool_calling": any(m.get("effective_tool_calling") for m in models),
            "any_json_object": any(m.get("effective_json_object") for m in models),
        }
    except (json.JSONDecodeError, OSError, AttributeError):
        return default


def _resolve_open_webui_flag(owui_cfg: Dict[str, Any], override_key: str, capability_value: bool) -> bool:
    """'segun_capacidades' (default) usa la verdad observada; 'activado'/
    'desactivado' son el escape manual del operador (ver config_global.yaml,
    sección gateway.open_webui)."""
    mode = (owui_cfg.get(override_key) or "segun_capacidades").strip().lower()
    if mode == "activado":
        return True
    if mode == "desactivado":
        return False
    return capability_value


def _bool_env(value: bool) -> str:
    return "true" if value else "false"


# =============================================================================
# nginx
# =============================================================================
def _nginx_locations_block(forwarded_proto: str) -> str:
    """Bloque de locations compartido entre las variantes del server{} (el
    combinado 80+443 self-signed, el catch-all 80 y el 443 con dominio real).
    Solo cambia entre variantes el valor de X-Forwarded-Proto.

    SSO por cabecera de confianza: '/' (el chat) exige sesión de Django antes
    de proxiar a Open WebUI. 'auth_request' hace una subpetición interna a
    Django (auth_check); si hay sesión activa, nginx toma la identidad de las
    cabeceras de RESPUESTA de esa subpetición (auth_request_set) y las reenvía
    a Open WebUI como petición -Open WebUI, con WEBUI_AUTH_TRUSTED_EMAIL_HEADER
    configurado, auto-aprovisiona/autentica sin mostrar su propio login (ver
    docker_images/openwebui/README.md). Sin sesión, Django devuelve 401 y
    error_page lo convierte en un 302 al login único del panel."""
    return f"""
    location = /healthz {{
        return 200 "ok\\n";
        add_header Content-Type text/plain;
    }}

    location /panel/static/ {{
        alias /usr/share/nginx/panel-static/;
    }}

    location /panel/ {{
        proxy_pass http://sooniverse_metrics/;
        # nginx NO combina proxy_set_header entre niveles: en cuanto una location
        # define uno propio (X-Script-Name), deja de heredar TODOS los del server{{}}
        # -incluido Host- y Django recibe como HTTP_HOST el nombre del upstream
        # ("sooniverse_metrics"), disparando DisallowedHost. Hay que repetirlos.
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto {forwarded_proto};
        proxy_set_header X-Script-Name /panel;
    }}

    location ~ ^/(v1|key|user|model|team|health|spend|global)(/|$) {{
        proxy_pass http://sooniverse_litellm;
    }}

    # Subpetición interna de auth_request -nunca alcanzable desde fuera
    # ('internal;'). Apunta a la ruta REAL del urlconf de Django
    # (/metrics/auth-check/, sin '/panel': ese prefijo solo lo añade la
    # location /panel/ de arriba al reescribir, aquí se proxia directo).
    location = /_auth {{
        internal;
        proxy_pass http://sooniverse_metrics/metrics/auth-check/;
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto {forwarded_proto};
    }}

    location @sooniverse_login_redirect {{
        return 302 /panel/metrics/login/?next=$request_uri;
    }}

    location / {{
        auth_request /_auth;
        auth_request_set $sooniverse_email $upstream_http_x_sooniverse_email;
        auth_request_set $sooniverse_name  $upstream_http_x_sooniverse_name;
        error_page 401 = @sooniverse_login_redirect;

        proxy_pass http://sooniverse_webui;
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        # Nunca confiar en una cabecera que traiga el propio cliente -solo la
        # que puso nginx a partir de la sesión de Django ya verificada.
        proxy_set_header X-Sooniverse-Email $sooniverse_email;
        proxy_set_header X-Sooniverse-Name  $sooniverse_name;
    }}
"""


_NGINX_PROXY_COMMON = """
    client_max_body_size 100M;
    proxy_http_version 1.1;
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
"""

_NGINX_STREAMING_COMMON = """
    # Streaming de tokens (SSE) y llamadas largas: sin buffering ni timeouts cortos.
    proxy_buffering           off;
    proxy_cache               off;
    proxy_read_timeout        3600s;
    proxy_send_timeout        3600s;
    chunked_transfer_encoding on;
"""

_ACME_CHALLENGE_LOCATION = """
    location ^~ /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }
"""


def render_nginx_conf(config: Dict[str, Any]) -> str:
    gw = config.get("gateway", {})
    tls = gw.get("tls", {}) or {}
    tls_enabled = bool(tls.get("habilitado", False))
    modo = tls.get("modo", "self-signed")
    letsencrypt_mode = tls_enabled and modo == "letsencrypt"

    litellm_cfg = gw.get("litellm", {}) or {}
    litellm_base_url = (litellm_cfg.get("base_url") or "http://litellm:4000").rstrip("/")
    parsed_litellm = urlsplit(
        litellm_base_url if "://" in litellm_base_url else f"//{litellm_base_url}"
    )
    litellm_upstream_server = parsed_litellm.netloc or "litellm:4000"

    common_header = f"""{GENERATED_HEADER}
# Ruteo:
#   /              -> Open WebUI (chat, WebSocket)
#   /v1/, /key/... -> LiteLLM Proxy (API OpenAI-compatible + gestión de keys)
#   /panel/        -> Django (métricas y API keys), /panel/static/ servido
#                     directamente por nginx desde el volumen compartido
#   /healthz       -> 200 fijo de nginx, sin depender de ningún upstream

map $http_upgrade $connection_upgrade {{
    default upgrade;
    ''      close;
}}

upstream sooniverse_webui   {{ server open-webui:8080; }}
upstream sooniverse_litellm {{ server {litellm_upstream_server};     }}
upstream sooniverse_metrics {{ server metrics:8000;     }}
"""

    if not letsencrypt_mode:
        # Comportamiento preexistente (tls deshabilitado o modo 'self-signed'):
        # un único server{} en 80 (+443 si tls.habilitado), sin split de bloques.
        server_name = tls.get("dominio") or "_"
        ssl_block = ""
        listen_443 = ""
        if tls_enabled:
            listen_443 = "    listen 443 ssl;\n"
            ssl_block = (
                "    ssl_certificate     /etc/nginx/certs/fullchain.pem;\n"
                "    ssl_certificate_key /etc/nginx/certs/privkey.pem;\n"
                "    ssl_protocols TLSv1.2 TLSv1.3;\n"
                "    ssl_ciphers HIGH:!aNULL:!MD5;\n"
            )
        forwarded_proto = "https" if tls_enabled else "$scheme"

        return f"""{common_header}
server {{
    listen 80;
{listen_443}    server_name {server_name};
{_NGINX_PROXY_COMMON}    proxy_set_header X-Forwarded-Proto {forwarded_proto};
{_NGINX_STREAMING_COMMON}
{ssl_block}{_nginx_locations_block(forwarded_proto)}}}
"""

    # --- modo 'letsencrypt': tres server{} -------------------------------------
    # 1. (opcional) 80/server_name=dominio: solo el reto ACME + 301 a https.
    # 2. 80 default_server/server_name=_: sirve la app en claro (acceso por IP
    #    desnuda, y fallback mientras el DNS del dominio no resuelve todavía).
    # 3. 443/server_name=dominio: la app real, con el certificado de Let's Encrypt.
    dominio = tls["dominio"]
    dominio_cfg = gw.get("dominio") or {}
    redirigir_http = bool(dominio_cfg.get("redirigir_http", True))

    domain_http_block = ""
    if redirigir_http:
        domain_http_block = f"""
server {{
    listen 80;
    server_name {dominio};
{_ACME_CHALLENGE_LOCATION}
    location / {{
        return 301 https://$host$request_uri;
    }}
}}
"""

    catchall_http_block = f"""
server {{
    listen 80 default_server;
    server_name _;
{_NGINX_PROXY_COMMON}    proxy_set_header X-Forwarded-Proto $scheme;
{_NGINX_STREAMING_COMMON}
{_ACME_CHALLENGE_LOCATION}{_nginx_locations_block("$scheme")}}}
"""

    https_block = f"""
server {{
    listen 443 ssl;
    http2 on;
    server_name {dominio};
{_NGINX_PROXY_COMMON}    proxy_set_header X-Forwarded-Proto https;
{_NGINX_STREAMING_COMMON}
    ssl_certificate     /etc/letsencrypt/live/{dominio}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{dominio}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
{_nginx_locations_block("https")}}}
"""

    return f"{common_header}{domain_http_block}{catchall_http_block}{https_block}"


# =============================================================================
# docker-compose.yml
# =============================================================================
def _ports_or_expose(service_port: int, expose_direct: bool) -> str:
    if expose_direct:
        return f'    ports:\n      - "{service_port}:{service_port}"\n'
    return f"    expose:\n      - \"{service_port}\"\n"


def render_docker_compose(config: Dict[str, Any], capabilities_dir: Optional[Path] = None) -> str:
    gw = config.get("gateway", {})
    expose_direct = bool(gw.get("exponer_puertos_directos", False))
    tls = gw.get("tls", {}) or {}
    tls_enabled = bool(tls.get("habilitado", False))
    aws_region = config.get("red_y_aislamiento", {}).get("region", "us-east-1")

    # Clave SSH del bastion, montada en el panel para el botón "Reiniciar" de
    # la card Pool vLLM (metrics/workers.py). Mismo cálculo de existencia que
    # TopologyBuilder.build_gateway() para decidir si va en file_mounts -si no
    # existe todavía en la máquina que corre este render, NO se monta (un bind
    # mount de un origen inexistente haría que Docker cree un directorio vacío
    # en su lugar, en vez de fallar limpiamente).
    cliente_cfg = config.get("cliente", {})
    gateway_cluster_name = f"sooniverse-{cliente_cfg.get('id', 'default')}-{cliente_cfg.get('entorno', 'prod')}-gw"
    gateway_ssh_key = Path.home() / ".sky" / "generated" / "ssh-keys" / f"{gateway_cluster_name}.key"
    ssh_key_volume = "      - ../../.ssh_bastion_key:/app/.ssh/bastion_key:ro\n" if gateway_ssh_key.exists() else ""

    litellm_cfg = gw.get("litellm", {}) or {}
    litellm_base_url = (litellm_cfg.get("base_url") or "http://litellm:4000").rstrip("/")
    parsed_litellm = urlsplit(
        litellm_base_url if "://" in litellm_base_url else f"http://{litellm_base_url}"
    )
    litellm_port = parsed_litellm.port or 4000

    litellm_ports = _ports_or_expose(litellm_port, expose_direct)
    webui_ports = _ports_or_expose(8080, expose_direct)
    metrics_ports = _ports_or_expose(8000, expose_direct)

    # Flags de tareas automáticas de Open WebUI derivados de la verdad
    # observada (fail-closed si aún no hay sondeo, ver _load_effective_capabilities).
    owui_cfg = gw.get("open_webui", {}) or {}
    effective = _load_effective_capabilities(capabilities_dir)
    tareas_automaticas_on = _resolve_open_webui_flag(
        owui_cfg, "tareas_automaticas", effective["any_json_object"]
    )
    code_interpreter_on = _resolve_open_webui_flag(
        owui_cfg, "code_interpreter", effective["any_tool_calling"]
    )
    task_generation_env = "\n".join(
        f"      {key}: \"{_bool_env(tareas_automaticas_on)}\"" for key in OPEN_WEBUI_TASK_ENV_KEYS
    )

    modo_tls = tls.get("modo", "self-signed")
    letsencrypt_mode = tls_enabled and modo_tls == "letsencrypt"

    proxy_ports = ['      - "80:80"']
    proxy_volumes = [
        "      - ./nginx/default.conf:/etc/nginx/conf.d/default.conf:ro",
        "      - panel_static:/usr/share/nginx/panel-static:ro",
    ]
    proxy_command = ""
    certbot_service = ""
    if tls_enabled:
        proxy_ports.append('      - "443:443"')
        if letsencrypt_mode:
            # Fuera de docker_images/gateway a propósito: ese directorio se
            # re-sincroniza en cada 'sky launch' (file_mounts), y un rsync sin
            # --delete no debería borrar los certs, pero tampoco es el sitio
            # para depender de eso -certbot y renovación viven en un path del
            # host ajeno al árbol sincronizado.
            proxy_volumes.append("      - /opt/sooniverse/letsencrypt:/etc/letsencrypt:ro")
            proxy_volumes.append("      - /opt/sooniverse/certbot-www:/var/www/certbot:ro")
            # nginx no relee ssl_certificate en caliente: recarga periódica para
            # tomar el certificado renovado por el sidecar 'certbot' sin caídas.
            proxy_command = (
                '    command: ["sh", "-c", '
                '"while :; do sleep 21600 & wait $!; nginx -s reload; done & nginx -g \'daemon off;\'"]\n'
            )
            certbot_service = """
  certbot:
    image: certbot/certbot
    container_name: sooniverse-certbot
    volumes:
      - /opt/sooniverse/letsencrypt:/etc/letsencrypt
      - /opt/sooniverse/certbot-www:/var/www/certbot
    entrypoint: ["sh", "-c", "trap exit TERM; while :; do certbot renew --webroot -w /var/www/certbot --quiet; sleep 43200 & wait $!; done"]
    networks: [gateway_net]
    restart: unless-stopped
"""
        else:
            proxy_volumes.append("      - ./nginx/certs:/etc/nginx/certs:ro")
    proxy_ports_block = "\n".join(proxy_ports)
    proxy_volumes_block = "\n".join(proxy_volumes)

    return f"""{GENERATED_HEADER}
# Única superficie pública del clúster (cuando gateway.exponer_puertos_directos
# es false, el default recomendado): los workers vLLM viven en la subred
# privada y solo son alcanzables desde aquí por IP interna de la VPC; litellm/
# open-webui/metrics no publican puertos al host, solo nginx.
#
# Arranque:  sudo docker compose --env-file ../../.env up -d --build
#
# PostgreSQL: por defecto se asume una instancia EXISTENTE (RDS o externa)
# definida en `.env`. Para levantar una local incluida en el stack:
#     sudo docker compose --env-file ../../.env --profile local-db up -d
#     (y fijar DB_HOST=postgres en el .env)

name: sooniverse-gateway

services:

  postgres:
    image: postgres:16-alpine
    container_name: sooniverse-postgres
    profiles: ["local-db"]
    environment:
      POSTGRES_DB: ${{DB_NAME:-sooniverse}}
      POSTGRES_USER: ${{DB_USER:-postgres}}
      POSTGRES_PASSWORD: ${{DB_PASSWORD:-postgres}}
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${{DB_USER:-postgres}} -d ${{DB_NAME:-sooniverse}}"]
      interval: 10s
      timeout: 5s
      retries: 10
    networks: [gateway_net]
    restart: unless-stopped

  redis:
    image: redis:7-alpine
    container_name: sooniverse-redis
    command: ["redis-server", "--appendonly", "no", "--maxmemory", "256mb", "--maxmemory-policy", "allkeys-lru"]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 3s
      retries: 5
    networks: [gateway_net]
    restart: unless-stopped

  litellm:
    image: ghcr.io/berriai/litellm:main-stable
    container_name: sooniverse-litellm
    command: ["--config", "/app/config.yaml", "--port", "{litellm_port}", "--num_workers", "4"]
    environment:
      # Esquema PROPIO ('litellm', no 'sooniverse'): su motor de migraciones
      # (Prisma) calcula un diff contra TODO lo que encuentra en el esquema y
      # puede intentar borrar objetos ajenos que no reconoce -confirmado en
      # despliegue real: un intento de DROP TABLE sobre 'api_key_registry'
      # bloqueado solo porque nuestras vistas dependían de ella, dejando a
      # LiteLLM sin ninguna tabla propia creada. Ver el comentario
      # "CONVIVENCIA CON LITELLM" en database/001_init_schema.sql.
      DATABASE_URL: postgresql://${{DB_USER:-postgres}}:${{DB_PASSWORD}}@${{DB_HOST:-postgres}}:${{DB_PORT:-5432}}/${{DB_NAME:-sooniverse}}?schema=litellm
      LITELLM_MASTER_KEY: ${{LITELLM_MASTER_KEY:-sk-sooniverse-master-change-me}}
      LITELLM_SALT_KEY: ${{LITELLM_SALT_KEY:-sooniverse-salt-change-me}}
      REDIS_HOST: redis
      REDIS_PORT: "6379"
      STORE_MODEL_IN_DB: "True"
      # PRIVACIDAD: LiteLLM no debe persistir el contenido de las peticiones.
      LITELLM_LOG: "ERROR"
      DISABLE_SCHEMA_UPDATE: "False"
    volumes:
      # Generado dinámicamente por scripts/render_litellm_config.py con las IPs
      # privadas de los workers que SkyPilot acaba de aprovisionar.
      - ./litellm_config.yaml:/app/config.yaml:ro
{litellm_ports}    depends_on:
      redis:
        condition: service_healthy
    healthcheck:
      test: ["CMD-SHELL", "python -c \\"import urllib.request;urllib.request.urlopen('http://localhost:{litellm_port}/health/liveliness')\\" || exit 1"]
      interval: 30s
      timeout: 10s
      retries: 5
      start_period: 60s
    extra_hosts:
      - "host.docker.internal:host-gateway"
    networks: [gateway_net]
    restart: unless-stopped

  open-webui:
    build:
      context: ../openwebui
      dockerfile: Dockerfile
    image: sooniverse/open-webui:0.11.0
    container_name: sooniverse-webui
    environment:
      # Todo el tráfico de chat pasa por el balanceador, nunca directo al worker.
      OPENAI_API_BASE_URL: {litellm_base_url}/v1
      OPENAI_API_KEY: ${{LITELLM_MASTER_KEY:-sk-sooniverse-master-change-me}}
      WEBUI_NAME: "Sooniverse AI"
      WEBUI_AUTH: "True"
      ENABLE_SIGNUP: ${{WEBUI_SIGNUP:-false}}
      ENABLE_OLLAMA_API: "False"
      WEBUI_SECRET_KEY: ${{SECRET_KEY:-sooniverse-webui-secret}}
      # 'user', no 'pending': con SSO por cabecera de confianza (ver abajo)
      # Django YA es el único gatekeeper -login_required exige cuenta activa
      # antes de que nginx deje pasar la petición-. Dejar el default de
      # fábrica 'pending' aquí bloqueaba a CUALQUIER usuario que no fuera el
      # primero jamás creado hasta una aprobación manual dentro del propio
      # panel de admin de Open WebUI -exactamente el segundo punto de
      # administración que esta unificación de login busca eliminar
      # (confirmado en una prueba real: un admin de Django autenticado por
      # SSO quedaba con role=pending en Open WebUI).
      DEFAULT_USER_ROLE: "user"
      WEBUI_URL: ${{GATEWAY_PUBLIC_URL:-}}
      # SSO por cabecera de confianza: Django es la única pantalla de login del
      # clúster (nginx la protege con 'auth_request' antes de proxiar aquí, ver
      # scripts/render_gateway_stack.py::_nginx_locations_block). Con esto
      # activo, el frontend de Open WebUI NUNCA muestra su propio formulario
      # -ver docker_images/openwebui/README.md-. Requiere
      # 'gateway.exponer_puertos_directos: false' (validado en
      # generate_infra.py): si el 8080 quedara publicado directo, cualquiera
      # podría inyectar estas cabeceras y suplantar a un usuario.
      WEBUI_AUTH_TRUSTED_EMAIL_HEADER: X-Sooniverse-Email
      WEBUI_AUTH_TRUSTED_NAME_HEADER: X-Sooniverse-Name
      ENABLE_LOGIN_FORM: "False"
      # Persistencia relacional en el MISMO esquema que LiteLLM y Django (ver
      # docs/03_ESTADO_Y_BD.md). 'webui_data' se mantiene: ficheros subidos y
      # el vector store local (Chroma) no son relacionales y siguen en disco.
      DATABASE_URL: postgresql://${{DB_USER:-postgres}}:${{DB_PASSWORD}}@${{DB_HOST:-postgres}}:${{DB_PORT:-5432}}/${{DB_NAME:-sooniverse}}?options=-csearch_path%3Dsooniverse
      DATABASE_SCHEMA: sooniverse
      DB_NAME: ${{DB_NAME:-sooniverse}}
      DB_USER: ${{DB_USER:-postgres}}
      DB_PASSWORD: ${{DB_PASSWORD}}
      DB_HOST: ${{DB_HOST:-postgres}}
      DB_PORT: ${{DB_PORT:-5432}}
      # Tareas automáticas (título/tags/autocompletado/follow-up/consultas de
      # RAG y búsqueda) dependen de response_format=json_object; encenderlas
      # sin que el modelo lo soporte es el 400 documentado contra vLLM (ver
      # scripts/test_model_capabilities.py::probe_json_object). Derivadas de
      # la verdad observada en sooniverse.model_capability -o del override
      # gateway.open_webui.tareas_automaticas si el operador lo fuerza-.
{task_generation_env}
      # Ídem para el intérprete de código, que depende de tool calling real.
      ENABLE_CODE_INTERPRETER: "{_bool_env(code_interpreter_on)}"
      # Sin backend propio en esta infra: apagados fijos, no dependen de capacidades.
      ENABLE_IMAGE_GENERATION: "false"
      ENABLE_WEB_SEARCH: "false"
      ENABLE_EVALUATION_ARENA_MODELS: "false"
    volumes:
      - webui_data:/app/backend/data
{webui_ports}    depends_on:
      - litellm
    healthcheck:
      test: ["CMD-SHELL", "python3 -c \\"import urllib.request;urllib.request.urlopen('http://localhost:8080/')\\" || exit 1"]
      interval: 30s
      timeout: 10s
      retries: 5
      start_period: 90s
    networks: [gateway_net]
    restart: unless-stopped

  openwebui-bootstrap:
    build:
      context: ../openwebui
      dockerfile: Dockerfile
    image: sooniverse/open-webui:0.11.0
    container_name: sooniverse-webui-bootstrap
    entrypoint: ["python3", "-m", "sooniverse.bootstrap_models"]
    environment:
      DATABASE_URL: postgresql://${{DB_USER:-postgres}}:${{DB_PASSWORD}}@${{DB_HOST:-postgres}}:${{DB_PORT:-5432}}/${{DB_NAME:-sooniverse}}?options=-csearch_path%3Dsooniverse
      DATABASE_SCHEMA: sooniverse
      DB_NAME: ${{DB_NAME:-sooniverse}}
      DB_USER: ${{DB_USER:-postgres}}
      DB_PASSWORD: ${{DB_PASSWORD}}
      DB_HOST: ${{DB_HOST:-postgres}}
      DB_PORT: ${{DB_PORT:-5432}}
      LITELLM_BASE_URL: {litellm_base_url}
      LITELLM_MASTER_KEY: ${{LITELLM_MASTER_KEY:-sk-sooniverse-master-change-me}}
      CLIENTE_ID: ${{CLIENTE_ID:-default}}
      ENTORNO: ${{ENTORNO:-prod}}
      OPENWEBUI_BASE_URL: http://open-webui:8080
      # Cuenta técnica de bootstrap (ver .env.example): la PRIMERA en firmar
      # en una instancia nueva, asciende a admin automáticamente incluso con
      # ENABLE_SIGNUP=false (backend/open_webui/routers/auths.py).
      OPENWEBUI_BOOTSTRAP_EMAIL: ${{OPENWEBUI_BOOTSTRAP_EMAIL:-bootstrap@sooniverse.internal}}
      OPENWEBUI_BOOTSTRAP_PASSWORD: ${{OPENWEBUI_BOOTSTRAP_PASSWORD}}
      # MISMO valor que el servicio 'open-webui': con SSO activo, /signup y
      # /signin por contraseña quedan bloqueados del lado del servidor sin
      # excepciones -bootstrap_models.py::authenticate() necesita saber el
      # nombre de esta cabecera para autenticarse él mismo con ella (ver
      # docker_images/openwebui/README.md). Sin esta línea, el bootstrap de
      # cada despliegue con dominio/SSO fallaría con 400 en /signin.
      WEBUI_AUTH_TRUSTED_EMAIL_HEADER: X-Sooniverse-Email
    depends_on:
      open-webui:
        condition: service_healthy
    networks: [gateway_net]
    restart: "no"
    profiles: ["bootstrap"]

  metrics:
    build:
      context: ../../django_metrics
      dockerfile: Dockerfile
    image: sooniverse/metrics-panel:1.0.0
    container_name: sooniverse-metrics
    environment:
      SECRET_KEY: ${{SECRET_KEY:-insecure-dev-key-change-me}}
      DEBUG: ${{DEBUG:-False}}
      ALLOWED_HOSTS: ${{ALLOWED_HOSTS:-*}}
      CSRF_TRUSTED_ORIGINS: ${{CSRF_TRUSTED_ORIGINS:-}}
      HTTPS_ACTIVO: ${{HTTPS_ACTIVO:-false}}
      FORCE_SCRIPT_NAME: ${{FORCE_SCRIPT_NAME:-/panel}}
      DB_NAME: ${{DB_NAME:-sooniverse}}
      DB_USER: ${{DB_USER:-postgres}}
      DB_PASSWORD: ${{DB_PASSWORD}}
      DB_HOST: ${{DB_HOST:-postgres}}
      DB_PORT: ${{DB_PORT:-5432}}
      LITELLM_BASE_URL: {litellm_base_url}
      LITELLM_MASTER_KEY: ${{LITELLM_MASTER_KEY:-sk-sooniverse-master-change-me}}
      CLIENTE_ID: ${{CLIENTE_ID:-default}}
      ENTORNO: ${{ENTORNO:-prod}}
      # Región AWS del despliegue -la usan las acciones de worker (apagar/
      # arrancar la instancia EC2 desde la card Pool vLLM, metrics/workers.py).
      AWS_REGION: {aws_region}
      # TIENE que ser la misma zona con la que scripts/db_setup.py corta los
      # buckets (sooniverse.app_setting.reporting_timezone). Si el panel
      # renderiza en una zona y la agregación se hizo en otra, los días salen
      # desplazados: es exactamente el bug que arregla 004_usage_analytics.sql.
      TIME_ZONE: ${{TIME_ZONE:-America/Bogota}}
      # Coste por hora de la infraestructura de inferencia, para la tarjeta de
      # tiempos muertos. Con 0 (por defecto) la columna de coste se oculta.
      METRICS_COSTE_HORA_USD: ${{METRICS_COSTE_HORA_USD:-0}}
      METRICS_REFRESH_INTERVAL: ${{METRICS_REFRESH_INTERVAL:-300}}
      DJANGO_SUPERUSER_USERNAME: ${{DJANGO_SUPERUSER_USERNAME:-admin}}
      DJANGO_SUPERUSER_PASSWORD: ${{DJANGO_SUPERUSER_PASSWORD:-}}
      DJANGO_SUPERUSER_EMAIL: ${{DJANGO_SUPERUSER_EMAIL:-admin@sooniverse.co}}
    volumes:
      - panel_static:/app/staticfiles
{ssh_key_volume}{metrics_ports}    depends_on:
      - litellm
    extra_hosts:
      - "host.docker.internal:host-gateway"
    networks: [gateway_net]
    restart: unless-stopped

  proxy:
    image: nginx:1.27-alpine
    container_name: sooniverse-proxy
    volumes:
{proxy_volumes_block}
    ports:
{proxy_ports_block}
{proxy_command}    depends_on:
      - open-webui
      - litellm
      - metrics
    networks: [gateway_net]
    restart: unless-stopped
{certbot_service}
networks:
  gateway_net:
    driver: bridge

volumes:
  pgdata:
  webui_data:
  panel_static:
"""


def _derive_tls_from_dominio(config: Dict[str, Any]) -> None:
    """Si gateway.dominio.habilitado es true, deriva gateway.tls.* a partir del catálogo
    para que tanto nginx como docker-compose se rendericen con la configuración adecuada."""
    gw = config.setdefault("gateway", {})
    dominio_cfg = gw.get("dominio") or {}
    if not dominio_cfg.get("habilitado", False):
        return

    seleccionado = dominio_cfg.get("seleccionado")
    disponibles = dominio_cfg.get("disponibles") or []
    entrada = next((e for e in disponibles if e.get("nombre") == seleccionado), None)
    if not entrada:
        return

    email_acme = entrada.get("email_acme")
    tls = gw.setdefault("tls", {})
    if tls.get("dominio") not in (None, seleccionado):
        return

    tls["habilitado"] = True
    tls["modo"] = "letsencrypt"
    tls["dominio"] = seleccionado
    tls["email_acme"] = email_acme


def render(config: Dict[str, Any], capabilities_dir: Optional[Path] = None) -> None:
    """`capabilities_dir`: directorio donde buscar `.sooniverse_capabilities.json`
    (ver `_load_effective_capabilities`) -normalmente el mismo `out_dir` que ya
    usa generate_infra.py para los manifiestos de ESTE cliente (multi-cliente,
    Fase 6). Si se omite, se asume que aún no hay sondeo (fail-closed)."""
    _derive_tls_from_dominio(config)
    NGINX_CONF_PATH.parent.mkdir(parents=True, exist_ok=True)
    NGINX_CONF_PATH.write_text(render_nginx_conf(config), encoding="utf-8", newline="\n")
    print(f"[OK] nginx     -> {NGINX_CONF_PATH.relative_to(REPO_ROOT)}")

    COMPOSE_PATH.write_text(render_docker_compose(config, capabilities_dir), encoding="utf-8", newline="\n")
    print(f"[OK] compose   -> {COMPOSE_PATH.relative_to(REPO_ROOT)}")

    gw = config.get("gateway", {})
    expose_direct = bool(gw.get("exponer_puertos_directos", False))
    if not expose_direct:
        print("[INFO] exponer_puertos_directos=false -> 4000/8000/8080 solo accesibles "
              "dentro de la red Docker; nginx (80/443) es la única puerta pública.")
    else:
        print("[WARNING] exponer_puertos_directos=true -> litellm/open-webui/metrics "
              "publican sus puertos directamente al host. Usar solo en dev/depuración.")


def _artifacts_dir_for(config_path: Path, config: Dict[str, Any]) -> Path:
    """Misma regla que generate_infra.artifacts_dir_for (duplicada a propósito,
    ver ese docstring): raíz del repo si --config es el config_global.yaml
    raíz, `.artifacts/<cliente>-<entorno>/` si no."""
    try:
        is_default_root_config = config_path.resolve() == (REPO_ROOT / "config_global.yaml").resolve()
    except OSError:
        is_default_root_config = False
    if is_default_root_config:
        return REPO_ROOT
    cliente = config["cliente"]
    return REPO_ROOT / ".artifacts" / f"{cliente['id']}-{cliente['entorno']}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Renderiza nginx/default.conf y docker-compose.yml del Gateway.")
    parser.add_argument("--config", default=str(REPO_ROOT / "config_global.yaml"))
    args = parser.parse_args()

    config_path = Path(args.config)
    with config_path.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    render(config, capabilities_dir=_artifacts_dir_for(config_path, config))
    return 0


if __name__ == "__main__":
    sys.exit(main())
