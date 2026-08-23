# 03. Estado y base de datos

## 1. Modelo de datos

Definido en `database/002_infra_state.sql` (aplicado después de `001_init_schema.sql`, en el mismo esquema `sooniverse`).

**Nota de esquema (encontrada en despliegue de prueba real, no teórica):** `sooniverse` aloja las tablas propias de este proyecto, las de Django y las de Open WebUI — pero **no** las de LiteLLM. LiteLLM Proxy vive en su propio esquema `litellm` (ambos esquemas, misma base de datos física). Motivo: el motor de migraciones de LiteLLM (Prisma) calcula un diff contra TODO lo que encuentra en el esquema activo y puede intentar borrar objetos que no reconoce como suyos; compartiendo `sooniverse` se observó un intento real de `DROP TABLE api_key_registry` (bloqueado solo porque las vistas de uso dependían de ella), que dejaba a LiteLLM sin ninguna de sus tablas creadas y al proxy completamente no funcional. Ver el comentario "CONVIVENCIA CON LITELLM" en `database/001_init_schema.sql` y §7 más abajo.

### `sooniverse.infra_deployment`

| Columna | Tipo | Notas |
|---|---|---|
| `deployment_id` | UUID, único | Identidad del ciclo de vida completo |
| `client_id`, `environment`, `region` | TEXT | Junto forman la clave de unicidad de "activo" |
| `cloud` | TEXT | `'aws'` (único valor hoy) |
| `status` | TEXT | `planning\|creating\|active\|degraded\|destroying\|destroyed\|error` |
| `managed_network` | BOOLEAN | Reservado para distinguir `gestion_red: auto` de `existente` a nivel de fila (hoy informativo) |
| `config_hash` | TEXT | sha256 del contrato completo (`config_hash_of()`, `scripts/generate_infra.py:886`) |
| `config_snapshot` | JSONB | Contrato completo **sin secretos** (ver `_strip_secrets()` más abajo) |
| `created_at`, `updated_at`, `destroyed_at`, `last_error` | — | Auditoría básica |

**Índice único parcial:** `ux_infra_deployment_active (client_id, environment, region) WHERE status NOT IN ('destroyed', 'error')`. Es lo que garantiza que dos "provision" concurrentes del mismo cliente no puedan crear dos VPCs para el mismo (cliente, entorno, región) — el segundo `INSERT` directo violaría la restricción a nivel de PostgreSQL, no solo a nivel de lógica Python (verificado en `tests/test_infra_state.py::test_unique_active_deployment_index_blocks_second_active_row`, que inserta SQL crudo para probarlo, no solo vía la API).

### `sooniverse.infra_resource`

Una fila por recurso AWS creado. `UNIQUE (deployment_id, resource_type, aws_id)` — de ahí que `record_resource()` sea un `UPSERT` (`ON CONFLICT ... DO UPDATE`), nunca inserta duplicados.

| Columna | Notas |
|---|---|
| `resource_type`, `component` | `component` es el mismo valor que el tag `sooniverse:component` en AWS |
| `aws_id` | `vpc-...`, `subnet-...`, `sg-...`, etc. |
| `delete_order` | Entero; `resources_in_delete_order()` ordena ascendente — es el orden inverso al de creación (ver `docs/04_DESTRUCCION.md`) |
| `managed_by_us` | `false` en modo `adopt_existing` (VPC manual) — el destroy nunca borra estas filas |
| `state` | `creating\|active\|deleting\|deleted\|orphan\|adopted\|error` |
| `attributes` | JSONB libre (p.ej. el nombre real del SG) |

### `sooniverse.infra_event`

Bitácora de auditoría: una fila por cada operación (`phase`, `action`, `status`, `message`, `duration_ms`). Toda escritura de `infra_resource`/`infra_deployment` en `PostgresInfraStateStore` va acompañada de un evento **en el mismo commit** (misma transacción `with conn:` de psycopg2) — nunca queda un cambio de estado sin su rastro de auditoría.

### `sooniverse.worker_node` (ampliada, no creada por 002)

`002_infra_state.sql` le añade con `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`: `deployment_id`, `subnet_id`, `security_group_id`, `last_health_check`, `health_status`. La tabla en sí (con `cluster_name`, `private_ip`, `port`, `is_healthy`, etc.) la crea `001_init_schema.sql` y la mantiene `scripts/sync_endpoints.py::register_in_db()`.

`004_usage_analytics.sql` la amplía otra vez con la **ficha de hardware y planificador**: `instance_type`, `gpu_count`, `max_num_seqs`, `max_num_batched_tokens`, `max_model_len`. Antes solo se guardaba `accelerator` (la cadena `"L4"`), lo que dejaba al benchmark de capacidad sin poder registrar bajo qué configuración midió — y un techo sin su configuración no se puede comparar entre corridas. Las puebla el mismo `register_in_db()`, desde los campos que `build_endpoints()` ya tenía a mano en el contrato.

