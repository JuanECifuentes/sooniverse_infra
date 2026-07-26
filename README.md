# Sooniverse Infrastructure (`sooniverse_infra`)

Infraestructura automatizada de **Sooniverse** para desplegar cargas de trabajo de IA
(LLMs y embeddings) sobre GPUs en AWS, con un **gateway unificado OpenAI-compatible**,
balanceo de carga, aislamiento de red y contabilidad de tokens por API Key.

Todo se deriva de un **contrato centralizado** (`config_global.yaml`) que un generador
en Python traduce a manifiestos de **SkyPilot** y stacks de **Docker Compose**.

> **Estado actual: Fase 1 completa** — Gateway (LiteLLM + PostgreSQL + Open WebUI +
> panel Django de métricas) y workers vLLM multi-nodo dentro de una VPC.

---

## 1. Arquitectura de la Fase 1

### 1.1 Topología de red

```
                         Internet
                            │
              80 · 4000 · 8000 · 8080
                            │
┌───────────────────────────┼─────────────────────── VPC ──────────────────────────┐
│                           ▼                                                       │
│  SUBRED PÚBLICA                          SUBRED PRIVADA                           │
│  ┌──────────────────────────────┐        ┌──────────────────────────────────────┐ │
│  │  NODO GATEWAY  (1 · t3.large)│        │  WORKERS vLLM  (N · GPU)             │ │
│  │  ── única IP pública ──      │        │  ── sin IP pública ──                │ │
│  │                              │        │                                      │ │
│  │  nginx        :80            │        │  worker-0   10.0.x.a:8007            │ │
│  │  LiteLLM      :4000  ────────┼───────▶│  worker-1   10.0.x.b:8007            │ │
│  │  Open WebUI   :8080          │  HTTP  │  worker-N   10.0.x.n:8007            │ │
│  │  Django       :8000          │ interno│                                      │ │
│  │  Redis        (interno)      │        │  vLLM + Qwen3.5 AWQ 4bit             │ │
│  │                              │◀───────┼── SSH tunelizado (bastion)           │ │
│  └──────────┬───────────────────┘        └──────────────────────────────────────┘ │
└─────────────┼─────────────────────────────────────────────────────────────────────┘
              │
              ▼
      PostgreSQL (RDS o externa)
      ├── public.LiteLLM_*        → Spend/Usage nativo de LiteLLM
      └── sooniverse.*            → API Keys, métricas agregadas, auditoría
```

**Reglas de aislamiento:**

| Componente | IP pública | Puertos expuestos | Acceso SSH |
|---|---|---|---|
| Nodo Gateway | ✅ Sí | 80, 4000, 8000, 8080 | Directo |
| Workers vLLM | ❌ No (`use_internal_ips: true`) | 8007 solo intra-VPC | A través del Gateway (bastion) |

Los workers solo alcanzan Internet vía **NAT Gateway** (necesario para descargar
pesos desde Hugging Face). Sin NAT, usa una AMI que ya contenga el modelo o
precalienta el volumen `hf_cache`.

### 1.2 Flujo de una petición

```
Cliente (SDK OpenAI / Open WebUI)
   │  POST /v1/chat/completions   Authorization: Bearer sk-...
   ▼
LiteLLM Proxy  :4000
   ├─ 1. Valida la API Key contra public."LiteLLM_VerificationToken"
   ├─ 2. Aplica cuotas (rpm_limit / tpm_limit / max_budget)
   ├─ 3. Enruta al worker según `routing_strategy` (latency-based por defecto)
   │      · descarta workers en cooldown (allowed_fails / cooldown_time)
   │      · Redis comparte el estado de latencia entre los 4 workers de LiteLLM
   ▼
Worker vLLM  10.0.x.y:8007/v1        (IP privada, nunca expuesta)
   │  respuesta + usage{prompt_tokens, completion_tokens, total_tokens}
   ▼
LiteLLM escribe en public."LiteLLM_SpendLogs"
   │      (SIN prompts ni respuestas: store_prompts_in_spend_logs=false)
   ▼
sooniverse.ingest_litellm_spendlogs()      → copia SOLO contadores
   ▼
sooniverse.token_usage_event               → grano fino por petición
   ▼
sooniverse.refresh_usage_rollups()         → agrega daily / weekly / monthly
   ▼
Panel Django  :8000/metrics/               → gráficas y tablas filtrables
```

### 1.3 Balanceo de carga

