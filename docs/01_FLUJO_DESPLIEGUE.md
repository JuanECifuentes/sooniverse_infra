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
- **Tras el launch:** `_gateway_public_ip()` (línea 1065) hace `sky status --ip <cluster>` para capturar la IP pública. Con `gateway.dominio.habilitado: true`, a continuación `_associate_gateway_eip()` asocia la Elastic IP reservada en la fase `network` a la instancia recién lanzada, refresca la caché de IP de SkyPilot (`sky status --refresh`) y verifica `sky exec <gw> true` -si esto falla, la fase aborta con un error explícito, porque TODAS las fases siguientes dependen de `sky exec` contra este mismo Gateway.
- **Duración:** 2-5 minutos (arranque de instancia + `setup` script: Docker, dependencias, certificado TLS -autofirmado, o Let's Encrypt con fallback automático a autofirmado si el DNS todavía no resuelve, ver Fase 3.5-).
- **Puede fallar:** cualquier error de `sky launch` (cuota de instancias, AMI no disponible en la región/AZ, fallo del `setup` script) se propaga como `CalledProcessError`; un fallo asociando la Elastic IP levanta `GatewayEipAssociationError` y aborta el despliegue.

## Fase 3.5 — `dominio` (best-effort, nunca aborta el despliegue)

- **Función:** `run_dominio_phase()` (`scripts/generate_infra.py`), invocada solo si `gateway.dominio.habilitado: true`.
- **Qué hace:** resuelve el registro DNS A del dominio elegido (`socket.getaddrinfo`) y lo compara con la IP del Gateway (ya la Elastic IP, tras la fase `gateway`). Si no coincide, sondea cada 15s hasta agotar `gateway.dominio.esperar_dns_segundos` (default 300s). Si sigue sin coincidir, imprime un `[WARNING]` con el registro A exacto que falta y **continúa el despliegue en HTTP** -nunca aborta, para no quemar el límite de 5 intentos/hora de Let's Encrypt reintentando contra un DNS que el operador todavía no configuró.
- **Si el DNS coincide:** emite/renueva el certificado real vía `sky exec <gw> 'docker run certbot/certbot certonly --webroot ...'` (nginx ya está arriba desde la fase `gateway`, sirviendo `/.well-known/acme-challenge/` desde el volumen `certbot-www`) y recarga nginx (`docker compose exec proxy nginx -s reload`).
- **Por qué existe como fase separada de `gateway`:** el `setup` script de la fase `gateway` (que corre ANTES de que nginx esté arriba) ya intenta un primer certbot en modo `--standalone`; si el DNS todavía no resolvía en ese momento (el operador puede crear el registro A *entre* `--only network` y el resto del despliegue, ver `Manual_Dominio_AWS.md`), cae a un autofirmado de respaldo para que nginx pueda arrancar. Esta fase reintenta la emisión real en cuanto el DNS esté listo, sin necesitar un redeploy completo (`--run --only dominio`).
- **Puede fallar (sin abortar el despliegue):** cualquier fallo de certbot vía `sky exec` se registra como `[WARNING]` (`infra_event`, `phase='dominio'`); el certificado autofirmado de respaldo sigue sirviendo hasta la siguiente corrida exitosa.

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

## Fase 6.5 — `capabilities`

- **Función:** dentro de `deploy()` (entre `endpoints` y `verify`): invoca `scripts/test_model_capabilities.py --config <config> --write-db --json <out_dir>/.sooniverse_capabilities.json` como subproceso, y a continuación `scripts/sync_openwebui_models.py --config <config> --apply`. **Best-effort**: ninguno de los dos aborta el despliegue si falla; un código de salida distinto de cero solo genera un `[WARNING]`.
- **Por qué después de `endpoints` y no antes:** necesita el Gateway ya sirviendo tráfico y LiteLLM ya recargado con el pool de workers (fase `endpoints`) — sondear un modelo que todavía no está en `litellm_config.yaml` daría solo resultados inconclusos.
- **Qué hace `test_model_capabilities.py --write-db`:** sondea cada modelo público declarado (`workloads[].nombre_publico`) a través del Gateway con peticiones mínimas reales — visión (imagen 1x1), tool calling (`tool_choice: auto`), `response_format: json_object` (lo que rompía las tareas automáticas de Open WebUI) y streaming. Los sondeos inconclusos (timeout, worker aún arrancando) se reintentan automáticamente (hasta 3 intentos, backoff 5/15/30s) antes de darlos por definitivos — fail-closed: un inconcluso persistente NUNCA cuenta como "soportado". El resultado se persiste en `sooniverse.model_capability` (`docs/03_ESTADO_Y_BD.md`) y también se escribe en `.sooniverse_capabilities.json` (usado por `render_gateway_stack.py` para los flags globales de Open WebUI).
- **Qué hace `sync_openwebui_models.py --apply`:** re-renderiza `docker_images/gateway/docker-compose.yml` (ahora con `.sooniverse_capabilities.json` disponible), lo empuja al Gateway, recrea el contenedor `open-webui` (los `ENABLE_*` son variables de entorno del servicio, así que `docker compose up -d` sí detecta el cambio y recrea, a diferencia del caso de `litellm_config.yaml` documentado en la fase `endpoints`), y corre el servicio one-shot `openwebui-bootstrap` (`docker_images/openwebui/overlay/sooniverse/bootstrap_models.py`) que upsertea, vía la API HTTP pública de Open WebUI, una fila `model` por modelo con `meta.capabilities` derivado de `sooniverse.model_capability`.
- **Duración:** 30s-3min (4 sondeos × hasta 3 reintentos por modelo, más la recreación del contenedor).
- **Puede fallar (sin abortar el despliegue):** un mismatch peligroso (`capacidades.vision: true` en el contrato pero el modelo lo rechaza) se imprime en la tabla y devuelve código 1, pero la infra queda `active`/`degraded` igual — corrige `config_global.yaml` y re-despliega, o re-corre `python scripts/test_model_capabilities.py --write-db` suelto tras arreglar el worker.
- **Por qué `GATEWAY_RUN_SCRIPT` persiste `CLIENTE_ID`/`ENTORNO` en `.env` (hallazgo de un despliegue de prueba real):** SkyPilot solo exporta las `envs:` del contrato (`CLIENTE_ID`, `ENTORNO`, ...) al script `setup:`/`run:` que corre durante `sky launch`; una invocación posterior de `docker compose` vía `sky exec` (exactamente lo que hace `sync_openwebui_models.py` en cada corrida de esta fase) **no hereda ese entorno de shell** — confirmado con `sky exec <gw> 'echo $CLIENTE_ID'` devolviendo vacío. Sin escribir esas dos variables en `.env` (idempotente, primer paso de `GATEWAY_RUN_SCRIPT`), `openwebui-bootstrap` consultaría `sooniverse.model_capability` con `client_id='default'` en vez del cliente real, sin encontrar la fila, y aplicaría el fallback fail-closed (todo apagado) en silencio en cada resincronización posterior al primer despliegue.

## Fase 6.6 — `capacidad`

- **Función:** dentro de `deploy()` (`scripts/generate_infra.py:1658-1685`), entre `capabilities` y `verify`: invoca `scripts/benchmark_capacity.py --config <config> --write-db --json <out_dir>/.sooniverse_capacity.json` como subproceso. **Best-effort**: un código de salida distinto de cero solo genera un `[WARNING]`; el despliegue continúa y termina igual.
- **Qué responde:** "¿cuántas peticiones y cuántos tokens por minuto aguanta esta infraestructura antes de degradar la respuesta?". Hasta esta fase, el panel podía decir cuánto se había consumido, pero no si el consumo estaba cerca de algún límite — el techo era una conjetura del operador, no un número medido.
- **Qué hace:** una **rampa acotada** de concurrencia (`capacidad.niveles_concurrencia`, por defecto `[1, 2, 4, 8, 16]`). En cada nivel lanza peticiones en bucle cerrado durante `segundos_por_nivel` y mide throughput, tokens de salida por segundo, TTFT y latencias p50/p95/p99. Tras cada nivel evalúa si parar: `errores` (tasa por encima de `umbral_error_pct`), `p95_degradado` (p95 por encima de `umbral_p95_degradacion` × el p95 del nivel 1), `saturacion_throughput` (un nivel más de concurrencia ya no aporta tokens/s, así que solo añade cola) o `presupuesto_agotado`. La **rodilla** es el último nivel que no disparó ninguna condición. Si la rampa termina sin doler, el motivo es `nivel_maximo` y el techo real puede ser mayor que el medido — la ficha del panel lo dice explícitamente, porque dimensionar con ese número infraestima.
- **Por qué se mide desde el Gateway y no desde la máquina del operador:** fuera de la VPC, el RTT del ISP de quien lanza el script domina el TTFT y limita la concurrencia alcanzable; se estaría midiendo la conexión del operador, no la infraestructura. El script se auto-invoca en remoto vía `sky exec` con `--local`, de modo que el camino medido es `127.0.0.1:80 → nginx → litellm → worker`: exactamente el del cliente real (Open WebUI). La columna `capacity_benchmark.origen` registra cuál de los dos se usó, porque los números de un origen **no son comparables** con los del otro.
- **Transporte del script al Gateway:** `scp`, no `sky rsync`. Ese subcomando **no existe** en la versión de SkyPilot de este repo (0.13.0 responde `Error: No such command 'rsync'`, confirmado en un despliegue real), así que no es un fallo de red que se pueda reintentar. Se reutiliza el patrón de `scripts/sync_openwebui_models.py`: resolver IP + clave generada por SkyPilot (`~/.sky/generated/ssh-keys/<cluster>.key`) y empujar por `scp`, con `sky exec` + heredoc como reserva. El push es necesario porque `scripts/` solo se sincroniza en el `sky launch` del Gateway (`file_mounts`): un `--only capacidad` en frío encontraría allí una copia vieja del script, o ninguna la primera vez.
- **Tráfico sintético y métricas de negocio:** el benchmark genera peticiones reales que LiteLLM registra en `SpendLogs`. Para que no contaminen el panel, el runner emite una API Key **efímera** (`duration: 1h`, borrada al terminar) que el driver registra en `sooniverse.api_key_registry` con `proposito = 'benchmark'`. El panel la excluye por defecto —con un chip visible para poder incluirla— y, sobre todo, el **pico observado** de la página de capacidad la excluye siempre: si no, el pico sería siempre el propio test de estrés y el semáforo de margen estaría en rojo permanente sin que existiera ningún problema real.
- **Duración:** ~2-4 minutos de GPU por workload. `capacidad.presupuesto_segundos` (default 240) es un **tope duro**, no una intención: `ConfigValidator._validate_capacidad` rechaza el contrato si `len(niveles) × segundos_por_nivel + warmup` lo supera, de modo que una rampa de 10 niveles × 60s no puede colarse y comerse 10 minutos de GPU en cada despliegue sin que nadie lo note.
- **Escribe:** una fila en `sooniverse.capacity_benchmark` (con la curva completa por nivel en JSONB) y el artefacto `<out_dir>/.sooniverse_capacity.json`.
- **Puede fallar (sin abortar el despliegue):** si no hay Gateway activo, el script imprime `[N/A]` y devuelve 0. Si la persistencia en PostgreSQL falla, se avisa con `[WARNING]` pero la medición ya viajó en el JSON. Con `capacidad.habilitado: false` la fase ni siquiera se ejecuta: log `[SKIP]`.

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
   │                     │──(si dominio) asocia Elastic IP + sky status --refresh──────────────▶│                    │
   │                     │──(si dominio) fase 'dominio': verifica DNS + certbot vía sky exec────▶│                    │
   │                     │──regenera bastion con gateway_ip                                     │                   │
   │                     │──sky launch workers (subred privada, vía bastion)───────────────────▶│                   │
   │                     │──sync_endpoints.py --apply──────────────────────────────────────────────────────────────▶│
   │                     │                                                                       │◀──discover IPs────│
   │                     │                                                                       │◀──push+reload litellm
   │                     │──test_model_capabilities.py --write-db (fail-closed, best-effort)──────────────────────▶│
   │                     │◀──sooniverse.model_capability actualizada──────────│                    │                 │
   │                     │──sync_openwebui_models.py --apply (best-effort)────────────────────────────────────────▶│
   │                     │                                                                       │◀──recrea open-webui + bootstrap modelos
   │                     │──benchmark_capacity.py --write-db (best-effort)────────────────────────────────────────▶│
   │                     │                                    │  sky exec: el script se mide A SÍ MISMO desde el   │
   │                     │                                    │  Gateway (rampa acotada, key efímera 'benchmark')  │
   │                     │◀──sooniverse.capacity_benchmark + .sooniverse_capacity.json───────────│                 │
   │                     │──verify_deployment.py (best-effort)                                   │                   │
   │◀──URLs + deployment_id──│                       │                       │                    │                 │
```

**Orden de fases (`PHASE_ORDER`, `scripts/generate_infra.py:1356`):**

```
network → gateway → dominio → workers → endpoints → capabilities → capacidad → verify
```

`--only` acepta `all` o cualquiera de esos nombres; sus `choices` se derivan de `PHASE_ORDER` (`["all", *PHASE_ORDER]`), así que añadir una fase nueva solo exige tocar esa lista y no se pueden desincronizar.

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
