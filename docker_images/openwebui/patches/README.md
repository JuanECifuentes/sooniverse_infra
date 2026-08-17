# Parches de Open WebUI

Mecanismo de última instancia para cambiar comportamiento de backend o frontend que **no** se puede
lograr con variables de entorno, `params`/`meta` del modelo en BD (ver
`docker_images/openwebui/overlay/sooniverse/bootstrap_models.py`), o el overlay de CSS
(`overlay/static/sooniverse.css`). Antes de escribir un patch, agota esas tres vías — un patch es
código de terceros modificado a mano y hay que rehacerlo en cada bump de tag.

## Cómo funciona

`docker_images/openwebui/Dockerfile` aplica, en orden alfabético, todo `*.patch` de este directorio con:

```bash
patch -p1 --fuzz=0 --forward < archivo.patch
```

**Si un patch no aplica limpio, el build falla.** Es intencional: un bump del tag base
(`ghcr.io/open-webui/open-webui:vX.Y.Z` en el `Dockerfile`) nunca debe descartar en silencio una
modificación nuestra — el build roto es la señal de que hay que revisar el patch a mano contra el
código nuevo.

## Cómo generar un patch

Contra un checkout local del tag exacto que usa el `Dockerfile`:

```bash
git clone --branch v0.11.0 --depth 1 https://github.com/open-webui/open-webui /tmp/owui-ref
cd /tmp/owui-ref
# ... edita el/los archivo(s) ...
git diff > /ruta/al/repo/docker_images/openwebui/patches/010-descripcion-corta.patch
```

Convención de nombre: `NNN-descripcion-corta.patch` (prefijo numérico de 3 dígitos para fijar el
orden de aplicación cuando un patch depende de otro).

## Rutas relevantes dentro de la imagen

- Backend sin compilar: `/app/backend/open_webui/` (Python, editable directo con un `.patch`).
- Frontend ya compilado: `/app/build/` (HTML/JS/CSS de producción — parchear aquí es frágil porque
  los nombres de archivo llevan hash de contenido; si el cambio es de frontend, prioriza parchear
  `index.html` si el nombre no cambia entre builds, o documenta explícitamente qué build hash asume
  el patch).

## Parches actuales

Ninguno todavía. Este directorio se creó vacío a propósito (ver plan de la iteración "Open WebUI
propio: Postgres + capacidades reales"): solo se añade un patch si la depuración en vivo contra la
infraestructura real demuestra que hace falta uno (por ejemplo, un parámetro que Open WebUI manda
siempre al LLM y que no se puede suprimir de otra forma).