`006_workers_y_login.sql` añade `instance_id` (id de instancia EC2, necesario para apagar/arrancar el nodo desde el panel — antes solo se guardaba la IP privada) y `estado_operativo` (`sano|degradado|desincronizado|apagado|reiniciando|desconocido`, con `CHECK`; es lo que pinta la card "Pool vLLM" — ver `django_metrics/metrics/services.py::estado_pool()`, que lo RECALCULA en cada carga cruzando `is_healthy`/`health_status` con la frescura de `last_seen_at` y con `LiteLLMClient().health()`, en vez de confiar ciegamente en lo que dejó la última sincronización). También endurece `health_status` con un `CHECK` real (antes era texto libre). La misma migración crea `sooniverse.worker_action` (auditoría de comprobar salud/reiniciar/apagar/arrancar, mismo papel que `api_key_audit` para las keys).

### `sooniverse.api_key_registry.proposito` (`004_usage_analytics.sql`)

Columna nueva, `VARCHAR(24) NOT NULL DEFAULT 'cliente'` con `CHECK (proposito IN ('cliente','benchmark','sistema'))` e índice parcial `WHERE proposito <> 'cliente'`.

Marca el tráfico que **no** es de negocio para que el panel pueda excluirlo. Hoy lo usa el benchmark de capacidad, cuya key efímera se registra con `proposito='benchmark'`. Se eligió una columna con `CHECK` en vez de filtrar por alias (`LIKE 'sooniverse-benchmark%'`) porque un alias es texto libre que el operador puede reutilizar o cambiar; esto es un contrato que el motor hace cumplir.

**Trampa de Django que hay que respetar al filtrar por esta columna:** `qs.exclude(api_key__proposito="benchmark")` genera un `NOT IN` sobre un `LEFT JOIN` que **también descarta las filas con `api_key_id NULL`** — los eventos cuya key no está registrada, que son legítimos y suelen ser mayoría en un despliegue recién hecho. La forma correcta está centralizada en `django_metrics/metrics/filtros.py::excluir_benchmark()`: `Q(api_key__isnull=True) | ~Q(api_key__proposito="benchmark")`.

### `sooniverse.model_capability` (`003_model_capabilities.sql`)

La verdad OBSERVADA por `scripts/test_model_capabilities.py --write-db` sobre cada modelo público desplegado, separada en tres capas por fila (`UNIQUE (client_id, environment, model_public_name)`):

| Capa | Columnas | Origen |
|---|---|---|
| Declarado | `declared_vision`, `declared_tool_calling`, `tool_call_parser` | `config_global.yaml: workloads[].capacidades` |
| Sondeado | `probed_vision`, `probed_tool_calling`, `probed_json_object`, `probed_streaming` | Petición HTTP mínima real contra el Gateway público. `NULL` = inconcluso (nunca se confunde con `false`) |
| Efectivo | `effective_vision`, `effective_tool_calling`, `effective_json_object` | Columnas `GENERATED ALWAYS AS` — fail-closed: declarado Y sondeo=`TRUE` (`effective_json_object` no tiene declaración en el contrato, así que exige directamente sondeo=`TRUE`) |

Ningún consumidor (`docker_images/openwebui/overlay/sooniverse/bootstrap_models.py`, `scripts/render_litellm_config.py`, `scripts/render_gateway_stack.py`, `scripts/sync_endpoints.py`) debe leer `declared_*`/`probed_*` directamente — siempre las columnas `effective_*`, para que la política fail-closed viva en un solo sitio (el motor de PostgreSQL, no repetida en cada script). Vista de inspección manual: `sooniverse.v_model_capability_effective`. Ver `docs/00_ARQUITECTURA.md` §4.7 para el razonamiento completo y `docs/01_FLUJO_DESPLIEGUE.md` (Fase 6.5) para cuándo se escribe.

### `sooniverse.app_setting` (`004_usage_analytics.sql`)

Tabla clave/valor mínima (`key` PK, `value`, `updated_at`). Hoy guarda una sola entrada: `reporting_timezone`. Ver §8 para el porqué — no es un cajón de sastre de configuración, existe para resolver un bug concreto de corte de buckets.

### `sooniverse.usage_hourly` (`004_usage_analytics.sql`)

Agregación **horaria** cortada en la zona de reporte, `UNIQUE (bucket_ts, api_key_key, model_name)`. La rellena `sooniverse.refresh_usage_hourly(p_since_days, p_timezone, p_retention_days)` desde `token_usage_event`.

