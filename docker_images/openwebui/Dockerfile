# ==============================================================================
# SOONIVERSE :: Open WebUI (imagen derivada, tag fijo)
# ==============================================================================
# Vendorizada para: (1) persistir en PostgreSQL/esquema sooniverse en vez de
# SQLite efímero, (2) aplicar parches de backend/frontend si la depuración en
# vivo los exige (ver patches/README.md), (3) dejar cableado el overlay visual
# del Manual de imagen Sooniverse (.claude/skills/sooniverse-ui-identity).
#
# Tag FIJO a propósito (nunca ':main'): ':main' es mutable y puede romper un
# despliegue sin que cambie nada en este repo. Para actualizar de versión, ver
# README.md de este directorio.
FROM ghcr.io/open-webui/open-webui:v0.11.0

WORKDIR /app/backend

# 'patch' para aplicar patches/*.patch en el build; psycopg2 ya viene en la
# imagen base (requirements.txt de Open WebUI), así que no hace falta libpq
# aparte para el wait-for-postgres de entrypoint.sh.
RUN apt-get update \
 && apt-get install -y --no-install-recommends patch \
 && rm -rf /var/lib/apt/lists/*

# Módulo propio: NO se ejecuta en el arranque normal del contenedor. Lo invoca
# el servicio one-shot 'openwebui-bootstrap' del docker-compose generado
# (scripts/render_gateway_stack.py) o scripts/sync_openwebui_models.py.
COPY overlay/sooniverse ./sooniverse

# Overlay visual: logos (splash, favicons), tokens y CSS de Sooniverse. Open WebUI
# sirve cualquier archivo de FRONTEND_BUILD_DIR como estático y los sincroniza
# con STATIC_DIR (/app/backend/open_webui/static).
COPY overlay/static/ /app/build/static/
COPY overlay/static/ /app/backend/open_webui/static/
COPY overlay/static/sooniverse.css /app/build/sooniverse.css
COPY overlay/static/favicon.png /app/build/favicon.png

# Configurar index.html para usar SVG nativos en splash/favicon y activar sooniverse.css
RUN sed -i 's|</head>|    <link rel="stylesheet" href="/sooniverse.css" />\n</head>|g' /app/build/index.html \
 && sed -i 's|/static/splash-dark.png|/static/splash-dark.svg|g' /app/build/index.html \
 && sed -i 's|/static/splash.png|/static/splash.svg|g' /app/build/index.html

# Parches de backend/frontend contra este tag exacto. Si un .patch no aplica
# limpio, el build FALLA a propósito: un bump de tag nunca debe descartar en
# silencio una modificación nuestra (ver patches/README.md).
COPY patches /tmp/sooniverse-patches
RUN set -eu; \
    if ls /tmp/sooniverse-patches/*.patch >/dev/null 2>&1; then \
        for p in /tmp/sooniverse-patches/*.patch; do \
            echo "==> Aplicando $p"; \
            patch -p1 --fuzz=0 --forward < "$p"; \
        done; \
    else \
        echo "==> Sin parches pendientes."; \
    fi; \
    rm -rf /tmp/sooniverse-patches

COPY entrypoint.sh /usr/local/bin/sooniverse-entrypoint.sh
RUN chmod +x /usr/local/bin/sooniverse-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/sooniverse-entrypoint.sh"]
