# 07. Referencia de CLI

Todos los scripts viven en `scripts/` y se ejecutan con `python scripts/<script>.py`. Todos aceptan `--config <ruta>` (default: `config_global.yaml` en la raíz del repo) salvo `list_deployments.py`, que es intencionalmente global (todos los clientes a la vez).

## `scripts/generate_infra.py`

Genera los manifiestos de la topología y, opcionalmente, la aprovisiona.

| Flag | Default | Descripción |
|---|---|---|
| `--config` | `config_global.yaml` | Ruta al contrato |
| `--out-dir` | (calculado) | Directorio de manifiestos; ver `docs/05_MULTICLIENTE.md` |
| `--run` | off | Aprovisiona en AWS tras generar los manifiestos |
| `--only` | `all` | `all\|network\|gateway\|dominio\|workers\|endpoints\|capabilities\|capacidad\|verify` (los `choices` se derivan de `PHASE_ORDER`, así que no pueden desincronizarse de las fases reales) |
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

**Ejemplo — re-medir la capacidad de un despliegue ya en marcha, sin tocar nada más:**

```bash
$ python scripts/generate_infra.py --run --only capacidad
--- [CAPACIDAD] Benchmark de capacidad (rampa acotada) ---
[EXEC] .../python scripts/benchmark_capacity.py --config config_global.yaml --write-db --json .sooniverse_capacity.json
```

Funciona en frío (sin relanzar el Gateway): el script se empuja por `scp` antes de ejecutarse. Con `capacidad.habilitado: false` en el contrato, la fase imprime `[SKIP]` y no gasta GPU.

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
| `--refresh` | Tras aplicar, corre el ETL de LiteLLM + rollups + agregación horaria |
| `--recompute-rollups [DIAS]` | **Mantenimiento.** Recalcula rollups y agregación horaria de los últimos `DIAS` días (default 3650) con la zona de reporte actual |
| `--backfill [DIAS]` | **Mantenimiento.** Reingesta por lotes el histórico de `LiteLLM_SpendLogs` para rellenar latencia/TTFT/estado/worker en filas ya ingeridas |

Los dos flags de mantenimiento operan sobre un esquema **ya aplicado** (no lo reaplican) y salen sin continuar con el resto del flujo. Si se pasan ambos, el backfill corre primero: rellenar los eventos antes de reagregarlos es el único orden que tiene sentido.

```bash
$ python scripts/db_setup.py
[SOONIVERSE DB] Objetivo: postgres@<host>:5432/sooniverse | zona de reporte: America/Bogota
[OK] Esquema aplicado desde 001_init_schema.sql
[OK] Esquema aplicado desde 002_infra_state.sql
[OK] Esquema aplicado desde 003_model_capabilities.sql
[OK] Esquema aplicado desde 004_usage_analytics.sql
[OK] Esquema aplicado desde 005_capacity_benchmark.sql
[OK] 5 archivo(s) aplicado(s): 001_init_schema.sql, ..., 005_capacity_benchmark.sql
[WARNING] La zona de reporte cambió (UTC -> America/Bogota). Los buckets históricos siguen cortados con la anterior; realinéalos con:
          python scripts/db_setup.py --recompute-rollups 3650
   [OK  ] sooniverse.api_key_registry
   ...
[SUCCESS] Base de datos lista para la Fase 1 (Gateway + Métricas).
```

**Cuándo usar `--recompute-rollups`.** Dos situaciones, ambas puntuales:

1. **Una vez tras instalar `004_usage_analytics.sql`.** Los buckets anteriores se cortaron con la zona horaria de la sesión (habitualmente UTC), no con la del panel: los días de la frontera están desplazados hasta 5 h.
2. **Cada vez que cambie `TIME_ZONE` en el `.env`.** `db_setup.py` detecta el cambio y lo avisa con el comando exacto, pero no lo ejecuta solo: reagregar todo el histórico es una operación cara que merece una decisión explícita.

```bash
$ python scripts/db_setup.py --recompute-rollups 3650
[OK] Recalculado con zona 'America/Bogota': 103 filas de rollup | 596 buckets horarios
```