LiteLLM trata cada worker como un *deployment* distinto del mismo `model_name`
lógico, por lo que el balanceo es transparente para el cliente:

```yaml
model_list:
  - model_name: sooniverse-qwen3.5          # ← lo que ve el cliente
    litellm_params: {api_base: http://10.0.1.10:8007/v1, weight: 1}
  - model_name: sooniverse-qwen3.5          # ← mismo nombre = mismo pool
    litellm_params: {api_base: http://10.0.1.11:8007/v1, weight: 1}
router_settings:
  routing_strategy: latency-based-routing
```

Estrategias admitidas en `gateway.load_balancing_strategy`:
`latency-based-routing` (por defecto), `simple-shuffle` (round-robin ponderado),
`least-busy`, `usage-based-routing`, `usage-based-routing-v2`.

### 1.4 Sincronización automática de endpoints

Las IPs privadas no se conocen hasta que AWS aprovisiona las instancias, así que
el generador orquesta este orden:

```
1. sky launch  gateway          → obtiene IP pública, queda como bastion SSH
2. genera .sky_config_workers.yaml  (vpc_name + use_internal_ips + ssh_proxy_command)
3. SKYPILOT_CONFIG=... sky launch  worker-<id>   (N nodos, sin IP pública)
4. sync_endpoints.py --apply
      ├─ descubre las IPs privadas (API de SkyPilot → marcadores en logs → status --ip)
      ├─ registra el inventario en sooniverse.worker_node
      ├─ regenera litellm_config.yaml
      ├─ lo envía al Gateway (sky rsync)
      └─ recarga SOLO el contenedor litellm (sin downtime del resto del stack)
```

Cada worker imprime `SOONIVERSE_WORKER_READY=<workload>|<ip>|<puerto>` al arrancar,
que es el marcador que `sync_endpoints.py` parsea como método de descubrimiento
de respaldo si la API de Python de SkyPilot no está disponible.

---

## 2. Estructura del repositorio

```
sooniverse_infra/
├── config_global.yaml              ← CONTRATO ÚNICO (source of truth)
├── .env                            ← credenciales (gitignored)
├── .env.example                    ← plantilla documentada
│
├── scripts/
│   ├── generate_infra.py           Genera manifiestos + orquesta el despliegue
│   ├── db_setup.py                 Ingesta database/init_schema.sql en PostgreSQL
│   ├── render_litellm_config.py    IPs de workers → config.yaml de LiteLLM
│   └── sync_endpoints.py           Descubre IPs privadas y recarga el balanceador
│
├── database/
│   └── init_schema.sql             Esquema `sooniverse` (idempotente)
│
├── docker_images/
│   ├── gateway/                    Stack del Nodo Gateway
│   │   ├── docker-compose.yml      litellm · open-webui · metrics · redis · nginx · [postgres]
│   │   ├── nginx/default.conf      Reverse proxy del puerto 80
│   │   └── litellm_config.yaml     GENERADO (gitignored)
│   └── qwen3.5/                    Stack del Worker vLLM (GPU)
│
├── django_metrics/                 Panel de Métricas y API Keys
│   ├── sooniverse_panel/           settings · urls · wsgi
│   ├── metrics/                    models(managed=False) · views · services · litellm_client
│   ├── templates/metrics/
│   └── static/css/
│       ├── theme-sooniverse.css    ← SOLO identidad de marca (intercambiable)
│       └── layout.css              ← SOLO maquetación
│
├── README.md                       Este archivo
└── MANUAL_DESPLIEGUE.md            Guía operativa paso a paso
```

---

## 3. El contrato: `config_global.yaml`

