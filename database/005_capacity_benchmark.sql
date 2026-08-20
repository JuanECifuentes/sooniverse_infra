-- =============================================================================
-- SOONIVERSE INFRA :: BENCHMARK DE CAPACIDAD
-- =============================================================================
-- Idempotente. Ejecutable N veces sin efectos colaterales.
--
-- QUÉ RESPONDE
--   "¿Cuántas peticiones y cuántos tokens por minuto aguanta esta infraestructura
--    antes de degradar la respuesta?" y, derivado de eso, "¿está por quedarse
--    corta?". Lo mide scripts/benchmark_capacity.py con una rampa ACOTADA de
--    concurrencia (para cuando la latencia degrada, no busca el punto de rotura)
--    durante la fase `capacidad` del despliegue.
--
-- POR QUÉ UNA TABLA Y NO UN ARCHIVO
--   El techo solo es interpretable junto a la configuración bajo la que se midió:
--   el mismo modelo en la misma GPU con max_num_seqs=2 y con max_num_seqs=16 da
--   números completamente distintos. Por eso cada corrida guarda un snapshot
--   inmutable de esa configuración, y por eso el panel puede comparar corridas.
--
-- SIN FK A infra_deployment (a propósito)
--   La medición debe SOBREVIVIR al destroy del despliegue que la produjo: es el
--   histórico con el que se dimensiona el siguiente. deployment_id queda como
--   referencia informativa, no como integridad referencial.
--
-- PRIVACIDAD: tráfico sintético, ningún dato de usuario. La API key del
--   benchmark se guarda por HASH, nunca en claro (regla heredada de
--   api_key_registry).
-- =============================================================================

SET search_path TO sooniverse;

CREATE TABLE IF NOT EXISTS sooniverse.capacity_benchmark (
    id                     BIGSERIAL PRIMARY KEY,
    run_id                 UUID        NOT NULL UNIQUE,   -- idempotencia del script
    client_id              TEXT        NOT NULL,
    environment            TEXT        NOT NULL,
    deployment_id          UUID,                          -- informativo, sin FK (ver cabecera)
    workload_id            TEXT        NOT NULL,
    model_public_name      TEXT        NOT NULL,

    -- ---- BAJO QUÉ CONFIGURACIÓN SE MIDIÓ (snapshot inmutable) ---------------
    instance_type          TEXT,
    accelerator            TEXT,
    gpu_count              INTEGER,
    replicas               INTEGER,
    max_num_seqs           INTEGER,
    max_num_batched_tokens INTEGER,
    max_model_len          INTEGER,
    gpu_memory_utilization NUMERIC(4, 3),
    enforce_eager          BOOLEAN,
    quantization           TEXT,
    vllm_version           TEXT,
    lb_strategy            TEXT,

    -- ---- PARÁMETROS DEL TEST -----------------------------------------------
    niveles_concurrencia   INTEGER[]   NOT NULL,
    prompt_tokens_objetivo INTEGER     NOT NULL,
    max_tokens             INTEGER     NOT NULL,
    segundos_por_nivel     INTEGER     NOT NULL,
    warmup_segundos        INTEGER     NOT NULL DEFAULT 0,
    streaming              BOOLEAN     NOT NULL DEFAULT TRUE,
    origen                 TEXT        NOT NULL DEFAULT 'gateway',
    benchmark_key_alias    TEXT,
    benchmark_key_hash     TEXT,       -- HASH, nunca la key en claro

    -- ---- RESULTADOS DERIVADOS ----------------------------------------------
    concurrencia_rodilla   INTEGER,    -- último nivel que aún cumplió los umbrales
    rpm_sostenido          NUMERIC(10, 2),
    tokens_salida_por_min  NUMERIC(12, 2),
    tokens_totales_por_min NUMERIC(12, 2),
    p50_base_ms            INTEGER,    -- nivel de concurrencia 1 = referencia
    p95_base_ms            INTEGER,
    ttft_p50_base_ms       INTEGER,
    ttft_p95_base_ms       INTEGER,
    p95_rodilla_ms         INTEGER,
    ttft_p95_rodilla_ms    INTEGER,
    itl_medio_rodilla_ms   NUMERIC(8, 2),   -- inter-token latency
    tasa_error_pct         NUMERIC(6, 3) NOT NULL DEFAULT 0,
    motivo_parada          TEXT        NOT NULL,
    usuarios_estimados     INTEGER,    -- rodilla traducida a personas concurrentes

    -- ---- CURVA COMPLETA POR NIVEL ------------------------------------------
    curva                  JSONB       NOT NULL DEFAULT '[]'::JSONB,
    notas                  JSONB       NOT NULL DEFAULT '{}'::JSONB,

    duracion_total_seg     NUMERIC(8, 2),
    started_at             TIMESTAMPTZ NOT NULL,
    finished_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT capacity_benchmark_motivo_chk CHECK (motivo_parada IN (
        'nivel_maximo', 'p95_degradado', 'errores',
        'saturacion_throughput', 'presupuesto_agotado', 'fallo'
    )),
    CONSTRAINT capacity_benchmark_origen_chk CHECK (origen IN ('gateway', 'operador'))
);

CREATE INDEX IF NOT EXISTS idx_capacity_benchmark_lookup
    ON sooniverse.capacity_benchmark (client_id, environment, workload_id, finished_at DESC);
CREATE INDEX IF NOT EXISTS idx_capacity_benchmark_deployment
    ON sooniverse.capacity_benchmark (deployment_id);

COMMENT ON TABLE sooniverse.capacity_benchmark IS
    'Una fila por corrida de scripts/benchmark_capacity.py. El techo solo es interpretable '
    'junto al snapshot de configuración de las columnas instance_type..lb_strategy.';

COMMENT ON COLUMN sooniverse.capacity_benchmark.curva IS
    'Un objeto por nivel de concurrencia: {"concurrencia","peticiones","exitos","errores",'
    '"p50_ms","p95_ms","p99_ms","max_ms","ttft_p50_ms","ttft_p95_ms","rps",'
    '"tokens_salida_por_seg","itl_medio_ms","duracion_seg"}. Los percentiles son POR NIVEL '
    'y no se recombinan entre niveles.';

COMMENT ON COLUMN sooniverse.capacity_benchmark.motivo_parada IS
    'nivel_maximo = la rampa acotada terminó sin doler (resultado deseable). '
    'p95_degradado / errores / saturacion_throughput = se encontró la rodilla. '
    'presupuesto_agotado = se acabó el tiempo de GPU asignado a la fase.';

COMMENT ON COLUMN sooniverse.capacity_benchmark.usuarios_estimados IS
    'concurrencia_rodilla x notas.factor_usuarios_por_slot. El factor se guarda en `notas` '
    'para que el número sea auditable y no mágico.';

COMMENT ON COLUMN sooniverse.capacity_benchmark.origen IS
    'gateway = medido desde el propio Gateway (127.0.0.1 -> nginx -> litellm -> worker), que '
    'es el camino del cliente real. operador = medido desde fuera de la VPC, donde el RTT del '
    'ISP domina el TTFT: los números NO son comparables entre orígenes.';

-- Última corrida por workload: es lo que consume el panel para el techo actual.
CREATE OR REPLACE VIEW sooniverse.v_capacity_benchmark_latest AS
SELECT DISTINCT ON (client_id, environment, workload_id) *
FROM sooniverse.capacity_benchmark
ORDER BY client_id, environment, workload_id, finished_at DESC;

-- =============================================================================
-- FIN
-- =============================================================================