| Grupo | Columnas | Notas |
|---|---|---|
| Instante | `bucket_ts` (TIMESTAMPTZ), `tz_name` | `tz_name` es auditoría: con qué zona se cortó esa fila |
| Dimensiones locales | `bucket_local_date`, `bucket_local_hour` (0-23), `bucket_local_isodow` (1=lunes) | Precalculadas para que el mapa de calor agrupe por columnas indexadas y no por `EXTRACT(... AT TIME ZONE ...)` en cada consulta |
| Identidad | `api_key_id`, `api_key_key` (generada), `model_name` | `api_key_key = COALESCE(api_key_id, 0)`; ver §9 |
| Contadores | `request_count`, `error_count`, `cache_hit_count`, `prompt_tokens`, `completion_tokens`, `total_tokens`, `spend_usd` | Recombinables entre buckets |
| Percentiles | `latency_p50_ms`, `latency_p95_ms`, `latency_p99_ms`, `latency_max_ms`, `ttft_p50_ms`, `ttft_p95_ms` | **NO recombinables** — ver §10 |
| Media honesta | `latency_sum_ms`, `latency_count` | Sí recombinables: permiten una media ponderada entre buckets sin volver a los eventos crudos |

Vistas asociadas: `sooniverse.v_usage_heatmap` (rejilla **completa** 7×24 vía `generate_series(1,7) CROSS JOIN generate_series(0,23)` + `LEFT JOIN`; sin densificar, las horas sin tráfico no tendrían fila y tanto el mapa como la detección de ocio mentirían) y `sooniverse.v_usage_ventanas_ociosas` (las celdas de esa rejilla con `request_count = 0`).

Por qué es una tabla aparte y no una granularidad más de `token_usage_rollup`: ver `docs/00_ARQUITECTURA.md` §4.8.

### `sooniverse.capacity_benchmark` (`005_capacity_benchmark.sql`)

Una fila por corrida de `scripts/benchmark_capacity.py` (`run_id` UUID único, que da idempotencia al script). Cuatro bloques:

| Bloque | Columnas | Notas |
|---|---|---|
| Identidad | `client_id`, `environment`, `deployment_id`, `workload_id`, `model_public_name` | `deployment_id` **sin FK a propósito**: la medición debe sobrevivir al destroy del despliegue que la produjo, porque es el histórico con el que se dimensiona el siguiente |
| Configuración medida | `instance_type`, `accelerator`, `gpu_count`, `replicas`, `max_num_seqs`, `max_num_batched_tokens`, `max_model_len`, `gpu_memory_utilization`, `enforce_eager`, `quantization`, `vllm_version`, `lb_strategy` | Snapshot inmutable. **Un techo sin su configuración no es interpretable**: el mismo modelo en la misma GPU con `max_num_seqs=2` y con `16` da números completamente distintos |
| Parámetros del test | `niveles_concurrencia` (INTEGER[]), `prompt_tokens_objetivo`, `max_tokens`, `segundos_por_nivel`, `warmup_segundos`, `streaming`, `origen`, `benchmark_key_alias`, `benchmark_key_hash` | `origen` ∈ `gateway\|operador`; los números de un origen no son comparables con los del otro. Se guarda el **hash** de la key, nunca la key en claro |
| Resultados | `concurrencia_rodilla`, `rpm_sostenido`, `tokens_salida_por_min`, `p50/p95_base_ms`, `ttft_p50/p95_base_ms`, `p95_rodilla_ms`, `itl_medio_rodilla_ms`, `tasa_error_pct`, `usuarios_estimados`, `motivo_parada`, `curva` (JSONB) | `motivo_parada` ∈ `nivel_maximo\|p95_degradado\|errores\|saturacion_throughput\|presupuesto_agotado\|fallo` |

`niveles_concurrencia` es `INTEGER[]` de PostgreSQL, no JSONB — el modelo Django lo mapea con `ArrayField`, no con `JSONField`: psycopg2 ya devuelve una lista de Python y un `JSONField` reventaría al intentar `json.loads()` sobre ella (fallo encontrado contra la BD real, no teórico).

`usuarios_estimados` es `concurrencia_rodilla × notas.factor_usuarios_por_slot`. El factor se guarda dentro de `notas` (JSONB) precisamente para que el número sea auditable y no un multiplicador mágico enterrado en el código.

Vista: `sooniverse.v_capacity_benchmark_latest` (`DISTINCT ON (client_id, environment, workload_id)`), que es lo que consume el panel para el techo vigente.

### Vistas

- `sooniverse.v_infra_deployment_summary`: un renglón por despliegue con conteo de recursos y coste estimado por hora (solo NAT+EIP; no incluye cómputo ni tráfico). La usa `scripts/list_deployments.py`.
- `sooniverse.v_infra_orphans`: recursos de despliegues ya `destroyed`/`error` que no quedaron marcados `deleted` — candidatos a revisar con `destroy_infra.py --scan-orphans`.