**Cuándo usar `--backfill`.** También una sola vez tras instalar `004`, y solo si interesa el histórico: el ETL antiguo nunca escribió `latency_ms`, `ttft_ms`, `status` ni `worker_endpoint` (ver `docs/03_ESTADO_Y_BD.md` §8), así que las filas viejas los tienen vacíos. El refresco normal solo mira las últimas 48 h (el `since_hours` de `refresh_metrics()`), así que por sí solo nunca alcanzaría a rellenarlas.

```bash
$ python scripts/db_setup.py --backfill 3650
   [PG] NOTICE:  backfill 2026-06-01 .. 2026-06-08: 0 nuevos, 214 enriquecidos
   ...
[OK] Backfill: 214 fila(s) insertada(s) o enriquecida(s).
[INFO] El backfill reescribe filas antiguas. Recomendado a continuación:
       VACUUM (ANALYZE) sooniverse.token_usage_event;
```

⚠️ El `VACUUM (ANALYZE)` no es opcional en una tabla grande: el backfill **reescribe** filas existentes (`UPDATE` = tupla muerta + tupla nueva en PostgreSQL), y sin recuperar ese espacio ni refrescar las estadísticas del planificador las consultas del panel se degradan. El script no lo ejecuta él mismo porque `VACUUM` no puede correr dentro de una transacción y bloquear el despliegue en una tabla de millones de filas sería peor que el problema.

## `scripts/benchmark_capacity.py`

Mide el techo real de la infraestructura con una rampa **acotada** de concurrencia: sube hasta que la latencia se degrada, el throughput deja de crecer o los errores superan el umbral, y para ahí. No busca el punto de rotura. Ver `docs/01_FLUJO_DESPLIEGUE.md` (Fase 6.6) para el porqué de cada decisión.

**Dos modos en un solo archivo:**

| Modo | Cuándo | Qué hace |
|---|---|---|
| **driver** (por defecto) | Lo que invoca la fase `capacidad` | Empuja el script al Gateway por `scp`, lo ejecuta allí con `--local` vía `sky exec`, recoge el JSON de vuelta y lo **persiste** en `sooniverse.capacity_benchmark` |
| **runner** (`--local`) | Dentro del Gateway | Mide contra `http://127.0.0.1`, emite el JSON entre centinelas y **no toca la base de datos** |

La separación existe porque medir desde fuera de la VPC mediría el ISP del operador, no la infraestructura; y porque conviene un único camino de escritura a PostgreSQL (el driver, que es quien vive junto al `.env`).

| Flag | Default | Descripción |
|---|---|---|
| `--config` | `config_global.yaml` | Contrato del que se leen `capacidad.*` y `workloads[]` |
| `--workload <id>` | todos los `llm-texto` | Medir un solo workload |
| `--gateway-ip` | (descubierto) | IP del Gateway en modo driver; si se omite, `sky status --ip` |
| `--gateway-url` | `http://127.0.0.1` | URL base a medir en modo `--local` |
| `--local` | off | Modo runner (ver arriba) |
| `--niveles` | del contrato | Rampa explícita, p. ej. `1,4,16` |
| `--segundos-por-nivel` | del contrato | Duración de cada escalón |
| `--warmup` | del contrato | Tráfico descartado antes de medir (absorbe el primer forward pass) |
| `--prompt-tokens` | del contrato | Tamaño objetivo del prompt sintético |
| `--max-tokens` | del contrato | Tokens de salida por petición; fijo para que todos los niveles generen el mismo trabajo |
| `--presupuesto-segundos` | del contrato | Tope duro del total de la corrida |
| `--write-db` | off | Persiste en `sooniverse.capacity_benchmark` (solo en modo driver) |
| `--json RUTA` | — | Artefacto JSON; `-` lo escribe en stdout entre centinelas |
| `--dry-run` | off | Imprime el plan de rampa y el coste estimado en segundos de GPU, sin generar tráfico |

**Ejemplo — ver qué se ejecutaría, sin gastar GPU:**

```bash
$ python scripts/benchmark_capacity.py --dry-run
[PLAN] 1 workload(s) · niveles [1, 2, 4, 8, 16] · 20s/nivel · warmup 10s ≈ 110s de GPU por workload (presupuesto 240s)
```

**Ejemplo — corrida real desde el operador (modo driver):**

