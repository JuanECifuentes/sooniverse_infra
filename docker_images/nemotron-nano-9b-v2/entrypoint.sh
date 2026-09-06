#!/bin/bash
set -e

# Entrypoint de la imagen vLLM para NVIDIA-Nemotron-Nano-9B-v2.
# Mismo patron que docker_images/qwen3.5/entrypoint.sh: todos los parametros
# clave se sobreescriben por variable de entorno, para servir cualquier
# checkpoint (AWQ-4bit de 5.5 GB o el BF16 oficial de 18.6 GB) sin editar este
# script ni reconstruir la imagen.
#
# Diferencias con el entrypoint de qwen3.5 (lo que manda la tarjeta del modelo):
#  - SOLO texto: no se pasan flags multimodales (--limit-mm-per-prompt, etc.).
#  - --trust-remote-code: la config usa auto_map (remote code de NemotronH).
#  - --mamba_ssm_cache_dtype float32: sin esto la precision se degrada
#    (seccion "Use it with vLLM" de la tarjeta del modelo).
#  - --no-enable-prefix-caching: vLLM 0.10.1 no soporta prefix caching con
#    arquitecturas hibridas Mamba; se deshabilita explicitamente.
#  - Tool calling nativo via --tool-call-parser nemotron_json + el plugin
#    descargado en el Dockerfile (ENABLE_TOOL_CALLING=0 por default).

MODEL_NAME="${MODEL_NAME:?Debes definir MODEL_NAME}"
PORT="${PORT:-8008}"
# La L4 de produccion (24 GB) con el BF16 (18.6 GB) permite 16k de contexto
# con MAX_NUM_SEQS bajo; con el AWQ-4bit default sobra memoria.
MAX_MODEL_LEN="${MAX_MODEL_LEN:-16384}"
# Solo se pasa a vLLM si se define: en 0.10.1 (motor V1) el default del
# planificador ya es adecuado y el chunked prefill va activo por defecto.
MAX_NUM_BATCHED_TOKENS="${MAX_NUM_BATCHED_TOKENS:-}"
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.90}"
MAX_NUM_SEQS="${MAX_NUM_SEQS:-16}"
ENFORCE_EAGER="${ENFORCE_EAGER:-1}"
DTYPE="${DTYPE:-auto}"
KV_CACHE_DTYPE="${KV_CACHE_DTYPE:-auto}"
TENSOR_PARALLEL_SIZE="${TENSOR_PARALLEL_SIZE:-1}"
# Exigido por la tarjeta del modelo para no degradar calidad (~280 MB por
# secuencia activa con este checkpoint).
MAMBA_SSM_CACHE_DTYPE="${MAMBA_SSM_CACHE_DTYPE:-float32}"
TRUST_REMOTE_CODE="${TRUST_REMOTE_CODE:-1}"
DISABLE_PREFIX_CACHING="${DISABLE_PREFIX_CACHING:-1}"
ENABLE_TOOL_CALLING="${ENABLE_TOOL_CALLING:-0}"
TOOL_CALL_PARSER="${TOOL_CALL_PARSER:-nemotron_json}"
TOOL_PARSER_PLUGIN="${TOOL_PARSER_PLUGIN:-/app/nemotron_toolcall_parser_no_streaming.py}"

EXTRA_ARGS=()
if [ "${ENFORCE_EAGER}" = "1" ]; then
  EXTRA_ARGS+=(--enforce-eager)
fi
if [ "${TRUST_REMOTE_CODE}" = "1" ]; then
  EXTRA_ARGS+=(--trust-remote-code)
fi
if [ "${DISABLE_PREFIX_CACHING}" = "1" ]; then
  EXTRA_ARGS+=(--no-enable-prefix-caching)
fi
if [ -n "${MAX_NUM_BATCHED_TOKENS}" ]; then
  EXTRA_ARGS+=(--max-num-batched-tokens "${MAX_NUM_BATCHED_TOKENS}")
fi
if [ "${ENABLE_TOOL_CALLING}" = "1" ]; then
  if [ -z "${TOOL_CALL_PARSER}" ]; then
    echo "ERROR: ENABLE_TOOL_CALLING=1 pero TOOL_CALL_PARSER esta vacio." >&2
    exit 1
  fi
  EXTRA_ARGS+=(--enable-auto-tool-choice --tool-call-parser "${TOOL_CALL_PARSER}")
  if [ -f "${TOOL_PARSER_PLUGIN}" ]; then
    EXTRA_ARGS+=(--tool-parser-plugin "${TOOL_PARSER_PLUGIN}")
  else
    echo "AVISO: TOOL_PARSER_PLUGIN no existe en ${TOOL_PARSER_PLUGIN}; tool calling probablemente falle." >&2
  fi
fi
# Escape para las pruebas locales (RTX 5070): vLLM 0.10.1 elige el backend de
# atencion por arquitectura; si FLASH_ATTN fallara en sm_120 se puede forzar
# otro (ej. FLASHINFER) exportando VLLM_ATTENTION_BACKEND al lanzar. IMPORTANTE:
# el compose siempre define la variable (a veces como cadena vacia) y vLLM
# 0.10.1 rechaza una cadena vacia como backend ("Invalid attention backend:
# ''"), asi que si llega vacia se desmonta del entorno.
if [ -n "${VLLM_ATTENTION_BACKEND}" ]; then
  export VLLM_ATTENTION_BACKEND
else
  unset VLLM_ATTENTION_BACKEND
fi

echo "==> Levantando ${MODEL_NAME}"
echo "    port=${PORT} max-model-len=${MAX_MODEL_LEN} gpu-mem-util=${GPU_MEMORY_UTILIZATION} max-num-seqs=${MAX_NUM_SEQS} tp=${TENSOR_PARALLEL_SIZE}"
echo "    mamba-ssm-cache-dtype=${MAMBA_SSM_CACHE_DTYPE} dtype=${DTYPE} trust-remote-code=${TRUST_REMOTE_CODE} no-prefix-caching=${DISABLE_PREFIX_CACHING} tool-calling=${ENABLE_TOOL_CALLING}"

exec vllm serve "${MODEL_NAME}" \
  --port "${PORT}" \
  --max-model-len "${MAX_MODEL_LEN}" \
  --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}" \
  --max-num-seqs "${MAX_NUM_SEQS}" \
  --tensor-parallel-size "${TENSOR_PARALLEL_SIZE}" \
  --dtype "${DTYPE}" \
  --kv-cache-dtype "${KV_CACHE_DTYPE}" \
  --mamba_ssm_cache_dtype "${MAMBA_SSM_CACHE_DTYPE}" \
  "${EXTRA_ARGS[@]}"