## 2. Ciclo de vida de `status`

```
planning ──▶ creating ──▶ active ──▶ degraded
                              │           │
                              ▼           ▼
                          destroying ──▶ destroyed
                              │
                              ▼
                            error
```

- `creating`: entre `open_deployment()` y el final exitoso de `provision()`.
- `active`: todos los recursos registrados están en estado `active`/`creating` (ver el chequeo final de `deploy()`).
- `degraded`: algún recurso no quedó `active` pero el despliegue sigue existiendo (no es un fallo duro, pero merece atención).
- `destroying` → `destroyed`: fijado por `destroy_infra.py` (`AwsNetworkManager.destroy()`).
- `error`: `last_error` tiene el motivo; el índice único de "activo" **excluye** `error`, así que un despliegue en `error` no bloquea un nuevo intento para el mismo (cliente, entorno, región) — es intencional: un fallo a medio camino no debe dejar al operador atascado.

## 3. `PostgresInfraStateStore` — filtrado de secretos

`_strip_secrets()` (`scripts/infra_state.py:264`) elimina recursivamente cualquier clave cuyo nombre (en minúsculas) contenga alguno de estos marcadores: `password`, `secret`, `master_key`, `salt_key`, `access_key`, `master-key`. Se aplica **antes** de cualquier `INSERT`/`UPDATE` de `config_snapshot` — nunca hay una ventana donde un secreto llegue a tocar disco en PostgreSQL, ni siquiera transitoriamente.

Verificado en `tests/test_infra_state.py::test_config_snapshot_strips_secrets` (contra la BD real, no un mock).

## 4. Espejo local (`InMemoryInfraStateStore` vs `PostgresInfraStateStore`)

- `InMemoryInfraStateStore` (`scripts/infra_state.py:143`): sin persistencia, usada por los tests con `moto` y por quien quiera correr `AwsNetworkManager` sin PostgreSQL disponible. **Nunca usar en producción** — un crash del proceso pierde todo el estado.
- `PostgresInfraStateStore` (línea 279): la real. Además de PostgreSQL, escribe `.sooniverse_state.<cliente>-<entorno>.json` (best-effort, nunca lanza si falla) tras cada cambio — sirve para inspección manual si la BD se cae a medio despliegue, pero **la fuente de verdad sigue siendo PostgreSQL**; ese JSON nunca se lee de vuelta por ningún script.

## 5. Reconstrucción del estado si la BD se corrompe/pierde

No hay un comando automático para esto (deliberado: reconstruir el estado de "qué recurso pertenece a qué deployment_id" a partir únicamente de tags de AWS es exactamente lo que hace `destroy_infra.py --scan-orphans`, que asume que la BD SÍ tiene el resto de los despliegues). Pasos manuales si la tabla `infra_deployment`/`infra_resource` se pierde por completo:

1. `aws ec2 describe-vpcs --filters Name=tag:sooniverse:managed,Values=true` (y equivalente para subnets/nat-gateways/security-groups/addresses) para inventariar qué existe realmente en la cuenta.
2. Para cada VPC encontrada, leer sus tags (`sooniverse:client-id`, `sooniverse:environment`, `sooniverse:deployment-id`) — ahí está toda la identidad que se perdió en la BD.
3. Reinsertar manualmente las filas en `infra_deployment` (`status: active`, mismo `deployment_id` que el tag) y en `infra_resource` (un `INSERT` por recurso, con el `aws_id` y `component` leídos de los tags).
4. A partir de ahí, `destroy_infra.py`/`generate_infra.py --run` vuelven a funcionar normalmente sobre ese despliegue.

Es deliberadamente un procedimiento manual y no un script: reconstruir estado desde tags es una operación de "último recurso" que merece que un humano revise cada fila antes de confiar en ella.

## 6. `plan_changes()` — reconciliación de cambios en caliente

`scripts/generate_infra.py:942`. Compara el `config_snapshot` del despliegue activo contra el contrato que se va a aplicar y clasifica cada diferencia:

| Clasificación | Cuándo | Qué implica |
|---|---|---|
| `no-op` | Sin diferencias relevantes | Nada que hacer |
| `in-place` | `cidr_permitido_gateway`, `cidr_admin_ssh`, `load_balancing_strategy`, o campos de workload que no afectan al hardware (`nombre_publico`, `peso_balanceo`, `asignacion_fraccional`) | Re-sincroniza SG (diff) o re-renderiza `litellm_config.yaml` + reload; no relanza clústeres |
| `recreate-cluster` | `replicas`, `accelerator`, `cantidad_gpus`, `tipo_instancia`, `puerto`, `hf_repo`, `modelo`, o un workload añadido/eliminado | Hay que relanzar (`sky launch`/`sky down`) solo ese clúster worker |
| `requires-destroy` | `vpc_cidr`, `azs`, `nat_gateway.modo` | No aplicable en caliente: `_open_state_store()` lanza `RequiresDestroyError` y aborta antes de tocar AWS |

