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

# Copiar requerimientos
COPY requirements_qwen35.txt .

# Instalar dependencias usando DIRECTAMENTE el pip de Conda con los índices de CUDA 13
RUN pip3 install --no-cache-dir --break-system-packages \
    --index-url https://pypi.org/simple \
    --extra-index-url https://download.pytorch.org/whl/cu130 \
    --extra-index-url https://pypi.nvidia.com \
    -r requirements_qwen35.txt

# Copiar tu script de entrada
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV CUDA_MODULE_LOADING=LAZY \
    VLLM_MM_INPUT_CACHE_GIB=8 \
    VLLM_ALLOW_LONG_MAX_MODEL_LEN=1 \
    VLLM_IMAGE_FETCH_TIMEOUT=30 \
    PYTHONUNBUFFERED=1 \
    MODEL_NAME=cyankiwi/Qwen3.5-2B-AWQ-4bit \
    HF_HOME=/root/.cache/huggingface

EXPOSE 8007

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]