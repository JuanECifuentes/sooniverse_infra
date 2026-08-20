# 00. Arquitectura

## 1. Qué es este sistema

`sooniverse_infra` despliega, para un cliente y entorno dados, una topología completa en AWS:

- Una **VPC dedicada** con subredes públicas y privadas, NAT Gateway, Internet Gateway y Security Groups — creada y destruida por `scripts/aws_network.py` (modo `gestion_red: auto`), o referenciada por nombre si ya existe (`gestion_red: existente`).
- Un **Nodo Gateway** (única puerta de entrada pública) corriendo LiteLLM + Open WebUI + Django (métricas/API keys) + Redis + nginx, en Docker Compose.
- N **Workers vLLM**, uno o más clústeres SkyPilot en la subred privada, sin IP pública.
- Un **estado persistente en PostgreSQL** (`sooniverse.infra_deployment` / `infra_resource` / `infra_event`) que es la fuente de verdad de qué existe y quién es dueño de qué.

## 2. Diagrama de red

```
                                   Internet
                                       │
                              ┌────────┴────────┐
                              │  Internet Gateway │
                              └────────┬────────┘
                                       │
                    ┌──────────────────┴──────────────────┐
                    │              VPC (10.0.0.0/16)        │
                    │                                        │
                    │  ┌───────────── Subred pública ──────┐ │
                    │  │                                    │ │
                    │  │   ┌──────────────────────────┐     │ │
                    │  │   │   NODO GATEWAY           │     │ │
    tu IP ──SSH────────────▶  sg-...-gateway           │     │ │
   (bastion)        │  │   │   nginx :80/:443         │     │ │
                    │  │   │   ├─ Open WebUI  :8080   │     │ │
                    │  │   │   ├─ LiteLLM     :4000   │     │ │
                    │  │   │   ├─ Django      :8000   │     │ │
                    │  │   │   └─ Redis                │     │ │
                    │  │   └──────────┬───────────────┘     │ │
                    │  └──────────────┼─────────────────────┘ │
                    │                 │ SG->SG (referencia,     │
                    │                 │ nunca CIDR)              │
                    │  ┌──────────────┼─────────────────────┐   │
                    │  │  Subred privada │                  │   │
                    │  │                 ▼                  │   │
                    │  │   ┌─────────────────────────┐      │   │
                    │  │   │  WORKERS vLLM            │      │   │
                    │  │   │  sg-...-workers           │      │   │
                    │  │   │  sin IP pública           │      │   │
                    │  │   │  puerto 8007 (interno)    │      │   │
                    │  │   └─────────────┬───────────┘      │   │
                    │  │                 │ salida a Internet  │   │
                    │  └─────────────────┼────────────────────┘   │
                    │            ┌───────┴────────┐                │
                    │            │  NAT Gateway    │                │
                    │            └───────┬────────┘                │
                    └────────────────────┼─────────────────────────┘
                                          │
                                     (misma IGW)
```

## 3. Diagrama de flujo de una petición de chat

```
Usuario ──HTTP(S)──▶ nginx :80/:443 (única superficie pública)
                        │
                        ├─ "/"         ──▶ Open WebUI  :8080  (WebSocket, chat)
                        ├─ "/v1/*"     ──▶ LiteLLM      :4000  (API OpenAI-compatible, SSE)
                        │                     │
                        │                     └─ balancea sobre N workers vLLM
                        │                        vía IP PRIVADA (10.0.x.y:8007)
                        ├─ "/panel/*"  ──▶ Django       :8000  (métricas, API keys)
                        └─ "/healthz"  ──▶ 200 fijo de nginx (no depende de upstreams)
```

## 4. Decisiones de diseño y por qué

### 4.1 ¿Por qué un bastion (Nodo Gateway) en vez de IPs públicas en los workers?

Los workers cargan pesos de modelos (varios GB) y ejecutan inferencia; no tienen ninguna razón de negocio para ser alcanzables desde Internet. Ponerlos en subred privada sin IP pública **elimina por completo** la superficie de ataque directa sobre esas instancias (que además tienen GPU y son las más caras de comprometer). El Gateway, que sí necesita ser público para servir el chat/API, actúa también como bastion SSH (`ssh_proxy_command` en `.sky_config_workers.yaml`, ver `scripts/generate_infra.py::TopologyBuilder.build_sky_workers_config`) para que SkyPilot pueda aprovisionar/depurar los workers sin exponerlos.

