# Sooniverse — Guía de optimización de infraestructura para despliegue de IA privada (v2)

**Contexto del lector:** desarrollador fullstack (Python, JS, HTML/CSS, PostgreSQL, Linux), con nociones básicas de AWS/GCP/Azure (crear/configurar instancias, imágenes, SES, SNS) y **sin experiencia previa en Kubernetes**. Esta guía está ordenada para que puedas avanzar con lo que ya sabes primero, y sumar conocimiento nuevo justo cuando lo necesites — no antes.

**Qué cambia en esta versión respecto a la v1:**
1. Se contemplan **dos modelos de despliegue**: infraestructura en la cuenta del cliente (BYOC) — el modelo principal — e infraestructura en la cuenta de Sooniverse (multi-tenant), con estrategia de aislamiento.
2. Soporte **multi-nube** (AWS como principal, GCP/Azure como opción) con la misma interfaz de variables.
3. Soporte para **múltiples modelos combinados** por cliente (ej. LLM de texto + embeddings) con asignación fraccional de GPU.
4. Roadmap reescrito, más granular, con una **ruta de aprendizaje** adaptada a tu nivel actual.

---

## 0. Arquitectura objetivo

```
                    ┌───────────────────────────┐
Cliente / App ─────▶│   DNS + Load Balancer      │
                    │  (dominio/ruta por cliente)│
                    └─────────────┬──────────────┘
                                  │
                    ┌─────────────▼──────────────┐
                    │  LiteLLM Proxy (Gateway)    │ ← API keys, budgets,
                    │  API compatible con OpenAI  │   tracking tokens/$,
                    └─────────────┬──────────────┘   rate limits, routing
                                  │
              ┌───────────────────┼───────────────────┐
              ▼                   ▼                   ▼
      ┌──────────────┐   ┌──────────────┐    ┌──────────────┐
      │ vLLM (LLM     │   │ TEI (embed-  │    │ vLLM (LLM     │
      │ texto)        │   │ dings)       │    │ texto, otro   │
      │ GPU dedicada  │   │ fracción GPU │    │ cliente)      │
      │ o fracción    │   │ o instancia  │    │               │
      └──────────────┘   │ pequeña      │    └──────────────┘
                          └──────────────┘
              ▲                   ▲                   ▲
              └───────────────────┴───────────────────┘
                 Orquestador de modelos (KubeAI / SkyServe)
                        + autoscaling de GPU

                          ▲
              ┌───────────┴────────────┐
              │  Terraform / SkyPilot   │  ← se ejecuta contra:
              │  (control plane)        │     (a) cuenta AWS/GCP del CLIENTE
              └─────────────────────────┘     (b) cuenta AWS/GCP de SOONIVERSE

  Frontend de chat: Open WebUI → habla con LiteLLM
  Observabilidad: Prometheus + Grafana + DCGM Exporter + Langfuse/Helicone
```

La diferencia clave de esta versión: el bloque de Terraform/SkyPilot ya no asume "una sola cuenta". Es un **control plane** que tú operas desde tu propia cuenta de gestión, y que se conecta a la cuenta destino (la del cliente o la tuya) según el modelo de despliegue contratado.

---

## 1. Modelos de despliegue: BYOC vs Sooniverse-hosted

### 1.1 Modo A — BYOC (Bring Your Own Cloud) — el modelo principal

La infraestructura vive en la cuenta AWS/GCP **del cliente**. Ventajas para venderlo: el cliente ve exactamente cuánto le cuesta el compute en su propia factura de AWS (transparencia total), los datos nunca salen de su cuenta (buen argumento de venta para clientes con requisitos de compliance), y tú no cargas con el costo de las GPUs en tu propia tarjeta.

**Cómo se implementa (patrón estándar en SaaS que despliegan en cuenta del cliente — lo usan herramientas como Datadog o Snowflake):**

1. El cliente crea, en su cuenta AWS, un **IAM Role** que confía en tu cuenta de Sooniverse, con un **External ID** único por cliente (previene ataques de "confused deputy" — sin este ID, cualquiera que adivine el ARN del rol podría intentar asumirlo).

