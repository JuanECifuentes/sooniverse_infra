#!/bin/bash
set -e

# Todos los parámetros clave se pueden sobreescribir por variable de entorno.
# Así puedes usar el mismo Dockerfile/imagen para cualquier modelo Qwen3.5
# sin tener que editar este script ni reconstruir la imagen.

MODEL_NAME="${MODEL_NAME:?Debes definir MODEL_NAME}"
PORT="${PORT:-8007}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-16384}"
MAX_NUM_BATCHED_TOKENS="${MAX_NUM_BATCHED_TOKENS:-$MAX_MODEL_LEN}"
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.95}"
MAX_NUM_SEQS="${MAX_NUM_SEQS:-2}"
ENFORCE_EAGER="${ENFORCE_EAGER:-1}"
DTYPE="${DTYPE:-half}"
KV_CACHE_DTYPE="${KV_CACHE_DTYPE:-auto}"
TENSOR_PARALLEL_SIZE="${TENSOR_PARALLEL_SIZE:-1}"
MM_PROCESSOR_KWARGS="${MM_PROCESSOR_KWARGS:-{\"max_pixels\": 602112}}"
CUDAGRAPH_CAPTURE_SIZES="${CUDAGRAPH_CAPTURE_SIZES:-[1, 2, 4, 8]}"

# Capacidades declaradas en config_global.yaml (ver scripts/generate_infra.py
# build_worker() y scripts/test_model_capabilities.py). Solo se le anuncia al
# cliente (LiteLLM/Open WebUI) lo que este checkpoint realmente soporta -evita
# el error "auto tool choice requires --enable-auto-tool-choice..." que da
# vLLM cuando Open WebUI manda tool_choice="auto" a un modelo sin esas
# banderas, y evita aceptar imágenes en un modelo sin torre de visión.
ENABLE_VISION="${ENABLE_VISION:-1}"
ENABLE_TOOL_CALLING="${ENABLE_TOOL_CALLING:-0}"
TOOL_CALL_PARSER="${TOOL_CALL_PARSER:-}"
if [ "${ENABLE_VISION}" = "1" ]; then
  LIMIT_MM_PER_PROMPT="${LIMIT_MM_PER_PROMPT:-{\"image\": 1, \"video\": 0}}"
else
  LIMIT_MM_PER_PROMPT="${LIMIT_MM_PER_PROMPT:-{\"image\": 0, \"video\": 0}}"
fi

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
if [ "${ENABLE_TOOL_CALLING}" = "1" ]; then
  if [ -z "${TOOL_CALL_PARSER}" ]; then
    echo "ERROR: ENABLE_TOOL_CALLING=1 pero TOOL_CALL_PARSER está vacío (define 'capacidades.tool_call_parser' en config_global.yaml)." >&2
    exit 1
  fi
  EXTRA_ARGS+=(--enable-auto-tool-choice --tool-call-parser "${TOOL_CALL_PARSER}")
fi

echo "==> Levantando ${MODEL_NAME}"
echo "    max-model-len=${MAX_MODEL_LEN} gpu-mem-util=${GPU_MEMORY_UTILIZATION} max-num-seqs=${MAX_NUM_SEQS} tp=${TENSOR_PARALLEL_SIZE}"
echo "    capacidades: vision=${ENABLE_VISION} tool_calling=${ENABLE_TOOL_CALLING} (parser=${TOOL_CALL_PARSER:-n/a})"

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