```bash
$ python scripts/benchmark_capacity.py --write-db --json .sooniverse_capacity.json
[EXEC] sky exec sooniverse-acme-prod-gw '<benchmark qwen3-5-llm>'
  [bench] warmup 10s (resultados descartados)...
  [bench] nivel concurrencia=1 durante 20s...
  [bench]   18 pet · 0.90 rps · p95=1180ms · errores=0.0%
  [bench] nivel concurrencia=2 durante 20s...
  ...
  [bench] parada: p95_degradado

=== sooniverse-qwen3.5 (qwen3-5-llm) ===
 CONC   PET     RPS    TOK/S   P95 ms   TTFT95  ERR %
    1    18    0.90     54.0     1180      290    0.0
    2    36    1.80    105.0     1310      310    0.0
    4    68    3.40    190.0     1690      420    0.0
    8   102    5.10    272.0     2410      680    0.0
   16   118    5.90    295.0     6240     1980    6.8
  rodilla: concurrencia=8 · 306.0 pet/min · 16320.0 tok/min · ~64 usuarios · parada=p95_degradado
[OK] Corrida 4b1e9a02 persistida en sooniverse.capacity_benchmark
```

El resultado se lee en el panel en `/panel/metrics/capacidad/`, junto al margen frente al pico observado y la ficha de la configuración bajo la que se midió.

## `scripts/render_gateway_stack.py`

Normalmente se invoca automáticamente (`generate_manifests()`), pero puede correrse suelto para inspeccionar el resultado sin generar manifiestos SkyPilot:

```bash
python scripts/render_gateway_stack.py --config config_global.yaml
```

## Comandos del panel Django (`django_metrics/`)

Se ejecutan con `python manage.py <comando>` desde `django_metrics/`, o dentro del contenedor `sooniverse-metrics`.

### `manage.py seed_ritmo`

Siembra consumo sintético con **forma horaria y semanal realista** en la base de datos.

Existe porque los seeders anteriores no permiten probar la analítica de ritmo de uso: `seed_demo` y `seed_fluctuacion` reparten los eventos con `hours=randint(0, 23)`, es decir, uniformemente a lo largo del día. Eso sirve para ver moverse la serie temporal, pero deja el **mapa de calor plano** y hace imposible probar la **detección de tiempos muertos** — con tráfico uniforme no hay ninguna franja ociosa que encontrar, así que la funcionalidad parecería rota sin estarlo.

Lo que genera en su lugar es un patrón de oficina reconocible: laborables con dos jorobas (media mañana y media tarde) y valle de comida, noches prácticamente muertas, fines de semana con una fracción pequeña del tráfico, latencia **correlacionada con la carga** (sin esa correlación el mapa de p95 saldría plano y no probaría nada) y una tasa de error que sube en los picos.

| Flag | Default | Descripción |
|---|---|---|
| `--dias` | 45 | Días de historia a generar |
| `--pico` | 14 | Peticiones por hora en el momento más cargado |
| `--con-benchmark` | off | Añade además una API Key con `proposito='benchmark'` y una ráfaga corta y brutal, para probar el filtro de exclusión del panel |
| `--clean` | off | Elimina lo generado por este comando y sale |
| `--seed` | 7 | Semilla del generador (reproducible) |

Todos los registros llevan el prefijo `ritmo-`, así que `--clean` es preciso y nunca toca métricas reales ni las de los otros seeders.

```bash
$ python manage.py seed_ritmo --dias 45 --con-benchmark
Ritmo sembrado: 4214 evento(s) de cliente + 600 de benchmark en 45 día(s). 103 fila(s) de rollup, 596 bucket(s) horario(s).
Para revertir: python manage.py seed_ritmo --clean
```

`--con-benchmark` es lo que permite verificar el filtro en los dos sentidos sin gastar GPU: con la casilla "Incluir tráfico de benchmark" desactivada el pico sintético **no** debe aparecer en el mapa de calor; activándola, sí.

### Otros seeders

- `manage.py seed_demo` — 45 días de datos sintéticos planos, prefijo `demo-`.
- `manage.py seed_fluctuacion` — onda seno + ruido, prefijo `fluct-`, pensado para ver moverse la línea de tendencia.
- `manage.py sync_metrics` — dispara el ETL de LiteLLM + rollups manualmente (es lo mismo que corre el job periódico del `entrypoint.sh`).
- `manage.py ensure_superuser` — crea/actualiza el superusuario del panel de forma idempotente.