Ver `docs/01_FLUJO_DESPLIEGUE.md` (Fase 1, "state") para dónde se invoca, y `tests/test_plan_changes.py` para el catálogo completo de casos.

## 7. Tablas de Open WebUI en el mismo esquema `sooniverse`

Desde esta iteración, Open WebUI (`docker_images/openwebui/`) persiste en PostgreSQL vía `DATABASE_URL`/`DATABASE_SCHEMA=sooniverse` en vez de SQLite (ver `docs/00_ARQUITECTURA.md` §4.7 y el servicio `open-webui` en `docker_images/gateway/docker-compose.yml`). Sus migraciones (Alembic, gestionadas por su propio código, nunca por `db_setup.py`) crean dentro de `sooniverse` sus propias tablas: `model`, `user`, `chat`, `config`, `tag`, `file`, `folder`, `group`, `function`, `tool`, `prompt`, `memory`, `channel`, `message`, `feedback`, `knowledge`, `note`, `alembic_version`.

Verificado que ninguna colisiona con los objetos de `database/*.sql` (`api_key_registry`, `token_usage_*`, `api_key_audit`, `worker_node`, `infra_*`, `model_capability`) ni con las tablas internas de Django (`auth_*`, `django_*`, con `search_path=sooniverse`, `sooniverse_panel/settings.py`). Es la razón por la que la tabla de este proyecto se llama `model_capability` y no `model`: ese nombre ya lo usa Open WebUI.

`db_setup.py` **nunca** toca las tablas de Open WebUI (no están en `database/*.sql`, así que quedan fuera de `apply_schema_dir()`/`EXPECTED_TABLES`) — su ciclo de vida de esquema es responsabilidad exclusiva del propio contenedor `open-webui` al arrancar.

### 7.1 Login único y API keys unificadas (`006_workers_y_login.sql`)

Desde esta iteración, Django es la única fuente de login del clúster (panel + chat, SSO por cabecera de confianza — ver `docker_images/openwebui/README.md`). `auth_user` de Django (schema `sooniverse`, gestionada por sus propias migraciones) es la identidad real; la tabla `user` de Open WebUI sigue existiendo (Alembic la sigue creando y usando internamente) pero queda como **espejo derivado**: se auto-aprovisiona vía `WEBUI_AUTH_TRUSTED_EMAIL_HEADER` la primera vez que un usuario de Django entra al chat, nunca al revés. No hay FK entre ambas tablas — el enlace es el **email**, no un id compartido (los ids de Open WebUI son `String` uuid; los de `auth_user`, enteros).

Las API keys pasan por el mismo tratamiento: `api_key_registry.origen` (`litellm` | `openwebui`) distingue el inventario real y gestionable (LiteLLM, sin cambios de comportamiento) del espejo de **solo lectura** de la tabla `sooniverse.api_key` de Open WebUI (creada por Alembic, guarda la clave **en claro** — confirmado contra el código fuente del tag fijado en el `Dockerfile`, `backend/open_webui/models/users.py::ApiKey`). `sooniverse.ingest_openwebui_apikeys()` (invocada en cada `refrescar_metricas()`) ingesta solo un hash SHA-256 + los últimos 4 caracteres, nunca la clave — y es la única de las dos ingestas de API keys que **borra** filas del registro (si el usuario borra su key en Open WebUI, desaparece también de `api_key_registry`; las de LiteLLM nunca se borran, solo se desactivan). El panel rechaza en el servidor cualquier intento de desactivar/reactivar una key con `origen='openwebui'` (`ApiKeyRegistry.gestionable`, `services.ApiKeyNoGestionableError`), no solo en la plantilla.

