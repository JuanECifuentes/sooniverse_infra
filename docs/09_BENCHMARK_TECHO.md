# 09. Benchmark de techo y verificación con peticiones normales

Este documento describe **cómo** se midió el techo de throughput de un workload (caso real: `cyankiwi/Qwen3.5-9B-AWQ-4bit`, ver `clients/bench-l4/` y `clients/bench-l40s/`) y **cómo verificar** ese techo con tráfico realista (8k tokens de entrada / 1k de salida). Los resultados están en `docs/INFORME_BENCHMARK_QWEN35-9B_L4_vs_L40S.md` y en crudo en `docs/benchmarks/*.json`.

## 1. Piezas

| Pieza | Para qué |
|---|---|
| `docker_images/qwen3.5-9b/` | Imagen vLLM del 9B: copia de `qwen3.5/` con `max-model-len 32768`, CUDA graphs (`ENFORCE_EAGER=0`), `max_num_seqs 64`, `max_num_batched_tokens 16384`, visión activa. Los `CUDAGRAPH_CAPTURE_SIZES` se generan solos hasta `MAX_NUM_SEQS`. |
| `clients/bench-l4/`, `clients/bench-l40s/` | Contratos efímeros (sin dominio, HTTP sobre IP). L40S usa `azs: 6` (ver §5). |
| `scripts/benchmark_ceiling.py` | Barrido de techo + perfil realista + visión + predicción. Solo librería estándar: corre en el Gateway sin instalar nada. |
| `scripts/persist_ceiling.py` | Guarda el resultado en `sooniverse.capacity_benchmark` (JSON completo en `notas`). |
| `scripts/benchmark_capacity.py` | La rampa corta de cada despliegue (fase `capacidad`). No busca el techo: para en la "rodilla". |

## 2. Cómo se mide el techo

Bucle cerrado (N hilos sin pausa) desde el Gateway, subiendo la concurrencia hasta que el throughput se aplana. Se mide **directo al worker** (`http://<ip-privada>:8007`, motor vLLM puro) y aparte **a través de LiteLLM** (camino real del cliente) para aislar el coste del Gateway.

- Cada nivel descarta `warm` segundos y mide la ventana siguiente. El throughput "oficial" sale de los **contadores del propio vLLM** (`vllm:generation_tokens_total`, `vllm:prompt_tokens_total`, vía `/metrics`); el calculado en el cliente se guarda al lado como contraste.
- Prompts únicos (nonce + palabras aleatorias) para que el prefix cache no infle el prefill. Longitud de salida fija con `ignore_eos`; razonamiento de Qwen3.5 desactivado (`enable_thinking: false`).
- Perfiles: `decode` (128 in / 512 out) → techo **D** de tokens de salida/s; `prefill` (8192 in / 1 out) → techo **P** de tokens de entrada/s; `realistic` (8192 / 1024); `vision` (1 imagen 640x480 + 128 out, con sonda que comprueba que el modelo ve la imagen).

```bash
# desde el Gateway (scp del script; python3 basta)
python3 benchmark_ceiling.py --label l4-direct \
  --base-url http://<worker-ip>:8007/v1 --metrics-url http://<worker-ip>:8007/metrics \
  --model cyankiwi/Qwen3.5-9B-AWQ-4bit \
  --levels "decode=1,4,8,16,32,48,64;realistic=1,2,4,8,16,24,32" --out ceiling.json

# a través de LiteLLM (camino real)
python3 benchmark_ceiling.py --label l4-gateway --base-url http://127.0.0.1/v1 \
  --model sooniverse-qwen3.5-9b --api-key $LITELLM_MASTER_KEY --profiles decode,realistic ...

# a la BD
python scripts/persist_ceiling.py --config clients/bench-l4/config_global.yaml \
  --json ceiling.json --deployment-id <uuid>
```

## 3. Cómo verificar el techo con peticiones normales (8k in / 1k out)

El techo sintético (D y P) no dice cuántas peticiones "de verdad" caben. Propuesta en tres pasos:

1. **Predecir.** Una petición 8k/1k ocupa la GPU `T = 8192/P + 1024/D` segundos (prefill a velocidad P, decode a velocidad agregada D). Predicción: `req/s = 1/T`; `tokens_salida/s = 1024/T`.
2. **Medir** el perfil `realistic` subiendo concurrencia hasta que el throughput deje de crecer o la KV cache llegue a ~100 % (ahí vLLM empieza a expulsar secuencias y el rendimiento cae).
3. **Comparar** `ratio = medido / predicho`. Un ratio ≥ 0.8 valida el techo; un ratio menor delata otro límite (KV cache, interferencia prefill/decode, cola). El ratio sirve además como **factor de degradación**: dimensionar con `techo_sintético × ratio`.

Resultado para Qwen3.5-9B: ratio entre **0.67 y 0.86** según GPU y criterio (ver informe §4) → usar un factor **0.7** sobre el techo sintético para tráfico 8k/1k.

Para producción, repetir el paso 2 con la distribución real de longitudes (percentiles p50/p95 de `sooniverse.token_usage_event`) en lugar de 8k/1k fijos.

## 4. Trampas conocidas (medidas, no supuestas)

- **Ventana corta + peticiones largas = ruido.** Con 8k/1k una petición dura 30-150 s; en una ventana de 150 s a concurrencia alta se solapan "oleadas" sincronizadas de prefill y decode, y el throughput de la ventana oscila ±15-25 % (el contador del motor y el del cliente difieren). Para cifras finas usar ventanas ≥ 5 min o promediar varias corridas.
- **El Gateway también tiene techo.** `t4g.large` (2 vCPU) con LiteLLM saturó la CPU a ~1.7k tokens/s streameados (L40S) y, a concurrencia 64, LiteLLM puso el único worker en *cooldown* (`allowed_fails: 2`, `cooldown_time: 30`) y devolvió HTTP 429 a la mayoría de peticiones. Con réplica única el cooldown convierte una ráfaga de fallos en indisponibilidad total.
- **KV cache manda en contextos largos.** Qwen3.5 es híbrido (Gated DeltaNet + atención completa cada 4 capas): KV pequeño por token, pero cada secuencia reserva además estado de las capas lineales. En L4 la KV llega a 87-99 % con 24-32 peticiones de 9k tokens.

## 5. Notas operativas de los despliegues de prueba

- `g6e.xlarge` (L40S) devolvió `InsufficientInstanceCapacity` en us-east-1a/b/c durante horas; AWS indicaba capacidad en d/f. Con `azs: 3` no hay subred en d. `azs` es inmutable (`requires-destroy`), así que hubo que destruir y recrear con `azs: 6`.
- `iam:CreateUser` denegado al usuario de despliegue: se omite el usuario de "Apagar/Arrancar" del panel (no afecta al despliegue).
- La fase `capacidad` integrada no devolvió resultado en ninguno de los dos despliegues ("No se encontró el JSON del benchmark en la salida remota"); no se diagnosticó, y `benchmark_ceiling.py` cubre la medición.