**Alternativa descartada:** IP pública en los workers + Security Group restrictivo por CIDR. Se descartó porque (a) un CIDR "seguro" cambia (IP del operador, oficinas, VPN) y tiende a degradar a `0.0.0.0/0` con el tiempo, y (b) no elimina el riesgo de un 0-day en el propio servidor vLLM expuesto a Internet.

### 4.2 ¿Por qué SG→SG (`UserIdGroupPairs`) y no CIDR entre Gateway y Workers?

Un Security Group referenciado por otro SG (en vez de por rango de IPs) sigue siendo válido aunque el Gateway cambie de IP privada (recreación, `sky stop`/`sky start`), y **puede crearse antes de que el recurso referenciado exista** — necesario porque el SG de workers se crea antes de saber la IP del Gateway. Ver `scripts/aws_network.py::AwsNetworkManager.ensure_security_groups` (líneas 546-582) y `_sync_ingress_sg_rules` (línea 628).

**Alternativa descartada:** abrir el puerto vLLM al CIDR de la subred pública completa. Funciona, pero es más permisivo que necesario (cualquier cosa en la subred pública, no solo el Gateway, podría alcanzar a los workers) y no es auto-descriptivo en la consola de AWS.

### 4.3 ¿Por qué NAT Gateway y no VPC Endpoints exclusivamente?

Los workers necesitan salir a Internet para descargar pesos de modelos desde HuggingFace — un destino arbitrario, no un servicio de AWS. Los VPC Endpoints (S3 gateway endpoint, gratis) cubren el tráfico a S3 sin pasar por NAT, pero **no sustituyen** al NAT para tráfico general a Internet. Por eso `nat_gateway.modo: none` solo es válido si `vpc_endpoints.s3: true` y el operador entiende que **cualquier destino que no sea S3 quedará inalcanzable** desde la subred privada (ver la regla cruzada en `ConfigValidator._validate_red_auto`, `scripts/generate_infra.py:188-252`).

**Coste de esta decisión:** un NAT Gateway cuesta ~\$0.045/hora + ~\$0.005/hora la EIP asociada (~\$35-36/mes) esté ocioso o no. Es la razón de ser del `destroy_infra.py` (ver `docs/04_DESTRUCCION.md`) y de `--scan-orphans`: un NAT olvidado es el escenario de "factura sorpresa" más común en este tipo de arquitectura.

### 4.4 ¿Por qué un módulo de red separado (`aws_network.py`) en vez de Terraform/CloudFormation/Pulumi?

Restricción explícita del proyecto (ver `docs/08_AGENTES_IA.md`): todo el ciclo de vida de infraestructura vive en Python/boto3, versionado junto al resto del generador, sin una segunda herramienta de estado (`terraform.tfstate`) que reconciliar con el estado en PostgreSQL. El "estado" único es `sooniverse.infra_deployment`/`infra_resource`.

### 4.5 ¿Por qué el mecanismo de propiedad exige DOS condiciones (BD + tags), no solo una?

Si solo se confiara en la BD: un operador podría borrar manualmente una fila y el `destroy` fallaría en encontrar qué borrar en AWS, dejando recursos huérfanos cobrando. Si solo se confiara en los tags de AWS: cualquier recurso con el tag correcto (creado a mano, o de otro sistema que reutilice el mismo esquema de tags por error) se borraría. Exigir ambas —fila en `infra_resource` con ese `deployment_id` **y** tags AWS que coincidan con ese mismo `deployment_id` en el momento de borrar— hace que ambas fuentes de verdad tengan que estar de acuerdo antes de una operación destructiva. Ver `AwsNetworkManager._tags_match_deployment` (`scripts/aws_network.py:787-819`) y su uso en `destroy` (línea 820).

### 4.6 ¿Por qué nginx como única puerta de entrada, y no publicar los puertos de cada servicio?

Cada puerto publicado al host (4000, 8080, 8000) es una superficie de ataque adicional sin las protecciones de nginx (rate limiting futuro, TLS terminado en un solo sitio, un único punto de auditoría de acceso). Con `gateway.exponer_puertos_directos: false` (default), litellm/open-webui/metrics usan `expose:` (solo red interna Docker) y únicamente nginx publica 80/443. Ver `scripts/render_gateway_stack.py` y `docs/06_RUNBOOK.md` para el caso de depuración donde sí conviene alternar el flag.

### 4.7 ¿Por qué las capacidades de un modelo se resuelven por BD (`sooniverse.model_capability`) y no quedándose en `config_global.yaml`?