```json
// Trust policy que el cliente crea en SU cuenta AWS
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "AWS": "arn:aws:iam::<CUENTA_SOONIVERSE>:root" },
    "Action": "sts:AssumeRole",
    "Condition": { "StringEquals": { "sts:ExternalId": "<id-unico-por-cliente>" } }
  }]
}
```

2. Desde tu cuenta de gestión, Terraform asume ese rol y despliega ahí:

```hcl
provider "aws" {
  alias = "cliente_acme"
  assume_role {
    role_arn    = "arn:aws:iam::<CUENTA_CLIENTE>:role/SooniverseDeployRole"
    external_id = var.acme_external_id
  }
}

module "infra_acme" {
  source    = "./modules/gpu-stack-aws"
  providers = { aws = aws.cliente_acme }
  cliente_id = "acme"
  workloads  = var.acme_workloads   # ver sección 3, multi-modelo
}
```

3. Le das al cliente un checklist de "onboarding" (documento repetible, no algo que armes a mano cada vez): qué permisos exactos necesita el rol (EC2, VPC, IAM limitado a recursos con el tag `sooniverse-managed=true`, CloudWatch), y el External ID que le generas.

4. **Importante — dónde vive el gateway en modo BYOC**: si el cliente exige que sus datos/prompts no salgan de su cuenta, no solo las GPUs van en su cuenta — **LiteLLM y Open WebUI también se despliegan dentro de la cuenta del cliente**. Tú operas ese stack de forma remota (vía el rol asumido + tu pipeline de CI/CD), pero los prompts nunca cruzan a tu infraestructura. Para poder facturar y dar soporte necesitas:
   - Acceso de solo lectura a las métricas de uso (la tabla de "spend" de LiteLLM) vía el mismo rol asumido, o un endpoint que el cliente autoriza explícitamente para reportar consumo agregado (sin contenido de los prompts).
   - Métricas de infraestructura (Prometheus) enviadas por `remote_write` a un Grafana central tuyo — esto sí es viable porque son solo métricas numéricas (uso de GPU, latencia), no contenido.

5. Para **GCP** el equivalente es un Service Account con **Workload Identity Federation** (sin llaves estáticas, más seguro) o, como punto de partida más simple mientras aprendes, un Service Account Key con roles acotados (`roles/compute.instanceAdmin.v1` scoped a un proyecto). Empieza con la opción simple y migra a Workload Identity Federation cuando tengas más clientes en GCP.

### 1.2 Modo B — Sooniverse-hosted (multi-tenant en tu propia cuenta)

Aquí sí necesitas resolver el aislamiento entre clientes, porque todos comparten tu cuenta. Niveles de aislamiento, de más simple/barato a más fuerte (elige según el cliente y crece cuando haga falta, no implementes el nivel más fuerte desde el día uno):

| Nivel | Qué es | Cuándo usarlo |
|---|---|---|
| 1. Tags + Security Groups | Todo en la misma VPC, separado solo por tags/IAM | Nunca para producción con datos de clientes distintos — solo para pruebas internas |
| 2. VPC dedicada por cliente | Cada cliente tiene su propia VPC dentro de tu cuenta AWS | **Punto de partida recomendado** — buen balance costo/aislamiento |
| 3. Cuenta AWS dedicada por cliente | Vía AWS Organizations, cada cliente en su propia sub-cuenta | Clientes enterprise que lo exigen contractualmente; más overhead operativo |
| 4. Namespace de Kubernetes por cliente | Con `NetworkPolicy` + `ResourceQuota` | Cuando migres a K8s (fase 6 del roadmap) — aislamiento de software, no de hardware |

**Recomendación concreta para empezar:** nivel 2 (VPC dedicada por cliente) en tu cuenta AWS, y cuando llegues a Kubernetes, namespaces por cliente con quotas. Sube al nivel 3 solo si un contrato específico lo exige.

### 1.3 Un mismo código para ambos modos

La clave de diseño: tu módulo de Terraform (o tu config de SkyPilot) no debería "saber" si está desplegando en modo BYOC o Sooniverse-hosted — solo recibe un `provider` distinto (rol asumido del cliente vs. tu propia cuenta) y un `cliente_id`. Esto evita mantener dos sistemas separados.

