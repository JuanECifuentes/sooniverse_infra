# 01. Flujo de despliegue (documento central)

Este documento recorre `python scripts/generate_infra.py --run` fase por fase. Cada fase indica: qué función la ejecuta, en qué archivo/línea (aproximada — puede desviarse unas pocas líneas tras futuros cambios, pero la función y el archivo son estables), qué lee del contrato, qué llama a AWS/BD, qué escribe, cuánto tarda en la práctica y qué puede fallar.

## 0. Resumen del comando

```bash
python scripts/generate_infra.py --config config_global.yaml --run
python scripts/generate_infra.py --run --dry-run                # plan, sin tocar AWS/BD
python scripts/generate_infra.py --run --only network            # una sola fase
```

`main()` (`scripts/generate_infra.py:1348`) hace: cargar+validar el contrato → generar manifiestos (incluye renderizar el stack del Gateway) → si `--run`, invocar `deploy()`.

## Fase 0 — `validate`

- **Función:** `load_config()` (`scripts/generate_infra.py:752`) → `ConfigValidator.validate()` (línea 112).
- **Lee:** el YAML completo del `--config` (default `config_global.yaml`).
- **Escribe:** nada.
- **Duración:** milisegundos.
- **Puede fallar:** `ConfigValidationError` con un mensaje específico del campo inválido (ver `tests/test_config_validator.py` para el catálogo completo de reglas). Nunca llega a tocar AWS/BD si esto falla — es la primera línea de defensa.

## Fase "render" — manifiestos y stack del Gateway

- **Función:** `generate_manifests()` (línea 1012). Antes de escribir los manifiestos SkyPilot, llama a `render_gateway_stack.render()` (`scripts/render_gateway_stack.py:322`), que regenera `docker_images/gateway/nginx/default.conf` y `docker_images/gateway/docker-compose.yml` a partir de `gateway.exponer_puertos_directos` y `gateway.tls`.
- **Lee:** todo el contrato.
- **Escribe:**
  - `<out_dir>/.sky_generated.gateway.yaml`, `.sky_generated.worker-<id>.yaml`, `.sky_config_workers.yaml` (`out_dir` = raíz del repo si `--config` es el `config_global.yaml` raíz, o `.artifacts/<cliente>-<entorno>/` en cualquier otro caso — ver `artifacts_dir_for()`, línea 59).
  - `docker_images/gateway/nginx/default.conf` y `docker-compose.yml` (siempre en la misma ruta; son compartidos entre clientes porque se regeneran justo antes de cada `sky launch` del Gateway de ese cliente).
- **Duración:** <1s.
- **Puede fallar:** solo por I/O (permisos, disco lleno).

## Fase 1 — `state`

- **Función:** dentro de `deploy()` (línea 1145): rama `red.get("gestion_red") == "auto"` (líneas ~1181-1198).
  - **No dry-run:** `_open_state_store()` (línea 1097) → `PostgresInfraStateStore.ping()` (aborta aquí si PostgreSQL no responde, **antes** de crear nada en AWS) → `get_active_deployment()` para leer el snapshot previo → `plan_changes()` (línea 942) para clasificar diferencias → si `requires_destroy`, lanza `RequiresDestroyError` y el proceso termina con `exit 1` → `open_deployment()` (crea la fila si no existía) → `update_config_snapshot()` si ya existía.
  - **Dry-run:** solo lectura (`get_active_deployment`), nunca escribe ni abre un deployment nuevo.
- **Lee:** `.env` (credenciales PostgreSQL), `sooniverse.infra_deployment` de esa (cliente, entorno, región).
- **Escribe (no dry-run):** fila en `infra_deployment` (nueva o `config_snapshot` actualizado) + evento en `infra_event`.
- **Duración:** ~1-2s (una conexión PostgreSQL + unas pocas queries).
- **Puede fallar:**
  - `DbSetupError` si PostgreSQL no es alcanzable (mensaje explícito, no crea nada en AWS).
  - `RequiresDestroyError` si el contrato cambió un campo no modificable en caliente (`vpc_cidr`, `azs`, `nat_gateway.modo`) — ver `docs/03_ESTADO_Y_BD.md` sección "plan_changes".

## Fase 2 — `network`

