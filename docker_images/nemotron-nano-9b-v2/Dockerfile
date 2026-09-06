# Imagen vLLM para NVIDIA-Nemotron-Nano-9B-v2 (arquitectura hibrida Mamba2-Transformer).
#
# Base: la imagen oficial vllm/vllm-openai:v0.10.1, la MISMA que la tarjeta del
# modelo (https://huggingface.co/nvidia/NVIDIA-Nemotron-Nano-9B-v2) usa en su
# ejemplo de Docker. Razon: es la generacion de stack (torch 2.7.1 + CUDA 12.8)
# que soporta tanto la L4 de produccion (sm_89, Ada) como la RTX 5070 de pruebas
# locales (sm_120, Blackwell), sin tecnologias exclusivas de las arquitecturas
# mas recientes. Ademas trae ya compilados los kernels Mamba/hybrid que la
# arquitectura necesita, sin peleas con versiones de torch/CUDA.
FROM vllm/vllm-openai:v0.10.1

WORKDIR /app

# Plugin de tool-calling nativo publicado en el repo del modelo (lo exige
# --tool-call-parser nemotron_json, ver tarjeta del modelo, seccion
# "Using Tool-Calling with a vLLM Server"). Si HF no lo sirviera, la imagen
# sigue funcionando con ENABLE_TOOL_CALLING=0 (el default).
RUN curl -fsSL -o /app/nemotron_toolcall_parser_no_streaming.py \
      "https://huggingface.co/nvidia/NVIDIA-Nemotron-Nano-9B-v2/raw/main/nemotron_toolcall_parser_no_streaming.py" \
    || echo "AVISO: plugin de tool-calling no disponible; imagen con ENABLE_TOOL_CALLING=0."

# Script de entrada parametrico (mismo patron que docker_images/qwen3.5):
# todo lo clave se configura por variables de entorno, asi la misma imagen
# sirve cualquier checkpoint Nemotron-Nano-9B-v2 sin reconstruir.
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Default: checkpoint AWQ-4bit (~5.5 GB) de cyankiwi, que arranca en GPUs de
# 12 GB (RTX 5070 de pruebas) y sobra memoria en la L4 de produccion. El repo
# oficial BF16 (nvidia/NVIDIA-Nemotron-Nano-9B-v2, ~18.6 GB) exige una GPU de
# 24 GB (L4/A10g) y se lanza con:
#   MODEL_NAME=nvidia/NVIDIA-Nemotron-Nano-9B-v2  (y MAX_NUM_SEQS bajo: el
#   cache Mamba en float32 consume ~280 MB por secuencia)
ENV CUDA_MODULE_LOADING=LAZY \
    VLLM_ALLOW_LONG_MAX_MODEL_LEN=1 \
    PYTHONUNBUFFERED=1 \
    MODEL_NAME=cyankiwi/NVIDIA-Nemotron-Nano-9B-v2-AWQ-4bit \
    HF_HOME=/root/.cache/huggingface

EXPOSE 8008

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
