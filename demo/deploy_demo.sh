#!/usr/bin/env bash
# ==============================================================================
# Sooniverse Demo - despliegue del Gateway (chat + panel + LiteLLM -> DeepInfra)
# en un VPS que YA aloja otros proyectos. Ver demo/DESPLIEGUE_DEMO.md.
# ==============================================================================
# Uso (desde cualquier directorio):
#   ./demo/deploy_demo.sh            # = up: despliega o actualiza (idempotente)
#   ./demo/deploy_demo.sh render     # solo regenera demo/generated/
#   ./demo/deploy_demo.sh status     # estado de los contenedores
#   ./demo/deploy_demo.sh logs [svc] # logs (todos o de un servicio)
#   ./demo/deploy_demo.sh down       # detiene el stack (conserva datos)
#
# Nunca toca puertos 80/443 ni nada fuera del proyecto 'sooniverse-demo'.
# ==============================================================================
set -euo pipefail

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$DEMO_DIR")"
ENV_FILE="$DEMO_DIR/.env.demo"
COMPOSE_FILE="$DEMO_DIR/docker-compose.demo.yml"

# Los archivos que escribe el contenedor 'tools' quedan con el dueño de quien
# ejecuta este script (ver 'user:' en docker-compose.demo.yml).
export DEMO_UID="$(id -u)"
export DEMO_GID="$(id -g)"

log()  { printf '\n===> %s\n' "$*"; }
warn() { printf '[WARNING] %s\n' "$*" >&2; }
die()  { printf '[ERROR] %s\n' "$*" >&2; exit 1; }

dc() { docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"; }

env_get() {
    # Último valor de KEY en .env.demo (sin comillas), vacío si no existe.
    grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -n1 | cut -d= -f2- | sed -e 's/^["'\'']//' -e 's/["'\'']$//'
}

wait_healthy() {
    local container="$1" timeout="${2:-300}" waited=0 status
    printf '     esperando a %s ' "$container"
    while [ "$waited" -lt "$timeout" ]; do
        status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container" 2>/dev/null || echo missing)"
        if [ "$status" = "healthy" ]; then printf ' OK\n'; return 0; fi
        printf '.'; sleep 5; waited=$((waited + 5))
    done
    printf ' TIMEOUT (%s)\n' "$status"
    return 1
}

preflight() {
    command -v docker >/dev/null || die "Docker no está instalado."
    docker compose version >/dev/null 2>&1 || die "Se requiere 'docker compose' v2."
    [ -f "$ENV_FILE" ] || die "No existe $ENV_FILE. Copia demo/.env.demo.example y rellénalo."

    local missing=() key val
    for key in DEMO_DOMAIN DEMO_HTTP_PORT DEEPINFRA_API_KEY DEMO_MODEL_ID LITELLM_MASTER_KEY \
               LITELLM_SALT_KEY DB_PASSWORD SECRET_KEY DJANGO_SUPERUSER_PASSWORD \
               OPENWEBUI_BOOTSTRAP_PASSWORD; do
        val="$(env_get "$key")"
        if [ -z "$val" ] || [[ "$val" == *CAMBIA-ESTO* ]]; then missing+=("$key"); fi
    done
    [ "${#missing[@]}" -eq 0 ] || die "Variables sin definir en .env.demo: ${missing[*]}"

    [[ "$(env_get LITELLM_MASTER_KEY)" == sk-* ]] || die "LITELLM_MASTER_KEY debe empezar por 'sk-'."
    [ "$(env_get DB_HOST)" = "postgres" ] || warn "DB_HOST no es 'postgres': se usará una BD externa al stack."

    local port; port="$(env_get DEMO_HTTP_PORT)"
    case "$port" in
        80|443) die "DEMO_HTTP_PORT=$port: esos puertos son del Nginx del host. Usa uno interno (p.ej. 8088)." ;;
        ''|*[!0-9]*) die "DEMO_HTTP_PORT inválido: '$port'." ;;
    esac
    # Puerto ocupado por algo que NO es nuestro propio proxy (redeploy).
    if ! docker ps --format '{{.Names}}' | grep -qx sooniverse-demo-proxy; then
        if command -v ss >/dev/null && ss -ltnH 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]${port}\$"; then
            die "El puerto $port ya está en uso en este servidor. Cambia DEMO_HTTP_PORT."
        fi
    fi
}

