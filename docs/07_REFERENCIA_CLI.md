# 07. Referencia de CLI

Todos los scripts viven en `scripts/` y se ejecutan con `python scripts/<script>.py`. Todos aceptan `--config <ruta>` (default: `config_global.yaml` en la raíz del repo) salvo `list_deployments.py`, que es intencionalmente global (todos los clientes a la vez).

## `scripts/generate_infra.py`

Genera los manifiestos de la topología y, opcionalmente, la aprovisiona.

| Flag | Default | Descripción |
|---|---|---|
| `--config` | `config_global.yaml` | Ruta al contrato |
| `--out-dir` | (calculado) | Directorio de manifiestos; ver `docs/05_MULTICLIENTE.md` |
| `--run` | off | Aprovisiona en AWS tras generar los manifiestos |
| `--only` | `all` | `all\|network\|gateway\|workers\|endpoints\|verify` |
| `--dry-run` | off | Con `--run`: solo imprime el plan, ninguna llamada mutante a AWS/PostgreSQL |
| `--init-db` | off | Corre `db_setup.py --refresh` localmente, ignorando `AUTO_INIT_DB` |
| `--no-auto-init-db` | off | Fuerza `AUTO_INIT_DB=false` para esta corrida sin editar el YAML |

**Ejemplo — generar manifiestos sin desplegar:**

```bash
$ python scripts/generate_infra.py
[SOONIVERSE INFRA] Leyendo contrato central...
[OK] nginx     -> docker_images\gateway\nginx\default.conf
[OK] compose   -> docker_images\gateway\docker-compose.yml
[INFO] exponer_puertos_directos=false -> 4000/8000/8080 solo accesibles dentro de la red Docker; nginx (80/443) es la única puerta pública.
[OK] Gateway     -> .sky_generated.gateway.yaml  (cluster: sooniverse-acme-prod-gw)
[OK] Worker 'qwen3-5-llm' -> .sky_generated.worker-qwen3-5-llm.yaml  (cluster: sooniverse-acme-prod-qwen3-5-llm, nodos: 1)
[OK] SkyPilot cfg -> .sky_config_workers.yaml  (VPC / IPs internas / bastion)
[INFO] AUTO_INIT_DB = true (la BD se inicializa en el despliegue)

[INFO] Para aprovisionar la topología en AWS:
       python scripts/generate_infra.py --run
       python scripts/generate_infra.py --run --dry-run          # plan, sin tocar AWS
       python scripts/generate_infra.py --run --only network     # solo la capa de red
       python scripts/generate_infra.py --run --only gateway     # solo el gateway
```

**Ejemplo — plan de red sin desplegar (primera vez, sin despliegue previo):**

```bash
$ python scripts/generate_infra.py --run --only network --dry-run
[ESTADO] (dry-run, solo lectura) deployment_id=(ninguno todavía)
--- [RED] Red AWS (VPC/subredes/NAT/Security Groups) ---
[RED] --dry-run: no existe un despliegue previo para acme/prod/us-east-1. Se crearía una VPC, subredes, NAT, route tables y Security Groups nuevos.
```

**Ejemplo — despliegue completo real:**

```bash
$ python scripts/generate_infra.py --run
...
[ESTADO] deployment_id=3f2a1c9e-... (1.4s)
--- [RED] Red AWS (VPC/subredes/NAT/Security Groups) ---
[RED] VPC=vpc-0abc... (sooniverse-acme-prod-vpc) SG-gateway=sg-0def... SG-workers=sg-0ghi... (94.2s)
--- [GATEWAY] Nodo Gateway (público) ---
[EXEC] sky launch -y -c sooniverse-acme-prod-gw .sky_generated.gateway.yaml
[INFO] IP pública del Gateway: 34.201.x.x
--- [WORKERS] Workers vLLM (subred privada) ---
> Workload 'qwen3-5-llm' (1 nodo/s)
[EXEC] sky launch -y -c sooniverse-acme-prod-qwen3-5-llm .sky_generated.worker-qwen3-5-llm.yaml
--- [ENDPOINTS] Sincronización de endpoints en LiteLLM ---
--- [VERIFY] Verificación de despliegue ---
==========================================================================
 Chat (Open WebUI) : http://34.201.x.x/
 API (LiteLLM)     : http://34.201.x.x/v1
 Panel (Django)    : http://34.201.x.x/panel/
 Salud (nginx)     : http://34.201.x.x/healthz
 deployment_id: 3f2a1c9e-...
==========================================================================
```

## `scripts/destroy_infra.py`

| Flag | Descripción |
|---|---|
| `--config` | Contrato del cliente a destruir |
| `--dry-run` | Imprime el plan, ninguna llamada mutante |
| `--yes` | Confirma sin preguntar (si se omite, pide escribir el `cliente.id`) |
| `--only network` | Salta `sky down` (asume que ya se hizo) |
| `--force` | Ignora `managed_by_us=False` — solo depuración |
| `--scan-orphans` | Barrido de recursos huérfanos en la región (no requiere `--config` apuntar a un cliente en particular, pero sí para saber la región) |
| `--purge-orphans` | Junto con `--scan-orphans --yes`: los borra |

**Ejemplo — dry-run:**

