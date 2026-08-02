# 08. Notas para agentes de IA

Si sos un agente (Claude Code u otro) trabajando en este repo, leé esto antes de tocar nada relacionado con infraestructura.

## 1. Qué NUNCA editar a mano (se regenera y se pierde)

| Archivo/patrón | Lo genera |
|---|---|
| `.sky_generated.*.yaml` | `scripts/generate_infra.py::generate_manifests()` |
| `.sky_config_gateway.yaml`, `.sky_config_workers.yaml` | `TopologyBuilder.build_sky_gateway_config()`/`build_sky_workers_config()` |
| `.sooniverse_endpoints.json` | `scripts/sync_endpoints.py::render_config()` |
| `docker_images/gateway/litellm_config.yaml` | `scripts/render_litellm_config.py` |
| `docker_images/gateway/nginx/default.conf` | `scripts/render_gateway_stack.py::render_nginx_conf()` |
| `docker_images/gateway/docker-compose.yml` | `scripts/render_gateway_stack.py::render_docker_compose()` |
| `.artifacts/<cliente>-<entorno>/*` | Igual que los `.sky_*` de arriba, con ruta por cliente |
| `.sooniverse_state.*.json` | `PostgresInfraStateStore._mirror()` — espejo de solo lectura, nunca se lee de vuelta |

Si necesitás cambiar algo que vive en uno de estos archivos, el cambio va en `config_global.yaml` (o el `.py` que lo genera), nunca directamente en el archivo generado.

## 2. Invariantes de diseño (no las rompas sin discutirlo explícitamente)

1. **Nada de Terraform/CloudFormation/CDK/Pulumi.** Todo el ciclo de vida de AWS es `boto3` puro dentro de `scripts/aws_network.py`. Si te piden "usar Terraform para X", es una señal de que el pedido contradice una decisión de arquitectura tomada explícitamente — señalalo en vez de implementarlo silenciosamente.
2. **El mecanismo de propiedad es DOBLE:** un recurso solo se borra si (a) está en `sooniverse.infra_resource` con el `deployment_id` correcto **y** (b) sus tags AWS reales coinciden con ese mismo `deployment_id` en el momento de borrar (`AwsNetworkManager._tags_match_deployment`). No lo simplifiques a "una sola condición" aunque parezca redundante.
3. **Nunca tocar la VPC por defecto de la cuenta.** `DefaultVpcGuardError` en `ensure_vpc()` debe seguir ahí. Si algún cambio futuro necesita crear una VPC, pasá siempre por `ensure_vpc()`, nunca por una llamada directa a `create_vpc`/`delete_vpc` en otro lugar.
4. **SG→SG, nunca CIDR, entre gateway y workers.** Es lo que permite crear el SG de workers antes de que exista el gateway, y lo que sobrevive a que el gateway cambie de IP.
5. **`gestion_red: existente` es un modo de compatibilidad real, no vestigial.** Cualquier cambio en `TopologyBuilder`/`AwsNetworkManager` tiene que seguir funcionando para un operador que apunta a una VPC creada a mano (`vpc_name`/`security_group_*` fijos en el contrato, sin `AwsNetworkManager` de por medio).
6. **Privacidad por diseño:** ninguna tabla de `database/*.sql` puede almacenar prompts ni respuestas — solo contadores. `store_prompts_in_spend_logs: false` y `turn_off_message_logging: true` (LiteLLM) no se tocan. Si una tarea pide "guardar el contenido de las conversaciones para auditoría", es un conflicto directo con este invariante — flaguéalo, no lo implementes silenciosamente.
7. **El DDL vive solo en `database/*.sql`, nunca en migraciones de Django.** Los modelos Django que leen estas tablas son `managed = False`.
8. **`--dry-run` no debe escribir NADA**, ni en AWS ni en PostgreSQL. Este proyecto tuvo un bug real de esto: el constructor de `AwsNetworkManager` abre un `deployment_id` nuevo si no recibe uno explícito, así que un `--dry-run` ingenuo terminaba insertando una fila real en `infra_deployment`. La corrección vive en `deploy()`: en dry-run, solo se instancia `AwsNetworkManager` si ya existe un `deployment_id` (lectura pura); si no existe, se imprime la intención sin construir el manager. Si tocás el flujo de `deploy()`, verificá que esta propiedad se mantenga (hay una forma barata de comprobarlo: correr `--dry-run` dos veces y confirmar con `list_deployments.py` que no aparece nada nuevo).
9. **Todo nombre de recurso/clúster/clave de estado lleva `{cliente.id}-{entorno}` (y región donde aplica).** Nunca un nombre global. `cliente.id` se valida contra `^[a-z0-9-]{1,20}$` — es el límite más estricto de los que imponen los recursos AWS derivados, así que no lo relajes sin revisar todos los usos (nombre de SG, tag Name, nombre de clúster SkyPilot).
10. **Todo comando destructivo requiere `--yes` o confirmación interactiva, y ofrece `--dry-run`.** Ver `confirm_destructive_action()` en `destroy_infra.py`.

