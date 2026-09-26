#!/bin/bash
set -e

# Entrypoint para Qwen/Qwen2.5-7B-Instruct-AWQ. Mismo patrón que
# docker_images/qwen3.5/entrypoint.sh; se quitan los flags multimodales (este
# checkpoint es solo texto) y se deja QUANTIZATION vacío para que vLLM
# auto-detecte el método AWQ del checkpoint (elige entre kernels 'awq' o
# 'awq_marlin' según la arquitectura de la GPU -Marlin en SM80+, awq puro en
# Turing/SM75).

MODEL_NAME="${MODEL_NAME:?Debes definir MODEL_NAME}"
PORT="${PORT:-8011}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-16384}"
MAX_NUM_BATCHED_TOKENS="${MAX_NUM_BATCHED_TOKENS:-8192}"
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.90}"
MAX_NUM_SEQS="${MAX_NUM_SEQS:-8}"
ENFORCE_EAGER="${ENFORCE_EAGER:-1}"
DTYPE="${DTYPE:-half}"
KV_CACHE_DTYPE="${KV_CACHE_DTYPE:-auto}"
TENSOR_PARALLEL_SIZE="${TENSOR_PARALLEL_SIZE:-1}"
QUANTIZATION="${QUANTIZATION:-}"
ENABLE_TOOL_CALLING="${ENABLE_TOOL_CALLING:-0}"
TOOL_CALL_PARSER="${TOOL_CALL_PARSER:-}"

EXTRA_ARGS=()
if [ -n "${QUANTIZATION}" ]; then
  EXTRA_ARGS+=(--quantization "${QUANTIZATION}")
fi
if [ "${ENFORCE_EAGER}" = "1" ]; then
  EXTRA_ARGS+=(--enforce-eager)
fi
if [ "${MAX_NUM_BATCHED_TOKENS}" -lt "${MAX_MODEL_LEN}" ]; then
  EXTRA_ARGS+=(--enable-chunked-prefill)
fi
if [ "${ENABLE_TOOL_CALLING}" = "1" ]; then
  if [ -z "${TOOL_CALL_PARSER}" ]; then
    echo "ERROR: ENABLE_TOOL_CALLING=1 pero TOOL_CALL_PARSER está vacío." >&2
    exit 1
  fi
  EXTRA_ARGS+=(--enable-auto-tool-choice --tool-call-parser "${TOOL_CALL_PARSER}")
fi
# Ver docker_images/qwen3.5/entrypoint.sh: VLLM_ATTENTION_BACKEND ya no es
# una env var en vLLM 0.24.0 -se traduce a '--attention-backend' por CLI.
if [ -n "${VLLM_ATTENTION_BACKEND}" ]; then
  EXTRA_ARGS+=(--attention-backend "${VLLM_ATTENTION_BACKEND}")
fi

echo "==> Levantando ${MODEL_NAME}"
echo "    port=${PORT} max-model-len=${MAX_MODEL_LEN} gpu-mem-util=${GPU_MEMORY_UTILIZATION} max-num-seqs=${MAX_NUM_SEQS} tp=${TENSOR_PARALLEL_SIZE}"

exec vllm serve "${MODEL_NAME}" \
  --port "${PORT}" \
  --max-model-len "${MAX_MODEL_LEN}" \
  --max-num-batched-tokens "${MAX_NUM_BATCHED_TOKENS}" \
  --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}" \
  --max-num-seqs "${MAX_NUM_SEQS}" \
  --tensor-parallel-size "${TENSOR_PARALLEL_SIZE}" \
  --dtype "${DTYPE}" \
  --kv-cache-dtype "${KV_CACHE_DTYPE}" \
  "${EXTRA_ARGS[@]}"