---

## 2. Multi-nube (AWS, GCP, y Azure si hace falta)

- Crea un módulo Terraform por proveedor con **la misma interfaz de variables** (`gpu_count`, `gpu_type`, `modelo`, `cliente_id`, `workloads`): `modules/gpu-stack-aws`, `modules/gpu-stack-gcp`, `modules/gpu-stack-azure`. El resto de tu pipeline (bootstrap Docker, LiteLLM, etc.) no necesita saber en qué nube corre.
- **SkyPilot** ya trae esta abstracción resuelta de fábrica: un mismo YAML puede decir `any_of: [cloud: aws, cloud: gcp]` y él decide dónde desplegar según precio/disponibilidad, o fuerzas el proveedor si el cliente ya tiene cuenta en uno específico. Dado que tú ya sabes Python, la curva de aprendizaje de SkyPilot es mucho menor que escribir y mantener tres módulos de Terraform distintos — es una razón fuerte para adoptarlo temprano, incluso antes de dominar Terraform a fondo.
- La capa de gateway (LiteLLM) y orquestador de modelos (KubeAI) no dependen de la nube — solo hablan HTTP contra el endpoint de vLLM/TEI, así que esa parte del stack es idéntica sin importar dónde corran las GPUs.

**Recomendación práctica de secuencia:** implementa primero AWS (tu nube principal, la que ya conoces) tanto en modo BYOC como Sooniverse-hosted; agrega GCP como segundo proveedor una vez que el patrón esté probado con AWS — replicar un módulo ya probado es mucho más rápido que diseñar los dos a la vez.

---

## 3. Infraestructura multi-modelo con asignación fraccional de GPU

Este es el requisito de "25% de una máquina para embeddings, el resto para el LLM de texto". Se resuelve en dos capas: **qué motor de inferencia usar por tipo de carga**, y **cómo repartir la GPU física**.

### 3.1 Motor de inferencia según tipo de carga

- **LLM de texto (chat/generación):** vLLM — ya lo tenías contemplado en la v1.
- **Embeddings:** en vez de forzar vLLM para esto, usa **Text Embeddings Inference (TEI)** de Hugging Face — es un motor especializado en Rust, mucho más liviano que vLLM, con arranque rápido y bajo consumo de memoria, pensado exactamente para este tipo de carga (búsqueda semántica, RAG, clustering). Corre como contenedor Docker igual que vLLM, así que se integra en el mismo pipeline.

### 3.2 Cómo repartir la GPU física entre modelos

Tres mecanismos, de más simple a más sofisticado:

| Mecanismo | Cómo funciona | Aislamiento | Cuándo usarlo |
|---|---|---|---|
| **Instancia dedicada pequeña** | El modelo de embeddings corre en su propia instancia con una GPU chica (ej. L4/T4) | Total (son máquinas distintas) | **Punto de partida recomendado.** Los modelos de embeddings (BGE, E5) son livianos — casi siempre sale más simple y barato darles su propia GPU pequeña que complicarte particionando una GPU grande. |
| **Time-slicing** | Varios procesos comparten una misma GPU física por turnos (scheduler por software) | Sin aislamiento de memoria — un proceso ruidoso puede afectar a otro | Cuando quieres compartir una GPU grande ya comprada/alquilada entre cargas ligeras, sin necesidad de aislamiento estricto (ej. ambientes de desarrollo/staging) |
| **MIG (Multi-Instance GPU)** | Partición real a nivel de hardware: cada partición tiene su propia memoria y núcleos dedicados | Aislamiento total, nivel hardware | Solo en GPUs Ampere en adelante (A100, A30, H100, H200, B200) — hasta 7 particiones por GPU. Es la opción correcta para **multi-tenant real en producción**, cuando varios clientes/modelos comparten una misma GPU física cara y necesitas garantías de que uno no afecte al otro. |

