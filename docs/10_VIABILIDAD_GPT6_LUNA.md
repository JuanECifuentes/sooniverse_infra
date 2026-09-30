# 10. Viabilidad de Sooniverse tras GPT-6 Luna

**Fecha:** 30 sep 2026 · **Base:** `Resumen_negocio.md`, `Sooniverse_Guia_de_Cotizacion.html`, `docs/INFORME_BENCHMARK_QWEN35-9B_L4_vs_L40S.md`, `docs/09_BENCHMARK_TECHO.md` y precios públicos de la fecha (fuentes al final).
**TRM usada:** 3.100 COP/USD (septiembre 2026, en mínimos históricos). Todas las cifras de nube en USD on-demand, us-east-1.

---

## 1. Veredicto corto

**La promesa actual ("70 % de ahorro contra OpenAI") está muerta y no se debe volver a decir.** No es un problema de precio de la implementación: con GPT-6 Luna, para casi cualquier cliente de una sola GPU, el costo de la infraestructura por sí sola ya es mayor que la factura de Luna.

**El proyecto no se debe abandonar, pero sí la oferta.** Lo que sigue teniendo valor es el activo técnico (despliegue automatizado BYOC en AWS y Azure, Gateway compatible con OpenAI, panel de métricas, metodología de benchmark) aplicado a un comprador distinto: **la empresa que no puede mandar sus datos a un proveedor de IA externo**, no la que quiere pagar menos.

Recomendación: **seguir 90 días con la oferta reposicionada y criterios de salida explícitos (§7)**. Si en ese plazo no hay tracción medible, congelar el desarrollo y quedarse con el negocio como consultoría.

---

## 2. Qué cambió, en números

### 2.1 El caso de referencia del guion se invierte

El guion vende: 200.000 ejecuciones/mes de ~10.000 tokens → **$9,2M COP** con proveedor premium vs **$2,38M COP** en plataforma privada (−74 %).

La misma carga en GPT-6 Luna (supuesto 9k entrada / 1k salida):
`200.000 × (9.000 × $0,10 + 1.000 × $0,50) / 1M = $280 USD ≈ $0,87M COP/mes`.

| Opción | Costo mensual |
|---|---:|
| Proveedor premium (guion) | $9,2M COP |
| Plataforma privada L4 24/7 (benchmark: $705 USD) | $2,19M COP |
| **GPT-6 Luna estándar** | **$0,87M COP** |
| GPT-6 Luna Batch (−50 %) | $0,43M COP |
| Qwen3.5-9B por API (DeepInfra, $0,10/$0,15) | $0,65M COP |

La plataforma privada queda **2,5× más cara que Luna** en el caso insignia. Y esa carga (200k × 10k tokens) ocupa ~45 % de una L4 24/7: no es un cliente pequeño.

### 2.2 El cliente mínimo de la guía de cotización desaparece

La guía arranca en un gasto de **$7,5M COP/mes**. Si ese gasto era en un modelo de gama GPT-5 ($1,25 / $10 por M) con peticiones 8k/1k, equivale a ~118.000 peticiones/mes. En Luna esas mismas peticiones cuestan **~$158 USD ≈ $0,49M COP/mes**. Es decir: **al cliente objetivo, OpenAI le ofrece hoy un 93 % de ahorro sin implementación, sin diagnóstico y cambiando una línea de código.** Sooniverse no puede ganar esa comparación.

### 2.3 Crítica que hay que aceptar: la promesa ya era frágil antes de Luna

- El 74 % se calculaba contra el **tier premium**. La comparación justa es contra el tier barato del mismo proveedor, y GPT-5.6 Luna (la versión anterior, al doble del precio actual) ya dejaba la plataforma privada en empate o pérdida para cargas de una sola GPU.
- **El mismo modelo abierto se vende por API más barato de lo que cuesta hospedarlo**: Qwen3.5-9B en DeepInfra a $0,10 / $0,15 por M. Una L40S on-demand al 100 % de uso sale a ~$0,05 / $0,18 por M. Autoalojar un modelo abierto solo gana en costo con uso muy alto, y los proveedores de API ganan porque reparten la GPU entre cientos de clientes (utilización), no porque tengan mejor hardware.
- Luna está en **Azure AI Foundry** con retención cero de datos, 28 regiones y Data Zones de EE. UU. y la UE. Para una empresa que ya acepta a Microsoft como encargado del tratamiento, el argumento "sus datos no salen" pierde fuerza: con Sooniverse los datos también viven en us-east-1 de AWS o Azure.

### 2.4 Sobre la hipótesis de que Luna es un precio "gancho"

