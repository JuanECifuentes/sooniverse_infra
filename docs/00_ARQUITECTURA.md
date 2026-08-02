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

## 5. Componentes y dónde viven

| Componente | Archivo(s) | Rol |
|---|---|---|
| Contrato central | `config_global.yaml` (o `clients/<id>/config_global.yaml`) | Única fuente de verdad declarativa |
| Validación del contrato | `scripts/generate_infra.py::ConfigValidator` | Falla rápido antes de tocar AWS/BD |
| Red AWS | `scripts/aws_network.py::AwsNetworkManager` | VPC/subredes/NAT/IGW/route tables/SGs |
| Estado persistente | `scripts/infra_state.py::PostgresInfraStateStore` | Mecanismo de propiedad, auditoría |
| Orquestación | `scripts/generate_infra.py::deploy()` | Máquina de fases: network→gateway→workers→endpoints→verify |
| Descubrimiento/sync | `scripts/sync_endpoints.py` | IPs privadas de workers → `litellm_config.yaml` |
| Render del stack Gateway | `scripts/render_gateway_stack.py` | nginx + docker-compose desde el contrato |
| Destrucción | `scripts/destroy_infra.py` | Orden inverso, huérfanos |
| Verificación | `scripts/verify_deployment.py` | 11 comprobaciones post-despliegue |
| Inventario multi-cliente | `scripts/list_deployments.py` | Todos los clientes/entornos |

Ver `docs/01_FLUJO_DESPLIEGUE.md` para el recorrido fase por fase con referencias exactas de línea.
