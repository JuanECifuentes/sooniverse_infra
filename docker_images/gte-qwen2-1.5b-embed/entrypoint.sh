#!/bin/bash
set -e

# Entrypoint de la imagen vLLM "solo embeddings" (--task embed).
# A diferencia de docker_images/qwen3.5/entrypoint.sh (runner de chat), aquí
# se QUITAN deliberadamente los flags que no aplican a un runner de *pooling*:
# --limit-mm-per-prompt, --mm-processor-kwargs, --allowed-local-media-path
# (no hay multimodal), --kv-cache-dtype/--enable-chunked-prefill (no hay
# generación autoregresiva, por tanto no hay KV cache que cachear ni prefill
# que trocear) y todo el bloque de tool-calling (no hay salida de texto que
# parsear como llamada a función).

MODEL_NAME="${MODEL_NAME:?Debes definir MODEL_NAME}"
PORT="${PORT:-8010}"
# gte-Qwen2-1.5B-instruct soporta hasta 32k nativo, pero 8k alcanza de sobra
# para el caso de uso (embeddings de documentos/consultas, no contexto largo).
MAX_MODEL_LEN="${MAX_MODEL_LEN:-8192}"
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.90}"
MAX_NUM_SEQS="${MAX_NUM_SEQS:-16}"
ENFORCE_EAGER="${ENFORCE_EAGER:-1}"
DTYPE="${DTYPE:-half}"
TENSOR_PARALLEL_SIZE="${TENSOR_PARALLEL_SIZE:-1}"
# La arquitectura Qwen2 de este checkpoint usa remote code para el pooling
# bidireccional (last-token pooling con máscara causal invertida); sin esto
# vLLM cae al forward causal normal del modelo base.
TRUST_REMOTE_CODE="${TRUST_REMOTE_CODE:-1}"

EXTRA_ARGS=()
if [ "${ENFORCE_EAGER}" = "1" ]; then
  EXTRA_ARGS+=(--enforce-eager)
fi
if [ "${TRUST_REMOTE_CODE}" = "1" ]; then
  EXTRA_ARGS+=(--trust-remote-code)
fi
# En vLLM 0.24.0 VLLM_ATTENTION_BACKEND ya NO es una variable de entorno
# -confirmado en la prueba local: solo emite un WARNING y no cambia nada- así
# que se traduce a '--attention-backend' por CLI (ver docker_images/qwen3.5/
# entrypoint.sh). 'XFORMERS' fue retirado del motor V1; usar 'TRITON_ATTN'
# para GPUs sin FlashAttention-2 (T4/Turing, SM75).
if [ -n "${VLLM_ATTENTION_BACKEND}" ]; then
  EXTRA_ARGS+=(--attention-backend "${VLLM_ATTENTION_BACKEND}")
fi

echo "==> Levantando ${MODEL_NAME} (runner=pooling convert=embed)"
echo "    port=${PORT} max-model-len=${MAX_MODEL_LEN} gpu-mem-util=${GPU_MEMORY_UTILIZATION} tp=${TENSOR_PARALLEL_SIZE}"

# CORREGIDO durante la prueba local (Fase 3): en vLLM 0.24.0 el atajo
# '--task embed' que documenta la tarjeta del modelo de gte-Qwen2 ya no
# existe -"unrecognized arguments: --task embed" al arrancar. El propio
# vLLM lo reemplazó por dos flags independientes: '--runner' (qué tipo de
# instancia -generate/pooling/draft-) y '--convert' (qué adaptador de
# pooling aplicarle a un modelo causal para usarlo como embedder). Para un
# embedder ambos hacen falta: 'pooling' solo no basta sin decirle CON QUÉ
# adaptador convertir el Qwen2ForCausalLM subyacente.
exec vllm serve "${MODEL_NAME}" \
  --runner pooling \
  --convert embed \
  --port "${PORT}" \
  --max-model-len "${MAX_MODEL_LEN}" \
  --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}" \
  --max-num-seqs "${MAX_NUM_SEQS}" \
  --tensor-parallel-size "${TENSOR_PARALLEL_SIZE}" \
  --dtype "${DTYPE}" \
  "${EXTRA_ARGS[@]}"
