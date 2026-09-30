# Informe: techo de throughput de Qwen3.5-9B AWQ-4bit en NVIDIA L4 vs L40S (AWS)

**Fecha de las pruebas:** 29-30 sep 2026 · **Región:** us-east-1 · **Modelo:** `cyankiwi/Qwen3.5-9B-AWQ-4bit` (visión activa) · **Motor:** vLLM 0.24.0, imagen `docker_images/qwen3.5-9b/` · **Método y trampas:** `docs/09_BENCHMARK_TECHO.md` · **Datos crudos:** `docs/benchmarks/*.json` y `sooniverse.capacity_benchmark` (BD de la prueba).

## 1. Resumen ejecutivo

| | **L4** (`g6.xlarge`) | **L40S** (`g6e.xlarge`) | L40S / L4 |
|---|---|---|---|
| Techo de **salida** (decode, prompts cortos) | **~930 tok/s** | **~3,070 tok/s** (aún crece al tope de 128 secuencias) | 3.3× |
| Techo de **entrada** (prefill puro, 8k) | **~3,010 tok/s** | **~11,600 tok/s** | 3.9× |
| Un solo usuario (velocidad de escritura) | 39 tok/s (25 ms/token) | 102 tok/s (9.7 ms/token) | 2.6× |
| Tiempo hasta 1er token con prompt de 8k | 2.7 s | 0.7 s | 3.9× |
| Petición **8k in / 1k out**, capacidad sostenida | **~0.17 req/s** (~180 tok/s de salida) | **~0.65 req/s** (~700 tok/s de salida) | 3.8× |
| Costo por hora (todo incluido) | $0.97 | $2.02 | 2.1× |
| **Tokens netos/mes 24/7, tráfico 8k/1k** | **≈ 4,100 M** | **≈ 15,800 M** | 3.8× |
| **Escenario realista (35 % de uso)** | **≈ 1,450 M/mes** | **≈ 5,500 M/mes** | 3.8× |
| Costo/petición 8k/1k al 100 % de uso vs GPT-6 Luna ($0.00134) | $0.00158 (**+18 %**) | $0.00086 (**−35 %**) | |
| Uso mínimo para empatar con GPT-6 Luna (8k/1k) | **nunca** (>100 %) | **65 %** | |

**Conclusiones clave**
1. **La L40S es la mejor compra por dólar:** rinde 3.3–3.9× por 2.1× de precio (1.6–1.9× más tokens por dólar).
2. **Contra GPT-6 Luna ($0.10 / $0.50 por M tokens entrada/salida), autoalojar solo gana con mucho uso y solo en L40S.** Con tráfico 8k/1k, la L40S empata a ~65 % de utilización; la L4 no empata ni al 100 %. Con tráfico "corto in / largo out" (chat), ambas ganan a partir de ~34 % (L40S) y ~54 % (L4). Al 35 % de uso (escenario realista) y GPU 24/7, **Luna sale entre 1.8× (L40S) y 3.4× (L4) más barata** (§6).
3. **El techo sintético sobrestima el tráfico real 8k/1k en 15-35 %** (ratio medido/predicho 0.67-0.86): dimensionar con factor 0.7 (§4).
4. **En 8k/1k el cuello de botella es el prefill y la KV cache, no el decode.** Por eso 8k/1k rinde 4-5× menos tokens de salida/s que el techo de decode.
5. **En L40S el Gateway (`t4g.large`, 2 vCPU) es el nuevo techo**: a través de LiteLLM el decode se aplana en ~1.7k tok/s (55 % del motor) y a concurrencia 64 hubo HTTP 429 masivo (§5).

## 2. Configuración desplegada

| Parámetro | L4 | L40S |
|---|---|---|
| Instancia / GPU | g6.xlarge · 1× L4 24 GB (Ada) | g6e.xlarge · 1× L40S 48 GB (Ada) |
| `max-model-len` | 32,768 | 32,768 |
| `max_num_seqs` | 64 | 128 |
| `max_num_batched_tokens` | 16,384 (chunked prefill) | 16,384 |
| `gpu_memory_utilization` | 0.92 | 0.92 |
| CUDA graphs | sí (FULL decode + PIECEWISE, hasta 64) | sí (hasta 128) |
| Atención / dtype / KV | FlashAttention-2 · half · KV auto | ídem |
| VRAM usada | 19.1 / 23.0 GB | 40.6 / 46.1 GB |
| KV cache disponible | 9.35 GiB = **287,961 tokens** (8.8× contextos de 32k) | 29.67 GiB = **914,028 tokens** (27.9× de 32k) |
| Visión | activa (sonda: ve la imagen; 336 tokens por imagen 640×480) | ídem |
| Arranque de vLLM (carga+compilación+grafos) | ~127 s | ~127 s |
| Gateway | t4g.large + LiteLLM (4 workers) + nginx | ídem |