```bash
$ python scripts/destroy_infra.py --dry-run
==========================================================================
 DESTRUCCIÓN: acme/prod (us-east-1)
==========================================================================
--- (dry-run) Se ejecutaría 'sky down' de workers y gateway ---
       sky down -y sooniverse-acme-prod-qwen3-5-llm
       sky down -y sooniverse-acme-prod-gw
--- [3/3] Capa de red AWS ---
  [ 10] sg-workers     sg-0ghi...  managed_by_us=True
  [ 11] sg-gateway     sg-0def...  managed_by_us=True
  [ 30] nat            nat-0jkl... managed_by_us=True
  [ 40] eip            eipalloc-... managed_by_us=True
  [ 51] rtb-public     rtb-0mno... managed_by_us=True
  [ 60] igw            igw-0pqr... managed_by_us=True
  [ 71] subnet-public  subnet-0stu... managed_by_us=True
  [ 80] vpc            vpc-0abc... managed_by_us=True
```

**Ejemplo — huérfanos:**

```bash
$ python scripts/destroy_infra.py --scan-orphans
[OK] No se encontraron recursos huérfanos.
```

## `scripts/verify_deployment.py`

Sin flags más allá de `--config`. Código de salida `0` si no hay fallos críticos (los `N/A` por falta de precondición no cuentan como fallo).

```bash
$ python scripts/verify_deployment.py
COMPROBACIÓN                                            ESTADO  DETALLE
----------------------------------------------------------------------------------------------------
Subred privada rutea a NAT                              [OK]   1 route table(s) verificadas
Subred pública rutea a IGW                               [OK]   1 route table(s) verificadas
Los workers no tienen IP pública                         [OK]   1 instancia(s) verificadas sin IP pública
SG de workers no acepta 0.0.0.0/0 en el puerto vLLM      [OK]   1 puerto(s) vLLM revisados, solo SG->SG
...
La BD registra los workers (sooniverse.worker_node)      [OK]   1 worker(s) registrados y sanos

11/11 comprobaciones OK/N-A (0 fallo(s) crítico(s))
```

## `scripts/sync_endpoints.py`

| Flag | Descripción |
|---|---|
| `--apply` | Render + push al Gateway + reload de LiteLLM (sin esto, dry-run: solo muestra el pool) |
| `--endpoints-file <json>` | Pool manual, salta el descubrimiento vía SkyPilot |
| `--skip-db` | No registrar el inventario en PostgreSQL |
| `--skip-push` | Solo render local, sin tocar el Gateway |
| `--watch` | Reconciliación periódica (implica `--apply`); Ctrl+C para detener |
| `--interval N` | Segundos entre corridas con `--watch` (default 60) |

**Ejemplo — dry-run:**

```bash
$ python scripts/sync_endpoints.py
[SYNC] Descubriendo el pool de workers vLLM...
   [sooniverse-acme-prod-qwen3-5-llm] 1 IP(s) privada(s) vía describe-instances: 10.0.128.12

[SYNC] Pool resultante (1 deployment/s):
   - sooniverse-qwen3.5           http://10.0.128.12:8007/v1  (peso 1, workload qwen3-5-llm, sano)

[INFO] Dry-run. Añade --apply para escribir el config y recargar LiteLLM.
```

**Ejemplo — modo watch en el Gateway (systemd, ver comentario en el código):**

```bash
python scripts/sync_endpoints.py --watch --interval 60
```

## `scripts/list_deployments.py`

Sin `--config` (global, todos los clientes). `--json` para scripting.

```bash
$ python scripts/list_deployments.py
CLIENTE          ENTORNO  REGIÓN       ESTADO       RECURSOS   NAT  EIP  USD/h    EDAD
------------------------------------------------------------------------------------------
acme             prod     us-east-1    active       8/8        1    1    0.0500   72.3h
------------------------------------------------------------------------------------------
Coste estimado acumulado (despliegues no destruidos): ~$0.0500/hora (~$36.00/mes) -- solo NAT+EIP, no incluye cómputo ni tráfico.
```

## `scripts/db_setup.py`

| Flag | Descripción |
|---|---|
| `--env-file` | Default `.env` |
| `--sql-dir` | Directorio con `.sql` a aplicar en orden lexicográfico (default `database/`) |
| `--sql <archivo>` | Un solo archivo (comportamiento legado; ignora `--sql-dir`) |
| `--check` | Solo verifica conexión y estado del esquema |
| `--refresh` | Tras aplicar, corre el ETL de LiteLLM + rollups |

```bash
$ python scripts/db_setup.py
[SOONIVERSE DB] Objetivo: postgres@<host>:5432/sooniverse
[OK] Esquema aplicado desde 001_init_schema.sql
[OK] Esquema aplicado desde 002_infra_state.sql
[OK] 2 archivo(s) aplicado(s): 001_init_schema.sql, 002_infra_state.sql
   [OK  ] sooniverse.api_key_registry
   ...
[SUCCESS] Base de datos lista para la Fase 1 (Gateway + Métricas).
```

## `scripts/render_gateway_stack.py`

Normalmente se invoca automáticamente (`generate_manifests()`), pero puede correrse suelto para inspeccionar el resultado sin generar manifiestos SkyPilot:

```bash
python scripts/render_gateway_stack.py --config config_global.yaml
```