render() {
    log "Construyendo contenedor de utilidades y renderizando demo/generated/"
    dc --profile tools build tools
    dc --profile tools run --rm tools python demo/render_demo.py --env-file demo/.env.demo
}

up() {
    preflight
    render

    log "1/6 PostgreSQL + Redis"
    dc up -d postgres redis
    wait_healthy sooniverse-demo-postgres 120 || die "PostgreSQL no arrancó (./demo/deploy_demo.sh logs postgres)."

    log "2/6 Esquema de base de datos (scripts/db_setup.py, idempotente)"
    dc --profile tools run --rm tools python scripts/db_setup.py --env-file demo/.env.demo --sql-dir database

    log "3/6 LiteLLM -> $(env_get DEMO_MODEL_ID)"
    dc up -d --force-recreate litellm
    wait_healthy sooniverse-demo-litellm 300 || die "LiteLLM no quedó sano (./demo/deploy_demo.sh logs litellm)."

    log "4/6 Key virtual de la interfaz de chat (se verá como 'Interfaz Web' en el panel)"
    dc --profile tools run --rm tools python scripts/ensure_openwebui_key.py \
        --env-file demo/.env.demo --base-url http://litellm:4000 \
        || warn "No se pudo asegurar la key del chat; usará la master key (consumo '(sin registro)')."

    log "5/6 Chat (Open WebUI) + Panel + nginx interno (127.0.0.1:$(env_get DEMO_HTTP_PORT))"
    # Compose relee .env.demo en cada invocación: si el paso 4 acaba de añadir
    # OPENWEBUI_LITELLM_API_KEY, open-webui se recrea con ella.
    dc up -d --build open-webui metrics
    dc up -d --force-recreate proxy
    wait_healthy sooniverse-demo-webui 420 || warn "Open WebUI tarda en arrancar; revisa: ./demo/deploy_demo.sh logs open-webui"

    log "6/6 Registro de modelos en el chat (openwebui-bootstrap)"
    dc --profile bootstrap run --rm openwebui-bootstrap \
        || warn "El bootstrap de modelos falló; reintenta con: ./demo/deploy_demo.sh"

    local port domain
    port="$(env_get DEMO_HTTP_PORT)"; domain="$(env_get DEMO_DOMAIN)"
    log "Comprobación local"
    if curl -fsS "http://127.0.0.1:${port}/healthz" >/dev/null 2>&1; then
        echo "     nginx interno OK en 127.0.0.1:${port}"
    else
        warn "nginx interno no responde en 127.0.0.1:${port} (./demo/deploy_demo.sh logs proxy)"
    fi

    cat <<EOF

===> Demo desplegado.
     Server block para el Nginx del host: demo/generated/nginx-host-${domain}.conf
     (instalación: paso 6 de demo/DESPLIEGUE_DEMO.md)

     Panel:  https://${domain}/panel/   (usuario: $(env_get DJANGO_SUPERUSER_USERNAME))
     Chat:   https://${domain}/
     API:    https://${domain}/v1   (modelo: $(env_get DEMO_MODEL_NAME))
EOF
}

case "${1:-up}" in
    up)     up ;;
    render) preflight; render ;;
    status) dc ps ;;
    logs)   shift; dc logs -f --tail=200 "$@" ;;
    down)   dc down ;;
    *)      die "Subcomando desconocido: $1 (usa: up | render | status | logs [svc] | down)" ;;
esac
