# Sooniverse Infrastructure (`sooniverse_infra`)

Infraestructura automatizada de **Sooniverse** para desplegar cargas de trabajo de IA
(LLMs y embeddings) sobre GPUs en AWS, con un **gateway unificado OpenAI-compatible**,
balanceo de carga, aislamiento de red y contabilidad de tokens por API Key.

Todo se deriva de un **contrato centralizado** (`config_global.yaml`) que un generador
en Python traduce a manifiestos de **SkyPilot** y stacks de **Docker Compose**.

> **Estado actual: Fases 1-8 completas** — Gateway (LiteLLM + PostgreSQL + Open WebUI +
> panel Django de métricas), workers vLLM multi-nodo, y la **capa de red completa
> (VPC/subredes/NAT/Security Groups) creada y destruida por este mismo generador vía
> boto3** (`gestion_red: auto`), con estado persistente en PostgreSQL, destrucción
> segura, multi-cliente y nginx como única puerta de entrada. Documentación completa
> en [`docs/`](docs/00_ARQUITECTURA.md).

---

## 1. Arquitectura

`gestion_red: auto` (recomendado, default) crea toda la red desde cero con
`scripts/aws_network.py::AwsNetworkManager`; `gestion_red: existente` conserva el
modo legado (VPC/SGs creados a mano, ver `Manual_VPC_SecurityGroup.md` como anexo
histórico). Ver `docs/00_ARQUITECTURA.md` para el detalle completo y las decisiones
de diseño.