Es posible, pero **no se puede construir el negocio sobre esa apuesta**:
- OpenAI declaró el precio de Luna **"permanente, no una promoción de lanzamiento"** (la promoción temporal es la de GPT-5.6 Sol, que vence el 21 nov 2026).
- Luna **bajó 50 %** frente a su predecesor. La tendencia de los últimos años es a la baja por nivel de capacidad, no al alza.
- Si algún día sube, el diagnóstico lo va a mostrar y la oferta de ahorro vuelve sola. Úsenlo como argumento secundario ("costo fijo y predecible, sin depender del precio de otro"), nunca como el principal.

---

## 3. Dónde gana todavía la plataforma privada (y dónde no)

### 3.1 Techo de ahorro por petición (8k in / 1k out, solo GPU al 100 % de uso)

| GPU / modalidad | $ por petición | vs Luna ($0,001335) |
|---|---:|---:|
| L4 on-demand | $0,00132 | ≈ empate (sin contar Gateway ni ocio) |
| L40S on-demand | $0,00080 | −40 % |
| L40S spot AWS ($0,87/h) | $0,00037 | −72 % |
| L40S Vast.ai (desde $0,535/h) | $0,00023 | −83 % |

Esos son **techos**: asumen la GPU llena todo el tiempo que está encendida.

### 3.2 Ahorro real mensual con uso y horario realistas (1 L40S, incluye $118/mes de Gateway, NAT, IP y disco)

| Horario encendido | Uso | Luna | L40S on-demand | L40S spot | L40S Vast |
|---|---:|---:|---:|---:|---:|
| 24/7 | 35 % | $798 | −85 % | +6 % | +36 % |
| 24/7 | 85 % | $1.939 | +24 % | +61 % | +74 % |
| 12 h × 22 días (programado) | 60 % | $495 | −23 % | +30 % | +48 % |
| 12 h × 22 días (programado) | 85 % | $701 | +13 % | +51 % | +63 % |

(Positivo = ahorro frente a Luna; negativo = la privada sale más cara.)

**Lecturas:**
1. **En on-demand no hay negocio de ahorro.** Ni con apagado programado.
2. **El apagado programado no hace ganarle a Luna; hace asequible la privacidad.** Con 12 h × 22 días la factura de una L40S baja de $1.476 a ~$609/mes (−59 %). Ese es su valor real: reducir la "prima de privacidad".
3. **El ahorro absoluto es chico frente a la implementación.** Una implementación de $35M COP (~$11.300 USD) necesita ~$1.400 USD/mes de ahorro para recuperarse en 8 meses. Eso solo se logra con una L40S en spot o Vast **al 85 % las 24 horas**, es decir unas **15.000M tokens/mes sostenidos**. En Colombia son muy pocos los clientes así.
4. **Spot y Vast mueven la aguja en costo, pero chocan con la privacidad:**
   - *Spot en la cuenta del cliente (AWS/Azure):* conserva la privacidad, pero `g6e.xlarge` tiene una tasa de interrupción **> 20 %**. Hace falta el sistema de "reacople" que ya pensaron para Vast; conviene construirlo primero para spot AWS/Azure, porque no rompe la promesa de privacidad.
   - *Vast.ai "community":* son máquinas de terceros sin garantías de aislamiento. **Contradice de frente el argumento de datos.** Solo sirve para clientes sin datos sensibles, que son justamente los que hoy deberían usar Luna o una API de modelo abierto. Vast "Secure Cloud" (centros de datos certificados) es más caro y hay que evaluarlo aparte. Conclusión: Vast **no** es prioridad.

### 3.3 Dónde la privada sí gana, sin discusión

- **Restricción dura de datos**: contratos que prohíben enviar datos del cliente final a proveedores de IA o subencargados no aprobados, políticas escritas que excluyen incluso Azure OpenAI/Bedrock, entidades públicas, salud con historia clínica, cumplimiento que exige saber en qué máquina se procesó cada documento.
- **Modelo congelado y versionado**: un proceso regulado validado sobre un modelo no cambia de comportamiento porque el proveedor lo actualice o lo retire. Luna es un alias que el proveedor puede mover.
- **Automatizar lo que hoy está bloqueado**: el valor ya no es "pague menos tokens", es **"automatice el proceso que su área de cumplimiento no le deja automatizar"**. El retorno sale de horas-persona, no de tokens.
- **Costo fijo y predecible** por capacidad, sin sorpresas de factura ni límites de tasa.

---

## 4. Nueva tesis de negocio

