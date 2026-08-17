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

### `sooniverse.model_capability` (`003_model_capabilities.sql`)

La verdad OBSERVADA por `scripts/test_model_capabilities.py --write-db` sobre cada modelo público desplegado, separada en tres capas por fila (`UNIQUE (client_id, environment, model_public_name)`):

| Capa | Columnas | Origen |
|---|---|---|
| Declarado | `declared_vision`, `declared_tool_calling`, `tool_call_parser` | `config_global.yaml: workloads[].capacidades` |
| Sondeado | `probed_vision`, `probed_tool_calling`, `probed_json_object`, `probed_streaming` | Petición HTTP mínima real contra el Gateway público. `NULL` = inconcluso (nunca se confunde con `false`) |
| Efectivo | `effective_vision`, `effective_tool_calling`, `effective_json_object` | Columnas `GENERATED ALWAYS AS` — fail-closed: declarado Y sondeo=`TRUE` (`effective_json_object` no tiene declaración en el contrato, así que exige directamente sondeo=`TRUE`) |

Ningún consumidor (`docker_images/openwebui/overlay/sooniverse/bootstrap_models.py`, `scripts/render_litellm_config.py`, `scripts/render_gateway_stack.py`, `scripts/sync_endpoints.py`) debe leer `declared_*`/`probed_*` directamente — siempre las columnas `effective_*`, para que la política fail-closed viva en un solo sitio (el motor de PostgreSQL, no repetida en cada script). Vista de inspección manual: `sooniverse.v_model_capability_effective`. Ver `docs/00_ARQUITECTURA.md` §4.7 para el razonamiento completo y `docs/01_FLUJO_DESPLIEGUE.md` (Fase 6.5) para cuándo se escribe.

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

**LiteLLM es la excepción deliberada: NO comparte `sooniverse`.** Vive en su propio esquema `litellm` (`database/001_init_schema.sql`, `CREATE SCHEMA IF NOT EXISTS litellm;`, y `DATABASE_URL=...?schema=litellm` en `docker_images/gateway/docker-compose.yml`, generado por `scripts/render_gateway_stack.py`). A diferencia de Alembic (Open WebUI) y de las migraciones nativas de Django, el motor de migraciones de LiteLLM (Prisma) calcula un diff contra **todo** lo que encuentra en el esquema activo del `search_path`, no solo contra sus propias tablas — compartiendo `sooniverse` se reprodujo en un despliegue real un intento de `DROP TABLE api_key_registry` (bloqueado por PostgreSQL solo porque `v_usage_daily`/`v_usage_weekly`/`v_usage_monthly`/`v_apikey_summary` dependían de ella), que dejaba a LiteLLM sin ninguna tabla propia (`LiteLLM_SpendLogs`, `LiteLLM_VerificationToken`, etc.) y al proxy completo respondiendo con errores en cada petición. La función `sooniverse.ingest_litellm_spendlogs()` sigue leyendo esas tablas, ahora con el esquema cualificado explícitamente (`FROM litellm."LiteLLM_SpendLogs"`) — una lectura entre esquemas de la misma base de datos, nunca escribe ahí.