```yaml
cliente:
  id: "acme"                        # [String] tenant
  entorno: "prod"                   # [Enum]  prod | dev
  modo: "byoc"                      # [Enum]  byoc | hosted

red_y_aislamiento:
  region: "us-east-1"
  image_id: "ami-0d001f8052688dc45" # AMI de los workers GPU
  vpc_name: null                    # tag Name de la VPC dedicada (null = VPC por defecto)
  workers_en_subred_privada: true   # true = sin IP pública + SSH vía bastion
  cidr_permitido_gateway: "0.0.0.0/0"
  security_group_workers: null      # SG pre-creado para acotar el ingreso al 8007
  tags_obligatorios: {...}          # AWS Resource Tags

gateway:
  habilitado: true
  tipo_instancia: "t3.large"        # CPU-only
  puertos_publicos: [80, 4000, 8080, 8000]
  load_balancing_strategy: "latency-based-routing"
  litellm: {num_retries, request_timeout, cooldown_time, allowed_fails}
  open_webui: {habilitado, signup_habilitado}
  django_metrics: {habilitado, puerto, metrics_refresh_interval}

base_de_datos:
  AUTO_INIT_DB: true                # ← interruptor de inicialización automática
  auto_refresh_metrics: true
  schema_file: "database/init_schema.sql"

workloads:
  - id: "qwen3-5-llm"
    hf_repo: "cyankiwi/Qwen3.5-2B-AWQ-4bit"
    tipo_tarea: "llm-texto"         # [Enum] llm-texto | embeddings
    accelerator: "L4"
    cantidad_gpus: 1                # GPUs por nodo
    replicas: 2                     # ← nodos worker balanceados por LiteLLM
    tipo_instancia: "g6.xlarge"
    puerto: 8007
    nombre_publico: "sooniverse-qwen3.5"   # nombre del modelo para el cliente
    peso_balanceo: 1
    asignacion_fraccional: {gpu_memory_utilization: 0.95, max_model_len: 16384}
```

Cada entrada de `workloads` produce **su propio clúster SkyPilot** con
`num_nodes = replicas`, lo que permite mezclar aceleradores distintos por modelo.

---

## 4. Uso rápido

```bash
# 1. Generar los manifiestos (sin tocar AWS)
python scripts/generate_infra.py

# 2. Inicializar el esquema PostgreSQL a mano
python scripts/db_setup.py --refresh

# 3. Desplegar la topología completa
python scripts/generate_infra.py --run

# 4. Re-sincronizar el balanceador tras escalar workers
python scripts/sync_endpoints.py --apply
```

Guía operativa detallada: **[MANUAL_DESPLIEGUE.md](MANUAL_DESPLIEGUE.md)**

---

## 5. Esquema de base de datos

Convive con las tablas nativas de LiteLLM (`public."LiteLLM_*"`, gestionadas por
Prisma) sin alterarlas. Todo lo propio vive en el esquema aislado `sooniverse`:

| Objeto | Propósito |
|---|---|
| `api_key_registry` | Registro administrativo de API Keys (alias, dueño, cuotas, estado). Correlaciona con LiteLLM por `litellm_token_hash`. Nunca guarda la key en claro. |
| `token_usage_event` | Contadores por petición. Idempotente por `litellm_request_id`. |
| `token_usage_rollup` | Agregación pre-calculada con discriminador `granularity` (`daily`/`weekly`/`monthly`). |
| `api_key_audit` | Bitácora inmutable del ciclo de vida de cada key. |
| `worker_node` | Inventario del pool vLLM que sincroniza `sync_endpoints.py`. |
| `ingest_litellm_spendlogs(horas)` | ETL: copia **solo contadores** desde `LiteLLM_SpendLogs`. |
| `refresh_usage_rollups(dias)` | Recalcula las tres granularidades con `UPSERT`. |
| `v_usage_daily` / `_weekly` / `_monthly` / `v_apikey_summary` | Vistas de lectura para el panel. |

### Privacidad por diseño

No se almacena contenido de conversaciones en ningún punto del pipeline:

- `store_prompts_in_spend_logs: false` y `turn_off_message_logging: true` en LiteLLM.
- El ETL solo proyecta `prompt_tokens`, `completion_tokens`, `total_tokens`,
  `spend`, `model`, `request_id` y `startTime`.
- Ninguna tabla de `sooniverse` tiene columnas de texto libre de conversación.
- La API Key en claro se muestra **una única vez** al emitirla; en BD solo queda
  el hash y un prefijo enmascarado para identificarla en la UI.

---

## 6. Panel de Métricas y API Keys (Django)

| Ruta | Función |
|---|---|
| `/metrics/` | Consumo de tokens con particiones **Diaria · Semanal · Mensual** y filtro por API Key, modelo y ventana. |
| `/metrics/serie.json` | La misma serie en JSON para integraciones externas. |
| `/metrics/api-keys/` | Alta, listado y desactivación/reactivación de API Keys con su consumo. |
| `/metrics/api-keys/<id>/` | Detalle: serie propia, cuotas, últimas peticiones y auditoría. |
| `/admin/` | Django admin sobre las mismas tablas. |
| `/healthz/` | Healthcheck del contenedor. |