- **Función:** `AwsNetworkManager.provision()` (`scripts/aws_network.py:682`), invocada desde `deploy()` solo si `gestion_red: auto` (si es `existente`, se omite por completo — log `[SKIP]`).
- **Antes:** `check_cidr_isolation()` (`scripts/generate_infra.py:818`) avisa (no aborta) si el `vpc_cidr` se solapa con otro cliente activo en la misma región.
- **Orden interno de `provision()`:** `ensure_vpc` → `ensure_subnets` → `ensure_internet_gateway` → `ensure_nat_gateways` → `ensure_route_tables` → `ensure_vpc_endpoints` → `ensure_security_groups`. Cada `ensure_*` busca primero por tags (`sooniverse:deployment-id` + `sooniverse:component`) antes de crear — de ahí que una segunda corrida sea rápida (log `[SKIP][RED:...]`).
- **Lee:** `red_y_aislamiento.*` vía `build_network_spec_from_config()` (línea 772).
- **Llama a AWS:** EC2 (`describe_vpcs`, `create_vpc`, `create_subnet`, `create_internet_gateway`, `allocate_address`, `create_nat_gateway`, `create_route_table`, `create_security_group`, `authorize_security_group_ingress`, ...).
- **Escribe en BD:** una fila en `infra_resource` por cada recurso, en el momento exacto en que la API de AWS devuelve el ID (antes de esperar a que esté disponible — así un crash a medio camino no pierde el rastro).
- **Duración real:** VPC/subredes/IGW/SGs son casi instantáneos; el NAT Gateway es lo lento (~1-3 minutos hasta `available`, el waiter tiene timeout configurable vía `nat_gateway.timeout_segundos`, default 300s).
- **Puede fallar:**
  - `DefaultVpcGuardError` si por algún error de configuración se intentara operar sobre la VPC por defecto (no debería ocurrir nunca en la práctica: `vpc_name` siempre se autogenera).
  - Timeout esperando el NAT Gateway → `NetworkError`.
  - Límite de cuenta AWS (VPCs por región, EIPs) → `ClientError` de boto3, propagado tal cual (mensaje de AWS es suficientemente claro).

## Fase 3 — `gateway`

- **Función:** dentro de `deploy()` (líneas ~1247-1270): construye `TopologyBuilder.build_sky_gateway_config()` (línea 685) con el `vpc_name`/`sg_gateway_name` reales de `NetworkOutputs`, y ejecuta `sky launch -y -c <cluster> .sky_generated.gateway.yaml` vía `_run_sky()` (línea 1053).
- **Lee:** `gateway.*`, `red_y_aislamiento.image_id`.
- **Llama a:** SkyPilot (que a su vez llama a EC2 `run_instances` en la subred pública, con el SG que ya creamos).
- **Escribe:** evento `infra_event` (`phase='gateway', action='sky_launch'`).
- **Tras el launch:** `_gateway_public_ip()` (línea 1065) hace `sky status --ip <cluster>` para capturar la IP pública.
- **Duración:** 2-5 minutos (arranque de instancia + `setup` script: Docker, dependencias, certificado TLS si aplica).
- **Puede fallar:** cualquier error de `sky launch` (cuota de instancias, AMI no disponible en la región/AZ, fallo del `setup` script) se propaga como `CalledProcessError`.

## Fase 4 — `bastion` (implícita, al principio de la fase `workers`)

- **Función:** `TopologyBuilder.build_sky_workers_config(gateway_ip=...)` (línea 707), llamada con la IP real capturada en la fase anterior. Regenera `.sky_config_workers.yaml` con `ssh_proxy_command` apuntando a esa IP y la clave SSH que SkyPilot generó para el clúster del Gateway (`~/.sky/generated/ssh-keys/<gateway_cluster>.key`).
- **Por qué existe como paso propio:** sin la IP real (que no se conoce hasta después de `sky launch` del Gateway), el bastion apuntaría a nada. `sync_endpoints.py` también refresca este archivo de forma independiente (`refresh_bastion_config()`, `scripts/sync_endpoints.py:131`) para corridas posteriores donde la IP pudo cambiar (`sky stop`/`sky start` manual).

## Fase 5 — `workers`

- **Función:** dentro de `deploy()` (líneas ~1272-1300): para cada `workloads[]`, `sky launch -y -c <cluster> .sky_generated.worker-<id>.yaml` con `SKYPILOT_CONFIG` apuntando al `.sky_config_workers.yaml` recién regenerado.
- **Lee:** `workloads[]` completo (accelerator, réplicas, puerto, imagen HF, fracción de VRAM).
- **Llama a:** SkyPilot → EC2 `run_instances` en la subred **privada** (`use_internal_ips: true`), sin IP pública.
- **Duración:** 3-10 minutos (instancia GPU + descarga de pesos del modelo desde HuggingFace vía NAT).
- **Puede fallar:** cuota de instancias GPU en la región/AZ (SkyPilot reintenta en otras AZ si el tipo de instancia no está disponible — comportamiento nativo de SkyPilot, no de este proyecto), fallo de red al descargar el modelo (NAT no disponible → ver `docs/06_RUNBOOK.md`).

## Fase 6 — `endpoints`

