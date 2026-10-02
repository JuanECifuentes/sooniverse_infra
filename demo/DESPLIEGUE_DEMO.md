# Despliegue del Demo — Gateway Sooniverse contra DeepInfra en un VPS compartido

Guía para levantar **solo la parte del Gateway** (interfaz de chat + panel de métricas + LiteLLM)
conectada a un modelo pequeño de **DeepInfra** (u otro proveedor OpenAI-compatible), en un
servidor **Hostinger que ya aloja otros proyectos**, con todo el tráfico pasando por **tu
dominio** a través de **LiteLLM**.

No usa SkyPilot, AWS, workers GPU ni `config_global.yaml`. Todo vive en `demo/`, y el
resto del repositorio se reutiliza sin modificarlo (imágenes de Open WebUI y del panel,
esquema de BD y renderers de nginx/LiteLLM).

---

## 1. Arquitectura

```
Usuario / cliente API
        │  https://demo.tudominio.com
        ▼
┌──────────────────────── VPS Hostinger ───────────────────────────────┐
│  Nginx del HOST (ya existente, :80/:443, TLS con certbot)            │
│     ├── otros-proyectos.com  → (sin cambios)                         │
│     └── demo.tudominio.com   → 127.0.0.1:8088                        │
│                                   │                                  │
│  ┌──── stack docker "sooniverse-demo" (red aislada demo_net) ─────┐  │
│  │  proxy (nginx)  :80 → publicado SOLO en 127.0.0.1:8088         │  │
│  │    ├── /        → open-webui  (chat; login único del panel)    │  │
│  │    ├── /panel/  → metrics     (Django: consumo, API keys)      │  │
│  │    └── /v1/...  → litellm ─────────────────────────────────────┼──┼──► api.deepinfra.com
│  │  postgres (esquemas sooniverse + litellm) · redis              │  │
│  └────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
```

- La **API key de DeepInfra** solo la ve el contenedor `litellm`. Los usuarios del chat y
  los clientes de `/v1` nunca la ven.
- El chat usa una **key virtual dedicada**, y el panel muestra su consumo como **"Interfaz Web"**.
- Los clientes externos usan **keys virtuales emitidas desde el panel**, cada una con
  presupuesto y límites propios. Su consumo aparece en el panel con el alias de cada key.
- No se publica ningún puerto nuevo hacia Internet; el demo solo escucha en `127.0.0.1:8088`.

## 2. Requisitos

