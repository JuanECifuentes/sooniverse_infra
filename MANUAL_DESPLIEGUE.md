# Manual de Despliegue · Fase 1

Guía operativa del **Nodo Gateway** (LiteLLM + PostgreSQL + Open WebUI + panel de
métricas) y de los **Workers vLLM** multi-nodo dentro de una VPC de AWS.

Arquitectura y decisiones de diseño: [README.md](README.md).

---

## Índice

1. [Requisitos previos](#1-requisitos-previos)
2. [Preparar la VPC](#2-preparar-la-vpc)
3. [Configurar el contrato](#3-configurar-el-contrato)
4. [Inicialización de la base de datos](#4-inicialización-de-la-base-de-datos)
5. [Despliegue multi-nodo](#5-despliegue-multi-nodo)
6. [Verificación post-despliegue](#6-verificación-post-despliegue)
7. [Crear y usar API Keys](#7-crear-y-usar-api-keys)
8. [Leer el panel de métricas](#8-leer-el-panel-de-métricas)
9. [Operación diaria](#9-operación-diaria)
10. [Solución de problemas](#10-solución-de-problemas)
11. [Apagado y limpieza](#11-apagado-y-limpieza)

---

## 1. Requisitos previos

### 1.1 Software local

```bash
python --version            # 3.11 o superior

python -m venv venv
source venv/bin/activate    # Windows PowerShell: .\venv\Scripts\Activate.ps1

pip install pyyaml psycopg2-binary "skypilot[aws]"
```

### 1.2 Credenciales de AWS

```bash
aws configure
# AWS Access Key ID / Secret / Default region (ej. us-east-1)

sky check                   # debe reportar AWS: enabled
```

Permisos mínimos del principal de aprovisionamiento: `EC2FullAccess`,
`IAMFullAccess` (SkyPilot crea el instance profile `skypilot-v1`) y `S3ReadOnly`.
Si el rol es restringido, replica la política oficial de SkyPilot para AWS.

### 1.3 PostgreSQL

El stack necesita una PostgreSQL **alcanzable desde el Nodo Gateway** (RDS,
instancia externa o el contenedor incluido). La comparten LiteLLM (tablas
`sooniverse."LiteLLM_*"`) y el panel de métricas (esquema `sooniverse`).

```bash
# Verificar alcance y estado del esquema antes de desplegar
python scripts/db_setup.py --check
```

### 1.4 Archivo `.env`

```bash
cp .env.example .env
```

Variables **obligatorias**:

| Variable | Descripción |
|---|---|
| `DB_NAME` `DB_USER` `DB_PASSWORD` `DB_HOST` `DB_PORT` | Conexión a PostgreSQL |
| `SECRET_KEY` | Clave de Django (aleatoria y larga) |
| `LITELLM_MASTER_KEY` | Master key del proxy. Debe empezar por `sk-`. **Cambiar.** |
| `LITELLM_SALT_KEY` | Sal de cifrado de LiteLLM. Fijar **una vez**: si cambia, las keys emitidas quedan ilegibles |
| `DJANGO_SUPERUSER_PASSWORD` | Contraseña de acceso al panel. Vacía ⇒ no se crea usuario |

> El `.env` es la **fuente autoritativa**: `db_setup.py` y el panel Django dan
> prioridad a este archivo sobre las variables del shell, para que un terminal con
> credenciales obsoletas no secuestre el despliegue.

> **`DB_HOST` no puede ser `localhost`** si PostgreSQL vive fuera del Gateway:
> desde dentro de los contenedores `localhost` es el propio contenedor. Usa el
> endpoint real, `postgres` (perfil `local-db`) o `host.docker.internal`.

---

## 2. La VPC (automática desde la Fase 2)

**Ya no hace falta crear nada a mano.** Con `red_y_aislamiento.gestion_red: "auto"`
(el default), `python scripts/generate_infra.py --run` crea la VPC, subredes
públicas/privadas, Internet Gateway, NAT Gateway, route tables y Security Groups
por sí solo, vía `scripts/aws_network.py::AwsNetworkManager` (boto3 puro, sin
Terraform ni CloudFormation). El detalle completo de cada recurso, su cálculo de
CIDR y su coste está en **[`docs/02_RED_AWS.md`](docs/02_RED_AWS.md)**.

Lo único que normalmente hay que ajustar en el contrato para este paso:

```yaml
red_y_aislamiento:
  region: "us-east-1"
  vpc_cidr: "10.0.0.0/16"      # distinto al de otros clientes activos en la misma región/cuenta
  azs: 1
  nat_gateway: {modo: "single"}   # single | per-az | none (none exige vpc_endpoints.s3: true)
  cidr_admin_ssh: "0.0.0.0/0"     # restringir a tu IP/VPN en producción real
```

Para verificar el plan sin crear nada: `python scripts/generate_infra.py --run --only network --dry-run`.

**Modo legado (`gestion_red: "existente"`):** si ya tenés una VPC creada a mano y
querés seguir operando exactamente como antes, fijá `vpc_name`/`security_group_workers`/
`security_group_gateway` en el contrato como se hacía anteriormente — ver
`Manual_VPC_SecurityGroup.md` (ahora anexo histórico) para el procedimiento manual
completo, que sigue siendo válido para este modo.

---

## 3. Configurar el contrato

Todo se define en `config_global.yaml`. Los campos que se tocan con más frecuencia:

```yaml
cliente:
  id: "acme"                        # nombre del tenant (afecta al nombre del clúster)
  entorno: "prod"                   # prod | dev

gateway:
  tipo_instancia: "t3.large"        # subir a t3.xlarge si hay muchos usuarios en WebUI
  load_balancing_strategy: "latency-based-routing"

base_de_datos:
  AUTO_INIT_DB: true                # ← ver sección 4

workloads:
  - id: "qwen3-5-llm"
    accelerator: "L4"
    cantidad_gpus: 1                # GPUs por nodo
    replicas: 2                     # ← número de nodos worker a balancear
    tipo_instancia: "g6.xlarge"
    puerto: 8007
    nombre_publico: "sooniverse-qwen3.5"
```

Generar los manifiestos y validar el contrato sin tocar AWS:

```bash
python scripts/generate_infra.py
```

Salida esperada:

```
[OK] Gateway     -> .sky_generated.gateway.yaml  (cluster: sooniverse-acme-prod-gw)
[OK] Worker 'qwen3-5-llm' -> .sky_generated.worker-qwen3-5-llm.yaml  (cluster: ..., nodos: 2)
[OK] SkyPilot cfg -> .sky_config_workers.yaml  (VPC / IPs internas / bastion)
[INFO] AUTO_INIT_DB = true (la BD se inicializa en el despliegue)
```

### Escalar el pool

Cambia `replicas` y vuelve a lanzar solo los workers:

```bash
# 1. editar replicas: 2 -> 4 en config_global.yaml
python scripts/generate_infra.py --run --only workers
python scripts/sync_endpoints.py --apply       # inyecta los nuevos endpoints en LiteLLM
```

### Añadir un segundo modelo

Agrega otra entrada a `workloads` con su propio `id`, `accelerator` y
`nombre_publico`. El generador crea un clúster independiente y LiteLLM publica
ambos modelos en el mismo endpoint `/v1`.

---

## 4. Inicialización de la base de datos

El flag `base_de_datos.AUTO_INIT_DB` decide **quién** ingesta
`database/init_schema.sql`.

### 4.1 Automática (`AUTO_INIT_DB: true`)

```yaml
base_de_datos:
  AUTO_INIT_DB: true
  auto_refresh_metrics: true        # además corre el ETL y los rollups
```

El `run` del Nodo Gateway ejecuta, antes de levantar los contenedores:

```bash
python3 scripts/db_setup.py --env-file .env --sql database/init_schema.sql --refresh
```

Es idempotente (`CREATE ... IF NOT EXISTS`), así que se puede repetir en cada
redespliegue sin destruir datos.

### 4.2 Manual (`AUTO_INIT_DB: false`)

Úsala cuando la BD la administra otro equipo, requiere ventana de cambio, o el
usuario del despliegue no tiene permisos de DDL.

```yaml
base_de_datos:
  AUTO_INIT_DB: false
```

El Gateway registra `AUTO_INIT_DB=false -> se omite...` y sigue arrancando. Aplica
el esquema tú mismo:

```bash
python scripts/db_setup.py            # aplica el esquema y verifica
python scripts/db_setup.py --refresh  # aplica + ETL + rollups
python scripts/db_setup.py --check    # solo verificar, sin escribir
```

### 4.3 Override puntual desde la CLI

Sin editar el YAML:

```bash
python scripts/generate_infra.py --run --no-auto-init-db   # fuerza false en esta corrida
python scripts/generate_infra.py --init-db                 # aplica el esquema desde tu máquina
```

### 4.4 Verificación

```bash
python scripts/db_setup.py --check
```

```
   [OK  ] sooniverse.api_key_registry
   [OK  ] sooniverse.token_usage_event
   [OK  ] sooniverse.token_usage_rollup
   [OK  ] sooniverse.api_key_audit
   [OK  ] sooniverse.worker_node
   [OK  ] LiteLLM_SpendLogs (detectada)
```

`LiteLLM_SpendLogs (pendiente...)` es **normal antes del primer arranque** del
Gateway: la crea LiteLLM con sus migraciones Prisma. El ETL la detecta sola en el
siguiente refresco.

---

## 5. Despliegue multi-nodo

### 5.1 Despliegue completo

```bash
python scripts/generate_infra.py --run
```

Orden de ejecución (impuesto por el aislamiento de red):

| Paso | Acción | Duración típica |
|---|---|---|
| 1 | `sky launch` del Gateway → IP pública, ingesta de BD, stack Docker | 8-12 min |
| 2 | Genera `.sky_config_workers.yaml` con `ssh_proxy_command` vía el Gateway | instantáneo |
| 3 | `sky launch` de cada clúster worker (`num_nodes = replicas`), sin IP pública | 15-25 min (drivers NVIDIA + pesos) |
| 4 | `sync_endpoints.py --apply` → IPs privadas en LiteLLM + recarga | 1-2 min |

**El Gateway va primero por necesidad**: es el bastion SSH sin el cual SkyPilot no
puede alcanzar workers que no tienen IP pública.

### 5.2 Despliegue por partes

```bash
python scripts/generate_infra.py --run --only gateway    # solo el Nodo Gateway
python scripts/generate_infra.py --run --only workers    # solo los workers (Gateway ya arriba)
```

### 5.3 Salida esperada al final

```
======================================================================
 LiteLLM      : http://<IP_GATEWAY>:4000
 Open WebUI   : http://<IP_GATEWAY>:8080
 Panel Django : http://<IP_GATEWAY>:8000/metrics/
======================================================================
```

El puerto 80 sirve todo junto vía nginx: `/` → Open WebUI, `/v1/` → LiteLLM,
`/panel/` → métricas.

---

## 6. Verificación post-despliegue

```bash
sky status                                    # todos los clústeres UP
sky status --ip sooniverse-acme-prod-gw       # IP pública del Gateway
GW=$(sky status --ip sooniverse-acme-prod-gw | tail -1)
```

### 6.1 Contenedores del Gateway

```bash
sky exec sooniverse-acme-prod-gw \
  "cd /home/ubuntu/sooniverse_infra/docker_images/gateway && sudo docker compose ps"
```

Deben estar `running`: `sooniverse-litellm`, `sooniverse-webui`,
`sooniverse-metrics`, `sooniverse-redis`, `sooniverse-proxy`.

### 6.2 Pool de workers

```bash
curl -s http://$GW:4000/health -H "Authorization: Bearer $LITELLM_MASTER_KEY" | jq
curl -s http://$GW:4000/v1/models -H "Authorization: Bearer $LITELLM_MASTER_KEY" | jq '.data[].id'
```

Debe listar `sooniverse-qwen3.5` y `healthy_endpoints` con **una entrada por
réplica**. Si `healthy_count` es menor que `replicas`, ve a la sección 10.2.

### 6.3 Petición end-to-end

```bash
curl -s http://$GW:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"sooniverse-qwen3.5","messages":[{"role":"user","content":"Di OK"}],"max_tokens":16}' | jq
```

### 6.4 Interfaces web

| URL | Qué comprobar |
|---|---|
| `http://<IP>:8080` | Open WebUI carga y el modelo aparece en el selector |
| `http://<IP>:8000/metrics/` | Panel de métricas (login con el superusuario) |
| `http://<IP>:8000/healthz/` | `{"status":"ok"}` |
| `http://<IP>/` | nginx sirve Open WebUI en el puerto 80 |

---

## 7. Crear y usar API Keys

### 7.1 Desde el panel (recomendado)

1. Abre `http://<IP_GATEWAY>:8000/metrics/api-keys/` e inicia sesión.
2. Completa **Crear API Key**:

| Campo | Significado |
|---|---|
| **Alias** | Nombre legible (`backend-produccion`). Obligatorio |
| **Responsable** | Email del equipo dueño |
| **Descripción** | Uso previsto de la credencial |
| **Modelos permitidos** | Lista por comas. Vacío ⇒ todos los modelos del pool |
| **Presupuesto máx. (USD)** | LiteLLM rechaza peticiones al superarlo |
| **Límite RPM / TPM** | Peticiones / tokens por minuto |
| **Vigencia** | `30d`, `24h`, `60m`. Vacío ⇒ sin expiración |

3. Pulsa **Emitir API Key**.
4. **Copia la clave del recuadro verde: no se vuelve a mostrar.** En BD solo queda
   el hash y un prefijo enmascarado (`sk-abcd…wxyz`).

Al emitirla ocurre, en una transacción:
- `POST /key/generate` en LiteLLM (quien realmente valida la key en cada petición).
- Fila en `sooniverse.api_key_registry` con los metadatos.
- Entrada `created` en `sooniverse.api_key_audit`.

### 7.2 Desactivar y reactivar

**Desactivar** (botón rojo) llama a `POST /key/block` en LiteLLM y marca
`is_active = false`. **Nunca borra la key**: se preserva todo el histórico de
consumo y la trazabilidad. **Reactivar** revierte la operación.

Si LiteLLM está momentáneamente inalcanzable, el registro local se marca igual y
el panel avisa; re-ejecuta la acción cuando el proxy vuelva para propagar el
bloqueo al proxy.

### 7.3 Usar la key

```bash
export SOONIVERSE_KEY="sk-..."

curl http://<IP_GATEWAY>:4000/v1/chat/completions \
  -H "Authorization: Bearer $SOONIVERSE_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"sooniverse-qwen3.5","messages":[{"role":"user","content":"Hola"}]}'
```

Con el SDK de OpenAI:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://<IP_GATEWAY>:4000/v1",
    api_key="sk-...",
)

resp = client.chat.completions.create(
    model="sooniverse-qwen3.5",
    messages=[{"role": "user", "content": "Hola"}],
)
print(resp.choices[0].message.content)
```

El balanceo entre réplicas es transparente: el cliente solo conoce
`sooniverse-qwen3.5`.

### 7.4 Vía API de LiteLLM (automatización)

```bash
curl -X POST http://<IP_GATEWAY>:4000/key/generate \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"key_alias":"ci-pipeline","models":["sooniverse-qwen3.5"],
       "max_budget":50,"rpm_limit":60,"duration":"90d"}'
```

Las keys creadas así **no aparecen en el registro administrativo** hasta que se
registren manualmente. Prefiere el panel para conservar metadatos y auditoría.

---

## 8. Leer el panel de métricas

`http://<IP_GATEWAY>:8000/metrics/`

### 8.1 Particiones temporales

El selector **Diario · Semanal · Mensual** cambia la granularidad de agregación:

| Partición | Bucket | Ventana por defecto |
|---|---|---|
| **Diario** | `date_trunc('day')` | 30 días |
| **Semanal** | `date_trunc('week')` (lunes) | ~24 semanas |
| **Mensual** | `date_trunc('month')` | ~24 meses |

Ajusta la ventana con el campo **Ventana (días)** o el parámetro `?dias=`.

### 8.2 Filtros

| Filtro | Efecto |
|---|---|
| **API Key** | Restringe **toda** la vista (gráfica, KPIs y tablas) a una credencial |
| **Modelo** | Aísla un modelo del pool |
| **Ventana (días)** | Sobrescribe la ventana por defecto de la partición |

Los filtros viven en la URL, así que son enlazables y compartibles:
`/metrics/?granularity=weekly&api_key=3&dias=90`

### 8.3 Indicadores

| KPI | Lectura |
|---|---|
| **Tokens totales** | Suma de `total_tokens` en la ventana. Debajo, tokens por petición |
| **Entrada · Salida** | `prompt_tokens` / `completion_tokens` y el % de generación |
| **Peticiones** | Volumen y badge de errores |
| **Gasto estimado** | Suma de `spend_usd` que reporta LiteLLM |

En la gráfica, cada barra es un bucket: el degradado representa el total y la
franja cian inferior la proporción de tokens de **entrada**.

El panel **Pool vLLM** muestra el estado de LiteLLM, cuántos nodos están sanos
(`sooniverse.worker_node`) y los modelos publicados.

### 8.4 Detalle por API Key

Desde `/metrics/api-keys/` pulsa el alias para ver serie propia, cuotas, las 50
últimas peticiones (solo contadores) y la bitácora de auditoría.

### 8.5 Frescura de los datos

Las métricas provienen de agregaciones pre-calculadas, no de las tablas en vivo.
Se refrescan:

- Automáticamente cada `METRICS_REFRESH_INTERVAL` segundos (300 por defecto).
- Al pulsar **Refrescar datos** en el panel.
- Manualmente:

```bash
python scripts/db_setup.py --refresh
# o dentro del contenedor:
sky exec sooniverse-acme-prod-gw \
  "sudo docker exec sooniverse-metrics python manage.py sync_metrics --since-hours 168"
```

> Si un consumo reciente no aparece, es latencia del ETL, no pérdida de datos: el
> evento ya está en `LiteLLM_SpendLogs` y entra en el siguiente refresco.

### 8.6 Consumo en JSON

```bash
curl -s "http://<IP>:8000/metrics/serie.json?granularity=monthly&api_key=3" \
  -b cookies.txt | jq
```

---

## 9. Operación diaria

### Escalar el pool

```bash
# replicas: 2 -> 4 en config_global.yaml
python scripts/generate_infra.py --run --only workers
python scripts/sync_endpoints.py --apply
```

### Re-sincronizar el balanceador

Tras reemplazar un worker, cambiar la estrategia de balanceo o si LiteLLM perdió
el pool:

```bash
python scripts/sync_endpoints.py              # dry-run: muestra el pool descubierto
python scripts/sync_endpoints.py --apply      # render + push al Gateway + reload
python scripts/sync_endpoints.py --apply --skip-db     # sin registrar inventario
python scripts/sync_endpoints.py --apply --skip-push   # solo render local
```

Recarga **únicamente** el contenedor `litellm`: Open WebUI, el panel y las
sesiones activas no se interrumpen.

### Pool manual (endpoints fuera de SkyPilot)

```bash
cat > endpoints.json <<'EOF'
[{"workload_id":"qwen3-5-llm","model_public_name":"sooniverse-qwen3.5",
  "hf_repo":"cyankiwi/Qwen3.5-2B-AWQ-4bit","ip":"10.0.1.50","port":8007,"weight":1}]
EOF
python scripts/sync_endpoints.py --endpoints-file endpoints.json --apply
```

### Cambiar la estrategia de balanceo

```bash
# gateway.load_balancing_strategy en config_global.yaml
python scripts/sync_endpoints.py --apply
```

### Logs

```bash
sky logs sooniverse-acme-prod-gw                        # arranque del Gateway
sky logs sooniverse-acme-prod-qwen3-5-llm               # arranque de los workers

sky exec sooniverse-acme-prod-gw "sudo docker logs --tail 100 sooniverse-litellm"
sky exec sooniverse-acme-prod-gw "sudo docker logs --tail 100 sooniverse-metrics"
```

### Actualizar el panel de métricas sin redesplegar

```bash
sky rsync ./django_metrics sooniverse-acme-prod-gw:/home/ubuntu/sooniverse_infra/
sky exec sooniverse-acme-prod-gw \
  "cd /home/ubuntu/sooniverse_infra/docker_images/gateway && \
   sudo docker compose --env-file ../../.env up -d --build --no-deps metrics"
```

### Datos de demostración

Para validar el panel sin tráfico real:

```bash
sky exec sooniverse-acme-prod-gw \
  "sudo docker exec sooniverse-metrics python manage.py seed_demo --dias 60"
# revertir (borra solo lo marcado como demo-*)
sky exec sooniverse-acme-prod-gw \
  "sudo docker exec sooniverse-metrics python manage.py seed_demo --clean"
```

---

## 10. Solución de problemas

### 10.1 El Gateway arranca pero LiteLLM no tiene modelos

**Síntoma:** `/v1/models` devuelve lista vacía; el log dice
`litellm_config.yaml generado SIN deployments`.

**Causa:** esperado en el primer arranque — las IPs privadas no existían aún.

```bash
python scripts/sync_endpoints.py --apply
```

### 10.2 `sync_endpoints.py` no descubre IPs

Prueba tres métodos en cascada: API de Python de SkyPilot → marcadores
`SOONIVERSE_WORKER_READY` en los logs → `sky status --ip`.

```bash
sky status                                                  # ¿el clúster está UP?
sky logs sooniverse-acme-prod-qwen3-5-llm --no-follow | grep SOONIVERSE_
```

Si el worker nunca imprimió el marcador, su `run` falló (ver 10.3). Como último
recurso, usa un pool manual (sección 9).

### 10.3 Un worker no arranca

```bash
sky logs sooniverse-acme-prod-qwen3-5-llm
sky exec sooniverse-acme-prod-qwen3-5-llm "nvidia-smi && sudo docker compose ps"
sky exec sooniverse-acme-prod-qwen3-5-llm \
  "cd /home/ubuntu/sooniverse_infra/docker_images/qwen3.5 && sudo docker compose logs --tail 80"
```

| Causa | Señal | Solución |
|---|---|---|
| Sin salida a Internet | `apt-get` o descarga de HF cuelga | Falta el NAT Gateway en la subred privada |
| VRAM insuficiente | `CUDA out of memory` | Bajar `gpu_memory_utilization` o `max_model_len` |
| Driver NVIDIA | `nvidia-smi` falla | Relanzar: el `setup` carga los módulos en caliente |
| Cuota de AWS | `InsufficientInstanceCapacity` | Otra región/AZ o pedir aumento de cuota de GPU |

### 10.4 El Gateway no alcanza a los workers

```bash
GW_WORKER_IP=10.0.x.y
sky exec sooniverse-acme-prod-gw "curl -sv --max-time 5 http://$GW_WORKER_IP:8007/health"
```

| Causa | Solución |
|---|---|
| Security Group sin la regla del 8007 | Regenerar manifiestos (el generador declara el puerto) o revisar el SG de `security_group_workers` |
| Clústeres en VPCs distintas | Fijar el mismo `vpc_name` para todos |
| vLLM aún cargando el modelo | Esperar: la primera carga tarda varios minutos |

### 10.5 `db_setup.py` no conecta

```
[ERROR DB] No se pudo conectar a PostgreSQL en X:5432 -> ...
```

| Causa | Solución |
|---|---|
| Credenciales incorrectas | Revisar `DB_*` en `.env` (**el archivo manda sobre el shell**) |
| Sin acceso de red | Security Group / `pg_hba.conf` deben permitir la IP del Gateway |
| Falta `psycopg2` | `pip install psycopg2-binary` |
| Base de datos inexistente | `createdb` previo: el script crea el **esquema**, no la BD |

### 10.6 El panel no muestra consumo

Diagnóstico en orden:

```bash
# 1. ¿Existe el esquema?
python scripts/db_setup.py --check

# 2. ¿LiteLLM está registrando?
psql -c 'SELECT COUNT(*) FROM sooniverse."LiteLLM_SpendLogs";'

# 3. ¿El ETL corrió?
psql -c 'SELECT COUNT(*) FROM sooniverse.token_usage_event;'

# 4. ¿Hay agregaciones?
psql -c "SELECT granularity, COUNT(*) FROM sooniverse.token_usage_rollup GROUP BY 1;"

# 5. Forzar refresco
python scripts/db_setup.py --refresh
```

Si (2) es 0 pero las peticiones funcionan, LiteLLM no tiene `DATABASE_URL`:
revisa las variables `DB_*` del `.env` del Gateway.

Si (2) > 0 y (3) es 0, el ETL no encuentra la tabla: reintenta con
`SELECT sooniverse.ingest_litellm_spendlogs(720);`

### 10.7 Las peticiones fallan con 401

| Causa | Solución |
|---|---|
| Key desactivada | Reactivarla en `/metrics/api-keys/` |
| `LITELLM_SALT_KEY` cambió | Las keys anteriores quedan ilegibles: reemitirlas. **No cambies la sal en producción** |
| Presupuesto agotado | Subir `max_budget` vía `POST /key/update` |
| Key expirada | Emitir una nueva con `duration` más amplio |

### 10.8 Los estáticos del panel no cargan

```bash
sky exec sooniverse-acme-prod-gw \
  "sudo docker exec sooniverse-metrics python manage.py collectstatic --noinput"
```

---

## 11. Apagado y limpieza

### Detener sin destruir (ahorra cómputo, conserva el disco)

```bash
sky stop sooniverse-acme-prod-qwen3-5-llm     # apaga las GPUs (el gasto caro)
sky start sooniverse-acme-prod-qwen3-5-llm    # reanudar
python scripts/sync_endpoints.py --apply      # las IPs privadas pueden cambiar
```

### Destruir la infraestructura

```bash
sky down sooniverse-acme-prod-qwen3-5-llm     # primero los workers
sky down sooniverse-acme-prod-gw              # después el bastion
```

> Destruye los workers **antes** que el Gateway: sin el bastion, SkyPilot pierde
> el acceso SSH a instancias sin IP pública y la limpieza se complica.

**La base de datos sobrevive**: `sky down` no toca PostgreSQL. El histórico de
métricas, API Keys y auditoría permanece intacto para el siguiente despliegue.

### Reset del esquema de métricas (destructivo)

```sql
-- Elimina TODO el histórico de métricas y API Keys de Sooniverse.
-- No afecta a las tablas nativas de LiteLLM en `public`.
DROP SCHEMA sooniverse CASCADE;
```

```bash
python scripts/db_setup.py                     # recrear vacío
```

---

## Referencia rápida de comandos

Ver también **[`docs/07_REFERENCIA_CLI.md`](docs/07_REFERENCIA_CLI.md)** (todas las
flags, con ejemplos reales de entrada y salida).

| Objetivo | Comando |
|---|---|
| Validar contrato y generar manifiestos | `python scripts/generate_infra.py` |
| Ver el plan completo sin tocar AWS/BD | `python scripts/generate_infra.py --run --dry-run` |
| Desplegar todo (VPC + gateway + workers + endpoints + verify) | `python scripts/generate_infra.py --run` |
| Solo la capa de red / Gateway / workers | `--only network` / `--only gateway` / `--only workers` |
| Forzar `AUTO_INIT_DB=false` | `... --run --no-auto-init-db` |
| Aplicar esquema de BD (todos los `.sql`) | `python scripts/db_setup.py` |
| Aplicar + ETL + rollups | `python scripts/db_setup.py --refresh` |
| Verificar BD sin escribir | `python scripts/db_setup.py --check` |
| Ver pool descubierto (dry-run) | `python scripts/sync_endpoints.py` |
| Sincronizar balanceador | `python scripts/sync_endpoints.py --apply` |
| Reconciliación periódica | `python scripts/sync_endpoints.py --watch --interval 60` |
| Verificar el despliegue (11 comprobaciones) | `python scripts/verify_deployment.py` |
| Inventario de todos los clientes | `python scripts/list_deployments.py` |
| Plan de destrucción sin tocar nada | `python scripts/destroy_infra.py --dry-run` |
| Destruir todo (VPC incluida) | `python scripts/destroy_infra.py --yes` |
| Buscar recursos huérfanos | `python scripts/destroy_infra.py --scan-orphans` |
| Estado de los clústeres SkyPilot | `sky status` |
| IP del Gateway | `sky status --ip sooniverse-<id>-<entorno>-gw` |
| Apagar GPUs (sin destruir la infra) | `sky stop sooniverse-<id>-<entorno>-<workload>` |

---

## Anexo A: fallback manual (`gestion_red: "existente"`)

Si por algún motivo no querés que este sistema gestione la VPC (cuenta compartida
con otras cargas, políticas internas, etc.), el procedimiento 100% manual sigue
disponible y documentado en **[`Manual_VPC_SecurityGroup.md`](Manual_VPC_SecurityGroup.md)**
(marcado ahora como anexo histórico). Fijá `red_y_aislamiento.gestion_red: "existente"`
y completá `vpc_name`/`security_group_workers`/`security_group_gateway` con los
valores que resulten de seguir ese anexo.
