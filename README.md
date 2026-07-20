# Sooniverse Infrastructure (Sooniverse Infra)

## 📌 Introducción
Este repositorio contiene la infraestructura automatizada de **Sooniverse** para el despliegue de cargas de trabajo de Inteligencia Artificial (LLMs y Embeddings) basadas en contenedores Docker acelerados por GPU sobre AWS. 

El sistema elimina la configuración manual mediante una arquitectura basada en un **contrato centralizado de configuración** (`config_global.yaml`) y un motor generador escrito en Python que interactúa directamente con **SkyPilot** y **Docker Compose**.

---

## 🏗️ Arquitectura del Sistema

A continuación se detalla el flujo desde la definición del contrato hasta la ejecución del contenedor en la nube:

```mermaid
graph TD
    A[config_global.yaml <br><i>Contrato Único</i>] -->|Lectura & Validación| B(scripts/generate_infra.py <br><i>Generador / CLI</i>)
    B -->|Genera| C[.sky_generated.yaml <br><i>Spec de SkyPilot</i>]
    B -->|Opcional: --run| D[SkyPilot CLI]
    D -->|Aprovisionamiento BYOC / Hosted| E[AWS EC2 Instance <br><i>e.g. g4dn.xlarge (GPU T4)</i>]
    E -->|Ejecuta Setup Remoto| F[Instalación de Docker + NVIDIA Toolkit]
    F -->|Montaje de Volumen Persistente| G[hf_cache/ <br><i>Caché de HuggingFace</i>]
    F -->|Lanza Run Remoto| H[Docker Compose: vLLM Service]
```

---

## 📄 Diseño del Contrato: `config_global.yaml`

El archivo `config_global.yaml` es la única fuente de verdad (*Source of Truth*). La estructura y restricciones del esquema se detallan a continuación para su correcto procesamiento por humanos y agentes de IA:

```yaml
# 1. Datos del Cliente y Entorno
cliente:
  id: "acme"                  # [String] ID único del cliente/inquilino (tenant)
  entorno: "prod"             # [Enum] prod | dev
  modo: "byoc"                # [Enum] byoc (cliente aporta cuenta AWS) | hosted (nuestra cuenta)

# 2. Variables de Red y Aislamiento
red_y_aislamiento:
  region: "us-east-1"         # [String] Región de AWS a desplegar
  image_id: "ami-0c7217cdde317cfec" # [String | null] ID de la AMI personalizada (opcional/dinámica)
  vpc_id: null                # [String | null] VPC dedicada. Usar null para VPC por defecto
  subnet_id: null             # [String | null] Subred específica
  tags_obligatorios:          # [Map] Metadatos aplicados como AWS Resource Tags
    cliente_id: "acme"
    modo: "byoc"
    entorno: "prod"
    gestionado_por: "sooniverse"

# 3. Perfil de Cargas de Trabajo (Multi-modelo)
workloads:
  - id: "qwen3-5-llm"
    modelo: "qwen3.5"
    hf_repo: "cyankiwi/Qwen3.5-2B-AWQ-4bit" # [String] Repositorio Hugging Face del modelo
    tipo_tarea: "llm-texto"    # [Enum] llm-texto | embeddings
    accelerator: "T4"          # [Enum] Tipo de GPU física (ej: T4, A10G, A100)
    cantidad_gpus: 1           # [Integer] Cantidad física de GPUs (> 0)
    tipo_instancia: "g4dn.xlarge" # [String | null] Override de instancia AWS (opcional)
    puerto: 8007               # [Integer] Puerto local y de red expuesto por la API
    asignacion_fraccional:
      habilitado: true         # [Boolean] Activa la optimización fraccional de GPU
      gpu_memory_utilization: 0.75 # [Float] Límite de VRAM dedicada (0.0 a 1.0)
      max_model_len: 16384     # [Integer] Contexto máximo permitido del modelo
```

---

## 🛠️ Requisitos Previos