`config_global.yaml` declara qué capacidades **debería** tener un checkpoint (`workloads[].capacidades`), pero es una promesa del operador, no una medición. El bug real que motivó este mecanismo: Open WebUI mandaba `tool_choice="auto"` (o `response_format: json_object` en sus tareas automáticas de título/tags) a un vLLM que nunca arrancó con las banderas necesarias, y el chat completo se caía con un 400 en cada mensaje.

La infraestructura admite **múltiples modelos** (`workloads[]` es una lista) y cada uno puede tener capacidades reales distintas del mismo tipo de tarea (`llm-texto`). Una constante global en config (p.ej. "esta instalación soporta visión: sí/no") no puede representar eso; hace falta una verdad **por modelo público** (`model_public_name`), y esa verdad solo se conoce sondeando el modelo ya desplegado (`scripts/test_model_capabilities.py`, ver `docs/01_FLUJO_DESPLIEGUE.md` fase `capabilities`).

Se eligió PostgreSQL (`sooniverse.model_capability`, `database/003_model_capabilities.sql`) en vez de, por ejemplo, un archivo JSON junto a los manifiestos, porque:
- Es la misma fuente de verdad que ya usan LiteLLM y Django (`docs/03_ESTADO_Y_BD.md`) — no se añade un segundo mecanismo de estado a reconciliar.
- Las columnas `effective_*` son `GENERATED ALWAYS AS` (declarado Y sondeo=TRUE): la política fail-closed vive en el motor, no repetida en cada uno de los tres consumidores (`docker_images/openwebui/overlay/sooniverse/bootstrap_models.py`, `scripts/render_litellm_config.py`, `scripts/render_gateway_stack.py`).
- Sobrevive a la recreación del Gateway (a diferencia de un archivo en disco de la instancia): un `sky launch` nuevo no pierde el historial de sondeos.

La cadena completa, de sondeo a interfaz:

```
scripts/test_model_capabilities.py --write-db
        │  (sondea vision/tool_calling/json_object/streaming vía el Gateway público,
        │   con reintento fail-closed sobre resultados inconclusos)
        ▼
sooniverse.model_capability (columnas effective_* GENERATED, fail-closed)
        │
        ├─► scripts/sync_endpoints.py::build_endpoints()  → litellm_config.yaml
        │     (model_info.supports_vision/supports_function_calling/max_input_tokens)
        │
        └─► scripts/sync_openwebui_models.py
              ├─ scripts/render_gateway_stack.py  → ENABLE_TITLE_GENERATION/
              │    ENABLE_CODE_INTERPRETER/... del contenedor open-webui (flags
              │    globales de la instancia, unión de capacidades efectivas)
              └─ docker_images/openwebui/overlay/sooniverse/bootstrap_models.py
                   → filas `model` de Open WebUI (meta.capabilities por modelo,
                     vía la API HTTP pública de Open WebUI, nunca su ORM interno)
```

### 4.8 ¿Por qué la agregación horaria vive en su propia tabla (`usage_hourly`) y no como una granularidad más de `token_usage_rollup`?

La pregunta de negocio que motivó esta tabla —"¿en qué momento del fin de semana está sin uso la máquina?"— es **incontestable** con el rollup existente: `token_usage_rollup.bucket_start` es de tipo `DATE`, así que la agregación más fina que puede representar es un día completo. No hay forma de saber si un sábado tuvo tráfico a las 10:00 o a las 23:00.

La opción aparentemente barata (añadir `'hourly'` al `CHECK` de `granularity` y ensanchar `bucket_start` a `TIMESTAMPTZ`) rompe cuatro cosas a la vez:

- El `CHECK (granularity IN ('daily','weekly','monthly'))` y el índice único `(granularity, bucket_start, api_key_key, model_name)`.
- El modelo Django `TokenUsageRollup.bucket_start = DateField` (`django_metrics/metrics/models.py`), y con él todo el panel que ya lee esa tabla.
- Todas las filas existentes, que habría que migrar.
- La semántica de las vistas `v_usage_daily`/`v_usage_weekly`/`v_usage_monthly`, que asumen un día por fila.

Y, aun pagando ese precio, el rollup **no tendría sitio** para lo que la analítica horaria necesita de verdad: percentiles de latencia y las dimensiones locales precalculadas. `sooniverse.usage_hourly` guarda, junto a los contadores, `bucket_local_date` / `bucket_local_hour` / `bucket_local_isodow` ya cortados en la zona de reporte, de modo que el mapa de calor agrupa por columnas indexadas en vez de recalcular un `EXTRACT(... AT TIME ZONE ...)` en cada consulta.

