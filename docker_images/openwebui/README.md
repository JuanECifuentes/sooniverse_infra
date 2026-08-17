# Open WebUI vendorizado (Sooniverse)

Imagen derivada de `ghcr.io/open-webui/open-webui`, construida in situ en el Nodo Gateway (igual que
`django_metrics`, sin push a ningún registry). Motivo de vendorizarla en vez de usar la imagen oficial
tal cual: persistencia en PostgreSQL (esquema `sooniverse`, compartido con LiteLLM y Django) en vez de
SQLite efímero, y un punto de extensión propio para ajustar qué capacidades del modelo expone la
interfaz (ver `overlay/sooniverse/bootstrap_models.py` y `sooniverse.model_capability` en
`database/003_model_capabilities.sql`).

## Estructura

```
docker_images/openwebui/
├── Dockerfile              FROM ghcr.io/open-webui/open-webui:v0.11.0 (tag FIJO)
├── entrypoint.sh           espera Postgres -> exec del start.sh original de la imagen
├── overlay/
│   ├── static/sooniverse.css      tokens del Manual de imagen (overlay visual, ver skill)
│   └── sooniverse/
│       ├── __init__.py
│       └── bootstrap_models.py    upsert de modelos + capabilities vía el ORM de Open WebUI
├── patches/                       ver patches/README.md
└── README.md                      este archivo
```

## Por qué imagen derivada y no fork del código fuente

Se evaluaron ambas opciones. Un fork completo (clonar `open-webui/open-webui` y compilar desde
fuente) permitiría tocar el frontend Svelte a fondo, pero cuesta un build de 10-20 min en la instancia
`t3.large` del gateway en cada despliegue y ~200MB de código de terceros versionado en este repo. La
imagen derivada + overlay + patches cubre el caso de uso actual (backend: bootstrap de modelos vía su
propio ORM; frontend: overlay de CSS + patches puntuales) con un build de segundos. Si en el futuro se
necesita tocar el frontend a fondo (más allá de lo que CSS/patches puntuales permiten), migrar a build
desde fuente reutiliza esta misma carpeta: cambiar el `FROM` por un `FROM node:... AS frontend-build` +
`FROM python:...` multi-stage, manteniendo `overlay/` y `patches/` intactos.

## Actualizar el tag base

1. Revisar el changelog de la nueva versión de Open WebUI contra `patches/*.patch` (¿sigue aplicando
   cada uno?) y contra `overlay/sooniverse/bootstrap_models.py` (¿cambió el esquema de
   `open_webui.models.models`?).
2. Cambiar el `FROM` en `Dockerfile`.
3. `sudo docker compose --env-file ../../.env build open-webui` localmente o en el gateway; si un
   patch no aplica, el build falla con el mensaje de `patch` — corregirlo ahí, no con `--fuzz` alto.
4. Repetir el Paso 8 del plan de despliegue (verificación end-to-end) antes de dar la actualización
   por buena.

## Qué NO persiste en PostgreSQL

El volumen `webui_data:/app/backend/data` se mantiene: ficheros subidos por los usuarios y el vector
store local (Chroma) de RAG siguen en disco, atados a la instancia del gateway. Solo lo relacional
(usuarios, chats, modelos, configuración) vive en `sooniverse.*`. Ver `docs/04_DESTRUCCION.md`.
