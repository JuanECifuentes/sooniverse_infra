# ==============================================================================
# Imagen vLLM para cyankiwi/Qwen3.8-27B-AWQ-INT4 (arquitectura qwen3_5).
# ==============================================================================
# SOBRE LAS VERSIONES (importante): la arquitectura qwen3_5 solo corre con
# vLLM muy reciente -la tarjeta de Qwen3.5 indica expresamente que hace falta
# "vLLM from the main branch"-. Por eso este Dockerfile replica EXACTAMENTE el
# stack congelado y probado de docker_images/qwen3.5 (vLLM 0.24.0 + torch
# 2.11.0 cu13), que es la unica combinacion conocida que sirve checkpoints
# qwen3_5 y la que el despliegue de produccion en L4 ya usa. No es un caso de
# "usar lo ultimo por inercia": no existe stack mas antiguo con soporte.
#
# Usamos la imagen oficial de PyTorch que ya viene con CUDA 13.0 configurado
FROM pytorch/pytorch:2.11.0-cuda13.0-cudnn9-runtime

WORKDIR /app

# Instalar dependencias básicas y herramientas de compilación
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    build-essential \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Copiar requerimientos (freeze idéntico al del worker qwen3.5 en producción)
COPY requirements_qwen38.txt .

# Instalar dependencias usando DIRECTAMENTE el pip de Conda con los índices de CUDA 13
RUN pip3 install --no-cache-dir --break-system-packages \
    --index-url https://pypi.org/simple \
    --extra-index-url https://download.pytorch.org/whl/cu130 \
    --extra-index-url https://pypi.nvidia.com \
    -r requirements_qwen38.txt

# Copiar tu script de entrada
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV CUDA_MODULE_LOADING=LAZY \
    VLLM_MM_INPUT_CACHE_GIB=8 \
    VLLM_ALLOW_LONG_MAX_MODEL_LEN=1 \
    VLLM_IMAGE_FETCH_TIMEOUT=30 \
    PYTHONUNBUFFERED=1 \
    MODEL_NAME=cyankiwi/Qwen3.8-27B-AWQ-INT4 \
    HF_HOME=/root/.cache/huggingface

EXPOSE 8009

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