**LiteLLM es la excepción deliberada: NO comparte `sooniverse`.** Vive en su propio esquema `litellm` (`database/001_init_schema.sql`, `CREATE SCHEMA IF NOT EXISTS litellm;`, y `DATABASE_URL=...?schema=litellm` en `docker_images/gateway/docker-compose.yml`, generado por `scripts/render_gateway_stack.py`). A diferencia de Alembic (Open WebUI) y de las migraciones nativas de Django, el motor de migraciones de LiteLLM (Prisma) calcula un diff contra **todo** lo que encuentra en el esquema activo del `search_path`, no solo contra sus propias tablas — compartiendo `sooniverse` se reprodujo en un despliegue real un intento de `DROP TABLE api_key_registry` (bloqueado por PostgreSQL solo porque `v_usage_daily`/`v_usage_weekly`/`v_usage_monthly`/`v_apikey_summary` dependían de ella), que dejaba a LiteLLM sin ninguna tabla propia (`LiteLLM_SpendLogs`, `LiteLLM_VerificationToken`, etc.) y al proxy completo respondiendo con errores en cada petición. La función `sooniverse.ingest_litellm_spendlogs()` sigue leyendo esas tablas, ahora con el esquema cualificado explícitamente (`FROM litellm."LiteLLM_SpendLogs"`) — una lectura entre esquemas de la misma base de datos, nunca escribe ahí.

## 8. Los seis huecos de datos que cerró `004_usage_analytics.sql`

El panel podía decir *cuánto* se había consumido, pero no *cuándo* ni *si quedaba margen*. El obstáculo no era la interfaz: eran seis huecos en los propios datos. Se documentan aquí con el antes/después porque varios eran invisibles (una columna que existe y siempre vale lo mismo no parece rota).

| # | Hueco | Antes | Después |
|---|---|---|---|
| 1 | El ETL no leía `endTime` ni `completionStartTime` | `token_usage_event.latency_ms` **NULL siempre**. No había ni una sola latencia medida en todo el sistema, y `token_usage_rollup.avg_latency_ms` era NULL en consecuencia | `latency_ms` = `endTime - startTime`, y columna nueva `ttft_ms` = `completionStartTime - startTime` |
| 2 | El ETL no leía `status` | `token_usage_event.status` se quedaba en su `DEFAULT 'success'` para todo, así que `error_count` era **0 siempre** y la tarjeta "Tasa de error" del panel era decorativa | `status` real; cualquier valor distinto de `'success'` cuenta como error en los rollups |
| 3 | El ETL no leía `api_base` | `worker_endpoint` **NULL siempre**: imposible atribuir carga a un worker concreto | Se extrae el `host:puerto` de `api_base` con `regexp_replace('^[a-z]+://([^/]+).*$', '\1')` |
| 4 | La agregación mínima era diaria | "¿A qué hora del sábado está parada la máquina?" era literalmente incontestable | Tabla `usage_hourly` (ver arriba y `docs/00_ARQUITECTURA.md` §4.8) |
| 5 | `DATE_TRUNC` usaba la zona horaria **de la sesión** | Django fija la conexión en UTC (`USE_TZ=True`) mientras el panel renderiza en `America/Bogota`: 5 h de desfase entre el bucket y el día que veía el usuario. Peor: el corte cambiaba según quién disparara el ETL (Django, `db_setup.py` o un `psql` de cron) | Ver §9 |
| 6 | El grupo `api_key_id IS NULL` duplicaba filas en cada refresco | Ver §9 | Ver §9 |

Columnas nuevas de `token_usage_event`, todas con `ADD COLUMN IF NOT EXISTS`: `ttft_ms`, `model_group`, `model_id`, `call_type`, `cache_hit`. Cada una se justifica por sí sola:

- **`ttft_ms`** — sin él no se distingue "el modelo tarda en arrancar" de "el modelo genera lento", que es justo la distinción que necesita cualquier diagnóstico de saturación.
- **`model_group`** / **`model_id`** — el nombre público con el que enrutó LiteLLM y el deployment concreto del pool. Más estables que la IP privada del worker, que cambia entre despliegues.
- **`cache_hit`** — un acierto de caché con `latency_ms=3` hunde el p95 y hace creer que la infraestructura es más rápida de lo que es. `latency_percentiles()` lo excluye por defecto.
- **`call_type`** — permite dejar fuera de los percentiles de chat las llamadas de embeddings o los health checks.

La política de privacidad se mantiene intacta: ninguna columna nueva almacena prompts, mensajes ni respuestas.

### Compatibilidad con Prisma: por qué el ETL se construye con SQL dinámico

`litellm."LiteLLM_SpendLogs"` la crea Prisma y su juego de columnas **cambia entre versiones de LiteLLM**. Un `SELECT` estático que nombre `completionStartTime` deja de funcionar entero en cuanto el proxy se actualiza (o se revierte) a una versión que no la tenga. Por eso `sooniverse.ingest_litellm_spendlogs_range()` construye su sentencia en tiempo de ejecución a partir de `information_schema`, con tres helpers:

