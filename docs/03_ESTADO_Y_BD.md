# 03. Estado y base de datos

## 1. Modelo de datos

Definido en `database/002_infra_state.sql` (aplicado después de `001_init_schema.sql`, en el mismo esquema `sooniverse`).

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