> **Plataforma de IA privada para procesos que no pueden salir de la empresa.**
> No competimos contra el precio de OpenAI; competimos contra "este proceso no se puede automatizar porque los datos no pueden salir".

Dos ofertas, porque el diagnóstico va a encontrar a los dos tipos de empresa:

| Oferta | Para quién | Qué es |
|---|---|---|
| **A. Plataforma privada** (producto principal) | Restricción dura de datos + proceso de volumen | Lo que ya existe: motor vLLM + Gateway + panel + chat, en su nube (BYOC) o hospedado, con apagado manual y programado |
| **B. Gateway de gobierno** (oferta de rescate) | Empresas que **sí** pueden usar Luna/Azure OpenAI | Solo el Gateway + panel: llaves por área, presupuestos, auditoría de quién envió qué, enrutamiento a Luna, y opcionalmente un nodo privado pequeño solo para lo sensible |

La oferta B convierte a Luna de competidor en componente y evita perder al prospecto que la oferta A tiene que rechazar. Técnicamente ya está: LiteLLM enruta a proveedores externos.

**El diagnóstico debe decir la verdad**: si la empresa puede usar Luna, se lo decimos y le ofrecemos B. Es el argumento de confianza más fuerte que tienen y el que les va a traer referidos (su canal principal).

---

## 5. Nuevos criterios de prospección

### 5.1 Criterios excluyentes (oferta A)

1. **Restricción de datos verificable y por escrito.** Pregunta filtro: *"¿Su área de cumplimiento aprobaría hoy Azure OpenAI con retención cero dentro de su propio tenant?"* Si la respuesta es sí → no es prospecto de A (pasa a B).
2. **Proceso de volumen, no de alto razonamiento** (igual que antes): extracción, clasificación, resumen, estructuración de documentos. Validado con ejemplos reales del cliente en el diagnóstico.
3. **Valor operativo medible ≥ $8M COP/mes**: el proceso consume hoy **≥ 2 personas de tiempo completo (~320 h/mes)**, o hay un ingreso/contrato que depende de automatizarlo. Esto reemplaza el criterio "gasta ≥ $7,5M COP/mes en APIs": con Luna, el gasto en APIs dejó de ser un buen indicador.
4. **Acepta un costo fijo de infraestructura** de $1,1M a $2,0M COP/mes (L4 o L40S con horario programado) más la cuota de operación.

### 5.2 Criterio de ahorro puro (segmento secundario, solo con spot en su nube)

Solo tiene sentido hablar de ahorro en tokens si el cliente cumple **todo**:
- ≥ **15.000M tokens/mes** sostenidos (≈ 1,5M peticiones 8k/1k), 24/7 o casi.
- Tolera latencia y reintentos (lotes, backoffice nocturno).
- Ya comparó contra Luna **Batch** y contra APIs de modelos abiertos, no contra el tier premium.

Si no cumple las tres, el diagnóstico recomienda Luna/B.

### 5.3 Perfiles que se mantienen, reordenados

1. **IPS, EPS y auditoría de cuentas médicas** (sube al primer lugar): historia clínica, datos sensibles, volumen alto, trabajo en horario laboral → el apagado programado les funciona perfecto.
2. **BPO y contact centers con contratos internacionales** que prohíben subencargados de IA. La señal "un cliente suyo le prohíbe contractualmente enviar datos a terceros" pasa de "señal fuerte" a **criterio de entrada**.
3. **Aseguradoras, cobranza, sector financiero** bajo Superfinanciera con políticas internas que excluyen IA externa.
4. **Sector público y entidades con datos reservados.**
5. **SaaS con IA embebida (antiguo Perfil A) baja al final**: su motivación era el costo por token y Luna se lo resuelve mejor. Solo califica si *sus* clientes le exigen no usar proveedores de IA externos.

### 5.4 Señales de alta intención que cambian

- "Ya migraron de proveedor premium a uno económico" → **deja de ser señal positiva**: esa empresa va a migrar a Luna.
- Nuevas señales: *"Cumplimiento les rechazó un proyecto de IA"*, *"tienen un piloto con IA detenido por datos"*, *"auditoría externa les pidió trazabilidad del procesamiento"*.

---

## 6. Precios propuestos

### 6.1 Por qué cambia la estructura

La guía actual calcula el precio como el mayor entre piso técnico (28 h × $1,26M COP/h) y "valor del ahorro" (múltiplo de meses de ahorro en tokens). Con Luna, la pata de "ahorro en tokens" vale casi cero para el cliente objetivo, así que la guía siempre caería en el piso técnico o en "No vender así".

