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

## 5. BYOC (Bring Your Own Cloud): cross-account AssumeRole + External ID

`red_y_aislamiento.aws_profile` no solo selecciona un perfil de credenciales fijas locales:
puede apuntar a un perfil de `~/.aws/config` que en sí mismo es una cadena de asunción de
rol (`role_arn` + `source_profile` + `external_id`). boto3 (y por lo tanto todo el código de
este repo, además de SkyPilot, que también resuelve credenciales vía boto3) resuelve esa
cadena automáticamente, con refresco transparente de credenciales temporales (~1h) — no hace
falta ningún código que llame a `sts.assume_role()` a mano.

**Flujo real (probado end-to-end):**

1. El cliente aplica el módulo de Terraform en `onboarding/aws-byoc-terraform/` en **su
   propia cuenta AWS**, con sus propias credenciales de administrador — nunca las de
   Sooniverse. Esto crea el rol `SooniverseDeployRole` con una trust policy hacia la cuenta
   de Sooniverse y un `external_id` secreto acordado, y le adjunta únicamente los permisos
   EC2/VPC/IAM-support/CloudWatch necesarios para desplegar (ver `main.tf`).
2. El cliente entrega a Sooniverse el `role_arn` de salida.
3. Sooniverse agrega a **su propio** `~/.aws/config` (nunca al del cliente) un perfil:
   ```ini
   [profile <cliente>-byoc]
   role_arn = arn:aws:iam::<CUENTA_CLIENTE>:role/SooniverseDeployRole
   source_profile = sooniverse-operator
   external_id = <el mismo secreto usado en terraform.tfvars>
   region = <región del cliente>
   ```
4. En `clients/<cliente>/config_global.yaml`, `red_y_aislamiento.aws_profile: "<cliente>-byoc"`.
   A partir de ahí, `generate_infra.py`/`destroy_infra.py`/`sync_endpoints.py`/
   `verify_deployment.py` operan exclusivamente sobre la cuenta del cliente — la cuenta de
   Sooniverse nunca recibe cargos de cómputo.

**Requisito de código (ya corregido, Fase BYOC):** todo lugar que invoque el binario `sky`
(`sky launch`/`down`/`start`/`exec`/`status`) o construya un cliente boto3 directo debe
propagar `AWS_PROFILE=<aws_profile>` en el entorno del subproceso o `profile_name=aws_profile`
en la sesión de boto3 — SkyPilot resuelve credenciales con la cadena estándar de boto3 y
**no** hereda `red_y_aislamiento.aws_profile` del contrato automáticamente. `AwsNetworkManager`
ya lo hacía; `generate_infra.py`, `destroy_infra.py`, `sync_endpoints.py` y
`verify_deployment.py` no lo hacían y fue corregido.

**Revocación:** el cliente puede cortar el acceso en cualquier momento con
`terraform destroy` sobre `onboarding/aws-byoc-terraform/` en su cuenta — invalida el rol al
instante, sin coordinación con Sooniverse.
- **Peering entre VPCs de distintos clientes:** no se crea ni se gestiona. `check_cidr_isolation()` solo avisa de solapes que *impedirían* un peering futuro, pero el peering en sí no es parte de este proyecto.
- **Verificación de disponibilidad de instancia GPU por AZ:** ver la limitación conocida en `docs/02_RED_AWS.md`, aplica igual en contexto multi-cliente (dos clientes en la misma región compiten por la misma cuota de instancias GPU de la cuenta).
- **Ejecución realmente concurrente de dos `generate_infra.py --run` en el mismo instante en la misma máquina:** el índice único de PostgreSQL evita que dos VPCs colisionen para el mismo cliente, pero no hay ningún lock que impida que dos procesos *distintos* (dos clientes distintos) corriendo *literalmente* al mismo tiempo pisen el archivo transitorio `docker_images/gateway/litellm_config.yaml` (compartido, regenerado justo antes de cada `sky launch`). En la práctica, el flujo normal es un operador ejecutando despliegues uno detrás de otro, no en paralelo estricto.