- **Función:** `deploy()` invoca `scripts/sync_endpoints.py --config <config> --apply` como subproceso.
- **Dentro de `sync_endpoints.py`:** `build_endpoints()` (línea 301) descubre IPs privadas por workload con la cascada de 4 métodos (`discover_worker_ips`, línea 273: API Python de SkyPilot → `describe_instances` por tag → parseo de logs → `sky status --ip`), corre `check_worker_health()` (línea 291) por endpoint, `render_config()` (línea 342) escribe `litellm_config.yaml` **solo con los sanos**, `register_in_db()` (línea 440) actualiza `sooniverse.worker_node`, y `push_and_reload()` (línea 371) empuja el archivo al Gateway y recarga únicamente el contenedor `litellm`.
- **Duración:** 10-30s (descubrimiento + health checks + `sky rsync`/`sky exec` + reload de un contenedor).
- **Puede fallar:** si el pool queda vacío (ningún worker respondió `/health`), el warning es visible pero **no aborta el despliegue** — es exactamente el comportamiento buscado: un worker caído no debe tumbar el resto.

## Fase 7 — `verify`

- **Función:** `deploy()` invoca `scripts/verify_deployment.py --config <config>` como subproceso, **best-effort**: un código de salida distinto de cero solo genera un `[WARNING]`, no aborta el reporte final.
- **Qué hace:** las 11 comprobaciones de `docs/06_RUNBOOK.md`/sección de aceptación (rutas de red, aislamiento de workers, salud de LiteLLM/nginx, registro en BD).

## Fase 8 — `report`

- **Función:** el bloque final de `deploy()` (líneas ~1370-1385): imprime las URLs (`http(s)://<gateway_ip>/`, `/v1`, `/panel/`, `/healthz`), y actualiza `infra_deployment.status` a `active` o `degraded` según el estado de los recursos registrados.

---

## Diagrama de secuencia — `provision` completo

```
operador          generate_infra.py         PostgreSQL            AwsNetworkManager        SkyPilot            sync_endpoints.py
   │                     │                       │                       │                    │                     │
   │──--run------------->│                       │                       │                     │                    │
   │                     │──validate config──────│                       │                     │                    │
   │                     │──ping()───────────────▶│                       │                    │                    │
   │                     │◀──ok───────────────────│                       │                    │                    │
   │                     │──get_active_deployment▶│                       │                    │                    │
   │                     │◀──existing? plan_changes                       │                    │                    │
   │                     │──open_deployment──────▶│                       │                    │                    │
   │                     │◀──deployment_id────────│                       │                    │                    │
   │                     │──provision()──────────────────────────────────▶│                    │                    │
   │                     │                       │◀──record_resource (x N, según se crea)──────│                    │
   │                     │◀──NetworkOutputs (vpc/sg reales)────────────────│                    │                    │
   │                     │──sky launch gateway────────────────────────────────────────────────▶│                    │
   │                     │◀──gateway_ip────────────────────────────────────────────────────────│                    │
   │                     │──regenera bastion con gateway_ip                                     │                   │
   │                     │──sky launch workers (subred privada, vía bastion)───────────────────▶│                   │
   │                     │──sync_endpoints.py --apply──────────────────────────────────────────────────────────────▶│
   │                     │                                                                       │◀──discover IPs────│
   │                     │                                                                       │◀──push+reload litellm
   │                     │──verify_deployment.py (best-effort)                                   │                   │
   │◀──URLs + deployment_id──│                       │                       │                    │                 │
```

## Diagrama de secuencia — `destroy` completo

Ver `docs/04_DESTRUCCION.md` para el detalle paso a paso; resumen:

```
operador        destroy_infra.py          SkyPilot            AwsNetworkManager           PostgreSQL
   │                  │                       │                       │                       │
   │──destroy--yes───▶│                       │                       │                       │
   │                  │──sky down workers────▶│                       │                       │
   │                  │──sky down gateway────▶│                       │                       │
   │                  │──get_active_deployment────────────────────────────────────────────────▶│
   │                  │◀──deployment_id + resources_in_delete_order────────────────────────────│
   │                  │──destroy(dry_run=False)──────────────────────▶│                        │
   │                  │                       │   revoca reglas SG, borra SG, VPC endpoints,   │
   │                  │                       │   NAT (+ wait), EIP, route tables, IGW,        │
   │                  │                       │   subredes, VPC -en ese orden, verificando       │
   │                  │                       │   tags antes de cada borrado-                   │
   │                  │◀──DestroyReport (éxitos/fallos/omitidos)───────│                        │
   │                  │                       │                       │──mark deleted + status=destroyed──▶│
   │◀──reporte final──│                       │                       │                       │
```
