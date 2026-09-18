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
#
# CORREGIDO: esto era un '|| echo WARNING' silencioso de un solo intento. Si
# el esquema 'sooniverse' todavía no existía cuando arrancó este contenedor
# (p.ej. AUTO_INIT_DB=false, o la BD alcanzable desde el Gateway pero no
# desde el operador en el momento de generate_infra.py::_ensure_db_schema),
# 'migrate' fallaba con "no schema has been selected to create in"
# (search_path=sooniverse sin fallback a public, ver sooniverse_panel/
# settings.py), el error se tragaba, 'ensure_superuser' de abajo fallaba
# DESPUÉS por la misma razón (también silenciado con '|| true'), y gunicorn
# arrancaba igual -con CERO admin creado y CERO tablas de Django, así que
# TODO login (panel y chat, que depende del mismo login vía auth_request)
# devolvía 500 sin ninguna señal más allá de un WARNING genérico y engañoso
# ("puede operar en modo lectura" -no puede operar en absoluto sin auth_user).
# Ahora se reintenta unas pocas veces (el esquema puede estar terminando de
# aplicarse en paralelo desde otro lado) y, si sigue fallando, el mensaje es
# explícito sobre el impacto real.
echo "[metrics] Aplicando migraciones internas de Django..."
MIGRATE_OK=0
for intento in 1 2 3; do
    if python manage.py migrate --noinput; then
        MIGRATE_OK=1
        break
    fi
    echo "[metrics] migrate falló (intento ${intento}/3); reintentando en 5s..."
    sleep 5
done
if [ "$MIGRATE_OK" -ne 1 ]; then
    echo "[metrics] ERROR: migrate falló tras 3 intentos. El esquema 'sooniverse' probablemente"
    echo "[metrics]        no existe todavía -ningún login (panel NI chat) va a funcionar hasta"
    echo "[metrics]        que se aplique. Ejecuta manualmente: python scripts/db_setup.py"
fi

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