## 3. Cómo validar un cambio SIN aprovisionar nada real

En orden de costo creciente:

1. **Sintaxis + imports:** `python -c "import ast; ast.parse(open('scripts/archivo.py', encoding='utf-8').read())"`.
2. **Validación del contrato:** `python -c "import sys; sys.path.insert(0,'scripts'); from generate_infra import ConfigValidator; import yaml; ConfigValidator.validate(yaml.safe_load(open('config_global.yaml')))"`.
3. **Tests unitarios (sin AWS, sin red):** `python -m pytest tests/test_aws_network.py tests/test_plan_changes.py tests/test_config_validator.py -q` — usan `moto` o son puramente en memoria.
4. **Generación de manifiestos (sin `--run`):** `python scripts/generate_infra.py` — escribe los `.sky_generated.*`/nginx/compose y falla rápido si algo en `TopologyBuilder`/`render_gateway_stack` está roto.
5. **`--dry-run` contra AWS/PostgreSQL reales** (requiere credenciales, pero no muta nada): `python scripts/generate_infra.py --run --only network --dry-run` y `python scripts/destroy_infra.py --dry-run`.
6. **Tests contra PostgreSQL real** (si hay `.env` con acceso): `python -m pytest tests/test_infra_state.py -q` — usa un `client_id` de prueba aislado (`pytest-infra-state`) y limpia sus propias filas; si tu cambio toca `infra_state.py`, corré esto y confirmá con `list_deployments.py` que no queda basura.
7. **Solo si el usuario lo pide explícitamente:** una corrida real (`--run` sin `--dry-run`). El NAT Gateway cuesta ~\$0.045/hora + ~\$0.005/hora la EIP esté ocioso o no — un despliegue de prueba olvidado son ~\$35/mes. Recordá siempre correr `destroy_infra.py --yes` al terminar de probar, y verificar con el checklist de `docs/04_DESTRUCCION.md` §6.

**Nunca** ejecutes una corrida real contra AWS sin que el usuario lo haya autorizado explícitamente para esa sesión — una autorización pasada no se extiende automáticamente a una corrida nueva.

## 4. Glosario del dominio

| Término | Significado |
|---|---|
| **Deployment** (`deployment_id`) | Un ciclo de vida completo de red+gateway+workers para un `(cliente, entorno, región)`. Fila en `sooniverse.infra_deployment`. |
| **Mecanismo de propiedad** | La regla de "solo se borra si BD Y tags AWS coinciden" (ver invariante #2). |
| **`gestion_red`** | `auto` (este sistema crea/destruye la VPC) vs `existente` (VPC manual, modo legado). |
| **Bastion** | El Nodo Gateway actuando de salto SSH hacia los workers sin IP pública (`ssh_proxy_command`). |
| **SG→SG** | Regla de Security Group cuyo origen es OTRO Security Group (`UserIdGroupPairs`), no un CIDR. |
| **Huérfano** (orphan) | Recurso AWS con tag `sooniverse:managed=true` que no está en ningún despliegue activo, o cuyo despliegue ya está `destroyed`/`error`. |
| **`plan_changes`** | Clasificación de un diff de contrato en `no-op\|in-place\|recreate-cluster\|requires-destroy` (ver `docs/03_ESTADO_Y_BD.md` §6). |
| **Artefactos** | Manifiestos SkyPilot + config de bastion + caché de endpoints de UN cliente, en `.artifacts/<cliente>-<entorno>/` (o la raíz del repo en modo legado). |
| **Espejo local** | El `.sooniverse_state.*.json` que escribe `PostgresInfraStateStore` tras cada cambio — diagnóstico de emergencia, nunca fuente de verdad. |
| **Workload** | Un modelo/tarea del array `workloads[]` del contrato; se traduce en un clúster SkyPilot propio (`sooniverse-<cliente>-<entorno>-<workload_id>`). |

## 5. Si el prompt de una tarea y el código real se contradicen

Gana el código real. Este proyecto se construyó fase por fase con un documento de especificación (`PROMPT_CLAUDE_CODE_sooniverse_red.md`) que quedó ligeramente desalineado del código a medida que avanzaba (ejemplos reales: el esquema de `worker_node` terminó con columnas distintas a las propuestas originalmente; `db_setup.py` pasó de "un solo archivo" a "todos los `.sql` en orden lexicográfico" como un módulo nuevo, no una ampliación). Si encontrás una discrepancia, verificala leyendo el código/tests actuales — no asumas que la especificación original sigue vigente.