| Recurso | Mínimo recomendado |
|---|---|
| RAM libre | ~3 GB (Open WebUI ≈ 1–2 GB; los límites `mem_limit` del compose topan el total en ≈ 4.3 GB) |
| Disco libre | ~10 GB (imágenes + volúmenes) |
| Software | Docker ≥ 24 con `docker compose` v2, Nginx en el host, certbot (`python3-certbot-nginx`), git |
| Acceso | SSH con `sudo` |
| DNS | Poder crear un registro **A** para el subdominio |
| Proveedor | Una API key de DeepInfra (https://deepinfra.com/dash/api_keys) |

Comprobaciones rápidas en el VPS:

```bash
docker compose version          # v2.x
nginx -v && systemctl is-active nginx
free -h && df -h /
ss -ltnp | grep -E ':(80|443|8088)\b'   # 80/443 = Nginx del host; 8088 debe estar LIBRE
```

Si el 8088 está ocupado, elige otro puerto libre para `DEMO_HTTP_PORT`.

## 3. DNS

En el panel DNS de tu dominio (Hostinger u otro proveedor), crea:

| Tipo | Nombre | Valor | TTL |
|---|---|---|---|
| A | `demo` | IP pública del VPS | 300 |

Verifica con `dig +short demo.tudominio.com`: debe devolver la IP del VPS **antes** de correr
certbot en el paso 6.

## 4. Código en el servidor

```bash
sudo mkdir -p /opt/sooniverse-demo && sudo chown "$USER": /opt/sooniverse-demo
git clone <URL-del-repo> /opt/sooniverse-demo
cd /opt/sooniverse-demo
```

El repo completo es necesario: el demo construye las imágenes desde `docker_images/openwebui/`
y `django_metrics/`, y aplica el esquema desde `database/`.

## 5. Configuración y despliegue del stack

```bash
cp demo/.env.demo.example demo/.env.demo
chmod 600 demo/.env.demo
nano demo/.env.demo
```

Rellena como mínimo:

| Variable | Qué poner |
|---|---|
| `DEMO_DOMAIN` | `demo.tudominio.com` |
| `DEMO_HTTP_PORT` | Puerto local libre (por defecto `8088`) |
| `DEEPINFRA_API_KEY` | Tu key de DeepInfra |
| `DEMO_MODEL_ID` | Id del modelo en https://deepinfra.com/models (p.ej. `meta-llama/Meta-Llama-3.1-8B-Instruct`) |
| `DEMO_MODEL_NAME` | Nombre público que verán el chat y la API (por defecto `sooniverse-demo`) |
| `LITELLM_MASTER_KEY` | `sk-` + `openssl rand -hex 32` |
| `LITELLM_SALT_KEY`, `DB_PASSWORD`, `SECRET_KEY`, `DJANGO_SUPERUSER_PASSWORD`, `OPENWEBUI_BOOTSTRAP_PASSWORD` | `openssl rand -hex 32` cada uno |
| `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `GATEWAY_PUBLIC_URL`, `PUBLIC_BASE_URL`, `CHAT_URL`, `SOONIVERSE_PANEL_URL` | Reemplaza `demo.tudominio.com` por tu subdominio |

> Sin comillas en los valores. `demo/.env.demo` está en `demo/.gitignore`; no lo subas al repo.

Despliega:

```bash
./demo/deploy_demo.sh
```

El script es idempotente; puedes repetirlo para actualizar. Hace lo siguiente:

1. **Preflight**: Docker/compose, variables obligatorias, puerto libre y que no sea 80/443.
2. **Render** de `demo/generated/`:
   - `litellm_config.yaml`: un único modelo enrutado a DeepInfra.
   - `nginx.conf`: el nginx interno, con las mismas rutas y SSO que producción.
   - `nginx-host-<dominio>.conf`: el server block para el Nginx del host.
3. Arranca **PostgreSQL + Redis** y aplica el esquema (`scripts/db_setup.py`, todos los `database/*.sql`).
4. Arranca **LiteLLM** y espera a que esté sano.
5. Crea la **key virtual del chat** (`scripts/ensure_openwebui_key.py`) y la guarda en
   `.env.demo` como `OPENWEBUI_LITELLM_API_KEY`.
6. Arranca **Open WebUI**, el **panel** y el **nginx interno**, y registra el modelo en el chat
   (`openwebui-bootstrap`).

La primera vez tarda varios minutos, porque construye las imágenes de Open WebUI y del panel.

## 6. Conectar el Nginx del host (TLS)

```bash
D=demo.tudominio.com
sudo cp demo/generated/nginx-host-$D.conf /etc/nginx/sites-available/$D.conf
sudo ln -s /etc/nginx/sites-available/$D.conf /etc/nginx/sites-enabled/$D.conf
sudo nginx -t && sudo systemctl reload nginx

# Certificado Let's Encrypt. certbot añade el bloque 443 y la redirección 80→443
sudo certbot --nginx -d $D --redirect
```

- El server block solo responde a `Host: demo.tudominio.com`, así que **los demás sitios del VPS no cambian**.
- Si tu Nginx usa `conf.d/` en vez de `sites-available/`, copia el archivo a `/etc/nginx/conf.d/$D.conf`.
- La renovación del certificado ya la gestiona el timer de certbot del host (`systemctl list-timers | grep certbot`).

## 7. Verificación

1. **Salud**: `curl -s https://demo.tudominio.com/healthz` → `ok`.
2. **Panel**: abre `https://demo.tudominio.com/panel/` y entra con `DJANGO_SUPERUSER_USERNAME` /
   `DJANGO_SUPERUSER_PASSWORD`.
3. **Chat**: abre `https://demo.tudominio.com/` (usa la misma sesión del panel), elige el modelo
   `sooniverse-demo` y envía un mensaje.
4. **Atribución**: espera ~1 minuto (`METRICS_REFRESH_INTERVAL=60`). En el panel, el consumo
   aparece con API Key **"Interfaz Web"**, no "(sin registro)".
5. **API por tu dominio**:
   1. En el panel → *API Keys*, crea una key para el cliente del demo, con presupuesto y límites.
   2. Prueba la llamada:
      ```bash
      curl https://demo.tudominio.com/v1/chat/completions \
        -H "Authorization: Bearer sk-<key-del-panel>" \
        -H "Content-Type: application/json" \
        -d '{"model":"sooniverse-demo","messages":[{"role":"user","content":"Hola"}]}'
      ```
   3. El consumo aparece en el panel bajo el alias de esa key.

   Cualquier SDK de OpenAI funciona con `base_url="https://demo.tudominio.com/v1"`.
6. **Superficie expuesta**: `ss -ltnp | grep 8088` debe mostrar solo `127.0.0.1:8088`.

## 8. Operación

| Acción | Comando |
|---|---|
| Estado | `./demo/deploy_demo.sh status` |
| Logs | `./demo/deploy_demo.sh logs` · `./demo/deploy_demo.sh logs litellm` |
| Cambiar de modelo | Editar `DEMO_MODEL_ID` (y/o `DEMO_MODEL_NAME`) y ejecutar `./demo/deploy_demo.sh` |
| Actualizar código | `git pull && ./demo/deploy_demo.sh` |
| Detener (conserva datos) | `./demo/deploy_demo.sh down` |
| Backup de la BD | `docker exec sooniverse-demo-postgres pg_dump -U sooniverse sooniverse \| gzip > demo-$(date +%F).sql.gz` |

**Otro proveedor OpenAI-compatible** (Together, Groq, OpenRouter, un vLLM propio…):

1. Define `DEMO_API_BASE` (p.ej. `https://api.together.xyz/v1`).
2. Pon la key de ese proveedor en `DEEPINFRA_API_KEY`; actúa como credencial genérica.
3. Ajusta `DEMO_MODEL_ID` y ejecuta `./demo/deploy_demo.sh`.

**Seguridad recomendada para una demo pública**:
- Crea keys de cliente con `max_budget` y RPM/TPM bajos.
- Pon un límite de gasto en la cuenta de DeepInfra.
- Desactiva las keys del demo al terminar (panel → API Keys → Desactivar).

## 9. Solución de problemas

| Síntoma | Causa probable / solución |
|---|---|
| `502 Bad Gateway` en el dominio | El stack no está arriba o el puerto no coincide. Revisa `./demo/deploy_demo.sh status`, `curl http://127.0.0.1:8088/healthz` y que `DEMO_HTTP_PORT` coincida con el `proxy_pass` del server block. |
| `403 CSRF verification failed` en el panel | `CSRF_TRUSTED_ORIGINS` debe ser exactamente `https://<dominio>` y `ALLOWED_HOSTS` debe incluir el dominio. Tras cambiarlos: `./demo/deploy_demo.sh`. |
| Login en bucle en el panel | `HTTPS_ACTIVO=true` sin HTTPS real (cookies `Secure`). Completa certbot, o usa `false` solo para pruebas por `http://`. |
| Consumo del chat como "(sin registro)" | La key del chat no se creó o no se registró. Repite `./demo/deploy_demo.sh`: el paso 4 repara el registro si la key ya existe. Revisa que `.env.demo` tenga `OPENWEBUI_LITELLM_API_KEY`. |
| El modelo no aparece en el chat | El bootstrap falló. Mira `./demo/deploy_demo.sh logs open-webui` y repite `./demo/deploy_demo.sh`. |
| Error 401/404 desde DeepInfra | Revisa `DEEPINFRA_API_KEY` y que `DEMO_MODEL_ID` exista en DeepInfra. Los detalles están en `./demo/deploy_demo.sh logs litellm`. |
| El VPS se queda sin memoria | Baja `mem_limit` de `open-webui` o amplía el plan. Revisa con `docker stats`. |

## 10. Desmontaje completo

Estos pasos no afectan a otros proyectos del VPS.

```bash
cd /opt/sooniverse-demo
docker compose -f demo/docker-compose.demo.yml --env-file demo/.env.demo down -v   # borra también los datos
docker image rm sooniverse-demo/open-webui:0.11.0 sooniverse-demo/metrics-panel:1.0.0 sooniverse-demo/tools:1.0.0
sudo rm /etc/nginx/sites-enabled/demo.tudominio.com.conf /etc/nginx/sites-available/demo.tudominio.com.conf
sudo nginx -t && sudo systemctl reload nginx
sudo certbot delete --cert-name demo.tudominio.com
```

Para terminar, borra el registro DNS `demo`.