**Nota importante sobre la nube:** en instancias dedicadas de AWS/GCP con GPU completa (ej. `p4d.24xlarge` con A100), tú mismo configuras MIG desde dentro de la instancia vía `nvidia-smi`, igual que harías on-prem. Algunos tipos de instancia más pequeños ya vienen como una fracción de GPU pre-configurada por el proveedor (no editable). Verifica esto al elegir el tipo de instancia.

### 3.3 Cómo se define esto por cliente (variables de entrada)

Un archivo de "perfil de carga de trabajo" por cliente es el contrato entre lo que vende comercial y lo que ejecuta tu infraestructura:

```yaml
# workloads/acme.yaml
cliente: acme
proveedor: aws
modo: byoc                      # o "sooniverse-hosted"
workloads:
  - tipo: llm-texto
    modelo: llama-3.1-70b
    motor: vllm
    gpu: A100-80GB
    gpu_fraccion: 1.0            # GPU completa
    replicas: 2
  - tipo: embeddings
    modelo: bge-m3
    motor: tei
    gpu: L4
    gpu_fraccion: 1.0            # instancia dedicada pequeña
    replicas: 1
  - tipo: embeddings-compartido
    modelo: e5-large
    motor: tei
    gpu: A100-80GB                # comparte GPU con el LLM de arriba, vía MIG
    gpu_fraccion: 0.25
    replicas: 1
```

Como ya sabes Python, la forma más natural de conectar esto con tu infraestructura es un script simple que lea este YAML y genere las variables de Terraform/SkyPilot correspondientes (`tfvars` o el YAML nativo de SkyPilot) — no necesitas un framework adicional para esto, es un problema de transformación de datos que ya sabes resolver.

---

## 4. Qué conocimientos adquirir (en orden, según tu perfil actual)

Ya tienes una base sólida: Python, JS, HTML/CSS, PostgreSQL, Linux, y nociones de instancias/imágenes/SES/SNS en la nube. Esto es lo que te falta, en el orden en que te va a hacer falta (no antes):

1. **Terraform (HCL) — 1 a 2 semanas.** Es lo más parecido a lo que ya haces "a mano" en la consola, solo que declarativo y reproducible. Con el tutorial oficial de HashiCorp Learn (gratis, interactivo) es suficiente para arrancar; no necesitas certificarte.
2. **IAM cross-account y redes (VPC, subnets, security groups, AssumeRole + External ID) — 1 semana, en paralelo con Terraform.** Es una extensión de lo que ya sabes de EC2, con foco en seguridad. Es *crítico* para el modo BYOC — sin esto no puedes ofrecer el modelo de negocio principal.
3. **Docker "GPU-aware" — pocos días si ya usas Docker.** Domina `nvidia-container-toolkit` (permite `docker run --gpus all`) y cómo montar volúmenes para persistir pesos de modelos sin re-descargarlos en cada deploy.
4. **SkyPilot — muy poca curva.** Su config es YAML + una API en Python, así que aprovechas directamente lo que ya sabes. Es tu punto de entrada a "orquestación multi-nube" sin tener que aprender Kubernetes todavía.
5. **PostgreSQL ya lo dominas** — ventaja real, porque LiteLLM usa Postgres como base de datos de persistencia (keys, spend, teams). No hay curva de aprendizaje ahí.
6. **Fundamentos de Kubernetes — cuando llegues a la fase 6 del roadmap, dedícale 3-4 semanas concentradas antes de llevar clientes reales.** No necesitas ser experto: necesitas entender bien Pods, Deployments, Services, Namespaces, ConfigMaps/Secrets, ResourceQuotas y, específico de GPU, cómo trabaja el NVIDIA GPU Operator (device plugins, node selectors/taints). Evita empezar por "Kubernetes the Hard Way" — es para entender internals, no para llegar rápido a producción. Mejor: usa un clúster gestionado (EKS o GKE) desde el día uno de tu aprendizaje, apóyate en el módulo interactivo "Kubernetes Basics" de kubernetes.io y algún curso estructurado de nivel principiante. Practica primero en un clúster de prueba (puedes usar `k3s` local, gratis, para no gastar en GPUs mientras aprendes los conceptos con nodos CPU).

