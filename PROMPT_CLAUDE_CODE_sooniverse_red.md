# PROMPT PARA CLAUDE CODE — `sooniverse_infra` · Fase 2: Red autogestionada, comunicación Gateway↔Workers y ciclo de vida completo

> Pega este archivo completo como primer mensaje en Claude Code, dentro del repositorio `sooniverse_infra`.

---

## 0. Quién eres y qué estás construyendo

Eres el ingeniero de plataforma responsable de `sooniverse_infra`, un **automatizador de infraestructura multi-cliente sobre AWS** que despliega cargas de IA (LLMs vía vLLM) detrás de un gateway unificado OpenAI-compatible.

El proyecto ya funciona parcialmente:

- ✅ Un contrato único `config_global.yaml` que un generador Python traduce a manifiestos SkyPilot y stacks Docker Compose.
- ✅ `scripts/generate_infra.py` levanta el **Nodo Gateway** (LiteLLM + Open WebUI + Django de métricas + Redis + nginx) y los **clústeres worker** vLLM con SkyPilot.
- ✅ `scripts/sync_endpoints.py` descubre IPs privadas de los workers y recarga el balanceador de LiteLLM.
- ✅ `scripts/db_setup.py` ingesta `database/init_schema.sql` en PostgreSQL (esquema `sooniverse`, convive con las tablas nativas `public."LiteLLM_*"`).
- ❌ **La VPC, subredes, Internet Gateway, NAT Gateway, route tables y Security Groups se crean A MANO desde la consola de AWS** (ver `Manual_VPC_SecurityGroup.md`). Este es el hueco principal.
- ❌ No existe un `destroy` real: hoy se hace `sky down` y luego borrado manual de la VPC, con riesgo de dejar NAT Gateways y Elastic IPs cobrando.
- ❌ La comunicación Gateway↔Workers depende de reglas de SG creadas a mano y de la suerte con los SG que SkyPilot genera por su cuenta.
- ❌ nginx existe pero no está verificado como **única puerta de entrada** (streaming SSE, WebSockets de Open WebUI, subpaths).

**Tu misión** es cerrar esos cuatro huecos de forma que **toda la infraestructura se cree, se modifique y se destruya exclusivamente mediante funciones Python del propio proyecto**, de manera idempotente, segura y multi-cliente.

---

## 1. Reglas inflexibles (léelas dos veces)

1. **Nada de Terraform, CloudFormation, CDK ni Pulumi.** Todo con `boto3` (ya viene con `skypilot[aws]`) desde funciones Python del repo. Si necesitas una dependencia nueva, justifícala y añádela a `requirements`.
2. **El `destroy` sólo puede eliminar recursos que este sistema creó.** Antes de borrar cualquier recurso AWS debes verificar **dos condiciones simultáneas**: (a) está registrado en la tabla de estado en PostgreSQL con el `deployment_id` correspondiente, y (b) sus tags AWS coinciden con ese `deployment_id`. Si falla cualquiera, **no se borra**: se registra como huérfano/ajeno y se reporta.
3. **Recursos preexistentes se adoptan, no se apropian.** Si el usuario apunta a una VPC que ya existía y no fue creada por nosotros, se marca `managed_by_us = false` y el destroy jamás la toca.
4. **Nunca tocar la VPC por defecto de AWS** ni recursos sin nuestros tags. Añade una guarda explícita que aborte si el `vpc_id` objetivo tiene `IsDefault = true`.
5. **Idempotencia total.** Ejecutar `provision` dos veces seguidas no debe crear recursos duplicados ni fallar. Ejecutar `destroy` dos veces no debe fallar.
6. **Privacidad por diseño (requisito de producto, no preferencia).** Ninguna tabla del esquema `sooniverse` puede almacenar prompts ni respuestas. Mantén `store_prompts_in_spend_logs: false` y `turn_off_message_logging: true` en LiteLLM.
7. **El DDL vive solo en `database/`.** Los modelos Django son `managed = False`. Si cambias la estructura de datos, el cambio va en un `.sql` idempotente dentro de `database/`, nunca en una migración de Django.
8. **No edites artefactos generados**: `.sky_generated.*.yaml`, `.sky_config_workers.yaml`, `.sooniverse_endpoints.json`, `docker_images/gateway/litellm_config.yaml`. Se regeneran.
9. **Multi-cliente desde el primer commit.** Ningún nombre de recurso, clúster, SG o clave de estado puede ser global: todo lleva `{cliente.id}-{entorno}` (y `region` donde aplique). Dos clientes deben poder desplegarse en paralelo en la misma cuenta AWS sin colisionar.
10. **Compatibilidad hacia atrás.** Quien hoy tiene su VPC creada a mano debe poder seguir operando sin cambios, activando el modo `gestion_red: existente`.
11. **Todo comando destructivo requiere confirmación explícita** (`--yes` o escribir el `cliente.id`) y ofrece `--dry-run` que imprime el plan sin tocar nada.
12. **Idioma:** código y nombres de variables en inglés; comentarios, logs de usuario y documentación en **español**.

---

## 2. Fase 0 — Reconocimiento obligatorio (no escribas código todavía)

Antes de tocar nada, explora el repositorio real. La documentación que tienes puede estar desactualizada respecto al código.

```bash
git status && git log --oneline -15
find . -maxdepth 3 -name "*.py" -not -path "./venv/*" | head -60
ls -la database/ scripts/ docker_images/gateway/ django_metrics/
cat config_global.yaml
cat .env.example
sed -n '1,120p' scripts/generate_infra.py
grep -rn "vpc_name\|use_internal_ips\|security_group\|ssh_proxy_command" --include="*.py" --include="*.yaml" .
python -c "import skypilot" 2>/dev/null; pip show skypilot 2>/dev/null | head -3
```

Luego **entrega un informe corto en el chat** (no un archivo) con:

- Estructura real de `generate_infra.py`: funciones existentes, dónde se cargan y validan el contrato, dónde se emite el YAML de SkyPilot, dónde se dispara `sky launch`.
- Cómo se construye hoy `.sky_config_workers.yaml` y qué claves de SkyPilot usa.
- Qué contiene `database/init_schema.sql` (lista de tablas, funciones y vistas).
- Qué expone hoy `docker_images/gateway/nginx/default.conf`.
- Versión instalada de SkyPilot y **qué claves de configuración de red soporta esa versión concreta** (`vpc_name`, `use_internal_ips`, `security_group_name`, `ssh_proxy_command`). Verifícalo contra el código instalado o la documentación, **no lo asumas**.
- Cualquier discrepancia entre la documentación (`README.md`, `MANUAL_DESPLIEGUE.md`) y el código real.

**Espera mi confirmación antes de pasar a la Fase 1.** Si detectas que algún supuesto de este prompt es incorrecto, dilo ahora y propón la corrección.

---

## 3. Fase 1 — Módulo de red AWS (`scripts/aws_network.py`)

### 3.1 Alcance

Un módulo nuevo, autocontenido y testeable, que gestiona el ciclo de vida completo de la capa de red.

Recursos que debe crear:

| # | Recurso | Notas |
|---|---|---|
| 1 | VPC | CIDR configurable, `enableDnsSupport` + `enableDnsHostnames` = true |
| 2 | Subred(es) pública(s) | Una por AZ solicitada, `MapPublicIpOnLaunch = true` |
| 3 | Subred(es) privada(s) | Una por AZ solicitada, sin IP pública |
| 4 | Internet Gateway | Adjunto a la VPC |
| 5 | Elastic IP | Una por NAT Gateway |
| 6 | NAT Gateway | En la subred **pública**; modo `single` (una para todas las AZ) o `per-az` |
| 7 | Route table pública | `0.0.0.0/0 → igw-*`, asociada a subredes públicas |
| 8 | Route table privada | `0.0.0.0/0 → nat-*`, asociada a subredes privadas |
| 9 | SG gateway | Entrada según contrato; salida abierta |
| 10 | SG workers | Entrada **solo por referencia SG→SG** desde el SG del gateway; salida abierta |
| 11 | VPC Endpoints (opcional) | Gateway endpoint de S3 (gratis, reduce coste de NAT). ECR/`logs` como interface endpoints solo si se activan explícitamente (cuestan) |

### 3.2 Convención de tags (es el mecanismo de propiedad — no la improvises)

**Todos** los recursos creados llevan exactamente estos tags:

```
sooniverse:managed        = "true"
sooniverse:client-id      = <cliente.id>
sooniverse:environment    = <cliente.entorno>
sooniverse:deployment-id  = <deployment_id>     # UUID v4 generado al primer provision
sooniverse:component      = vpc | subnet-public | subnet-private | igw | eip | nat |
                            rtb-public | rtb-private | sg-gateway | sg-workers | vpce-s3
sooniverse:created-at     = <ISO-8601 UTC>
Name                      = sooniverse-<client>-<env>-<component>[-<az>]
```

Además, mezcla los `red_y_aislamiento.tags_obligatorios` del contrato (los del cliente ganan solo si no colisionan con el prefijo `sooniverse:`; si colisionan, aborta con error claro).

### 3.3 API pública del módulo

```python
@dataclass(frozen=True)
class NetworkSpec:
    client_id: str
    environment: str
    region: str
    vpc_cidr: str
    az_count: int
    public_subnet_cidrs: list[str]
    private_subnet_cidrs: list[str]
    nat_mode: str                    # "single" | "per-az" | "none"
    enable_s3_endpoint: bool
    admin_cidrs: list[str]           # SSH al gateway
    public_cidrs: list[str]          # HTTP/HTTPS al gateway
    gateway_public_ports: list[int]
    worker_ports: list[int]          # p.ej. [8007] derivado de workloads[].puerto
    extra_tags: dict[str, str]

@dataclass(frozen=True)
class NetworkOutputs:
    deployment_id: str
    vpc_id: str
    vpc_name: str
    availability_zones: list[str]
    public_subnet_ids: list[str]
    private_subnet_ids: list[str]
    internet_gateway_id: str
    nat_gateway_ids: list[str]
    elastic_ip_allocation_ids: list[str]
    public_route_table_id: str
    private_route_table_ids: list[str]
    sg_gateway_id: str
    sg_gateway_name: str
    sg_workers_id: str
    sg_workers_name: str
    managed_by_us: bool

class AwsNetworkManager:
    def __init__(self, spec: NetworkSpec, state: InfraStateStore,
                 session: boto3.Session | None = None,
                 deployment_id: str | None = None) -> None: ...

    # --- creación (todas idempotentes, prefijo ensure_) ---
    def ensure_vpc(self) -> str: ...
    def ensure_subnets(self) -> tuple[list[str], list[str]]: ...
    def ensure_internet_gateway(self) -> str: ...
    def ensure_nat_gateways(self) -> list[str]: ...
    def ensure_route_tables(self) -> None: ...
    def ensure_vpc_endpoints(self) -> list[str]: ...
    def ensure_security_groups(self) -> tuple[str, str]: ...

    # --- orquestación ---
    def provision(self, dry_run: bool = False) -> NetworkOutputs: ...
    def adopt_existing(self, vpc_name_or_id: str) -> NetworkOutputs: ...
    def status(self) -> dict: ...
    def plan_destroy(self) -> list[PlannedDeletion]: ...
    def destroy(self, dry_run: bool = False, force: bool = False) -> DestroyReport: ...
    def scan_orphans(self) -> list[dict]: ...
```

### 3.4 Requisitos de implementación

- **Cliente boto3** con reintentos: `botocore.config.Config(retries={"max_attempts": 10, "mode": "adaptive"})`. Región tomada del contrato, credenciales del entorno/`AWS_PROFILE`.
- **Búsqueda antes de crear**: cada `ensure_*` primero hace `describe_*` filtrando por los tags `sooniverse:deployment-id` + `sooniverse:component`. Si existe y está en estado sano, lo reutiliza y lo registra; si existe pero en estado terminal (`deleted`, `failed`), lo purga del estado y recrea.
- **Waiters reales, no `sleep`**: usa los waiters de boto3 (`vpc_available`, `nat_gateway_available`, `nat_gateway_deleted`) y para lo que no tenga waiter, un bucle con backoff exponencial y timeout configurable (por defecto 300 s para NAT).
- **Cálculo automático de CIDRs**: si el contrato solo da `vpc_cidr` y `azs`, subdivide determinísticamente (p. ej. `/16` → `/20` públicas empezando en `.0.` y privadas a partir de la mitad alta). El resultado debe ser reproducible y estar documentado. Permite override manual explícito.
- **Selección de AZ**: `describe_availability_zones` filtrando `state=available` y ordenando por nombre, para que sea determinista entre corridas. Verifica además que el tipo de instancia GPU del workload esté disponible en esa AZ (`describe_instance_type_offerings`) y, si no, elige otra AZ y **avisa en el log**.
- **Registro en estado en el momento exacto de la creación**: escribe la fila en PostgreSQL *inmediatamente después* de que la API de AWS devuelva el ID, **antes** de esperar a que esté disponible. Así, si el proceso muere a media creación, el destroy sigue sabiendo qué limpiar.
- **Logging estructurado** con el módulo `logging`, no `print`. Prefijos consistentes: `[RED]`, `[RED:VPC]`, `[RED:NAT]`, `[ESTADO]`, `[DESTROY]`. Cada creación registra el ID y el tiempo empleado.

### 3.5 Reglas de Security Group

`sg-sooniverse-<client>-<env>-gateway`:

| Dirección | Protocolo | Puerto | Origen/Destino | Condición |
|---|---|---|---|---|
| In | TCP | 22 | `red_y_aislamiento.cidr_admin_ssh` (nuevo campo, default `0.0.0.0/0` con **warning ruidoso**) | siempre |
| In | TCP | 80 | `cidr_permitido_gateway` | siempre |
| In | TCP | 443 | `cidr_permitido_gateway` | si TLS activado |
| In | TCP | 4000, 8000, 8080 | `cidr_permitido_gateway` | **solo si** `gateway.exponer_puertos_directos: true` |
| Out | all | all | `0.0.0.0/0` | siempre |

`sg-sooniverse-<client>-<env>-workers`:

| Dirección | Protocolo | Puerto | Origen | Nota |
|---|---|---|---|---|
| In | TCP | 22 | **referencia al SG del gateway** | SSH vía bastion |
| In | TCP | cada `workloads[].puerto` | **referencia al SG del gateway** | vLLM |
| In | TCP | cada `workloads[].puerto` | **referencia a sí mismo** | solo si algún workload tiene `replicas > 1` y requiere comunicación inter-nodo (tensor/pipeline parallel) |
| Out | all | all | `0.0.0.0/0` | salida vía NAT |

> Usa **siempre** `UserIdGroupPairs` (SG→SG), nunca CIDR, para el acceso a workers. Es lo que permite crear la regla antes de que exista el gateway.
> Importante para el destroy: las referencias SG→SG crean dependencias. Antes de borrar SGs hay que **revocar primero todas las reglas** de ambos, y solo entonces eliminarlos.

### 3.6 Integración con SkyPilot (verifica contra la versión instalada)

El módulo debe producir la configuración que SkyPilot consume. Genera/actualiza `.sky_config_workers.yaml` (y el equivalente para el gateway) con, al menos:

```yaml
aws:
  vpc_name: <nombre o id de la VPC, según lo que soporte la versión instalada>
  use_internal_ips: true
  security_group_name: <nombre del SG>     # SkyPilot espera el NOMBRE, no el sg-id — CONFIRMA
  ssh_proxy_command: ssh -W %h:%p -i <key> -o StrictHostKeyChecking=no ubuntu@<ip_publica_gateway>
```

Puntos a resolver explícitamente durante la implementación:

- `security_group_name` en SkyPilot admite string o mapa `glob-de-cluster → nombre-sg`. Usa el mapa para asignar `sg-...-gateway` al clúster del gateway y `sg-...-workers` a los clústeres worker. **Verifica el formato exacto en la versión instalada antes de escribirlo.**
- Si SkyPilot, pese a recibir un SG propio, sigue creando sus propios `sky-sg-*` dentro de nuestra VPC, esos SG **son consecuencia de nuestro despliegue** y el destroy debe eliminarlos (ver 5.3). Detéctalos por VPC + prefijo de nombre, y regístralos en el estado como `discovered_dependency` para que el borrado siga siendo trazable.
- Las claves SSH que crea SkyPilot (`sky-key-*`) son de ámbito de cuenta y compartidas entre despliegues: **no las borres** por defecto. Menciona esto en la documentación.

---

## 4. Fase 2 — Estado en PostgreSQL

### 4.1 Nuevo archivo `database/002_infra_state.sql` (idempotente)

No reescribas `init_schema.sql`: crea un archivo nuevo y haz que `db_setup.py` aplique **todos** los `.sql` de `database/` en orden lexicográfico. Renombra el existente a `001_init_schema.sql` manteniendo un symlink o compatibilidad con el nombre viejo si el código lo referencia (revísalo).

Tablas mínimas (ajusta nombres a la convención real del repo):

```sql
CREATE TABLE IF NOT EXISTS sooniverse.infra_deployment (
    id                BIGGENERATED... /* usa el patrón del repo */,
    deployment_id     UUID        NOT NULL UNIQUE,
    client_id         TEXT        NOT NULL,
    environment       TEXT        NOT NULL,
    region            TEXT        NOT NULL,
    cloud             TEXT        NOT NULL DEFAULT 'aws',
    status            TEXT        NOT NULL,   -- planning|creating|active|degraded|destroying|destroyed|error
    managed_network   BOOLEAN     NOT NULL DEFAULT TRUE,
    config_hash       TEXT,                   -- sha256 del config_global.yaml efectivo
    config_snapshot   JSONB,                  -- contrato completo, sin secretos
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    destroyed_at      TIMESTAMPTZ,
    last_error        TEXT
);

-- Solo un despliegue activo por (cliente, entorno, región)
CREATE UNIQUE INDEX IF NOT EXISTS ux_infra_deployment_active
    ON sooniverse.infra_deployment (client_id, environment, region)
    WHERE status NOT IN ('destroyed', 'error');

CREATE TABLE IF NOT EXISTS sooniverse.infra_resource (
    id              ...,
    deployment_id   UUID        NOT NULL REFERENCES sooniverse.infra_deployment(deployment_id) ON DELETE CASCADE,
    resource_type   TEXT        NOT NULL,   -- vpc|subnet|igw|eip|nat|route_table|security_group|vpc_endpoint|sky_cluster
    component       TEXT        NOT NULL,   -- el tag sooniverse:component
    aws_id          TEXT,                   -- vpc-..., subnet-..., sg-...
    aws_arn         TEXT,
    region          TEXT        NOT NULL,
    availability_zone TEXT,
    parent_aws_id   TEXT,
    delete_order    INT         NOT NULL,   -- orden inverso de destrucción
    managed_by_us   BOOLEAN     NOT NULL DEFAULT TRUE,
    state           TEXT        NOT NULL,   -- creating|active|deleting|deleted|orphan|adopted|error
    attributes      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at      TIMESTAMPTZ,
    UNIQUE (deployment_id, resource_type, aws_id)
);

CREATE TABLE IF NOT EXISTS sooniverse.infra_event (
    id              ...,
    deployment_id   UUID        NOT NULL,
    phase           TEXT        NOT NULL,   -- network|gateway|workers|endpoints|destroy
    action          TEXT        NOT NULL,
    resource_ref    TEXT,
    status          TEXT        NOT NULL,   -- started|ok|warning|error
    message         TEXT,
    duration_ms     INT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Amplía también `sooniverse.worker_node` (ya existe) con: `deployment_id UUID`, `cluster_name TEXT`, `subnet_id TEXT`, `security_group_id TEXT`, `last_health_check TIMESTAMPTZ`, `health_status TEXT`. Usa `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`.

Añade vistas de lectura: `v_infra_deployment_summary` (recursos por despliegue, coste estimado, edad) y `v_infra_orphans`.

### 4.2 `scripts/infra_state.py`

Capa de acceso (`InfraStateStore`), sin ORM, con `psycopg2` como el resto del repo:

```python
class InfraStateStore:
    def open_deployment(self, client_id, environment, region, config_hash, config_snapshot) -> str
    def get_active_deployment(self, client_id, environment, region) -> dict | None
    def set_deployment_status(self, deployment_id, status, error=None) -> None
    def record_resource(self, deployment_id, **fields) -> None     # UPSERT
    def mark_resource_state(self, deployment_id, aws_id, state) -> None
    def list_resources(self, deployment_id, only_active=True) -> list[dict]
    def resources_in_delete_order(self, deployment_id) -> list[dict]
    def log_event(self, deployment_id, phase, action, status, message=None, duration_ms=None) -> None
    def close_deployment(self, deployment_id) -> None
```

Requisitos:

- Toda escritura de estado es **transaccional** y ocurre en el mismo commit que su evento de auditoría.
- Si PostgreSQL no está alcanzable al arrancar un `provision`, **aborta antes de crear nada en AWS** con un mensaje claro. Nunca crees recursos que no puedas registrar.
- Fallback de emergencia: además del estado en BD, escribe un espejo local `.sooniverse_state.<client>-<env>.json` tras cada cambio. Sirve para recuperación manual si la BD se pierde; documenta que la fuente de verdad es PostgreSQL.
- Los secretos (`LITELLM_MASTER_KEY`, `DB_PASSWORD`, etc.) **jamás** entran en `config_snapshot`. Filtra por lista de claves sensibles.

### 4.3 Panel Django (opcional pero deseable)

Añade modelos `managed = False` para `infra_deployment`, `infra_resource` e `infra_event`, y una vista `/metrics/infra/` que liste despliegues activos, sus recursos, edad y coste estimado del NAT/EIP acumulado. Reutiliza `theme-sooniverse.css` + `layout.css` sin añadir dependencias de CDN (la VPC puede no tener salida).

---

## 5. Fase 3 — Orquestación del ciclo de vida

### 5.1 Contrato ampliado (`config_global.yaml`)

Añade la sección de red autogestionada, manteniendo compatibilidad con la existente:

```yaml
red_y_aislamiento:
  region: "us-east-1"
  gestion_red: "auto"                 # [Enum] auto | existente
                                      #   auto      = el sistema crea y destruye la VPC
                                      #   existente = comportamiento actual (VPC manual)
  vpc_name: null                      # en modo auto se autogenera: sooniverse-<cliente>-<entorno>-vpc
  vpc_cidr: "10.0.0.0/16"
  azs: 1                              # nº de AZ; >1 solo si necesitas HA real
  subredes:
    publicas: null                    # null = cálculo automático determinista
    privadas: null
  nat_gateway:
    modo: "single"                    # single | per-az | none
    timeout_segundos: 300
  vpc_endpoints:
    s3: true                          # gateway endpoint, gratis
    ecr: false                        # interface endpoints, tienen coste
  workers_en_subred_privada: true
  cidr_permitido_gateway: "0.0.0.0/0"
  cidr_admin_ssh: "0.0.0.0/0"         # ← restringir en producción
  security_group_workers: null        # null en modo auto (lo crea el sistema)
  tags_obligatorios: {}