- `sooniverse._spendlog_has(col)` — ¿existe la columna en esta versión?
- `sooniverse._spendlog_expr(col, fallback)` — `sl."col"` o el literal de reserva.
- `sooniverse._spendlog_ts_expr(col)` — **la trampa importante.** Prisma mapea `DateTime` a `timestamp` **SIN zona horaria**; verificado contra la BD real de este proyecto, donde `startTime`, `endTime` y `completionStartTime` son las tres `timestamp without time zone`. Comparar eso con `NOW()` o guardarlo en un `TIMESTAMPTZ` aplica un cast implícito que usa la zona **de la sesión**: el mismo bug del hueco #5, pero dentro del propio ETL, desplazando `event_ts` según quién lo dispare. Prisma persiste en UTC, así que cuando la columna es naive el helper la ancla ahí con `AT TIME ZONE 'UTC'`.

Otra sorpresa confirmada contra la BD real: **`cache_hit` es de tipo `text`, no boolean**. El ETL normaliza con `lower(...) IN ('true','t','1')` en vez de castear directamente.

Se descartó la alternativa obvia (una vista de compatibilidad dentro del esquema `litellm`): crear objetos propios ahí es exactamente lo que prohíbe el comentario "CONVIVENCIA CON LITELLM" de `001_init_schema.sql`, por el diff agresivo de Prisma descrito en §7.

`sooniverse._delta_ms(desde, hasta)` es defensiva a propósito: devuelve `NULL` si el orden es inconsistente (reloj desincronizado) o si la diferencia supera 2 horas, para que un outlier absurdo no contamine los percentiles.

### `ON CONFLICT DO NOTHING` → `DO UPDATE` acotado

El ETL original usaba `ON CONFLICT (litellm_request_id) DO NOTHING`, así que una fila ya ingerida **nunca se volvía a tocar**. Con las columnas nuevas eso significaba que la latencia, el estado y el worker jamás llegarían a las filas de las últimas 48 h — precisamente las que mira el panel. Ahora hay `DO UPDATE`, con tres salvaguardas:

1. El `SET` solo cubre campos derivados y usa `COALESCE(EXCLUDED.x, e.x)`: un re-ingest **nunca borra** un dato que ya teníamos, solo rellena huecos.
2. Una guarda `WHERE (...) IS DISTINCT FROM (...)`: en régimen estacionario el `UPDATE` toca **0 filas** y la corrida cuesta lo mismo que el `DO NOTHING` original. Sin ella, cada pasada reescribiría toda la ventana (WAL, tuplas muertas, todos los índices tocados) aunque no cambiara ni un campo.
3. El backfill del histórico completo vive en una función aparte (`sooniverse.backfill_litellm_spendlogs`), por lotes y de invocación manual, no en el camino del refresco periódico.

## 9. Zona horaria de reporte y el bug de duplicados

### `app_setting.reporting_timezone`

`DATE_TRUNC` sobre un `TIMESTAMPTZ` usa el parámetro `TimeZone` **de la sesión que ejecuta la función**. Como Django con `USE_TZ=True` fija la conexión en UTC y el panel renderiza en `settings.TIME_ZONE` (`America/Bogota`), había 5 h de desfase entre el día del bucket y el día que veía el usuario. Y el corte no era ni siquiera estable: el mismo día se recalculaba con fronteras distintas según lo disparara Django, `db_setup.py` o un `psql` de cron.

`refresh_usage_rollups(p_since_days, p_timezone)` y `refresh_usage_hourly(...)` reciben ahora la zona **explícita** y truncan sobre `event_ts AT TIME ZONE v_tz` (un timestamp naive en hora local, cuyo `::DATE` ya no depende de la sesión). Pero un parámetro no basta como única defensa —el fallo que se arregla es exactamente "alguien llamó a la función sin pasar la zona"—, así que el DEFAULT dentro del motor también tiene que ser correcto: de ahí `sooniverse.reporting_timezone(p_override)`, que cae a `app_setting.reporting_timezone` y, en último término, a `'UTC'`.

`scripts/db_setup.py::sync_reporting_timezone()` reconcilia ese valor desde `.env:TIME_ZONE` en cada despliegue y **avisa si cambió**, recordando que los buckets históricos siguen cortados con la zona anterior y que hay que realinearlos con `--recompute-rollups 3650`. El contenedor del panel recibe la misma variable (`TIME_ZONE` en el servicio `metrics` de `docker_images/gateway/docker-compose.yml`): si el panel renderizara en una zona y la agregación se hubiera hecho en otra, volveríamos al bug original.

Detalle que se corrigió a la vez: el límite inferior de `refresh_usage_rollups` es ahora el **inicio del día local** hace N días. Con un `NOW() - N days` a secas, el bucket más antiguo del rango se recalculaba a partir de un día *parcial* de eventos y se guardaba con un total menor que el real.

