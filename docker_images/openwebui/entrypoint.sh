#!/usr/bin/env bash
# ==============================================================================
# SOONIVERSE :: Arranque de Open WebUI (imagen derivada)
# ==============================================================================
# Solo espera a PostgreSQL y delega el arranque real (Alembic + uvicorn) al
# start.sh original de la imagen base: las migraciones de Open WebUI las
# sigue gestionando su propio código, no este script.
set -euo pipefail

if [ -n "${DATABASE_URL:-}" ] && echo "${DATABASE_URL}" | grep -q '^postgresql'; then
    echo "[openwebui] Esperando PostgreSQL (${DB_HOST:-desconocido}:${DB_PORT:-5432})..."
    for i in $(seq 1 60); do
        if python3 -c "
import os, sys, psycopg2
try:
    psycopg2.connect(dbname=os.environ['DB_NAME'], user=os.environ['DB_USER'],
                      password=os.environ['DB_PASSWORD'], host=os.environ['DB_HOST'],
                      port=os.environ['DB_PORT'], connect_timeout=3).close()
except Exception:
    sys.exit(1)
" 2>/dev/null; then
            echo "[openwebui] PostgreSQL disponible."
            break
        fi
        sleep 3
    done
else
    echo "[openwebui] DATABASE_URL no apunta a PostgreSQL; se omite la espera."
fi

# Personalización de marca en caliente (post-despliegue):
# Si el cliente coloca sus propias imágenes en /app/backend/data/branding/ (volumen persistente webui_data),
# se aplican sobre los directorios estáticos de la aplicación sin tener que recompilar la imagen Docker.
if [ -d "/app/backend/data/branding" ]; then
    echo "[openwebui] Aplicando archivos de marca personalizados desde /app/backend/data/branding..."
    mkdir -p /app/backend/open_webui/static /app/build/static
    cp -rf /app/backend/data/branding/* /app/backend/open_webui/static/ 2>/dev/null || true
    cp -rf /app/backend/data/branding/* /app/build/static/ 2>/dev/null || true
fi

# Configuración en runtime del botón de navegación chat -> panel
# (overlay/static/sooniverse-nav.js, enlazado desde index.html por el Dockerfile).
# Se escribe aquí -no en build- porque el destino depende del entorno:
# en producción nginx sirve el panel bajo /panel/ en el mismo origen; en
# desarrollo local el panel vive en otro puerto (p. ej. http://localhost:8000).
PANEL_URL_CFG="${SOONIVERSE_PANEL_URL:-/panel/}"
LOGOUT_URL_CFG="${SOONIVERSE_LOGOUT_URL:-${PANEL_URL_CFG%/}/metrics/logout/}"
echo "[openwebui] Botón de navegación chat -> panel: ${PANEL_URL_CFG}"
echo "[openwebui] Endpoint de logout: ${LOGOUT_URL_CFG}"
printf 'window.__SOONIVERSE_PANEL_URL__ = "%s";\nwindow.__SOONIVERSE_LOGOUT_URL__ = "%s";\n' "${PANEL_URL_CFG}" "${LOGOUT_URL_CFG}" > /app/build/sooniverse-nav-config.js

echo "[openwebui] Delegando arranque a la imagen base (Alembic + uvicorn)."
cd /app/backend
exec bash start.sh