Cambios:
1. **El valor se mide en horas-persona del proceso**, no en tokens ahorrados.
2. **Menos pago inicial, más recurrente.** El valor de la plataforma ahora es operarla (horarios, actualizaciones, reacople, auditoría), y el recurrente vale más que el mismo dinero cobrado una sola vez. Además baja la barrera de entrada en un mercado que se puso más difícil.
3. **La infraestructura la paga el cliente directo a su nube (BYOC)**, o se refactura con un 15 % de gestión si es hospedada.

### 6.2 Tabla propuesta (COP, antes de IVA)

| | Diagnóstico | Implementación | Operación gestionada | Incluye |
|---|---:|---:|---:|---|
| **Pre-diagnóstico** | gratis | — | — | Formulario + prueba ácida "¿puede usar Luna?" |
| **Diagnóstico** | **$2,5M** (deducible 45 días) | — | — | Restricción de datos, medición del proceso en horas-persona, **prueba de calidad del modelo abierto vs Luna con 50-100 ejemplos del cliente**, dimensionamiento y cotización en firme |
| **Esencial** | — | **$24M** | **$2,2M/mes** (12 meses) | 1 nodo L4/T4 en horario programado, 1 modelo, 1 integración, panel con apagado manual y programado, 2 ventanas de mantenimiento al año |
| **Profesional** | — | **$38M** | **$3,5M/mes** | L40S o 2 nodos, LLM + embeddings, hasta 3 integraciones, chat interno, 4 ventanas, respuesta en 2 días hábiles |
| **Regulado** | — | **desde $60M** | **desde $5,5M/mes** | Multinodo, reacople ante caída/spot, SLA en horas, evaluación trimestral de modelos, informe de auditoría |
| **Gateway de gobierno (B)** | — | **$9M** | **$1,2M/mes** | Gateway + panel + llaves y presupuestos por área, enrutado a Luna/Azure |

Comparación con lo actual (Esencial vs "desde $35M + $7,4M/año"): el cliente paga **$11M menos al inicio**; Sooniverse factura en el año 1 **$50,4M en vez de $42,4M**, y el 52 % queda como ingreso recurrente.

### 6.3 Regla de retorno (6-8 meses) con la nueva estructura

Para el cliente:
`retorno (meses) = implementación / (valor operativo mensual − cuota − infraestructura)`

Ejemplo, IPS con 4 auditores de cuentas médicas (~$5,5M COP/mes cada uno con carga prestacional = $22M/mes) y automatización del 50 % del trabajo:

| Concepto | COP/mes |
|---|---:|
| Valor operativo liberado | $11,0M |
| Cuota Esencial | −$2,2M |
| Infraestructura L4, 12 h × 22 días (~$330 USD) | −$1,0M |
| **Beneficio neto** | **$7,8M** |
| **Retorno de $24M** | **3,1 meses** |

Por eso el criterio 5.1.3 exige ≥ $8M/mes de valor operativo: con eso, Esencial se recupera en menos de 6 meses incluso si se automatiza solo la mitad de lo previsto.

**Qué cambiar en la guía de cotización** (siguiente paso, no hecho todavía): reemplazar "Gasto actual en IA" por "Horas-persona del proceso × costo cargado × % automatizable", agregar la cuota mensual como parte del precio, y agregar el veredicto *"Recomiende Luna / Gateway de gobierno"* cuando no hay restricción de datos.

---

## 7. Criterios de decisión: seguir o abandonar

**Seguir** los próximos 90 días (hasta el **31 dic 2026**) con estas metas mínimas. Si no se cumplen, **congelar el desarrollo** (no más features) y seguir solo como consultoría a demanda:

| Métrica | Día 45 | Día 90 |
|---|---:|---:|
| Conversaciones con prospectos que cumplen 5.1.1 (restricción de datos) | ≥ 12 | ≥ 25 |
| Diagnósticos pagados | ≥ 2 | ≥ 5 |
| Implementaciones firmadas (A o B) | — | ≥ 2, al menos 1 de A |

**Señal de abandono temprano:** si de las primeras 12 conversaciones calificadas, más de 9 responden "sí, cumplimiento aprobaría Azure OpenAI", el mercado de la oferta A es demasiado pequeño en Colombia; pivotar entero a B o parar.

**Qué NO hacer en esos 90 días:** construir el apagado automático, integrar Vast.ai o agregar modelos nuevos. Solo lo que desbloquea ventas (§8).

---

## 8. Hoja de ruta técnica, priorizada por impacto en ventas

