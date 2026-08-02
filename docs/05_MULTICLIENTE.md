# 05. Multi-cliente

## 1. Dar de alta un cliente nuevo

```bash
cp -r clients/_ejemplo clients/<id-del-cliente>
# editar clients/<id-del-cliente>/config_global.yaml:
#   - cliente.id, cliente.entorno
#   - red_y_aislamiento.region, vpc_cidr (distinto al de otros clientes activos
#     en la misma región/cuenta -- generate_infra.py avisa si detecta un solape)
#   - red_y_aislamiento.aws_profile si este cliente vive en otra cuenta AWS
#   - workloads[] según lo contratado

python scripts/generate_infra.py --config clients/<id>/config_global.yaml --run
```

Todos los comandos (`generate_infra.py`, `destroy_infra.py`, `sync_endpoints.py`, `verify_deployment.py`) aceptan `--config` apuntando a ese archivo.

## 2. Convenciones de nombres (sin excepción)

| Recurso | Patrón | Ejemplo (`cliente.id=acme`, `entorno=prod`) |
|---|---|---|
| Clúster gateway | `sooniverse-<cliente>-<entorno>-gw` | `sooniverse-acme-prod-gw` |
| Clúster worker | `sooniverse-<cliente>-<entorno>-<workload_id>` | `sooniverse-acme-prod-qwen3-5-llm` |
| VPC (tag Name) | `sooniverse-<cliente>-<entorno>-vpc` | `sooniverse-acme-prod-vpc` |
| Security Group | `sooniverse-<cliente>-<entorno>-{gateway,workers}` | `sooniverse-acme-prod-gateway` |

`cliente.id` se valida (`ConfigValidator._validate_cliente`, `scripts/generate_infra.py:124`) contra `^[a-z0-9-]{1,20}$` — minúsculas, guiones, sin espacios, máximo 20 caracteres. Es el límite más estricto de los que imponen los distintos recursos AWS (nombre de SG ≤ 255, tag `Name` sin restricción particular), así que cumplirlo garantiza que todos los nombres derivados sean válidos.

## 3. Aislamiento

| Dimensión | Mecanismo |
|---|---|
| Recursos AWS | Nombres únicos por `<cliente>-<entorno>`, tags `sooniverse:client-id`/`sooniverse:environment`/`sooniverse:deployment-id` |
| Red (CIDR) | `check_cidr_isolation()` avisa (no bloquea) si dos clientes activos en la misma región comparten/solapan `vpc_cidr` (`docs/02_RED_AWS.md` §"Aislamiento entre clientes") |
| Artefactos locales | `.artifacts/<cliente>-<entorno>/` (manifiestos SkyPilot, `.sky_config_workers.yaml`, caché de endpoints) — ver `artifacts_dir_for()`, `scripts/generate_infra.py:59` |
| Estado | Una fila por `(client_id, environment, region)` en `sooniverse.infra_deployment`, con el índice único que impide colisión |
| Credenciales AWS | `red_y_aislamiento.aws_profile` (opcional) selecciona el perfil de `~/.aws/credentials`/`~/.aws/config` para ese cliente |
| Base de datos | Compartida entre todos los clientes (mismo esquema `sooniverse`), pero cada tabla de estado lleva `client_id`/`deployment_id` — no hay fuga de datos entre clientes porque cada query filtra por esas columnas |

### Compatibilidad hacia atrás

Si `--config` es el `config_global.yaml` de la **raíz** del repo (el único punto de entrada antes de la Fase 6), los artefactos se siguen escribiendo en la raíz sin subcarpeta — una instalación de un solo cliente que ya existía no cambia de comportamiento. Cualquier otro `--config` (incluido `clients/_ejemplo/...`) obtiene su propio `.artifacts/<cliente>-<entorno>/` automáticamente.

## 4. Inventario de todos los clientes

```bash
python scripts/list_deployments.py          # tabla
python scripts/list_deployments.py --json    # para scripting
```

Lee `sooniverse.v_infra_deployment_summary` — **todos** los clientes/entornos/regiones, no solo el del `--config` que se le pase a otros comandos (este script no toma `--config` en absoluto, es intencionalmente global).

## 5. Límites conocidos (no implementados)

- **BYOC con AssumeRole + External ID:** hoy `aws_profile` selecciona un perfil de credenciales *locales*; no hay flujo de "el cliente aprueba acceso desde su propia cuenta AWS sin compartirnos credenciales permanentes". El hook está documentado (no implementado) en `AwsNetworkManager.__init__`, `scripts/aws_network.py:198-220`.
- **Peering entre VPCs de distintos clientes:** no se crea ni se gestiona. `check_cidr_isolation()` solo avisa de solapes que *impedirían* un peering futuro, pero el peering en sí no es parte de este proyecto.
- **Verificación de disponibilidad de instancia GPU por AZ:** ver la limitación conocida en `docs/02_RED_AWS.md`, aplica igual en contexto multi-cliente (dos clientes en la misma región compiten por la misma cuota de instancias GPU de la cuenta).
- **Ejecución realmente concurrente de dos `generate_infra.py --run` en el mismo instante en la misma máquina:** el índice único de PostgreSQL evita que dos VPCs colisionen para el mismo cliente, pero no hay ningún lock que impida que dos procesos *distintos* (dos clientes distintos) corriendo *literalmente* al mismo tiempo pisen el archivo transitorio `docker_images/gateway/litellm_config.yaml` (compartido, regenerado justo antes de cada `sky launch`). En la práctica, el flujo normal es un operador ejecutando despliegues uno detrás de otro, no en paralelo estricto.