Decisiones de afinado para throughput (imagen 9B vs la 2B): CUDA graphs activados (la 2B iba en eager), 64→128 secuencias, chunk de prefill de 16k, y `CUDAGRAPH_CAPTURE_SIZES` generados automáticamente hasta `MAX_NUM_SEQS`. **Experimento en L4:** subir a 96 secuencias **no mejora** (64 → 938, 80 → 867, 96 → 880 tok/s): el cómputo ya está saturado a 64, así que 64 es el óptimo en L4. En L40S el techo seguía subiendo al tope (2,737 → 3,067 tok/s entre 96 y 128 secuencias): probablemente hay algo más de margen con `max_num_seqs` > 128 (no medido).

## 3. Resultados del barrido de techo (directo al motor vLLM)

Cifras del contador del propio vLLM; ventana de 45 s tras 15 s de calentamiento; prompts únicos (sin prefix cache); salida fija.

**Perfil decode (≈170 tokens de entrada, 512 de salida)**

| Conc. | L4 tok/s | L4 ms/token | L40S tok/s | L40S ms/token |
|---:|---:|---:|---:|---:|
| 1 | 39 | 25.4 | 102 | 9.7 |
| 8 | 267 | 29.2 | 710 | 11.0 |
| 16 | 456 | 33.1 | 1,239 | 12.4 |
| 32 | 686 | 43.3 | 1,976 | 15.4 |
| 48 | 839 | 52.9 | – | – |
| 64 | **928** | 64.9 | 2,675 | 22.9 |
| 96 | – | – | 2,737 | 32.5 |
| 128 | – | – | **3,067** | 39.2 |

**Perfil prefill (8,192 tokens de entrada, 1 de salida):** L4 **~3,010 tok/s** (planos de 1 a 8 concurrentes; una petición = 2.7 s). L40S **~11,600 tok/s** (0.7 s por petición).

**Visión (1 imagen 640×480 + 128 tokens de salida), sin llegar a saturar:** L4 a concurrencia 16: ~296 tok/s de salida y 2.4 req/s (TTFT p50 1.4 s). L40S a concurrencia 32: ~881 tok/s y 6.9 req/s (TTFT p50 0.5 s).

## 4. Verificación del techo con peticiones normales (8k in / 1k out)

Modelo: una petición ocupa la GPU `T = 8192/P + 1024/D`.

| | L4 | L40S |
|---|---|---|
| P (prefill) / D (decode) medidos | 3,010 / 928 tok/s | 11,616 / 3,067 tok/s |
| Segundos de GPU por petición 8k/1k | 3.84 s | 1.04 s |
| **Predicho** | 0.26 req/s · 267 tok/s salida | 0.96 req/s · 982 tok/s salida |
| **Medido, mejor nivel** (motor / cliente) | 215 / 143 tok/s (c=16) · 0.14–0.21 req/s | 844 / 580 tok/s (c=64) · 0.57–0.82 req/s |
| **Medido, meseta robusta** | ~180 tok/s · **~0.17 req/s** (c=8-32) | ~700 tok/s · **~0.65 req/s** (c=32-48) |
| Ratio medido/predicho | 0.67 – 0.80 | 0.71 – 0.86 |

Lectura: el techo predicho **se confirma con una degradación de ~15-35 %** (interferencia prefill/decode, expulsiones por KV). Los límites reales del 8k/1k:
- **L4:** la KV llega a 58 % con 16 peticiones, 87 % con 24 y 98.6 % con 32; a partir de 24 hay peticiones esperando y baja el rendimiento. **Punto de trabajo razonable: 4-8 concurrentes** (TTFT p50 9-12 s, latencia total p95 44-69 s): la L4 con prompts de 8k es lenta para uso interactivo.
- **L40S:** crece hasta ~64 concurrentes (KV 73 %); a 96 la KV toca 100 % y cae a ~625 tok/s. **Punto de trabajo recomendado: 16 concurrentes** → 550 tok/s de salida, TTFT p50 5.5 s (p95 9.4 s), latencia p95 32 s; o 8 concurrentes → 428 tok/s, TTFT p95 5.3 s.