Para desplegar con un dominio propio y HTTPS (certbot/Let's Encrypt) en vez de
la IP efímera por defecto, ver **[`Manual_Dominio_AWS.md`](Manual_Dominio_AWS.md)**
— el único paso manual que exige es crear el registro DNS A antes de desplegar.

Si el usuario con el que despliegas no tiene permisos IAM para que
`--run` cree solo el usuario dedicado a Apagar/Arrancar workers desde el
panel, ver **[`Manual_Usuario_IAM_Workers.md`](Manual_Usuario_IAM_Workers.md)**
para crearlo a mano desde la consola AWS.

### 1.1 Topología de red

```
                                   Internet
                                       │
                              ┌────────┴────────┐
                              │ Internet Gateway │
                              └────────┬────────┘
┌──────────────────────────────────────┼──────────────────────── VPC ───────────────┐
│                                       ▼                                            │
│  SUBRED PÚBLICA                                    SUBRED PRIVADA                  │
│  ┌───────────────────────────────┐        ┌──────────────────────────────────────┐ │
│  │  NODO GATEWAY (1 · t4g.large)  │        │  WORKERS vLLM (N · GPU)              │ │
│  │  ── única IP pública ──       │        │  ── SIN IP pública ──                │ │
│  │                                │        │                                      │ │
│  │  nginx   :80 (+443 si TLS)    │        │  worker-0   10.0.x.a:8007            │ │
│  │   ├─ /        → Open WebUI    │        │  worker-1   10.0.x.b:8007            │ │
│  │   ├─ /v1/     → LiteLLM ──────┼───────▶│  worker-N   10.0.x.n:8007            │ │
│  │   ├─ /panel/  → Django        │  SG->SG│                                      │ │
│  │   └─ /healthz → 200 fijo      │        │  vLLM + modelo(s) del contrato       │ │
│  │  (4000/8080/8000 NO publicados│◀───────┼── SSH tunelizado (bastion)           │ │
│  │   al host por defecto)        │        └──────────────┬───────────────────────┘ │
│  └──────────┬─────────────────────┘                       │ salida a Internet      │
└─────────────┼──────────────────────────────────────────────┼────────────────────────┘
              │                                        ┌──────┴──────┐
              ▼                                        │ NAT Gateway  │
      PostgreSQL (RDS o externa)                        └─────────────┘
      └── sooniverse.*            → API Keys, métricas, tablas de LiteLLM y ESTADO DE INFRAESTRUCTURA
                                     (infra_deployment / infra_resource / infra_event)
```

**Reglas de aislamiento:**

| Componente | IP pública | Puertos expuestos | Acceso SSH |
|---|---|---|---|
| Nodo Gateway | ✅ Sí | 80 (+443 si TLS); 4000/8080/8000 solo si `exponer_puertos_directos: true` | Directo |
| Workers vLLM | ❌ No (`use_internal_ips: true`) | 8007 solo intra-VPC, solo desde el SG del gateway (SG→SG) | A través del Gateway (bastion) |

Los workers solo alcanzan Internet vía **NAT Gateway** (necesario para descargar
pesos desde Hugging Face; `nat_gateway.modo: none` solo es válido con
`vpc_endpoints.s3: true`, y aun así limita la salida a S3). Ver `docs/02_RED_AWS.md`
para el detalle de cada recurso y su coste.

### 1.2 Flujo de una petición

```
Cliente (SDK OpenAI / Open WebUI)
   │  POST /v1/chat/completions   Authorization: Bearer sk-...
   ▼
LiteLLM Proxy  :4000
   ├─ 1. Valida la API Key contra sooniverse."LiteLLM_VerificationToken"
   ├─ 2. Aplica cuotas (rpm_limit / tpm_limit / max_budget)
   ├─ 3. Enruta al worker según `routing_strategy` (latency-based por defecto)
   │      · descarta workers en cooldown (allowed_fails / cooldown_time)
   │      · Redis comparte el estado de latencia entre los 4 workers de LiteLLM
   ▼
Worker vLLM  10.0.x.y:8007/v1        (IP privada, nunca expuesta)
   │  respuesta + usage{prompt_tokens, completion_tokens, total_tokens}
   ▼
LiteLLM escribe en sooniverse."LiteLLM_SpendLogs"
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
├── clients/_ejemplo/                Plantilla para dar de alta un cliente nuevo
├── .env                            ← credenciales (gitignored)
├── .env.example                    ← plantilla documentada
│
├── scripts/
│   ├── generate_infra.py           Valida el contrato, genera manifiestos, orquesta --run
│   ├── aws_network.py              VPC/subredes/NAT/IGW/route tables/SGs vía boto3
│   ├── infra_state.py              Estado persistente (PostgresInfraStateStore)
│   ├── destroy_infra.py            Destrucción en orden inverso + --scan-orphans
│   ├── verify_deployment.py        11 comprobaciones post-despliegue
│   ├── list_deployments.py         Inventario de todos los clientes/entornos
│   ├── render_gateway_stack.py     nginx/default.conf + docker-compose.yml generados
│   ├── db_setup.py                 Ingesta database/*.sql (orden lexicográfico) en PostgreSQL
│   ├── render_litellm_config.py    IPs de workers → config.yaml de LiteLLM
│   └── sync_endpoints.py           Descubre IPs privadas y recarga el balanceador
│
├── database/
│   ├── 001_init_schema.sql         Esquema `sooniverse`: API Keys, métricas, auditoría
│   └── 002_infra_state.sql         infra_deployment / infra_resource / infra_event
│
├── docker_images/
│   ├── gateway/                    Stack del Nodo Gateway (GENERADO por render_gateway_stack.py)
│   │   ├── docker-compose.yml      litellm · open-webui · metrics · redis · nginx · [postgres]
│   │   ├── nginx/default.conf      nginx: única puerta de entrada pública
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
├── docs/                           Documentación completa (arquitectura, flujo,
│                                    red AWS, estado/BD, destrucción, multi-cliente,
│                                    runbook, referencia CLI, notas para agentes IA)
├── tests/                          pytest: moto (sin AWS real) + PostgreSQL real (skip si no hay)
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
  aws_profile: null                 # perfil de ~/.aws/credentials por cliente (opcional)
  image_id: "ami-0d001f8052688dc45" # AMI de los workers GPU
  gestion_red: "auto"                # auto (crea/destruye la VPC) | existente (legado, manual)
  vpc_cidr: "10.0.0.0/16"
  azs: 1
  nat_gateway: {modo: "single", timeout_segundos: 300}   # single | per-az | none
  vpc_endpoints: {s3: true, ecr: false}
  workers_en_subred_privada: true   # true = sin IP pública + SSH vía bastion
  cidr_permitido_gateway: "0.0.0.0/0"
  cidr_admin_ssh: "0.0.0.0/0"        # restringir en producción real
  security_group_workers: null      # null en modo 'auto': lo crea AwsNetworkManager
  security_group_gateway: null
  tags_obligatorios: {...}          # AWS Resource Tags

gateway:
  habilitado: true
  tipo_instancia: "t4g.large"        # CPU-only
  exponer_puertos_directos: false   # false (recomendado): nginx es la única puerta pública
  puertos_publicos: [4000, 8080, 8000]   # solo aplican si exponer_puertos_directos: true
  load_balancing_strategy: "latency-based-routing"
  tls: {habilitado: false, modo: "self-signed", dominio: null}
  litellm: {num_retries, request_timeout, cooldown_time, allowed_fails}
  open_webui: {habilitado, signup_habilitado}
  django_metrics: {habilitado, puerto, metrics_refresh_interval}

base_de_datos:
  AUTO_INIT_DB: true                # ← interruptor de inicialización automática
  auto_refresh_metrics: true
  schema_dir: "database"            # aplica TODOS los .sql en orden lexicográfico

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

# 2. Ver el plan de red sin crear nada (requiere PostgreSQL alcanzable, no muta nada)
python scripts/generate_infra.py --run --dry-run

# 3. Desplegar la topología completa (VPC + gateway + workers + endpoints + verify)
python scripts/generate_infra.py --run

# 4. Re-sincronizar el balanceador tras escalar workers
python scripts/sync_endpoints.py --apply

# 5. Verificar el despliegue
python scripts/verify_deployment.py

# 6. Inventario de todos los clientes
python scripts/list_deployments.py

# 7. Destruir todo (VPC incluida) cuando ya no se necesite
python scripts/destroy_infra.py --dry-run
python scripts/destroy_infra.py --yes
```

Guía operativa detallada: **[MANUAL_DESPLIEGUE.md](MANUAL_DESPLIEGUE.md)** · Documentación completa: **[docs/](docs/00_ARQUITECTURA.md)**

---

## 5. Esquema de base de datos

Todas las tablas de LiteLLM (`sooniverse."LiteLLM_*"`, gestionadas por Prisma), las tablas de Django y los objetos propios viven de forma consolidada en el esquema `sooniverse`:

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

Django es la **única fuente de login del clúster** (panel + chat). Un usuario staff entra en
`/metrics/login/` y accede al panel; cualquier usuario activo (staff o no) que abra el chat pasa
transparentemente por esa misma sesión (SSO por cabecera de confianza vía nginx `auth_request`,
ver `docker_images/openwebui/README.md`) — Open WebUI nunca muestra su propio formulario.

| Ruta | Función |
|---|---|
| `/metrics/login/` | Login único (usuario o correo). |
| `/metrics/` | Consumo de tokens con particiones **Diaria · Semanal · Mensual** y filtro por API Key, modelo y ventana. |
| `/metrics/serie.json` | La misma serie en JSON para integraciones externas. |
| `/metrics/workers/<id>/<accion>/` | Acciones sobre un worker (comprobar salud, reiniciar vLLM, apagar/arrancar) desde la card "Pool vLLM". |
| `/metrics/api-keys/` | Alta, listado y desactivación/reactivación de API Keys de LiteLLM, más el espejo de solo lectura de las de Open WebUI. |
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
pip install -r requirements-dev.txt                 # pytest + moto, solo para correr tests/

aws configure                                        # credenciales de aprovisionamiento
sky check                                            # valida el acceso de SkyPilot a AWS
cp .env.example .env                                 # y completar credenciales
```

---

## 8. Notas para agentes de IA

Ver **[`docs/08_AGENTES_IA.md`](docs/08_AGENTES_IA.md)** para la lista completa
(qué archivos nunca editar a mano porque se regeneran, invariantes de diseño,
cómo validar un cambio sin aprovisionar nada real, y un glosario del dominio).
Resumen mínimo:

1. **No edites artefactos generados**: `.sky_generated.*.yaml`,
   `.sky_config_*.yaml`, `.sooniverse_endpoints.json`,
   `docker_images/gateway/litellm_config.yaml`, `docker_images/gateway/nginx/default.conf`
   ni `docker_images/gateway/docker-compose.yml`. Modifica `config_global.yaml` y
   regenera con `python scripts/generate_infra.py`.
2. **Nunca añadas columnas de prompts o respuestas** a las tablas de `sooniverse`.
   La política de privacidad es un requisito del producto, no una preferencia.
3. **El DDL vive solo en `database/*.sql`.** Los modelos de Django son
   `managed = False`; añadir una migración que cree estas tablas rompe el diseño.
4. **Nada de Terraform/CloudFormation/CDK/Pulumi.** Todo el ciclo de vida de AWS
   es `boto3` puro en `scripts/aws_network.py`.
5. **Validación rápida sin aprovisionar nada:**
   ```bash
   python -c "import sys;sys.path.insert(0,'scripts');from generate_infra import load_config;load_config('config_global.yaml')"
   python -m pytest tests/test_aws_network.py tests/test_plan_changes.py tests/test_config_validator.py -q
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
| 1 | Gateway LiteLLM + PostgreSQL + Open WebUI + panel de métricas y API Keys | ✅ Completa |
| 2 | Red AWS autogestionada por boto3 (`AwsNetworkManager`): VPC/subredes/NAT/IGW/SGs | ✅ Completa |
| 3 | Estado persistente en PostgreSQL (mecanismo de propiedad, `plan_changes`) | ✅ Completa |
| 4 | Orquestación del ciclo de vida completo (`--run`, `destroy_infra.py`, `verify_deployment.py`) | ✅ Completa |
| 5 | Comunicación Gateway↔Workers robusta (bastion, 4º método de descubrimiento, health checks) | ✅ Completa |
| 6 | nginx como única puerta de entrada + TLS self-signed | ✅ Completa |
| 7 | Multi-cliente (`clients/<id>/`, aislamiento de CIDR/artefactos/credenciales) | ✅ Completa |
| 8 | Pruebas (moto + PostgreSQL real + smoke de nginx) y documentación completa (`docs/`) | ✅ Completa |
| 9 | Modo BYOC real (IAM AssumeRole + External ID) — hook documentado, no implementado | Pendiente |
| 10 | Segundo proveedor de nube (GCP) | Pendiente |
| 11 | Kubernetes (EKS/GKE + GPU Operator + Karpenter + KubeAI) | Pendiente |
| 12 | TLS `letsencrypt`/`acm` (hoy solo `self-signed`) | Pendiente |

Detalle y criterios de decisión: `sooniverse-optimizacion-infraestructura-i_v2.md`
y `docs/00_ARQUITECTURA.md`.
