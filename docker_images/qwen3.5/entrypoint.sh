#!/bin/bash
set -e

# Todos los parámetros clave se pueden sobreescribir por variable de entorno.
# Así puedes usar el mismo Dockerfile/imagen para cualquier modelo Qwen3.5
# sin tener que editar este script ni reconstruir la imagen.

MODEL_NAME="${MODEL_NAME:?Debes definir MODEL_NAME}"
PORT="${PORT:-8007}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-16384}"
MAX_NUM_BATCHED_TOKENS="${MAX_NUM_BATCHED_TOKENS:-$MAX_MODEL_LEN}"
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.75}"
MAX_NUM_SEQS="${MAX_NUM_SEQS:-2}"
ENFORCE_EAGER="${ENFORCE_EAGER:-1}"
DTYPE="${DTYPE:-half}"
KV_CACHE_DTYPE="${KV_CACHE_DTYPE:-auto}"
TENSOR_PARALLEL_SIZE="${TENSOR_PARALLEL_SIZE:-1}"
LIMIT_MM_PER_PROMPT="${LIMIT_MM_PER_PROMPT:-{\"image\": 1, \"video\": 0}}"
MM_PROCESSOR_KWARGS="${MM_PROCESSOR_KWARGS:-{\"max_pixels\": 602112}}"
CUDAGRAPH_CAPTURE_SIZES="${CUDAGRAPH_CAPTURE_SIZES:-[1, 2, 4, 8]}"

# Cuantización: déjalo vacío para que vLLM la auto-detecte (funciona con
# modelos AWQ/GPTQ ya cuantizados en el repo). Si necesitas forzarla,
# define QUANTIZATION=awq (o gptq, etc.) al levantar el contenedor.
EXTRA_ARGS=()
if [ -n "${QUANTIZATION}" ]; then
  EXTRA_ARGS+=(--quantization "${QUANTIZATION}")
fi
if [ "${ENFORCE_EAGER}" = "1" ]; then
  EXTRA_ARGS+=(--enforce-eager)
fi

echo "==> Levantando ${MODEL_NAME}"
echo "    max-model-len=${MAX_MODEL_LEN} gpu-mem-util=${GPU_MEMORY_UTILIZATION} max-num-seqs=${MAX_NUM_SEQS} tp=${TENSOR_PARALLEL_SIZE}"

exec vllm serve "${MODEL_NAME}" \
  --port "${PORT}" \
  --max-model-len "${MAX_MODEL_LEN}" \
  --max-num-batched-tokens "${MAX_NUM_BATCHED_TOKENS}" \
  --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}" \
  --max-num-seqs "${MAX_NUM_SEQS}" \
  --tensor-parallel-size "${TENSOR_PARALLEL_SIZE}" \
  --dtype "${DTYPE}" \
  --limit-mm-per-prompt "${LIMIT_MM_PER_PROMPT}" \
  --mm-processor-kwargs "${MM_PROCESSOR_KWARGS}" \
  --allowed-local-media-path /tmp \
  --kv-cache-dtype "${KV_CACHE_DTYPE}" \
  --compilation-config "{\"cudagraph_capture_sizes\": ${CUDAGRAPH_CAPTURE_SIZES}}" \
  "${EXTRA_ARGS[@]}"