| # | Qué | Por qué | Esfuerzo |
|---|---|---|---|
| 1 | **Apagado manual + programado** (AWS y Azure) — ver `docs/11_PLAN_APAGADO_PROGRAMADO.md` | Baja la factura de infraestructura de la oferta A entre 55 % y 70 %; es lo que hace vendible la "prima de privacidad" | Bajo: el botón de AWS ya existe |
| 2 | **Prueba de calidad cliente vs Luna** (script reutilizable para el diagnóstico) | Sin esto, el primer comprador técnico pregunta "¿y por qué no Luna?" y no hay respuesta con datos. El informe de benchmark no comparó calidad | Bajo |
| 3 | **Benchmark de un MoE en L40S** (p. ej. Qwen3.5-35B-A3B AWQ) | Pocos parámetros activos por token: probablemente más throughput y mejor calidad que el 9B en la misma GPU. Es la palanca más barata para mejorar costo por token y calidad a la vez (hipótesis, sin medir) | Medio |
| 4 | **Gateway más grande en L40S** (`c7g.xlarge`) y `allowed_fails`/cooldown ajustados | El informe mostró que el `t4g.large` limita al 55 % del motor y deja el pool en cooldown | Bajo |
| 5 | **Reacople en spot AWS/Azure** (en la cuenta del cliente) | Abre el segmento de ahorro puro (§5.2) sin romper la privacidad | Alto |
| 6 | Apagado automático por ocio / despertar por demanda | Ver §9 | Alto |
| 7 | Vast.ai | Solo si aparece demanda real de clientes sin datos sensibles que no quieran API | Alto, choca con la tesis |

---

## 9. Sobre el apagado automático propuesto (para después)

El diseño que plantean es correcto en lo esencial. Tres precisiones:

1. **La regla de los 15 segundos deja fuera el scale-to-zero.** Hoy el arranque de vLLM solo (carga + compilación + CUDA graphs) tarda ~127 s, más 30-90 s de la VM. Aun con caché de compilación persistente se estaría en 1-2 minutos. Así que, con su propia regla, **el automático solo aplica a multinodo**: siempre queda 1 encendido y se despiertan o apagan los adicionales. Está bien así.
2. **Cómo medir el 75 % de capacidad sin usar peticiones ni % de GPU**: con el mismo modelo del benchmark. Cada intervalo, la "ocupación equivalente" del nodo es
   `U = (Δ prompt_tokens / P + Δ generation_tokens / D) / Δt`
   donde P y D son los techos medidos de prefill y decode (`sooniverse.capacity_benchmark`) y los Δ salen de los contadores `vllm:prompt_tokens_total` y `vllm:generation_tokens_total` de `/metrics`. Una petición densa de 8k pesa lo que tiene que pesar. Hay que complementarlo con dos señales de saturación: `vllm:num_requests_waiting > 0` sostenido y `vllm:kv_cache_usage_perc > 0,85`. Despertar el siguiente nodo si `U > 0,75` durante 5 minutos **o** si hay cola sostenida.
3. **El ocio de 30 minutos** se mide igual (Δ tokens = 0 y `num_requests_running = 0`), y el apagado de un nodo adicional debe drenar primero: sacarlo de LiteLLM, esperar a que `num_requests_running` llegue a 0 y recién ahí apagar.

---

## Fuentes

- Precios GPT-6 Luna: [OpenRouter](https://openrouter.ai/openai/gpt-6-luna), [llm-stats](https://llm-stats.com/models/gpt-6-luna), [Codersera — GPT-6 Sol y Luna](https://codersera.com/blog/gpt-6-sol-luna-complete-guide-2026/) (precio "permanente", −50 % vs GPT-5.6, Batch −50 %).
- Luna en Azure: [Microsoft — GPT-6 en Foundry](https://azure.microsoft.com/en-us/blog/gpt-6-astra-sol-and-luna-for-production-agents-in-microsoft-foundry/), [Requesty — regiones Azure](https://www.requesty.ai/models/azure/gpt-6-luna-eastus2).
- Qwen3.5-9B por API: [computeprices — DeepInfra](https://computeprices.com/providers/deep-infra/models/qwen3-5-9b), [anotherwrapper](https://anotherwrapper.com/llm-pricing/qwen3.5-9b).
- Spot `g6e.xlarge`: [DoiT](https://compute.doit.com/spot/us-east-1/g6e.xlarge).
- Vast.ai: [computeprices — L40S](https://computeprices.com/providers/vast/gpus/l40s), [RTX 4090](https://computeprices.com/providers/vast/gpus/rtx4090).
- TRM: [Portafolio, sep 2026](https://www.portafolio.co/economia/finanzas/precio-del-dolar-hoy-en-colombia-asi-cotiza-la-divisa-este-15-de-septiembre-502515).