Antes de ejecutar el generador, asegúrate de tener configurado tu entorno:

1. **WSL (Windows Subsystem for Linux) / Linux**: Entorno de ejecución preferido.
2. **Python 3.10+** (Recomendado Python 3.12):
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r docker_images/qwen3.5/requirements_qwen35.txt pyyaml
   ```
3. **Credenciales de AWS**:
   Instala y configura AWS CLI con acceso de Administrador/Aprovisionamiento:
   ```bash
   aws configure
   ```
4. **SkyPilot CLI**:
   ```bash
   pip install "skypilot[aws]"
   sky check
   ```

---

## 🚀 Manual de Uso (CLI)

El script principal de automatización se encuentra en [scripts/generate_infra.py](file:///c:/Users/Cifu/Documents/Servicios/sooniverse_infra/scripts/generate_infra.py).

### 1. Solo Generar YAML de Infraestructura
Crea el manifiesto de SkyPilot `.sky_generated.yaml` sin lanzarlo:
```bash
python scripts/generate_infra.py
```

### 2. Generar y Lanzar Dinámicamente en AWS (Transparente)
Ejecuta la validación, genera el manifiesto e invoca la inicialización y despliegue del clúster en la nube AWS de inmediato:
```bash
python scripts/generate_infra.py --run
```

### 3. Argumentos Soportados del CLI
* `--config <ruta>`: Ruta personalizada al archivo de configuración de entrada (por defecto: `config_global.yaml`).
* `--output <ruta>`: Ruta personalizada del archivo generado para SkyPilot (por defecto: `.sky_generated.yaml`).
* `--run`: Inicia inmediatamente el proceso de `sky launch` tras generar el archivo.

---

## 🤖 Manual para Agentes de IA (AI Agents Instructions)

Si eres un **agente de IA** encargado de gestionar o escalar este repositorio de infraestructura, sigue estas pautas automatizadas:

### 1. Flujo de Modificación
1. **No edites `.sky_generated.yaml`**: Cualquier cambio en la infraestructura debe realizarse modificando el archivo central [config_global.yaml](file:///c:/Users/Cifu/Documents/Servicios/sooniverse_infra/config_global.yaml).
2. **Reglas de Validación**:
   - `cliente.entorno` solo acepta `"prod"` o `"dev"`.
   - `cliente.modo` solo acepta `"byoc"` o `"hosted"`.
   - `workloads[].tipo_tarea` solo acepta `"llm-texto"` o `"embeddings"`.
   - `workloads[].cantidad_gpus` debe ser un entero estrictamente mayor a `0`.
3. **Generación**: Después de modificar la configuración, ejecuta siempre `python scripts/generate_infra.py` para sincronizar los cambios locales con el manifiesto.

### 2. Comando Rápido de Validación por CLI
Para verificar si un cambio en el YAML es sintáctica y conceptualmente válido sin lanzar recursos físicos:
```bash
python -c "from scripts.generate_infra import load_config; load_config('config_global.yaml')"
```
*Si la ejecución sale con código `0`, el archivo es completamente válido.*

---

## 💾 Persistencia de Modelos y Caché (`hf_cache`)

Para evitar que los modelos de Hugging Face de gran volumen (varios gigabytes) se vuelvan a descargar cada vez que el clúster se reinicia o reconstruye, la infraestructura implementa el siguiente mecanismo:

1. **Montaje a Nivel de SkyPilot**:
   El manifiesto mapea el directorio del espacio de trabajo local en la máquina host remota (`~/sooniverse_infra`).
2. **Volumen de Docker**:
   En el servidor remoto, el directorio `~/sooniverse_infra/docker_images/qwen3.5/hf_cache` es mapeado dentro del contenedor de vLLM en `/root/.cache/huggingface`.
3. **Ciclo de Vida**:
   Los archivos descargados por primera vez persistirán en el volumen del host AWS EC2, reduciendo los tiempos de inicialización subsecuentes de minutos a escasos segundos.