**Nota sobre alternativas a Kubernetes:** existe HashiCorp Nomad, un orquestador más simple que Kubernetes y con sintaxis HCL (la misma que Terraform, así que reaprovechas conocimiento). Es una opción real si en el futuro Kubernetes te resulta demasiado pesado de operar. La razón por la que esta guía prioriza Kubernetes de todos modos es que el ecosistema de orquestadores de modelos especializados (KubeAI, vLLM Production Stack, NVIDIA GPU Operator) asume Kubernetes — con Nomad tendrías que construir tú mismo buena parte de esa capa. Vale la pena tenerlo en el radar, no como primera opción.

---

## 5. Roadmap paso a paso (detallado)

### Fase 0 (semanas 1-2) — Fundamentos + estandarizar la imagen
- Completar el tutorial de Terraform y crear/destruir una instancia EC2 de prueba (sin GPU todavía, para aprender el flujo).
- Empaquetar el proceso que hoy haces a mano en un `Dockerfile` versionado por modelo (`sooniverse/vllm-<modelo>:<version>`), probando con `nvidia-container-toolkit` en una sola instancia GPU.
- Definir la convención de tags/naming por cliente desde ya (`cliente_id`, `modo=byoc|hosted`) — es la base de todo lo que sigue.

### Fase 1 (semanas 3-4) — Gateway + chat sobre lo que ya existe (quick win)
- Levantar LiteLLM + PostgreSQL + Open WebUI vía Docker Compose en una máquina de gestión, conectados a las instancias GPU que hoy ya administras manualmente.
- Crear las primeras API keys de prueba y validar que el tracking de tokens/costo funciona.
- Este paso no depende de nada de lo demás — puedes tenerlo funcionando esta misma semana con tu infraestructura actual.

### Fase 2 (semanas 5-7) — Aprovisionamiento con Terraform, modo Sooniverse-hosted primero
- Escribe el módulo `modules/gpu-stack-aws` (instancia + bootstrap Docker vía `cloud-init`), primero solo para tu propia cuenta AWS.
- Prueba creando/destruyendo instancias para un "cliente ficticio" hasta que el flujo sea confiable.
- Conecta el bootstrap para que registre automáticamente la nueva instancia en la config de LiteLLM (evita el paso manual de "agregar el endpoint a mano").

### Fase 3 (semanas 8-10) — Habilitar modo BYOC
- Documenta el checklist de onboarding para que el cliente cree su IAM Role con External ID (sección 1.1).
- Adapta el módulo de Terraform para aceptar un `assume_role_arn` por cliente — mismo código, distinto destino.
- **Prueba el flujo completo en una segunda cuenta AWS tuya (simulando ser "cliente")** antes de hacerlo con un cliente real — esto te va a ahorrar errores costosos de permisos mal configurados.

### Fase 4 (semanas 11-13) — Multi-modelo y asignación fraccional
- Define el esquema de "perfil de carga de trabajo" (YAML, sección 3.3) y el script en Python que lo traduce a variables de Terraform.
- Agrega soporte de TEI para embeddings, primero como instancia dedicada pequeña (la opción más simple, sección 3.2).
- Solo si ya tienes un cliente que lo justifique, evalúa MIG para compartir una GPU grande entre LLM + embeddings.

### Fase 5 (semanas 14-16) — Segundo proveedor de nube
- Replica el módulo de Terraform para GCP con la misma interfaz de variables.
- En paralelo, evalúa migrar la lógica multi-nube a SkyPilot para no mantener dos módulos de Terraform por separado — puedes migrar gradualmente, cliente por cliente.

### Fase 6 (mes 5 en adelante, cuando el volumen lo justifique) — Kubernetes
- Dedica 3-4 semanas solo a fundamentos (ver sección 4, punto 6) antes de tocar producción.
- Empieza con un clúster gestionado pequeño (EKS o GKE) **solo en modo Sooniverse-hosted** — no migres BYOC todavía.
- Instala NVIDIA GPU Operator + Karpenter (autoscaling de nodos GPU) + KubeAI (orquestador de modelos: scale-to-zero, balanceo consciente del caché KV).
- Migra un cliente piloto de bajo riesgo, valida estabilidad durante al menos 2-3 semanas.
- Solo después de tener esto sólido, evalúa cómo aplicar el mismo patrón dentro de cuentas BYOC (típicamente terminas ofreciendo "Sooniverse administra un clúster EKS que vive en la cuenta del cliente" — esto es más avanzado, déjalo para cuando ya domines Kubernetes en tu propia cuenta).

