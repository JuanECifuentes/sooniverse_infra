#!/usr/bin/env bash
# ==============================================================================
# SOONIVERSE :: Arranque del panel de Métricas
# ==============================================================================
set -euo pipefail

echo "[metrics] Esperando PostgreSQL en ${DB_HOST}:${DB_PORT}..."
for i in $(seq 1 60); do
    if python -c "
import os, sys, psycopg2
try:
    psycopg2.connect(dbname=os.environ['DB_NAME'], user=os.environ['DB_USER'],
                     password=os.environ['DB_PASSWORD'], host=os.environ['DB_HOST'],
                     port=os.environ['DB_PORT'], connect_timeout=3).close()
except Exception:
    sys.exit(1)
" 2>/dev/null; then
        echo "[metrics] PostgreSQL disponible."
        break
    fi
    sleep 3
done

# Solo las tablas propias de Django (auth, sessions, admin). Las tablas de
# métricas son `managed = False`: las crea database/init_schema.sql.
echo "[metrics] Aplicando migraciones internas de Django..."
python manage.py migrate --noinput || echo "[metrics] WARNING: migrate falló; el panel puede operar en modo lectura."

echo "[metrics] Recolectando estáticos..."
# El '>/dev/null 2>&1 || true' anterior ocultó durante mucho tiempo un fallo
# real: un sourceMappingURL de un .js vendorizado apuntaba a un .map que no se
# distribuye, collectstatic abortaba y NUNCA se escribía staticfiles.json. El
# panel seguía funcionando porque WhiteNoise cae a rutas sin hash, así que ni el
# cache-busting ni la precompresión se estaban aplicando y nadie se enteró.
# Ahora el error se ve; no se aborta el arranque porque el panel sigue siendo
# usable con estáticos sin hash, pero tiene que quedar en los logs.
if ! python manage.py collectstatic --noinput --clear; then
    echo "[metrics] WARNING: collectstatic falló. El panel servirá los estáticos SIN hash"
    echo "[metrics]          (sin cache-busting ni precompresión). Revisa el error de arriba."
fi

# Superusuario opcional e idempotente para acceder al panel.
if [ -n "${DJANGO_SUPERUSER_PASSWORD:-}" ]; then
    echo "[metrics] Asegurando superusuario '${DJANGO_SUPERUSER_USERNAME:-admin}'..."
    python manage.py ensure_superuser || true
fi

# Refresco periódico de métricas (ETL LiteLLM -> rollups) en segundo plano.
if [ "${METRICS_REFRESH_INTERVAL:-300}" -gt 0 ] 2>/dev/null; then
    echo "[metrics] Job de refresco cada ${METRICS_REFRESH_INTERVAL}s"
    (
        while true; do
            sleep "${METRICS_REFRESH_INTERVAL}"
            python manage.py sync_metrics --quiet || true
        done
    ) &
fi

echo "[metrics] Sirviendo en 0.0.0.0:8000"
exec gunicorn sooniverse_panel.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers 3 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile -
