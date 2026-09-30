# 11. Plan: apagado manual y programado de workers (AWS y Azure)

**Estado:** plan, sin implementar. **Fecha:** 30 sep 2026.
**Alcance:** apagar/encender workers desde el panel (manual) y por horario (programado con `django_q2`), en AWS y Azure. **Fuera de alcance:** el apagado automático por ocio y el despertar por demanda (ver `docs/10_VIABILIDAD_GPT6_LUNA.md` §9), el spot/reacople y Vast.ai.

---

## 1. Qué existe hoy

| Pieza | Estado | Dónde |
|---|---|---|
| Botones Apagar/Arrancar en la card "Pool vLLM" | ✅ solo AWS | `templates/metrics/dashboard.html:342-445`, `static/js/worker-actions.js` |
| Vista + servicio | ✅ síncrono, "dispara y olvida" (no confirma el estado final) | `metrics/views.py:763` `worker_accion`, `metrics/services.py:590` `ejecutar_accion_worker` |
| Llamadas a la nube | ✅ `boto3` `stop_instances` / `start_instances`, con chequeo DryRun | `metrics/workers.py:67-216` |
| Usuario IAM de mínimo privilegio | ✅ AWS hospedado, ❌ AWS BYOC (el rol no tiene `iam:CreateUser`) | `scripts/aws_iam_worker_control.py`, `Manual_Usuario_IAM_Workers.md` |
| Bitácora de acciones | ✅ se escribe, ❌ no se ve en ningún lado | `sooniverse.worker_action` |
| Azure | ❌ nada: sin SDK en el panel, sin identidad con permisos, `instance_id` en NULL | — |
| Programador de tareas | ❌ solo un bucle bash que corre `sync_metrics` | `django_metrics/entrypoint.sh:77-86` |
| Drenaje antes de apagar | ❌ LiteLLM sigue enviando tráfico a la IP apagada | `docker_images/gateway/litellm_config.yaml` |

### Problemas que hay que corregir antes o durante la implementación

1. **Credenciales del panel mezcladas con las de despliegue.** Las llaves del usuario `worker-ctrl` se escriben como `AWS_ACCESS_KEY_ID` en el `.env` **de la raíz del repo**, el mismo archivo para todos los clientes y con el nombre que `.env.example` asigna a "aprovisionamiento vía SkyPilot". Consecuencias:
   - Desplegar el cliente B puede dejar al Gateway del cliente A con la llave de B, que no tiene permiso sobre los workers de A. Los botones de A dejan de funcionar en el siguiente `sky launch`.
   - Si alguien pone en ese `.env` las credenciales de despliegue, el panel queda corriendo 24/7 con permisos amplios. Es justo lo que el módulo IAM quiere evitar.
   - **Tarea para ti:** revisa si tu `.env` local tiene `AWS_ACCESS_KEY_ID` lleno y de qué usuario es. Yo no lo leí porque contiene secretos.
2. **Cualquier usuario del panel puede apagar workers.** Hay que exigir el rol Administrador.
3. **El estado no se confirma.** Tras la gracia de 120 s el nodo suele quedar como `desincronizado`.
4. **El dashboard consulta `/health` de LiteLLM en cada carga**, y eso incluye el worker apagado (timeout de hasta 30 s).
5. **Reiniciar por SSH usa el usuario `ubuntu`**, que en Azure es `azureuser`.
6. **SkyPilot no se entera**: después de apagar desde el panel, `sky status` sigue diciendo `UP` hasta que se corre `sky status --refresh`.

---

## 2. Diseño

### 2.1 Principio: estado deseado + reconciliación

No se programa "apagar a las 19:00" como un evento único. Cada minuto, una tarea calcula **qué estado debería tener cada worker ahora** (según su horario y cualquier orden manual vigente), lo compara con el **estado real en la nube** y actúa solo si difieren. Ventajas:
- Si el programador estuvo caído a las 19:00, a las 19:07 apaga igual.
- Es idempotente: el mismo cálculo dos veces no hace nada dos veces.
- Detecta y corrige cambios hechos a mano en la consola de la nube.

### 2.2 Precedencia

1. **Orden manual con vigencia**: un encendido manual dentro de una ventana de apagado se respeta **hasta el próximo cambio de ventana** (campo `override_hasta`). Así, encender a las 21:00 "para una urgencia" no se deshace un minuto después, y a la mañana siguiente el horario vuelve a mandar.
2. **Horario**: ventanas "encendido de HH:MM a HH:MM" por día de la semana, en `America/Bogota`. Admiten cruzar la medianoche.
3. **Sin horario**: el worker queda como esté (comportamiento de hoy).

Reglas de seguridad del reconciliador:
- **Nunca apaga el Gateway** (solo filas de `worker_node`).
- **Drena antes de apagar:** espera a que `vllm:num_requests_running` llegue a 0 (tope configurable, por defecto 10 min) y después apaga.
- **Candado por worker** (`SELECT ... FOR UPDATE SKIP LOCKED`) para que la acción manual y la programada no choquen.
- En Azure siempre se usa **deallocate**, nunca `power_off`: una VM "detenida" sin desasignar se sigue cobrando. Si encuentra una VM en `stopped`, la desasigna.

### 2.3 Componentes

```
Panel (gunicorn)                       Programador (qcluster, mismo imagen)
  ├─ botón Apagar/Encender  ──async_task──▶  tarea cambiar_energia(worker, accion)
  ├─ CRUD de horarios                        ├─ drenar → nube.apagar/encender
  └─ bitácora visible                        ├─ esperar estado final (poll)
                                             ├─ esperar /health de vLLM (encendido)
        Postgres (broker ORM de django_q2)   └─ registrar duración y resultado
                                           tarea reconciliar_horarios (cada 1 min)
                                                         │
                               metrics/nube.py  ─────────┴────────────
                               ├─ ControladorAWS  (boto3, usuario worker-ctrl)
                               └─ ControladorAzure (azure-mgmt-compute, identidad administrada del Gateway)
```

- **`metrics/nube.py`**: interfaz `disponible()`, `estado(worker)`, `apagar(worker)`, `encender(worker)`. Mueve ahí el código de `workers.py` para AWS y agrega Azure. El controlador se elige por `worker_node.cloud`.
- **`metrics/tareas.py`**: `cambiar_energia` y `reconciliar_horarios`.
- **Acciones manuales asíncronas**: la vista encola la tarea, escribe `worker_action` con estado `solicitada` y responde enseguida. La card muestra "Encendiendo… (1:42)" hasta que la tarea confirma. Así el request web no se queda colgado 3 minutos.
- **Duración del arranque medida en cada encendido** (VM lista, vLLM `/health` 200, primera respuesta a través de LiteLLM). Es el dato que decide si algún día aplica la regla de los 15 s del apagado automático.

### 2.4 `django_q2`

- `django-q2` en `django_metrics/requirements.txt`, `django_q` en `INSTALLED_APPS`.
- **Broker ORM (Postgres)**, no Redis: las tareas quedan en la misma base, sobreviven a reinicios y no suman una dependencia. El volumen es mínimo (una tarea por minuto más las acciones manuales).
  ```python
  Q_CLUSTER = {
      "name": "sooniverse", "orm": "default", "workers": 2,
      "timeout": 1200, "retry": 1500, "max_attempts": 1,
      "catch_up": False,   # la reconciliación ya cubre lo perdido
      "ack_failures": True, "save_limit": 500,
  }
  ```
  Nota: `retry` debe ser mayor que `timeout` (lo exige django_q2).
- Las tablas de `django_q` se crean con el `migrate` que ya corre `entrypoint.sh`, dentro del esquema `sooniverse` por el `search_path`.
- **Nuevo servicio en el compose del Gateway** (`render_gateway_stack.py`): `metrics-scheduler`, la misma imagen `sooniverse/metrics-panel`, `command: qcluster`, `restart: unless-stopped`, con las mismas variables de BD y de nube que `metrics`. `entrypoint.sh` recibe el rol: `web` (hace migrate, collectstatic y gunicorn, como hoy) o `qcluster` (espera a que la BD y las migraciones estén listas y hace `exec python manage.py qcluster`).
- `reconciliar_horarios` se registra como `Schedule` de django_q2 cada minuto desde un comando idempotente (`ensure_schedules`) que corre en el arranque.
- El bucle bash de `sync_metrics` se puede pasar a un `Schedule` más adelante. No es necesario para esto.

### 2.5 Modelo de datos: `database/007_energia_workers.sql`

```sql
-- Identificar el recurso en cualquier nube
ALTER TABLE sooniverse.worker_node
  ADD COLUMN IF NOT EXISTS cloud             VARCHAR(16),   -- 'aws' | 'azure'
  ADD COLUMN IF NOT EXISTS cloud_region      VARCHAR(64),
  ADD COLUMN IF NOT EXISTS cloud_resource_id TEXT,          -- ARN/instance-id o ID ARM de la VM
  ADD COLUMN IF NOT EXISTS power_state       VARCHAR(16),   -- observado: running|stopped|starting|stopping|unknown
  ADD COLUMN IF NOT EXISTS power_state_at    TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS override_hasta    TIMESTAMPTZ,   -- vigencia de la última orden manual
  ADD COLUMN IF NOT EXISTS override_estado   VARCHAR(16);   -- 'encendido' | 'apagado'

-- Ventanas de encendido (un worker puede tener varias)
CREATE TABLE IF NOT EXISTS sooniverse.worker_schedule (
  id              BIGSERIAL PRIMARY KEY,
  worker_node_id  BIGINT NOT NULL REFERENCES sooniverse.worker_node(id) ON DELETE CASCADE,
  dias_semana     SMALLINT[] NOT NULL,          -- 0=lunes … 6=domingo
  hora_encendido  TIME NOT NULL,
  hora_apagado    TIME NOT NULL,                -- < hora_encendido = cruza medianoche
  zona_horaria    VARCHAR(64) NOT NULL DEFAULT 'America/Bogota',
  activo          BOOLEAN NOT NULL DEFAULT TRUE,
  creado_por      VARCHAR(150),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Bitácora: de dónde vino la acción y cuánto tardó
ALTER TABLE sooniverse.worker_action
  ADD COLUMN IF NOT EXISTS origen      VARCHAR(16) NOT NULL DEFAULT 'manual', -- manual|programado|reconciliacion
  ADD COLUMN IF NOT EXISTS duracion_s  NUMERIC(8,1),
  ADD COLUMN IF NOT EXISTS detalle     JSONB;       -- tiempos por fase: vm_lista, vllm_health, primera_respuesta
```

Hay que ampliar el CHECK de `estado_operativo` con `encendiendo`, `apagando` y `drenando`. Se sigue la convención del repo (modelos `managed=False` y SQL numerado), y `worker_schedule` se registra en `admin.py`.

**`scripts/sync_endpoints.py::register_in_db`** debe llenar `cloud`, `cloud_region` y `cloud_resource_id`:
- En AWS ya tiene `instance_id`. Hay que asegurarlo también cuando el descubrimiento entra por la API Python de SkyPilot (hoy queda en NULL en ese camino).
- En Azure, listar las VMs del RG con la etiqueta `rol=worker` (igual que `verify_deployment.py:259`) y asociarlas por IP privada.

### 2.6 Específico de AWS

- Se mantiene el usuario IAM `sooniverse-{cliente}-{entorno}-worker-ctrl` (política por etiquetas `cliente_id`/`entorno`/`rol=worker`, ya probada).
- **Credenciales separadas por cliente:** el despliegue las guarda en `clients/<cliente>/.secrets/panel.env` (ignorado por git), con los nombres `PANEL_AWS_ACCESS_KEY_ID` y `PANEL_AWS_SECRET_ACCESS_KEY`. Se montan en el Gateway como archivo aparte, y el compose las mapea a `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` **solo dentro** de `metrics` y `metrics-scheduler`. El `.env` raíz deja de recibirlas.
- La IP privada se conserva en stop/start (comportamiento estándar de EC2). No hay que tocar LiteLLM.
- Se cobra el EBS del worker mientras está apagado (~$0,08/GB-mes en gp3). Está incluido en el costo fijo del informe.

### 2.7 Específico de Azure

- **Identidad:** una identidad administrada **nueva y solo para el Gateway**, `sooniverse-{cliente}-{entorno}-panel-msi`. Los workers conservan la de hoy, sin rol. Si se reutilizara la identidad compartida, cualquier worker podría apagar a los demás.
- **Permiso:** rol integrado **Virtual Machine Contributor** (`9980e02c-c2be-4d73-94e8-173b1dc7cf3c`) con alcance **solo el Resource Group** `sooniverse-{cliente}-{entorno}-rg`.
  - ¿Por qué no un rol personalizado mínimo (`read` + `start` + `deallocate`)? Azure Lighthouse solo permite delegar la asignación de **roles integrados**. VM Contributor limitado a un RG que ya es enteramente de Sooniverse es un compromiso aceptable. En la modalidad hospedada sí se puede usar el rol personalizado mínimo.
- **Credencial en el contenedor:** `ManagedIdentityCredential(client_id=AZURE_PANEL_MSI_CLIENT_ID)` a través del IMDS de la VM. No hay secretos en disco. Como alternativa (si IMDS no es accesible desde Docker), un Service Principal con secreto en `panel.env`.
- **Operaciones:** `virtual_machines.begin_deallocate` / `begin_start`, y `instance_view().statuses` para leer `PowerState/*`.
- **Chequeo "disponible"** (el equivalente al DryRun de AWS): `AuthorizationManagementClient.permissions.list_for_resource_group` y verificar que estén `virtualMachines/deallocate/action` y `start/action`. Fail-closed.
- **IP privada:** fijarla como **Static** en la NIC de cada worker después del lanzamiento (`network_interfaces.begin_create_or_update` con `private_ip_allocation_method="Static"` y la IP actual). Con asignación dinámica, Azure puede cambiar la IP tras un deallocate, y LiteLLM apunta a la IP cruda. La IP pública de salida ya es estática (`azure_worker_egress_ip.py`) y se sigue cobrando apagada (~$3,6/mes).
- **Reiniciar por SSH:** usar `azureuser` en Azure (tomar el usuario de `cloud_remote.py`).
- **Asignación automática del rol en el despliegue** (fase de red de Azure en `generate_infra.py`): `RoleAssignmentCreateParameters(principal_type="ServicePrincipal", ...)`. En BYOC (Lighthouse) hay que incluir `delegated_managed_identity_resource_id` con el ID de la identidad; sin eso, Azure rechaza la asignación entre tenants.

### 2.8 LiteLLM y el tráfico fuera de horario

- Con el worker apagado, LiteLLM devuelve error tras reintentos. Para que la integración del cliente reciba un mensaje útil, se agrega en nginx una respuesta **503 con `Retry-After`** y un JSON tipo OpenAI: `{"error":{"message":"Plataforma fuera de horario. Próximo encendido: lun 07:00","type":"service_unavailable"}}`. Se activa cuando **todos** los workers de un modelo están apagados. El panel escribe un archivo de estado que nginx lee, o LiteLLM usa un callback `pre_call`; se decide al implementar.
- **Decisión por defecto: no desviar a un proveedor externo** fuera de horario. Hacerlo contradice la promesa de privacidad. Se deja como opción explícita de la oferta B (Gateway de gobierno).
- El dashboard deja de pedir `/health` de los workers con `power_state = stopped`.

### 2.9 Panel (UI)

- La card "Pool vLLM" muestra el estado real (`Encendido`, `Encendiendo 1:42`, `Drenando`, `Apagado desde 19:00`, `Próximo encendido lun 07:00`) y los botones Apagar/Encender también en Azure.
- Aviso en la confirmación: *"Es el único worker de `<modelo>`: las peticiones fallarán hasta que lo enciendas."*
- **Nueva sección "Horarios"** con una rejilla semanal por worker: plantillas "Laboral 7-19 L-V" y "24/7", más edición libre. Muestra el **ahorro estimado** con las horas apagadas × `METRICS_COSTE_HORA_USD`.
- **Bitácora visible:** las últimas 20 acciones (quién, origen, resultado, duración).
- Solo el grupo **Administrador** apaga, enciende y edita horarios. Los demás ven.

---

## 3. Fases y estimación

| Fase | Contenido | Estimado |
|---|---|---|
| **F0. Correcciones** | Credenciales por cliente (`panel.env`), exigir rol Administrador, columnas `cloud*`, `sync_endpoints` con Azure, IP estática en Azure | 1 día |
| **F1. Manual multi-nube** | `nube.py` (AWS + Azure), identidad y rol de Azure en el despliegue, acciones asíncronas con confirmación de estado y medición de arranque, drenaje, bitácora visible | 2 días |
| **F2. Programado** | `django_q2`, servicio `metrics-scheduler`, `007_energia_workers.sql`, reconciliador, UI de horarios, 503 fuera de horario | 2 días |
| **F3. Pruebas y documentación** | Unitarias (moto + mocks de Azure), pruebas reales (§5), runbook en `docs/06_RUNBOOK.md` | 1-2 días |

Total: **6-7 días de trabajo**. Los pasos manuales (§4) se pueden hacer en paralelo con F0-F1.

---

## 4. Pasos manuales en las nubes

> Haz primero **4.1** y **4.3**, que son los de las cuentas de Sooniverse y los necesitamos para las pruebas. Los pasos BYOC (**4.2** y **4.4**) son para cada cliente y se agregan a sus manuales de onboarding.

### 4.1 AWS — cuenta de Sooniverse (modalidad hospedada y pruebas)

Objetivo: que el usuario **con el que despliegas** pueda crear el usuario `worker-ctrl` de cada cliente. Hoy falla con `iam:CreateUser` denegado (`docs/09_BENCHMARK_TECHO.md` §5).

1. Entra a la consola de AWS con un usuario administrador → **IAM** → **Users**.
2. Abre el usuario con el que corres `generate_infra.py --run`. Si no sabes cuál es, en tu terminal: `aws sts get-caller-identity` (el ARN termina en `user/<nombre>`).
3. Pestaña **Permissions** → **Add permissions** → **Create inline policy** → pestaña **JSON**.
4. Pega la política de `Manual_Usuario_IAM_Workers.md` §8 (`GestionarSoloUsuariosWorkerCtrl` + `ResolverElAccountId`). Queda restringida a usuarios `sooniverse-*-worker-ctrl`.
5. **Next** → nombre `sooniverse-gestion-worker-ctrl` → **Create policy**.
6. Verificación desde tu terminal (no crea nada):
   `aws iam simulate-principal-policy --policy-source-arn <ARN de tu usuario> --action-names iam:CreateUser --resource-arns arn:aws:iam::<cuenta>:user/sooniverse-x-dev-worker-ctrl`
   El resultado debe decir `"EvalDecision": "allowed"`.
7. **No hagas nada más en IAM.** El usuario `worker-ctrl` lo crea el despliegue. Las dos Elastic IP sueltas de pruebas anteriores (`cliente-test-aws-dev`, `acme-prod`, ~$3,6/mes cada una) se pueden liberar en **EC2 → Elastic IPs** si ya no las usas.

### 4.2 AWS — cuenta de un cliente BYOC

Agrego al Terraform de onboarding (`onboarding/aws-byoc-terraform/main.tf`) el mismo bloque IAM restringido a `user/sooniverse-*-worker-ctrl`. El cliente solo tiene que:
1. Actualizar la carpeta de onboarding que le enviamos.
2. Correr `terraform plan` (debe mostrar **1 cambio**: la política del rol) y luego `terraform apply`.

Si el cliente no quiere que Sooniverse cree usuarios IAM, la alternativa es que él siga `Manual_Usuario_IAM_Workers.md` §3-6 y nos pase el access key por un canal seguro.

### 4.3 Azure — suscripción de Sooniverse (hospedado y pruebas)

Objetivo: que el Service Principal operador (el de `AZURE_CLIENT_ID`) pueda asignar **solo** el rol Virtual Machine Contributor a identidades, y nada más.

1. Portal de Azure → **Subscriptions** → tu suscripción → **Access control (IAM)** → **Add** → **Add role assignment**.
2. Pestaña **Privileged administrator roles** → elige **Role Based Access Control Administrator** → **Next**.
3. **Members** → *User, group, or service principal* → **Select members** → busca el SP operador (`sooniverse-operator` o el nombre que tenga) → **Select**.
4. Pestaña **Conditions** → **Add condition** → *"Allow user to only assign selected roles to selected principals (fewer privileges)"* → **Configure**:
   - Roles: **Virtual Machine Contributor** (y nada más).
   - Principal types: **Service principals** (las identidades administradas cuentan como tal).
   - **Save**.
5. **Review + assign**.
6. Verificación: **Access control (IAM)** → **Role assignments** → filtra por el SP. Debe aparecer *Role Based Access Control Administrator* con la condición.
7. **Atajo solo para la primera prueba** (si prefieres no esperar la automatización), después de desplegar:
   RG `sooniverse-<cliente>-<entorno>-rg` → **Access control (IAM)** → **Add role assignment** → **Virtual Machine Contributor** → *Managed identity* → `sooniverse-<cliente>-<entorno>-panel-msi` → **Review + assign**.

### 4.4 Azure — suscripción de un cliente BYOC (Lighthouse)

Agrego al Terraform de onboarding (`onboarding/azure-byoc-terraform/main.tf`) una tercera `authorization`:
```hcl
authorization {
  principal_id                  = var.sooniverse_principal_id
  role_definition_id            = "18d7d88d-d35e-4fb5-a5c3-7773c20a72d9"  # User Access Administrator
  principal_display_name        = var.sooniverse_principal_display_name
  delegated_role_definition_ids = ["9980e02c-c2be-4d73-94e8-173b1dc7cf3c"] # solo puede asignar VM Contributor
}
```
Con esto, Lighthouse deja que Sooniverse asigne **únicamente** VM Contributor, **únicamente** a identidades administradas. Es el patrón documentado por Microsoft para este caso. El cliente:
1. Actualiza la carpeta de onboarding.
2. `terraform plan` → debe mostrar **un cambio en la definición de Lighthouse** (y, según la versión del provider, también la asignación). Revisa que no aparezca nada más, y luego `terraform apply`.
3. En el portal: **Service providers** → **Service provider offers** → debe verse la oferta de Sooniverse con 3 autorizaciones.
4. Si el plan muestra un *reemplazo* (destroy/create) de la definición, la delegación se corta durante el apply. **Hazlo sin despliegues en curso.** Esto lo verifico yo primero contra la suscripción de prueba antes de enviárselo a un cliente.

### 4.5 Cuotas y entorno de prueba

- AWS: cuota de **G and VT instances** on-demand ≥ 4 vCPU en us-east-1 (ya la usaron en los benchmarks).
- Azure: cuota de **NCASv3_T4** ≥ 4 vCPU en la región de prueba (`scripts/azure_check_gpu_quota.py`).
- Desplegar dos entornos baratos cuando el código F1 esté listo: `cliente-test-aws` (L4) y `cliente-test-azure` (T4). Costo estimado de todas las pruebas: **$15-25 USD** (unas 6 h de GPU entre las dos nubes, más Gateways).

---

## 5. Plan de pruebas

### 5.1 Automáticas (pytest, sin nube)

| Área | Casos |
|---|---|
| Cálculo del estado deseado | ventanas por día, cruce de medianoche, varias ventanas, zona horaria, `override_hasta` vigente y vencido, sin horario = no toca |
| Reconciliador | idempotencia (dos corridas = una acción), candado (manual + programado a la vez), no toca el Gateway, VM Azure en `stopped` → deallocate |
| AWS | stop/start/estado con `moto`, política IAM real (el usuario no puede parar una instancia sin las etiquetas) |
| Azure | `ControladorAzure` con el SDK simulado: deallocate/start/instance_view, fail-closed sin permiso |
| Vista y roles | solo Administrador, `async_task` encolado, `worker_action` en `solicitada` → `ok`/`error` |
| Render del compose | aparece `metrics-scheduler`, credenciales solo en los servicios del panel, nunca en `litellm` |

### 5.2 Reales, en cada nube (criterios de aceptación)

| # | Prueba | Aceptación |
|---|---|---|
| R1 | Apagar manual | La consola de la nube muestra `stopped` / `Stopped (deallocated)` en ≤ 3 min. El panel muestra "Apagado". `worker_action` en `ok` |
| R2 | Encender manual | Se miden: VM `running`, vLLM `/health` 200, primera respuesta vía Gateway. Misma IP privada. Sin nueva descarga de pesos (logs de vLLM). Driver NVIDIA cargado (`nvidia-smi`). **Se anotan los tiempos** |
| R3 | Drenaje | Se lanza una petición larga (8k/1k) y se ordena apagar: la petición termina bien y el apagado ocurre después |
| R4 | Programado | Ventana que apaga en +5 min y enciende en +15 min: ambas transiciones ocurren solas, con `origen=programado` |
| R5 | Resiliencia del programador | Se detiene `metrics-scheduler` antes de un cambio de ventana y se levanta 5 min después: reconcilia en ≤ 1 min |
| R6 | Override manual | Se enciende a mano dentro de una ventana de apagado: sigue encendido hasta el siguiente cambio de ventana y luego vuelve al horario |
| R7 | Fuera de horario | Una llamada a `/v1/chat/completions` con todo apagado recibe **503 con `Retry-After`** y el mensaje de próximo encendido, no un timeout |
| R8 | Permisos negativos | Con la credencial del panel: apagar el Gateway → denegado; tocar un recurso de otro cliente/RG → denegado |
| R9 | SkyPilot | Tras 3 ciclos, `sky status --refresh` es coherente, y `generate_infra.py --run` con un worker apagado no rompe el despliegue |
| R10 | Costo | A las 48 h, Cost Explorer / Azure Cost Management muestra las horas de GPU reducidas en la proporción del horario |

Resultado de R2: si el arranque completo baja de 15 s (no se espera: hoy vLLM solo tarda ~127 s), se reabre el diseño del apagado automático sin nodo mínimo. Si no, queda confirmado el diseño multinodo con 1 nodo siempre encendido.

---

## 6. Riesgos y decisiones abiertas

| Riesgo / decisión | Propuesta por defecto |
|---|---|
| IMDS de Azure no accesible desde el contenedor Docker | Probar en R1. Plan B: Service Principal con secreto en `panel.env` |
| En Azure, `start` puede fallar por falta de capacidad de GPU en la región (el recurso se liberó al desasignar) | Reintento con espera exponencial durante 15 min, alerta en el panel, y en la bitácora "sin capacidad en la región". Documentarlo en el contrato: el encendido programado es *best effort* frente a la capacidad del proveedor |
| AWS `InsufficientInstanceCapacity` al encender (ya se vio con `g6e`) | Igual que arriba. Un worker apagado no tiene capacidad reservada |
| Tráfico fuera de horario | 503 claro, sin desviar a proveedores externos (§2.8) |
| Quién puede operar | Solo el grupo Administrador |
| Rol en Azure BYOC | VM Contributor con alcance al RG de Sooniverse (restricción de Lighthouse) |