Precisión: con peticiones de 30-150 s y ventanas de 150 s, el throughput por ventana oscila ±15-25 % (el contador del motor y el del cliente difieren, sobre todo en L4 y a concurrencia alta). Las cifras de meseta ("~180" y "~700") están redondeadas de forma conservadora; para contratos con SLA conviene repetir con ventanas ≥ 5 min.

## 5. Efecto del Gateway (camino real vía LiteLLM)

| Prueba | Motor directo | A través de LiteLLM |
|---|---|---|
| L4 decode, c=16 / c=64 | 456 / 928 tok/s | 441 / 887 tok/s (−3 % / −4 %, 0 errores) |
| L4 8k/1k, c=8 | 131 tok/s | 130 tok/s |
| L40S 8k/1k, c=16 | 550 tok/s | 539 tok/s (4 errores 429) |
| L40S decode, c=32 (motor: 1,976 tok/s) | 1,976 tok/s | 1,667 tok/s (−16 %); c=48: 1,743 tok/s (se aplana) |
| L40S decode, c=64 | 2,675 tok/s | **543 de 696 peticiones con HTTP 429** |

Causa: los 4 workers de LiteLLM consumen ~190 % de los 2 vCPU del `t4g.large` (verificado con `top`); a c=64 aparecen fallos, y con **una sola réplica** `allowed_fails: 2` + `cooldown_time: 30` deja al pool entero en cooldown ("No deployments available… Try again in 30 seconds"). Con tráfico 8k/1k (pocos tokens streameados por segundo) el Gateway no limita hasta ~550-650 tok/s. **Recomendaciones (no probadas):** para L40S con tráfico corto/chat, subir el Gateway (p. ej. `c7g.xlarge`/`2xlarge`), y para pools de una réplica subir `allowed_fails` o desactivar el cooldown.

## 6. Costos y comparación con GPT-6 Luna

