# 02. Red AWS

Cada recurso que sigue lo crea y destruye `scripts/aws_network.py::AwsNetworkManager`. Ninguno se crea a mano.

## Tabla de recursos

| # | Recurso | Por qué existe | Cómo se etiqueta | Depende de | Coste/hora aprox. |
|---|---|---|---|---|---|
| 1 | VPC | Aislamiento de red por cliente/entorno | `sooniverse:component=vpc` | — | Gratis |
| 2 | Subred pública (por AZ) | Aloja el Gateway | `subnet-public` | VPC | Gratis |
| 3 | Subred privada (por AZ) | Aloja los workers | `subnet-private` | VPC | Gratis |
| 4 | Internet Gateway | Salida/entrada de la subred pública | `igw` | VPC | Gratis |
| 5 | Elastic IP | IP fija para el NAT Gateway | `eip` | — | ~\$0.005 |
| 6 | NAT Gateway | Salida a Internet de la subred privada (descarga de modelos) | `nat` | Subred pública, EIP | ~\$0.045 |
| 7 | Route table pública | `0.0.0.0/0 → igw-*` | `rtb-public` | VPC, IGW | Gratis |
| 8 | Route table privada | `0.0.0.0/0 → nat-*` | `rtb-private` | VPC, NAT | Gratis |
| 9 | SG gateway | Entrada pública controlada (22/80/443, +4000/8000/8080 si `exponer_puertos_directos`) | `sg-gateway` | VPC | Gratis |
| 10 | SG workers | Entrada SOLO por referencia al SG del gateway (SG→SG) | `sg-workers` | VPC, SG gateway | Gratis |
| 11 | VPC Endpoint S3 (gateway) | Reduce tráfico por NAT hacia S3 | `vpce-s3` | VPC, route tables privadas | Gratis |

**Coste dominante:** NAT Gateway + su EIP, ~\$0.05/hora ≈ \$36/mes, **corriendo esté o no en uso**. Es la razón de ser de `destroy_infra.py` y de `--scan-orphans`.

## Cálculo de CIDR (determinista)

`compute_subnet_cidrs()` (`scripts/aws_network.py:172`) subdivide el `vpc_cidr` (típicamente `/16`) en bloques `/20`:

- Toma `max(vpc_prefix + 4, 20)` como prefijo de subred (para un `/16`, eso da `/20`: 16 bloques de 4096 IPs).
- Las primeras `az_count` subredes `/20` (empezando en el bloque `.0.`) son **públicas**.
- Las siguientes `az_count`, empezando en la **mitad alta** del espacio disponible (offset = `total_bloques // 2`), son **privadas**.
- Es reproducible: la misma entrada siempre da la misma salida, y `provision()` puede volver a llamarlo en cada corrida sin necesitar recordar el resultado.

Ejemplo con `vpc_cidr: 10.0.0.0/16`, `azs: 2`:

```
Bloques /20 disponibles: 10.0.0.0/20, 10.0.16.0/20, ..., 10.0.240.0/20  (16 bloques)
Públicas:  10.0.0.0/20, 10.0.16.0/20      (bloques 0-1)
Privadas:  10.0.128.0/20, 10.0.144.0/20    (bloques 8-9, mitad alta)
```

Si el operador prefiere fijar los CIDR a mano, `red_y_aislamiento.subredes.publicas`/`privadas` los sobreescribe explícitamente (validado por `ConfigValidator._validate_red_auto`: deben estar contenidos en `vpc_cidr` y no solaparse entre sí).

## Selección de Availability Zone

`AwsNetworkManager._available_azs()` (línea 338): `describe_availability_zones` filtrando `state=available`, ordenadas alfabéticamente, se toman las primeras `azs`. Es determinista entre corridas (mismo orden siempre) mientras la disponibilidad de AZ en la cuenta no cambie.