gateway:
  exponer_puertos_directos: false     # false = solo 80/443 vía nginx (recomendado)
  tls:
    habilitado: false
    modo: "self-signed"               # self-signed | letsencrypt | acm
    dominio: null
    email: null
```

Extiende `ConfigValidator` con: `gestion_red ∈ {auto, existente}`; `nat_gateway.modo ∈ {single, per-az, none}`; validación de que los CIDR de subred estén contenidos en `vpc_cidr` y no se solapen; error explícito si `workers_en_subred_privada: true` y `nat_gateway.modo: none` sin `vpc_endpoints` suficientes; `azs >= 1`.

### 5.2 Orden de aprovisionamiento (`generate_infra.py --run`)

Refactoriza el orquestador para que sea una máquina de fases explícita, cada una con su registro en `infra_event`:

```
FASE 0  validate     Cargar y validar contrato · calcular config_hash
FASE 1  state        Abrir/recuperar deployment en PostgreSQL · aplicar database/*.sql si AUTO_INIT_DB
FASE 2  network      AwsNetworkManager.provision()  ← NUEVO
                     VPC → subredes → IGW → EIP → NAT → route tables → endpoints → SGs
FASE 3  render       Generar .sky_generated.gateway.yaml y .sky_config_*.yaml con los IDs reales
FASE 4  gateway      sky launch del gateway en subred PÚBLICA con sg-*-gateway
                     → capturar IP pública y privada, registrarlas en el estado
FASE 5  bastion      Regenerar .sky_config_workers.yaml con ssh_proxy_command apuntando al gateway
FASE 6  workers      sky launch de cada clúster worker en subred PRIVADA con sg-*-workers
FASE 7  endpoints    sync_endpoints.py --apply → descubrir IPs privadas, poblar worker_node,
                     renderizar litellm_config.yaml, push al gateway, reload del contenedor litellm
FASE 8  verify       Comprobaciones de conectividad y salud (ver 5.5)
FASE 9  report       Resumen final con URLs, IDs de recursos y coste estimado por hora
```

Cada fase debe:
- Ser reanudable: si el estado dice que ya está `active`, se salta con un log `[SKIP]`.
- Poder ejecutarse aislada: `--only network|gateway|workers|endpoints|verify`.
- Dejar el `deployment.status` correcto ante fallo (`error` + `last_error`), nunca colgado en `creating`.

### 5.3 Destrucción (`scripts/destroy_infra.py`)

```bash
python scripts/destroy_infra.py --dry-run
python scripts/destroy_infra.py --yes
python scripts/destroy_infra.py --only network --yes
python scripts/destroy_infra.py --client acme --env prod --yes
python scripts/destroy_infra.py --scan-orphans
```

**Orden estricto de destrucción** (inverso de la creación; cada paso espera a que el anterior termine de verdad):

1. **Clústeres SkyPilot worker** (`sky down` de cada uno). Primero los workers: sin el gateway como bastion, SkyPilot pierde el SSH a instancias sin IP pública.
2. **Clúster SkyPilot gateway** (`sky down`).
3. **Verificación de ENIs**: `describe_network_interfaces` filtrando por las subredes. Si queda alguna, espera con backoff (los ENI de instancias terminadas tardan). Timeout → reportar y abortar limpiamente **sin borrar la VPC**.
4. **Security Groups**: revocar **todas** las reglas de ingreso/egreso de todos los SG de la VPC (para romper referencias SG→SG), luego borrar. Incluye los `sky-sg-*` que SkyPilot haya creado dentro de nuestra VPC. **Nunca** borres el SG `default` de la VPC (no se puede; se va con la VPC).
5. **VPC Endpoints**.
6. **NAT Gateways**: `delete_nat_gateway` + waiter `nat_gateway_deleted`. Es lento (puede tardar minutos): usa timeout generoso y logging de progreso.
7. **Elastic IPs**: `release_address` de cada allocation. **Este es el paso que más se olvida y sigue cobrando.** Verifica después con `describe_addresses` que no queda ninguna nuestra.
8. **Route tables**: desasociar (`disassociate_route_table`) y borrar las no-principales. La *main* route table se va con la VPC.
9. **Internet Gateway**: `detach_internet_gateway` + `delete_internet_gateway`.
10. **Subredes**.
11. **VPC**.
12. **Cierre de estado**: marcar cada recurso `deleted` con `deleted_at`, y el deployment `destroyed`.

Requisitos del destroy:

- **`--dry-run` imprime una tabla** con: tipo, ID, nombre, orden de borrado, y si es `managed_by_us`. No hace ni una llamada mutante.
- **Reintentos ante `DependencyViolation`**: hasta N intentos con backoff. Si persiste, reporta *qué* recurso tiene la dependencia (haz el `describe` que lo revele: ENIs, instancias, endpoints) en lugar de un error genérico.
- **Continuar ante fallos parciales**: si un recurso no se puede borrar, márcalo `error` y sigue con el resto; al final devuelve un `DestroyReport` con éxitos, fallos y recursos que requieren intervención manual, más los comandos AWS CLI exactos para resolverlos a mano.
- **`--scan-orphans`**: busca en la región recursos con tag `sooniverse:managed=true` que **no** estén en la BD, o cuyo deployment esté `destroyed`. Los lista con su antigüedad y coste estimado. Con `--purge-orphans --yes` los elimina. Este es el seguro contra estados corruptos.
- **Nunca** borres la base de datos PostgreSQL ni el esquema. El histórico de métricas, API Keys y auditoría sobrevive a la destrucción de la infraestructura. Documéntalo.

### 5.4 Modificación (`--run` sobre un despliegue existente)

Volver a ejecutar `--run` tras cambiar el contrato debe converger, no duplicar:

- Cambio en `replicas` → relanzar solo el clúster worker afectado + `sync_endpoints`.
- Cambio en `cidr_permitido_gateway`, `cidr_admin_ssh` o puertos → recalcular reglas de SG: **añadir las que faltan y revocar las que sobran** (diff, no "borrar todo y recrear").
- Cambio en `vpc_cidr`, `azs` o `nat_gateway.modo` → **no es modificable en caliente**: detéctalo comparando con `config_snapshot` y aborta con un mensaje que explique que requiere destroy + provision, indicando exactamente qué campo cambió.
- Cambio en `load_balancing_strategy` o en `workloads[].nombre_publico` → solo re-render de `litellm_config.yaml` + reload del contenedor.

Implementa esto como una función `plan_changes(current_snapshot, new_config) -> ChangePlan` que clasifique cada diferencia en `no-op | in-place | recreate-cluster | requires-destroy`, y muéstrala antes de aplicar.

### 5.5 Verificación automática (FASE 8)

Un módulo `scripts/verify_deployment.py` que ejecute y registre en `infra_event`:

| # | Comprobación | Método |
|---|---|---|
| 1 | La subred privada rutea a NAT | `describe_route_tables` → hay `0.0.0.0/0 → nat-*` |
| 2 | La subred pública rutea a IGW | idem con `igw-*` |
| 3 | Los workers **no** tienen IP pública | `describe_instances` → `PublicIpAddress is None` |
| 4 | El SG de workers no acepta `0.0.0.0/0` en el puerto vLLM | inspección de reglas |
| 5 | El gateway alcanza cada worker | desde el gateway: `curl -sf --max-time 5 http://<ip>:<puerto>/health` |
| 6 | El worker tiene salida a Internet (NAT vivo) | desde el worker vía bastion: `curl -sfI https://huggingface.co` |
| 7 | LiteLLM lista los modelos esperados | `GET /v1/models` con la master key |
| 8 | El pool tiene tantos endpoints sanos como réplicas | `GET /health` de LiteLLM |
| 9 | Petición end-to-end responde | `POST /v1/chat/completions` con `max_tokens: 16` |
| 10 | nginx sirve `/`, `/v1/`, `/panel/` y `/healthz` en el puerto 80 | `curl` desde fuera |
| 11 | La BD registra los workers | `SELECT` en `sooniverse.worker_node` |

Salida: tabla con ✅/❌ por comprobación y código de salida distinto de cero si alguna crítica falla. Debe poder ejecutarse suelto: `python scripts/verify_deployment.py`.

---

## 6. Fase 4 — Comunicación Gateway ↔ Workers

### 6.1 Plano de red

```
Internet ──80/443──▶ [nginx] ──▶ Open WebUI :8080   (subred pública)
                        │
                        ├──▶ LiteLLM :4000
                        │        │
                        │        └── HTTP privado ──▶ worker-N 10.0.x.y:8007  (subred privada)
                        │
                        └──▶ Django :8000

SSH: tu máquina ──▶ gateway (IP pública) ──ProxyCommand──▶ workers (sin IP pública)
Salida de workers a Internet: ──▶ NAT Gateway ──▶ IGW
```

### 6.2 Trabajo requerido

1. **Bastion fiable.** El `ssh_proxy_command` debe construirse con la IP pública real del gateway y la clave que SkyPilot use realmente (localízala, no la asumas). Añade `-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null` y un `ConnectTimeout`. Si el gateway se recrea y cambia de IP, regenera el archivo automáticamente antes de tocar los workers.
2. **Descubrimiento robusto de endpoints.** Mantén la cascada actual (API Python de SkyPilot → marcador `SOONIVERSE_WORKER_READY=<workload>|<ip>|<puerto>` en logs → `sky status --ip`) y **añade un cuarto método**: `describe_instances` filtrando por tag de clúster y subred privada, tomando `PrivateIpAddress`. Es el más fiable porque no depende del estado interno de SkyPilot.
3. **Persistencia del inventario.** Cada endpoint descubierto se escribe en `sooniverse.worker_node` con `deployment_id`, `cluster_name`, `subnet_id`, `security_group_id`, IP privada, puerto, `workload_id`, `model_public_name`, peso y `health_status`.
4. **Health check activo antes de publicar.** No metas un worker en `litellm_config.yaml` hasta que responda `/health`. Los que no respondan se registran como `unhealthy` y se reintentan; el reload se hace igual con los sanos, sin bloquear el despliegue completo.
5. **Reload sin downtime.** Mantén el comportamiento actual: recargar **solo** el contenedor `litellm`, sin tocar Open WebUI, el panel ni las sesiones activas.
6. **Reconciliación periódica (opcional, deseable).** Un comando `sync_endpoints.py --watch --interval 60` que detecte workers caídos o IPs cambiadas tras un `sky stop`/`sky start` y re-sincronice. Documenta que es opcional y cómo dejarlo como systemd unit en el gateway.

---

## 7. Fase 5 — nginx como única puerta de entrada + interfaz de chat

Reescribe `docker_images/gateway/nginx/default.conf` (y hazlo **plantilla generada** desde el contrato, no estático, para que respete `tls.habilitado`, dominio y puertos).

### 7.1 Enrutado

| Ruta | Destino | Notas |
|---|---|---|
| `/` | `open-webui:8080` | Interfaz de chat. **Requiere upgrade a WebSocket** |
| `/v1/` | `litellm:4000` | API OpenAI-compatible. **Requiere streaming SSE sin buffering** |
| `/key/`, `/health` (LiteLLM) | `litellm:4000` | Gestión de keys, protegido |
| `/panel/` | `metrics:8000` | Django. Configura `FORCE_SCRIPT_NAME=/panel` y `STATIC_URL` acorde, o el panel romperá los enlaces |
| `/panel/static/` | ficheros estáticos | `collectstatic` a un volumen compartido y `alias` en nginx |
| `/healthz` | respuesta 200 propia de nginx | healthcheck del contenedor, sin depender de upstreams |

### 7.2 Directivas que **no** puedes olvidar

```nginx
# Streaming de chat completions (SSE): sin esto el chat "no responde" hasta terminar
proxy_buffering            off;
proxy_cache                off;
proxy_read_timeout         3600s;
proxy_send_timeout         3600s;
chunked_transfer_encoding  on;

# WebSockets de Open WebUI
proxy_http_version 1.1;
proxy_set_header   Upgrade    $http_upgrade;
proxy_set_header   Connection $connection_upgrade;   # via map, no "upgrade" literal

# Cabeceras de origen (Django y Open WebUI las necesitan tras un proxy)
proxy_set_header Host              $host;
proxy_set_header X-Real-IP         $remote_addr;
proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;

client_max_body_size 100M;   # subida de ficheros en Open WebUI
```

Añade el `map $http_upgrade $connection_upgrade { default upgrade; '' close; }` en el contexto `http`.

### 7.3 Endurecimiento

- Cuando `gateway.exponer_puertos_directos: false`, los contenedores `litellm`, `open-webui` y `metrics` **no publican puertos al host** en `docker-compose.yml` (solo `expose:` en la red interna de Docker). Solo nginx publica 80/443. El SG del gateway se ajusta en consecuencia. Este debe ser el **modo por defecto**.
- Verifica que Django tenga `ALLOWED_HOSTS` y `CSRF_TRUSTED_ORIGINS` correctos tras el proxy, y `SECURE_PROXY_SSL_HEADER` si hay TLS.
- Configura `ALLOWED_HOSTS` y el `WEBUI_URL` de Open WebUI con la IP/dominio real del gateway, inyectado desde el generador.
- TLS: implementa al menos `self-signed` (para pruebas, genera el cert en el `setup` del gateway) y deja el hook documentado para `letsencrypt` (certbot en un contenedor sidecar) y `acm` (requiere ALB, fuera de alcance de esta fase). Si `tls.habilitado: false`, mantén solo el 80 y añade un warning en el resumen final.

---

## 8. Fase 6 — Multi-cliente

1. **Selección de contrato por CLI**: `--config clients/acme/config_global.yaml`, con default al `config_global.yaml` de la raíz. Crea `clients/_ejemplo/config_global.yaml` documentado.
2. **Nombres derivados sin excepción**: clústeres `sooniverse-<cliente>-<entorno>-gw` / `-<workload>`; VPC `sooniverse-<cliente>-<entorno>-vpc`; SGs `sg-sooniverse-<cliente>-<entorno>-{gateway,workers}`. Valida longitud y caracteres permitidos por AWS (SG name ≤ 255, sin espacios; tag Name sin restricciones raras) y **normaliza** `cliente.id` (minúsculas, `[a-z0-9-]`, máx. 20 caracteres) abortando si no cumple.
3. **Aislamiento de CIDR entre clientes**: si dos despliegues activos en la misma región comparten CIDR, avisa (no es error si las VPC no están peered, pero impedirá peering futuro). Sugiere un CIDR libre consultando las VPC existentes.
4. **Artefactos por cliente**: los generados van a `.artifacts/<cliente>-<entorno>/` en vez de la raíz, para que dos clientes no se pisen. Mantén compatibilidad leyendo los antiguos si existen.
5. **Comando de inventario**: `python scripts/list_deployments.py` que muestre todos los despliegues de la BD con cliente, entorno, región, estado, nº de recursos, antigüedad y coste estimado acumulado.
6. **Aislamiento de credenciales**: soporta `AWS_PROFILE` por cliente vía el contrato (`red_y_aislamiento.aws_profile`) para cuentas separadas. Deja preparado (comentado y documentado, sin implementar) el hook de `AssumeRole` + External ID para el futuro modo BYOC.

---

## 9. Fase 7 — Pruebas

1. **Unitarias con `moto`** (`pip install moto[ec2]`) para todo `aws_network.py`: creación idempotente, tags correctos, reglas SG→SG, orden de destroy, guarda anti-VPC-default, negativa a borrar recursos ajenos. Sin llamadas reales a AWS.
2. **Unitarias de `plan_changes`**: cada tipo de cambio clasificado correctamente.
3. **Test de estado** contra una PostgreSQL efímera (o `psycopg2` + esquema `sooniverse_test`), verificando el índice único de despliegue activo y el `delete_order`.
4. **Validación del contrato**: casos válidos e inválidos de `ConfigValidator`.
5. **Smoke test de nginx**: levantar solo el stack del gateway con upstreams simulados y comprobar las rutas, el upgrade de WebSocket y el paso de SSE.
6. `make test` o `python -m pytest` debe correr todo sin credenciales de AWS ni red.

---

## 10. Fase 8 — Documentación (entregable de primera clase, no un apéndice)

El objetivo declarado por el cliente: **que una IA o un desarrollador nuevo entienda el sistema paso a paso, perfectamente.**

Crea `docs/` con:

| Archivo | Contenido |
|---|---|
| `docs/00_ARQUITECTURA.md` | Diagrama ASCII de red y de flujo de petición, componentes, decisiones de diseño y por qué (por qué bastion, por qué SG→SG, por qué NAT, alternativas descartadas y su coste) |
| `docs/01_FLUJO_DESPLIEGUE.md` | **El documento central.** Recorrido fase por fase (0→9). Para *cada* fase: qué función Python la ejecuta, en qué archivo y línea aproximada, qué lee del contrato, qué llamadas a AWS hace, qué escribe en la BD, qué artefacto genera, cuánto tarda, qué puede fallar y cómo se detecta |
| `docs/02_RED_AWS.md` | Cada recurso de red: qué es, por qué existe, cómo se calcula su CIDR, cómo se etiqueta, dependencias con los demás, coste por hora |
| `docs/03_ESTADO_Y_BD.md` | Modelo de datos completo: cada tabla, cada columna, ciclo de vida de los estados, diagrama de transiciones, y cómo reconstruir el estado si la BD se corrompe |
| `docs/04_DESTRUCCION.md` | Orden inverso paso a paso, por qué ese orden, qué se borra y qué NO (la BD, las claves SSH de SkyPilot), gestión de huérfanos, checklist de verificación de costes cero |
| `docs/05_MULTICLIENTE.md` | Cómo dar de alta un cliente nuevo de cero, convenciones de nombres, aislamiento, límites conocidos |
| `docs/06_RUNBOOK.md` | Troubleshooting: síntoma → causa → diagnóstico → solución. Migra y amplía la sección 10 de `MANUAL_DESPLIEGUE.md` con los fallos nuevos de red |
| `docs/07_REFERENCIA_CLI.md` | Todos los comandos y flags, con ejemplos reales de entrada y salida |
| `docs/08_AGENTES_IA.md` | Reglas para agentes: qué no editar, invariantes del diseño, cómo validar cambios sin aprovisionar, glosario de términos del dominio |

Además:

- **Actualiza `README.md`**: nueva topología, `gestion_red: auto` como camino recomendado, la sección "Hoja de ruta" marcando el avance, y la sección 8 (Notas para agentes de IA) con las invariantes nuevas.
- **Actualiza `MANUAL_DESPLIEGUE.md`**: la sección 2 ("Preparar la VPC") pasa de "hazlo a mano" a "lo hace el sistema"; el proceso manual se conserva como **Anexo A: fallback manual**.
- **Reetiqueta `Manual_VPC_SecurityGroup.md`** como documento histórico/anexo, con una nota al principio indicando que su procedimiento ahora está automatizado y dónde vive el código equivalente.
- **Docstrings** en toda función pública, con formato consistente, explicando parámetros, retorno y efectos secundarios sobre AWS y sobre la BD.
- **Un diagrama de secuencia en texto** del `provision` completo y otro del `destroy`, dentro de `docs/01` y `docs/04`.

---

## 11. Criterios de aceptación

El trabajo está terminado cuando **todo** esto es cierto:

- [ ] `python scripts/generate_infra.py --run` sobre una cuenta AWS limpia crea VPC, subredes, IGW, NAT, EIP, route tables y SGs sin ninguna acción manual, y termina con el chat funcionando en `http://<ip-gateway>/`.
- [ ] Ejecutarlo dos veces seguidas no crea recursos duplicados y la segunda corrida es notablemente más rápida (todo `[SKIP]` salvo lo que cambió).
- [ ] `python scripts/destroy_infra.py --dry-run` lista exactamente los recursos creados, en orden de borrado, sin tocar nada.
- [ ] `python scripts/destroy_infra.py --yes` deja la cuenta sin recursos con tag `sooniverse:managed=true` de ese despliegue. **Verificado explícitamente**: `describe_addresses` sin EIP nuestras, `describe_nat_gateways` todos en `deleted`, `describe_vpcs` sin la nuestra.
- [ ] El destroy **no** borra: la base de datos, el esquema `sooniverse`, recursos de otros clientes, recursos sin nuestros tags, la VPC por defecto.
- [ ] Dos clientes (`acme` y `globex`) pueden desplegarse simultáneamente en la misma cuenta y región sin colisiones, y destruir uno no afecta al otro. **Demuéstralo** al menos con el `--dry-run` de ambos.
- [ ] Los workers no tienen IP pública y solo son alcanzables desde el SG del gateway (verificado por `verify_deployment.py`).
- [ ] El puerto 80 sirve chat (`/`), API (`/v1/`) y panel (`/panel/`); el streaming de tokens llega incrementalmente y el WebSocket de Open WebUI conecta.
- [ ] Con `exponer_puertos_directos: false`, los puertos 4000/8000/8080 **no** son alcanzables desde Internet.
- [ ] `sooniverse.infra_deployment` / `infra_resource` / `infra_event` reflejan fielmente lo que existe en AWS tras cada operación.
- [ ] `python scripts/verify_deployment.py` pasa las 11 comprobaciones.
- [ ] `python -m pytest` pasa sin credenciales de AWS.
- [ ] Un desarrollador que solo lea `docs/01_FLUJO_DESPLIEGUE.md` puede explicar qué hace cada fase y dónde está su código.

---

## 12. Método de trabajo

- **No hagas todo de golpe.** Trabaja fase por fase (secciones 2→10), y **para al final de cada fase** para mostrarme: qué archivos tocaste, un resumen de las decisiones no obvias, y qué falta. Espera confirmación antes de continuar.
- **Un commit por fase**, mensaje en español, formato convencional: `feat(red): módulo AwsNetworkManager con provisión idempotente`.
- **Cuando el prompt y el código real se contradigan, gana el código real**: dímelo y propón la adaptación en lugar de forzar el diseño.
- **Cuando algo dependa de la versión de SkyPilot o del comportamiento exacto de una API de AWS, verifícalo** (leyendo el paquete instalado, la documentación, o con una prueba mínima) en lugar de asumir. Marca explícitamente cualquier supuesto que no hayas podido verificar.
- **No inventes nombres de funciones o tablas existentes.** Léelos del repositorio.
- **Seguridad de costes:** durante el desarrollo, todo lo que pruebes contra AWS real hazlo con `--dry-run` o `moto` salvo que yo autorice explícitamente una corrida real. El NAT Gateway cuesta ~$0.045/hora esté ocioso o no, más ~$0.005/hora la EIP: un despliegue olvidado son ~$35/mes.

Empieza por la **Fase 0 (reconocimiento)** y devuélveme el informe.