**Precios usados.** GPT-6 Luna (estándar): **$0.10 / M tokens de entrada, $0.50 / M de salida**; con caché de entrada $0.01/M; Batch/Flex $0.05/$0.25 ([eesel](https://www.eesel.ai/blog/gpt-6-luna-pricing), [OpenRouter](https://openrouter.ai/openai/gpt-6-luna)). AWS on-demand us-east-1: `g6.xlarge` **$0.8048/h**, `g6e.xlarge` **$1.861/h** ([DoiT](https://compute.doit.com/spot/us-east-1/g6e.xlarge)). Costos fijos de la arquitectura (por hora): Gateway t4g.large $0.0672 + NAT $0.045 + 2 IPv4 públicas $0.010 + EBS ≈ $0.039 = **$0.161/h** (estos tres últimos rubros son estimaciones de la tarifa publicada; verificar en la factura). **Todo incluido: L4 $0.966/h = $705/mes · L40S $2.022/h = $1,476/mes** (730 h). El tráfico de datos por NAT es despreciable.

**Costo por petición**

| Perfil | Luna estándar | L4 (100 % uso) | L40S (100 % uso) |
|---|---|---|---|
| 8k in / 1k out (8,234 + 1,024 tok) | **$0.001335** ($0.144 / M tokens totales) | $0.001578 (+18 %) | $0.000864 (−35 %) |
| chat 170 in / 512 out | $0.000273 | $0.000148 (−46 %) | $0.000094 (−66 %) |
| $ por M tokens de **salida** (techo decode) | $0.50 | $0.29 | $0.18 |

**Uso mínimo (utilización de la GPU 24/7) para empatar con Luna:** 8k/1k: L4 118 % (imposible), L40S **65 %**; chat: L4 54 %, L40S 34 %. Contra Luna en Batch (50 %) o con caché de entrada de Luna (RAG con prompts repetidos), autoalojar empata aún más tarde (L40S 8k/1k vs Batch: 129 %).

**Costo mensual, tráfico 8k/1k (Luna estándar vs GPU 24/7 encendida)**

| Uso medio de la capacidad | Peticiones/mes L4 | Luna L4 | Auto L4 | Peticiones/mes L40S | Luna L40S | Auto L40S |
|---|---:|---:|---:|---:|---:|---:|
| 10 % | 45 k | $60 | $705 | 171 k | $228 | $1,476 |
| 25 % | 112 k | $149 | $705 | 427 k | $570 | $1,476 |
| **35 % (realista)** | 156 k | $209 | $705 | 598 k | $798 | $1,476 |
| 50 % | 223 k | $298 | $705 | 854 k | $1,141 | $1,476 |
| 75 % | 335 k | $447 | $705 | 1,281 k | $1,711 | $1,476 |
| 100 % | 447 k | $597 | $705 | 1,708 k | $2,281 | $1,476 |

(El "ahorro" del autoalojado solo existe en la fila de 75-100 % de la L40S. Apagar la GPU fuera de horario —el panel tiene botón Apagar/Arrancar— reduce el costo fijo proporcionalmente a las horas apagadas y mueve el punto de equilibrio a favor del autoalojado.)

## 7. Cuántos millones de tokens por mes

Mes = 730 h = 2,628,000 s. "Netos" = entrada + salida procesadas por la GPU al 100 % de utilización, 24/7.

| Escenario (24/7) | L4 | L40S |
|---|---:|---:|
| **A. Techo teórico de decode** (salida) | 2,444 M | 8,060 M |
| A'. Techo decode, entrada+salida (prompts cortos) | 3,088 M | 10,641 M |
| **B. Techo teórico de prefill puro** (solo entrada) | 7,910 M | 30,527 M |
| **C. Tráfico 8k/1k a capacidad sostenida — "tokens netos"** | **4,136 M** (entrada 3,679 M + salida 457 M; 447 k peticiones) | **15,815 M** (entrada 14,065 M + salida 1,749 M; 1.71 M peticiones) |
| **D. Escenario más realista**: 8k/1k con 35 % de uso medio (horario laboral + picos) | **≈ 1,450 M** (156 k peticiones) | **≈ 5,500 M** (598 k peticiones) |

Cómo leerlo: **C es el techo utilizable** (con el factor 0.7 ya incluido, porque sale de lo medido, no de A/B); A y B son cotas superiores sintéticas que ningún tráfico real alcanza. **D es el número para planificar** costos: 35 % equivale, por ejemplo, a carga sostenida en ~12 h/día de un día laboral o a 24/7 con valles profundos; con menos de ~25 % de uso el autoalojado pierde contra Luna en cualquier caso. Sensibilidad: 10 % → 414 M (L4) / 1,581 M (L40S); 25 % → 1,034 M / 3,954 M; 50 % → 2,068 M / 7,907 M; 75 % → 3,102 M / 11,861 M.

## 8. Recomendación

- **Si el objetivo es costo por token:** Luna gana salvo que se sostenga > ~65 % de uso en L40S. La L4 no compite en 8k/1k; solo en chat corto con > 54 % de uso.
- **Si el objetivo es soberanía de datos / previsibilidad / visión propia:** L40S, punto de trabajo 16 concurrentes por GPU (≈550 tok/s de salida en 8k/1k, TTFT p95 < 10 s), Gateway más grande si se espera tráfico de chat por encima de ~1.5k tok/s, y escalar por réplicas (LiteLLM ya balancea) antes que subir `max_num_seqs`.
- **No se comparó calidad** del 9B AWQ-4bit contra GPT-6 Luna: la comparación de costos asume que el 9B es suficiente para la tarea.

## 9. Limitaciones

Una corrida por nivel y una sola AZ/instancia por GPU (variabilidad entre instancias no medida) · ventanas de 45 s (decode) y 150 s (8k/1k), con ruido descrito en §4 · razonamiento (`thinking`) desactivado y longitud de salida fija (`ignore_eos`), sin prefix cache (peor caso: con prompts compartidos el prefill real sería menor) · costos de AWS y Luna según fuentes públicas a la fecha del informe; NAT/EBS/IPv4 estimados · la fase `capacidad` integrada no devolvió resultado en los dos despliegues (no diagnosticado; ver `docs/09_BENCHMARK_TECHO.md` §5).

## 10. Estado de la infraestructura

Ambas infraestructuras fueron destruidas con `destroy_infra.py --yes` (L4: 17 recursos, L40S: 26, 0 fallos) y verificadas por separado con la API de AWS: sin instancias, NAT, volúmenes ni VPCs de los benchmarks. Quedan **dos Elastic IPs sueltas de pruebas anteriores** (`cliente-test-aws-dev` y `acme-prod`, ~$3.6/mes cada una) que **no** son de este benchmark y no se tocaron. Los resultados quedaron en `sooniverse.capacity_benchmark` (run_id `7ce0e8a8-8578-586b-a10e-d931d67d314f` L4 · `fd9e409e-260b-5be1-88ae-3ce5b244dd41` L40S).
