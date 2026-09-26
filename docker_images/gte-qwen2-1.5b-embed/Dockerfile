# Worker vLLM en modo "solo embeddings" (--task embed) para
# Alibaba-NLP/gte-Qwen2-1.5B-instruct. Fase 4/Fase 3 del plan: primera imagen
# de embeddings del repo -tipo_tarea: "embeddings" en config_global.yaml
# estaba muerto hasta ahora (ver docs/03_ESTADO_Y_BD.md).
#
# Mismo stack CUDA13/vLLM que docker_images/qwen3.5 (requirements_embed.txt es
# una copia de requirements_qwen35.txt) para no introducir una segunda matriz
# de compatibilidad de dependencias -el pin de vllm==0.24.0 ya trae los
# kernels sm_75 compilados (ver el docstring de entrypoint.sh).
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

COPY requirements_embed.txt .

RUN pip3 install --no-cache-dir --break-system-packages \
    --index-url https://pypi.org/simple \
    --extra-index-url https://download.pytorch.org/whl/cu130 \
    --extra-index-url https://pypi.nvidia.com \
    -r requirements_embed.txt

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV CUDA_MODULE_LOADING=LAZY \
    PYTHONUNBUFFERED=1 \
    MODEL_NAME=Alibaba-NLP/gte-Qwen2-1.5B-instruct \
    HF_HOME=/root/.cache/huggingface

EXPOSE 8010

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
