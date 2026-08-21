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

echo "[openwebui] Delegando arranque a la imagen base (Alembic + uvicorn)."
cd /app/backend
exec bash start.sh