> **Limitación conocida (no implementada):** el proyecto original contemplaba verificar que el tipo de instancia GPU del workload esté disponible en la AZ elegida (`describe_instance_type_offerings`) y elegir otra si no. Hoy esa verificación **no está implementada**; si una AZ no tiene el tipo de instancia solicitado, SkyPilot fallará al lanzar ese worker con un error nativo de AWS/SkyPilot (no de este proyecto). Workaround manual: fijar `azs: 1` con una región/AZ que sepas que tiene el tipo de instancia, o dejar que SkyPilot reintente (tiene su propia lógica de fallback entre zonas dentro de la región).

## Reglas de Security Group (exactas)

### `sg-sooniverse-<cliente>-<entorno>-gateway`

| Dirección | Protocolo | Puerto | Origen | Condición |
|---|---|---|---|---|
| In | TCP | 22 | `cidr_admin_ssh` | Siempre (con `[WARNING]` ruidoso si es `0.0.0.0/0`) |
| In | TCP | 80 | `cidr_permitido_gateway` | Siempre |
| In | TCP | 443 | `cidr_permitido_gateway` | Solo si `gateway.tls.habilitado` |
| In | TCP | 4000, 8000, 8080 | `cidr_permitido_gateway` | Solo si `gateway.exponer_puertos_directos: true` |
| Out | all | all | `0.0.0.0/0` | Siempre |

### `sg-sooniverse-<cliente>-<entorno>-workers`

| Dirección | Protocolo | Puerto | Origen | Nota |
|---|---|---|---|---|
| In | TCP | 22 | SG del gateway (referencia, no CIDR) | SSH vía bastion |
| In | TCP | cada `workloads[].puerto` | SG del gateway | Tráfico LiteLLM→vLLM |
| In | TCP | cada `workloads[].puerto` | **sí mismo** (auto-referencia) | Comunicación inter-nodo (tensor/pipeline parallel); se aplica siempre, es un no-op inofensivo si no se usa |
| Out | all | all | `0.0.0.0/0` | Salida vía NAT |

Implementado en `ensure_security_groups()` / `_sync_ingress_sg_rules()` (`scripts/aws_network.py:546-651`). Las reglas se **sincronizan por diff** (se añaden las que faltan, se revocan las que sobran) en cada `provision()`, no se borran y recrean todas.

## Integración con SkyPilot (verificado contra la versión instalada, 0.13.0)

- `vpc_name`: string o `null` únicamente (SkyPilot empareja por tag `Name`, confirmado leyendo `sky/provision/aws/config.py::get_vpc_id_by_name`).
- `security_group_name`: string, o lista de mapas `{glob-de-cluster: nombre}` (primera coincidencia gana) — confirmado en `sky/utils/schemas.py`. Este proyecto usa siempre la forma string simple, porque cada rol (gateway/workers) ya tiene su propio archivo `.sky_config_*.yaml`.
- SkyPilot empareja/crea SGs **por nombre** (`group_name`), no por `sg-id` — confirmado en `sky/provision/aws/config.py::_get_or_create_vpc_security_group`.
- Si SkyPilot recibe un `security_group_name` personalizado, **no auto-abre los puertos declarados en `resources.ports`** sobre ese SG (solo emite un warning) — es justamente el comportamiento que este proyecto necesita, porque el SG ya lo gestiona `AwsNetworkManager` con reglas más finas que "abrir el puerto a todos".
- Las claves SSH que SkyPilot genera (`~/.sky/generated/ssh-keys/sky-key-*` y `<cluster>.key`) son de ámbito de cuenta/usuario local, compartidas entre despliegues — **el destroy nunca las toca**.

## Aislamiento entre clientes (CIDR)

`check_cidr_isolation()` (`scripts/generate_infra.py:818`) consulta, antes de cada `provision()`, los despliegues activos en PostgreSQL para la misma región y compara `vpc_cidr`. Si se solapan, imprime un `[WARNING]` (no aborta — dos VPCs con CIDR solapado son perfectamente funcionales mientras nunca se interconecten vía peering) y sugiere el primer `/16` libre dentro de `10.0.0.0/8` (`_suggest_free_cidr()`, línea 809).