Todos los modelos son `managed = False`: el DDL es responsabilidad exclusiva de
`database/init_schema.sql`, nunca de las migraciones de Django (que solo crean
`auth`, `sessions`, `admin`).

Comandos de gestión:

```bash
python manage.py sync_metrics --since-hours 168   # ETL + recálculo manual
python manage.py ensure_superuser                 # superusuario idempotente
python manage.py seed_demo --dias 60              # datos sintéticos para validar el panel
python manage.py seed_demo --clean                # revertir los datos demo
```

### Tema intercambiable

La identidad visual (`Manual_de_imagen_sooniverse.md`) está encapsulada en un
único archivo:

- **`static/css/theme-sooniverse.css`** — colores, degradados, sombras, bordes,
  radios, tipografía de marca, estados `:hover`/`:focus` y transiciones.
- **`static/css/layout.css`** — `display`, `grid`, `flex`, dimensiones, `margin`,
  `padding`, `position` y responsive.

El tema **no contiene ninguna regla de maquetación**, así que reemplazar solo
`theme-sooniverse.css` re-tematiza el panel completo sin romper la estructura.
Las gráficas son CSS/SVG puro: cero dependencias de CDN, necesario en una VPC
sin salida a Internet.

---

## 7. Requisitos previos

```bash
# Python 3.11+
python -m venv venv && source venv/bin/activate     # Windows: venv\Scripts\activate
pip install pyyaml psycopg2-binary "skypilot[aws]"
pip install -r django_metrics/requirements.txt      # solo para correr el panel en local

aws configure                                        # credenciales de aprovisionamiento
sky check                                            # valida el acceso de SkyPilot a AWS
cp .env.example .env                                 # y completar credenciales
```

---

## 8. Notas para agentes de IA

1. **No edites artefactos generados**: `.sky_generated.*.yaml`,
   `.sky_config_workers.yaml`, `.sooniverse_endpoints.json` ni
   `docker_images/gateway/litellm_config.yaml`. Modifica `config_global.yaml` y
   regenera con `python scripts/generate_infra.py`.
2. **Reglas de validación** (`ConfigValidator`): `cliente.entorno` ∈ {prod, dev};
   `cliente.modo` ∈ {byoc, hosted}; `workloads[].tipo_tarea` ∈ {llm-texto, embeddings};
   `cantidad_gpus` y `replicas` enteros > 0; `gateway.load_balancing_strategy` de la
   lista permitida; `base_de_datos.AUTO_INIT_DB` booleano obligatorio.
3. **Nunca añadas columnas de prompts o respuestas** a las tablas de `sooniverse`.
   La política de privacidad es un requisito del producto, no una preferencia.
4. **El DDL vive solo en `database/init_schema.sql`.** Los modelos de Django son
   `managed = False`; añadir una migración que cree estas tablas rompe el diseño.
5. **Validación rápida sin aprovisionar nada:**
   ```bash
   python -c "import sys;sys.path.insert(0,'scripts');from generate_infra import load_config;load_config('config_global.yaml')"
   python scripts/db_setup.py --check
   cd django_metrics && python manage.py check
   ```

---

## 9. Persistencia de modelos (`hf_cache`)

Para no re-descargar pesos de varios GB en cada reinicio:

1. SkyPilot monta el workspace local en `~/sooniverse_infra` del host remoto.
2. En el worker, `~/sooniverse_infra/docker_images/qwen3.5/hf_cache` se mapea a
   `/root/.cache/huggingface` dentro del contenedor vLLM.
3. Los pesos persisten en el volumen del host EC2 mientras el clúster exista,
   reduciendo el arranque de minutos a segundos.

---

## 10. Hoja de ruta

| Fase | Alcance | Estado |
|---|---|---|
| **1** | Gateway LiteLLM + PostgreSQL + Open WebUI + panel de métricas y API Keys | ✅ Completa |
| 2 | Aprovisionamiento declarativo, modo Sooniverse-hosted primero | Pendiente |
| 3 | Modo BYOC (IAM AssumeRole + External ID) | Pendiente |
| 4 | Multi-modelo y asignación fraccional de GPU (TEI para embeddings, MIG) | Pendiente |
| 5 | Segundo proveedor de nube (GCP) | Pendiente |
| 6 | Kubernetes (EKS/GKE + GPU Operator + Karpenter + KubeAI) | Pendiente |

Detalle y criterios de decisión: `sooniverse-optimizacion-infraestructura-i_v2.md`.