**Regla que todo consumidor debe respetar: los percentiles NO se recombinan.** `latency_p95_ms` es el p95 *de esa hora concreta*. Promediar (o sacar el máximo de) los p95 de los trece lunes de un trimestre **no da** el p95 del lunes: un percentil no es una media, y no existe ninguna operación aritmética que reconstruya el percentil del conjunto a partir de los percentiles de sus partes. Para un percentil sobre cualquier ventana mayor de una hora hay que volver a los eventos crudos, y la única vía soportada para hacerlo es la función `sooniverse.latency_percentiles(desde, hasta, api_keys[], modelos[], incluir_cache)`.

Lo que sí es recombinable, y por eso se guarda explícitamente, es el par `latency_sum_ms` / `latency_count`: permite una media ponderada honesta entre buckets sin tocar `token_usage_event`. La vista `sooniverse.v_usage_heatmap` usa exactamente ese par para su columna `latency_media_ms`, y expone `p95_peor_hora` con un nombre deliberadamente incómodo (es el **máximo** de los p95 horarios, no el p95 agregado) para que nadie lo confunda con lo segundo.

## 5. Componentes y dónde viven

| Componente | Archivo(s) | Rol |
|---|---|---|
| Contrato central | `config_global.yaml` (o `clients/<id>/config_global.yaml`) | Única fuente de verdad declarativa |
| Validación del contrato | `scripts/generate_infra.py::ConfigValidator` | Falla rápido antes de tocar AWS/BD |
| Red AWS | `scripts/aws_network.py::AwsNetworkManager` | VPC/subredes/NAT/IGW/route tables/SGs |
| Estado persistente | `scripts/infra_state.py::PostgresInfraStateStore` | Mecanismo de propiedad, auditoría |
| Orquestación | `scripts/generate_infra.py::deploy()` | Máquina de fases: network→gateway→workers→endpoints→capabilities→capacidad→verify |
| Esquema de analítica de uso | `database/004_usage_analytics.sql` | ETL enriquecido (latencia/TTFT/estado/worker), `usage_hourly`, `app_setting`, corte de buckets en hora local |
| Esquema del benchmark | `database/005_capacity_benchmark.sql` | `sooniverse.capacity_benchmark`: techo medido + snapshot de la config bajo la que se midió |
| Benchmark de capacidad | `scripts/benchmark_capacity.py` | Rampa acotada de concurrencia desde el propio Gateway; responde "¿cuánto aguanta la infra antes de degradar?" |
| Analítica de ritmo de uso | `django_metrics/metrics/analytics.py` | Mapa de calor semanal, perfil horario y detección de ventanas ociosas |
| Margen y proyección | `django_metrics/metrics/capacidad.py` | Cruza el techo medido con el pico observado; proyección con puerta de confianza (r²) |
| Filtros temporales del panel | `django_metrics/metrics/filtros.py` | Único punto donde se resuelve la zona horaria del panel; evita que vuelva a colarse un corte en UTC |
| Descubrimiento/sync | `scripts/sync_endpoints.py` | IPs privadas de workers → `litellm_config.yaml` |
| Render del stack Gateway | `scripts/render_gateway_stack.py` | nginx + docker-compose desde el contrato |
| Interfaz de chat | `docker_images/openwebui/` | Open WebUI vendorizado (imagen derivada, tag fijo, Postgres/esquema `sooniverse`, overlay visual, patches) |
| Sondeo de capacidades reales | `scripts/test_model_capabilities.py` | Sondea vision/tool_calling/json_object/streaming contra el modelo desplegado; persiste en `sooniverse.model_capability` (fail-closed) |
| Sincronización Open WebUI ↔ capacidades | `scripts/sync_openwebui_models.py`, `docker_images/openwebui/overlay/sooniverse/bootstrap_models.py` | Aplica la verdad observada a los flags del contenedor y a los modelos de la interfaz |
| Destrucción | `scripts/destroy_infra.py` | Orden inverso, huérfanos |
| Verificación | `scripts/verify_deployment.py` | 11 comprobaciones post-despliegue |
| Inventario multi-cliente | `scripts/list_deployments.py` | Todos los clientes/entornos |

Ver `docs/01_FLUJO_DESPLIEGUE.md` para el recorrido fase por fase con referencias exactas de línea.