**Criterio para decidir cuándo saltar a Kubernetes:** no lo hagas "porque toca" — hazlo cuando notes que estás perdiendo tiempo real actualizando/reiniciando servicios a mano en más de ~5-10 clientes simultáneos, o cuando un cliente exige SLA de auto-recuperación que hoy no puedes garantizar manualmente.

---

## 6. Tabla resumen de tecnologías

| Necesidad | Herramienta recomendada | Nota para tu caso |
|---|---|---|
| Aprovisionar GPUs, multi-nube, multi-cuenta | **Terraform** (control fino) + **SkyPilot** (abstracción multi-nube, más simple) | Empieza con Terraform en AWS por familiaridad; adopta SkyPilot para simplificar cuando sumes GCP |
| Cross-account (modo BYOC) | **IAM AssumeRole + External ID** | Patrón estándar de la industria, no hay que inventar nada |
| Motor de inferencia — LLM de texto | **vLLM** | Igual que en v1 |
| Motor de inferencia — embeddings | **Text Embeddings Inference (TEI)** | Más liviano que vLLM para esta carga específica |
| Reparto de GPU entre modelos | Instancia dedicada chica → time-slicing → **MIG** | Empieza simple (instancia dedicada), sube a MIG solo si el volumen lo justifica |
| Gateway / API keys / tracking | **LiteLLM Proxy** | En BYOC, se despliega dentro de la cuenta del cliente |
| Interfaz de chat | **Open WebUI** | Igual que en v1 |
| Orquestador de modelos sobre K8s | **KubeAI** | Recomendado sobre vLLM Production Stack por menor complejidad operativa, dado que vienes sin experiencia en K8s |
| Autoscaling de nodos GPU | **Karpenter** (AWS) / autoscaler nativo (GKE) | Solo entra en juego en la fase 6 |
| Aislamiento multi-tenant (modo hosted) | VPC dedicada por cliente → namespace K8s con quotas → cuenta AWS dedicada | Empieza en el nivel de VPC, sube solo si un contrato lo exige |
| Observabilidad | Prometheus + Grafana + DCGM Exporter + LiteLLM UI | Igual que en v1 |

---

## 7. Notas de seguridad y costo

- **External ID obligatorio siempre** en cualquier rol cross-account — sin costo adicional, evita el ataque de "confused deputy".
- **Principio de menor privilegio en el rol que el cliente te da**: pide solo los permisos que realmente usas (EC2, VPC, IAM limitado a recursos con tu tag), nunca `AdministratorAccess`. Es un argumento de venta ("solo pedimos acceso a lo que gestionamos") tanto como una práctica de seguridad.
- **En modo BYOC, la factura de compute la ve y paga el cliente directamente** — tu modelo de cobro es por el servicio de gestión, no por el hardware. Esto simplifica tu operación financiera (no cargas con el riesgo de fluctuación de precios de GPU).
- **En modo Sooniverse-hosted, monitorea utilización de GPU desde el día uno** (aunque sea con `nvidia-smi` manual al inicio) — es común que clústeres sin optimizar mantengan GPUs prendidas muy por debajo de su capacidad real, y ahí es donde se va el margen del negocio.

---

### Resumen del cambio de enfoque
La v1 asumía una sola cuenta y un solo modelo por cliente. Esta versión reconoce que el producto real de Sooniverse tiene dos modalidades comerciales (BYOC y hosted) que comparten el mismo código de infraestructura, y que "el modelo" que corres por cliente puede ser en realidad una combinación de cargas (texto + embeddings) con necesidades de GPU distintas. El roadmap está secuenciado para que cada fase produzca algo vendible por sí sola, sin esperar a tener todo el stack completo para empezar a operar con clientes reales.