**Nota sobre DST:** `ts AT TIME ZONE tz` → truncar → `AT TIME ZONE tz` es ambiguo en la hora repetida del cambio de horario. `America/Bogota` no tiene DST, así que hoy es inocuo; en una zona que sí lo tenga habrá una hora al año con dos buckets colapsados en uno. Está documentado en el comentario de `refresh_usage_hourly`. La alternativa (guardar en UTC y desplazar en el panel) incumpliría el requisito de que el corte coincida con lo que ve el usuario.

### El bug de duplicados con `api_key_id NULL`

`token_usage_rollup` tenía `CONSTRAINT rollup_unique_bucket UNIQUE (granularity, bucket_start, api_key_id, model_name)`. En PostgreSQL, **dos filas con `NULL` en una columna del `UNIQUE` no colisionan**. Los eventos cuya key no está en `api_key_registry` se agrupan bajo `api_key_id IS NULL`, así que para ese grupo el `ON CONFLICT` nunca disparaba y `refresh_usage_rollups` **insertaba una fila nueva en cada refresco** — con el job periódico corriendo cada 5 minutos por defecto.

El arreglo es una columna generada con centinela:

```sql
ALTER TABLE sooniverse.token_usage_rollup
    ADD COLUMN IF NOT EXISTS api_key_key BIGINT
        GENERATED ALWAYS AS (COALESCE(api_key_id, 0)) STORED;
```

seguida de la purga de los duplicados ya acumulados (conservando el cálculo más reciente por grano), el `DROP CONSTRAINT` del `UNIQUE` antiguo y un `CREATE UNIQUE INDEX ux_rollup_bucket (granularity, bucket_start, api_key_key, model_name)`. El orden importa: la purga tiene que ir **antes** de crear el índice, o su creación falla. `usage_hourly` nace ya con la misma columna generada.

Se eligió el centinela `0` en vez de `UNIQUE NULLS NOT DISTINCT` (más limpio) porque este último exige PostgreSQL 15 y la versión mínima no está fijada en el contrato del proyecto.

**Trampa de aridad al redefinir la función:** añadir `p_timezone TEXT DEFAULT NULL` a `refresh_usage_rollups(INTEGER)` **no reemplaza** la función, crea una **sobrecarga**. La llamada existente `SELECT sooniverse.refresh_usage_rollups(90)` pasaría a resolver contra dos candidatas y PostgreSQL respondería `function ... is not unique`, rompiendo `db_setup.refresh_metrics` y `metrics/services.refrescar_metricas`. Por eso `004_usage_analytics.sql` abre con un `DROP FUNCTION IF EXISTS sooniverse.refresh_usage_rollups(INTEGER);` **antes** del `CREATE OR REPLACE`. Por el mismo motivo, `ingest_litellm_spendlogs` **conserva** su firma `(INTEGER) RETURNS INTEGER`: ambos consumidores hacen `cur.fetchone()[0]` sobre un escalar. La lógica nueva vive en `ingest_litellm_spendlogs_range(p_from, p_to)`, que devuelve `TABLE(inserted, updated)`, y la función vieja pasó a ser un envoltorio que preserva el contrato. `tests/test_sql_schema.py` verifica ambas cosas por posición en el texto del archivo.

## 10. Los percentiles no se recombinan

Es la regla más fácil de romper de todo el esquema, así que se repite aquí: `usage_hourly.latency_p95_ms` es el p95 **de esa hora concreta**. Promediar o maximizar los p95 de trece lunes **no da** el p95 del lunes; un percentil no es una media y no hay operación aritmética que lo reconstruya desde sus partes.

Para cualquier percentil sobre una ventana mayor de una hora, la única vía soportada es:

```sql
SELECT * FROM sooniverse.latency_percentiles(
    p_from, p_to, p_api_key_ids := NULL, p_models := NULL, p_incluir_cache := FALSE
);
```

que vuelve a `token_usage_event` y devuelve `(muestras, p50_ms, p95_ms, p99_ms, ttft_p50_ms, ttft_p95_ms)`. Excluye los aciertos de caché por defecto, por el motivo del §8.

En el panel esto se traduce en una decisión concreta: el mapa de calor en modo "peticiones"/"tokens" agrega con el ORM sobre `usage_hourly` (sumas, recombinables), pero en modo "latencia p95" cae a SQL crudo con `percentile_cont` sobre los eventos —es la consulta más cara del panel, y por eso vive en un endpoint aparte y tiene un tope duro de 90 días (`filtros.P95_MAX_DIAS`)—.

## 11. Retención

`refresh_usage_hourly(..., p_retention_days)` (default 400) borra los buckets horarios más viejos que ese umbral al final de cada corrida. Es la única tabla de este esquema con purga automática: `usage_hourly` crece con 24 filas por día y combinación (api_key, modelo), mientras que `token_usage_rollup` crece con una por día y `capacity_benchmark` con una por despliegue.
