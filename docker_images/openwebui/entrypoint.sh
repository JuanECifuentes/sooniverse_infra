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

echo "[openwebui] Delegando arranque a la imagen base (Alembic + uvicorn)."
cd /app/backend
exec bash start.sh
