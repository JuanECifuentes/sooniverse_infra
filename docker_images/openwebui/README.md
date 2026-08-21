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
│   ├── static/                    assets visuales (splash.png, logo.png, favicons, sooniverse.css)
│   └── sooniverse/
│       ├── __init__.py
│       └── bootstrap_models.py    upsert de modelos + capabilities vía el ORM de Open WebUI
├── patches/                       ver patches/README.md
└── README.md                      este archivo
```

## Gestión de Logos y Branding (Personalización)

### Empaquetado por defecto en despliegues
- **Logo de Inicio (Splash Screen)**: Usa `images/animado.svg` empacado como `splash.png`/`splash-dark.png`/`splash.svg` en `overlay/static/`.
- **Favicons y Resto de Logos**: Usa `images/logo_sin_fondo.svg` empacado como `logo.png`, `logo.svg`, `favicon.png`, `favicon.ico`, `apple-touch-icon.png` y manifest de la PWA.

### Cómo personalizar los logos post-despliegue (Cliente final)
Para cambiar la marca en un servidor desplegado en vivo **sin necesidad de recompilar la imagen Docker**:

1. **Vía volumen persistente (`webui_data`)**:
   Coloca tus imágenes personalizadas en la carpeta `/app/backend/data/branding/` del contenedor (que vive en el volumen persistente `webui_data`). Al reiniciar el servicio (`docker compose restart open-webui`), `entrypoint.sh` sobrescribirá automáticamente la carpeta de estáticos con tus imágenes.
2. **Vía Panel de Administración Web**:
   Un administrador puede ir a **Admin Panel > Settings > Interface** en Open WebUI y actualizar las URLs del logo o favicons directamente desde la interfaz de usuario.

## Por qué imagen derivada y no fork del código fuente

Se evaluaron ambas opciones. Un fork completo (clonar `open-webui/open-webui` y compilar desde
fuente) permitiría tocar el frontend Svelte a fondo, pero cuesta un build de 10-20 min en la instancia
`t4g.large` del gateway en cada despliegue y ~200MB de código de terceros versionado en este repo. La
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
