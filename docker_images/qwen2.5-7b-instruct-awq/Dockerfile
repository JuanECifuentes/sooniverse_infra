# Worker vLLM para Qwen/Qwen2.5-7B-Instruct-AWQ. Segundo modelo generativo de
# repuesto para la prueba multi-modelo en T4 (Fase 3 del plan): si los
# kernels Triton de Mamba2 de nemotron-nano-9b-v2 no caben en los 64 KB de
# memoria compartida de Turing (riesgo documentado en su propio docstring),
# este modelo lo reemplaza -mismo checkpoint AWQ que ya usa el resto del
# repo (formato AWQ real, NO compressed-tensors: vLLM sí tiene kernels AWQ
# para SM75, a diferencia de Marlin/Machete que exigen SM80+, ver
# docker_images/qwen3.8/docker-compose.yml).
#
# Mismo stack CUDA13/vLLM que docker_images/qwen3.5 (requirements_qwen25.txt
# es una copia de requirements_qwen35.txt).
FROM pytorch/pytorch:2.11.0-cuda13.0-cudnn9-runtime

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    build-essential \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

COPY requirements_qwen25.txt .

RUN pip3 install --no-cache-dir --break-system-packages \
    --index-url https://pypi.org/simple \
    --extra-index-url https://download.pytorch.org/whl/cu130 \
    --extra-index-url https://pypi.nvidia.com \
    -r requirements_qwen25.txt

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV CUDA_MODULE_LOADING=LAZY \
    PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
    VLLM_ALLOW_LONG_MAX_MODEL_LEN=1 \
    PYTHONUNBUFFERED=1 \
    MODEL_NAME=Qwen/Qwen2.5-7B-Instruct-AWQ \
    HF_HOME=/root/.cache/huggingface

EXPOSE 8011

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
