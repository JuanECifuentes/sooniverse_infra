-- =============================================================================
-- SOONIVERSE INFRA :: FASE 1 :: ESQUEMA DE MÉTRICAS Y API KEYS
-- =============================================================================
-- Idempotente. Ejecutable N veces sin efectos colaterales.
--
-- PRIVACIDAD (regla dura del proyecto):
--   Este esquema NO almacena prompts ni respuestas. Únicamente contadores
--   (prompt_tokens, completion_tokens, total_tokens), timestamps e identificadores
--   de API Key. No se crea ninguna columna de texto libre de conversación.
--
-- CONVIVENCIA CON LITELLM:
--   LiteLLM Proxy y Django gestionan sus tablas en el esquema `sooniverse`.
--   Nunca se altera ni se borra nada de LiteLLM. Este archivo:
--     1) Crea el esquema `sooniverse`.
--     2) Vincula sus tablas con las de LiteLLM por `token` (hash de la API key).
--     3) Provee una función ETL que copia SOLO contadores desde
--        LiteLLM_SpendLogs hacia sooniverse.token_usage_event.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS sooniverse;

SET search_path TO sooniverse;

-- -----------------------------------------------------------------------------
-- 1. REGISTRO DE API KEYS
-- -----------------------------------------------------------------------------
-- Espejo administrativo de las keys creadas en LiteLLM. Guarda metadatos de
-- negocio (dueño, cliente, cuotas, estado) que LiteLLM no modela.
-- `litellm_token_hash` es la llave de correlación con LiteLLM_VerificationToken.token
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sooniverse.api_key_registry (
    id                  BIGSERIAL PRIMARY KEY,
    key_alias           VARCHAR(120) NOT NULL,
    litellm_token_hash  VARCHAR(255) UNIQUE,
    key_prefix          VARCHAR(24),              -- p.ej. 'sk-...abcd' para mostrar en UI
    cliente_id          VARCHAR(64)  NOT NULL DEFAULT 'default',
    entorno             VARCHAR(16)  NOT NULL DEFAULT 'prod',
    owner_email         VARCHAR(254),
    descripcion         VARCHAR(500),
    is_active           BOOLEAN      NOT NULL DEFAULT TRUE,
    max_budget_usd      NUMERIC(12, 4),
    tpm_limit           INTEGER,
    rpm_limit           INTEGER,
    -- JSONB (no TEXT[]): lo consume el ORM de Django como JSONField y admite
    -- listas vacías sin castings explícitos.
    allowed_models      JSONB        NOT NULL DEFAULT '[]'::JSONB,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    deactivated_at      TIMESTAMPTZ,
    expires_at          TIMESTAMPTZ,
    CONSTRAINT api_key_registry_entorno_chk CHECK (entorno IN ('prod', 'dev', 'staging'))
);

CREATE INDEX IF NOT EXISTS idx_apikey_cliente     ON sooniverse.api_key_registry (cliente_id, entorno);
CREATE INDEX IF NOT EXISTS idx_apikey_active      ON sooniverse.api_key_registry (is_active) WHERE is_active;
CREATE INDEX IF NOT EXISTS idx_apikey_token_hash  ON sooniverse.api_key_registry (litellm_token_hash);

COMMENT ON TABLE sooniverse.api_key_registry IS
    'Registro administrativo de API Keys. Correlaciona con LiteLLM_VerificationToken vía litellm_token_hash. No almacena la key en claro.';

-- Compatibilidad con instalaciones previas donde `allowed_models` era TEXT[].
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'sooniverse' AND table_name = 'api_key_registry'
          AND column_name = 'allowed_models' AND udt_name = '_text'
    ) THEN
        ALTER TABLE sooniverse.api_key_registry
            ALTER COLUMN allowed_models DROP DEFAULT,
            ALTER COLUMN allowed_models TYPE JSONB
                USING COALESCE(to_jsonb(allowed_models), '[]'::JSONB),
            ALTER COLUMN allowed_models SET DEFAULT '[]'::JSONB;
        RAISE NOTICE 'allowed_models migrado de TEXT[] a JSONB.';
    END IF;
END $$;

-- -----------------------------------------------------------------------------
-- 2. EVENTOS DE CONSUMO DE TOKENS (grano fino, sin contenido)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sooniverse.token_usage_event (
    id                  BIGSERIAL PRIMARY KEY,
    api_key_id          BIGINT REFERENCES sooniverse.api_key_registry (id) ON DELETE SET NULL,
    litellm_token_hash  VARCHAR(255),             -- redundante a propósito: sobrevive al borrado del registro
    litellm_request_id  VARCHAR(255) UNIQUE,      -- idempotencia del ETL
    model_name          VARCHAR(160) NOT NULL DEFAULT 'unknown',
    worker_endpoint     VARCHAR(255),             -- IP privada / alias del worker vLLM que atendió
    prompt_tokens       INTEGER      NOT NULL DEFAULT 0 CHECK (prompt_tokens     >= 0),
    completion_tokens   INTEGER      NOT NULL DEFAULT 0 CHECK (completion_tokens >= 0),
    total_tokens        INTEGER      NOT NULL DEFAULT 0 CHECK (total_tokens      >= 0),
    spend_usd           NUMERIC(14, 8) NOT NULL DEFAULT 0,
    latency_ms          INTEGER,
    status              VARCHAR(24)  NOT NULL DEFAULT 'success',
    event_ts            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    ingested_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_usage_event_ts      ON sooniverse.token_usage_event (event_ts DESC);
CREATE INDEX IF NOT EXISTS idx_usage_key_ts        ON sooniverse.token_usage_event (api_key_id, event_ts DESC);
CREATE INDEX IF NOT EXISTS idx_usage_hash_ts       ON sooniverse.token_usage_event (litellm_token_hash, event_ts DESC);
CREATE INDEX IF NOT EXISTS idx_usage_model         ON sooniverse.token_usage_event (model_name);
-- No se indexa `event_ts::DATE`: el cast de TIMESTAMPTZ a DATE depende de la
-- zona horaria de la sesión y PostgreSQL lo rechaza en índices (no IMMUTABLE).
-- Las agregaciones por día se sirven desde `token_usage_rollup`.

COMMENT ON TABLE sooniverse.token_usage_event IS
    'Contadores de tokens por petición. PROHIBIDO agregar columnas con prompts, mensajes o respuestas.';

-- -----------------------------------------------------------------------------
-- 3. AGREGACIONES PRE-CALCULADAS (daily / weekly / monthly)
-- -----------------------------------------------------------------------------
-- Tabla única con discriminador de granularidad: evita 3 tablas casi idénticas y
-- permite al panel Django filtrar con un solo query parametrizado.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sooniverse.token_usage_rollup (
    id                  BIGSERIAL PRIMARY KEY,
    granularity         VARCHAR(10)  NOT NULL,    -- daily | weekly | monthly
    bucket_start        DATE         NOT NULL,    -- inicio del periodo (date_trunc)
    api_key_id          BIGINT REFERENCES sooniverse.api_key_registry (id) ON DELETE CASCADE,
    model_name          VARCHAR(160) NOT NULL DEFAULT 'unknown',
    request_count       BIGINT       NOT NULL DEFAULT 0,
    prompt_tokens       BIGINT       NOT NULL DEFAULT 0,
    completion_tokens   BIGINT       NOT NULL DEFAULT 0,
    total_tokens        BIGINT       NOT NULL DEFAULT 0,
    spend_usd           NUMERIC(16, 8) NOT NULL DEFAULT 0,
    avg_latency_ms      NUMERIC(12, 2),
    error_count         BIGINT       NOT NULL DEFAULT 0,
    computed_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT rollup_granularity_chk CHECK (granularity IN ('daily', 'weekly', 'monthly')),
    CONSTRAINT rollup_unique_bucket UNIQUE (granularity, bucket_start, api_key_id, model_name)
);

CREATE INDEX IF NOT EXISTS idx_rollup_lookup ON sooniverse.token_usage_rollup (granularity, bucket_start DESC);
CREATE INDEX IF NOT EXISTS idx_rollup_key    ON sooniverse.token_usage_rollup (api_key_id, granularity, bucket_start DESC);

-- -----------------------------------------------------------------------------
-- 4. AUDITORÍA DE CICLO DE VIDA DE API KEYS
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sooniverse.api_key_audit (
    id              BIGSERIAL PRIMARY KEY,
    api_key_id      BIGINT REFERENCES sooniverse.api_key_registry (id) ON DELETE SET NULL,
    key_alias       VARCHAR(120),
    action          VARCHAR(32)  NOT NULL,    -- created | updated | deactivated | reactivated | deleted | quota_exceeded
    actor           VARCHAR(254) NOT NULL DEFAULT 'system',
    detalle         JSONB        NOT NULL DEFAULT '{}'::JSONB,
    source_ip       INET,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT api_key_audit_action_chk CHECK (
        action IN ('created', 'updated', 'deactivated', 'reactivated', 'deleted', 'quota_exceeded', 'rotated')
    )
);

CREATE INDEX IF NOT EXISTS idx_audit_key ON sooniverse.api_key_audit (api_key_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_ts  ON sooniverse.api_key_audit (created_at DESC);

COMMENT ON COLUMN sooniverse.api_key_audit.detalle IS
    'Metadatos estructurados del cambio (campos modificados, cuotas). Nunca contenido de peticiones.';

-- -----------------------------------------------------------------------------
-- 5. INVENTARIO DE WORKERS vLLM (sincronizado por SkyPilot en cada despliegue)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sooniverse.worker_node (
    id              BIGSERIAL PRIMARY KEY,
    cluster_name    VARCHAR(160) NOT NULL,
    node_rank       INTEGER      NOT NULL DEFAULT 0,
    private_ip      VARCHAR(64)  NOT NULL,
    port            INTEGER      NOT NULL DEFAULT 8007,
    model_name      VARCHAR(160),
    accelerator     VARCHAR(64),
    is_healthy      BOOLEAN      NOT NULL DEFAULT TRUE,
    registered_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_seen_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT worker_node_unique UNIQUE (cluster_name, private_ip, port)
);

CREATE INDEX IF NOT EXISTS idx_worker_cluster ON sooniverse.worker_node (cluster_name, is_healthy);

-- -----------------------------------------------------------------------------
-- 6. TRIGGER: mantener updated_at
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION sooniverse.fn_touch_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_apikey_touch ON sooniverse.api_key_registry;
CREATE TRIGGER trg_apikey_touch
    BEFORE UPDATE ON sooniverse.api_key_registry
    FOR EACH ROW EXECUTE FUNCTION sooniverse.fn_touch_updated_at();

-- -----------------------------------------------------------------------------
-- 7. ETL :: LITELLM_SPENDLOGS -> TOKEN_USAGE_EVENT
-- -----------------------------------------------------------------------------
-- Copia SOLO contadores. Se salta silenciosamente si LiteLLM aún no ha corrido
-- sus migraciones Prisma (primer arranque del stack).
-- Idempotente por `litellm_request_id`.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION sooniverse.ingest_litellm_spendlogs(p_since_hours INTEGER DEFAULT 48)
RETURNS INTEGER AS $$
DECLARE
    v_inserted INTEGER := 0;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'sooniverse' AND table_name = 'LiteLLM_SpendLogs'
    ) THEN
        RAISE NOTICE 'LiteLLM_SpendLogs no existe todavía; ETL omitido.';
        RETURN 0;
    END IF;

    INSERT INTO sooniverse.token_usage_event (
        api_key_id, litellm_token_hash, litellm_request_id, model_name,
        prompt_tokens, completion_tokens, total_tokens, spend_usd, event_ts
    )
    SELECT
        reg.id,
        sl."api_key",
        sl."request_id",
        COALESCE(NULLIF(sl."model", ''), 'unknown'),
        COALESCE(sl."prompt_tokens", 0),
        COALESCE(sl."completion_tokens", 0),
        COALESCE(sl."total_tokens", COALESCE(sl."prompt_tokens", 0) + COALESCE(sl."completion_tokens", 0)),
        COALESCE(sl."spend", 0),
        COALESCE(sl."startTime", NOW())
    FROM sooniverse."LiteLLM_SpendLogs" sl
    LEFT JOIN sooniverse.api_key_registry reg ON reg.litellm_token_hash = sl."api_key"
    WHERE sl."startTime" >= NOW() - (p_since_hours || ' hours')::INTERVAL
    ON CONFLICT (litellm_request_id) DO NOTHING;

    GET DIAGNOSTICS v_inserted = ROW_COUNT;
    RETURN v_inserted;
END;
$$ LANGUAGE plpgsql;

-- -----------------------------------------------------------------------------
-- 8. RECÁLCULO DE AGREGACIONES
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION sooniverse.refresh_usage_rollups(p_since_days INTEGER DEFAULT 90)
RETURNS INTEGER AS $$
DECLARE
    v_total INTEGER := 0;
    v_rows  INTEGER;
    v_gran  TEXT;
    v_trunc TEXT;
BEGIN
    FOREACH v_gran IN ARRAY ARRAY['daily', 'weekly', 'monthly'] LOOP
        v_trunc := CASE v_gran WHEN 'daily' THEN 'day' WHEN 'weekly' THEN 'week' ELSE 'month' END;

        EXECUTE format($fmt$
            INSERT INTO sooniverse.token_usage_rollup (
                granularity, bucket_start, api_key_id, model_name,
                request_count, prompt_tokens, completion_tokens, total_tokens,
                spend_usd, avg_latency_ms, error_count, computed_at
            )
            SELECT
                %L,
                DATE_TRUNC(%L, e.event_ts)::DATE,
                e.api_key_id,
                e.model_name,
                COUNT(*),
                SUM(e.prompt_tokens),
                SUM(e.completion_tokens),
                SUM(e.total_tokens),
                SUM(e.spend_usd),
                AVG(e.latency_ms),
                COUNT(*) FILTER (WHERE e.status <> 'success'),
                NOW()
            FROM sooniverse.token_usage_event e
            WHERE e.event_ts >= NOW() - INTERVAL '%s days'
            GROUP BY 2, 3, 4
            ON CONFLICT (granularity, bucket_start, api_key_id, model_name) DO UPDATE SET
                request_count     = EXCLUDED.request_count,
                prompt_tokens     = EXCLUDED.prompt_tokens,
                completion_tokens = EXCLUDED.completion_tokens,
                total_tokens      = EXCLUDED.total_tokens,
                spend_usd         = EXCLUDED.spend_usd,
                avg_latency_ms    = EXCLUDED.avg_latency_ms,
                error_count       = EXCLUDED.error_count,
                computed_at       = NOW()
        $fmt$, v_gran, v_trunc, p_since_days);

        GET DIAGNOSTICS v_rows = ROW_COUNT;
        v_total := v_total + v_rows;
    END LOOP;

    RETURN v_total;
END;
$$ LANGUAGE plpgsql;

-- -----------------------------------------------------------------------------
-- 9. VISTAS DE LECTURA PARA EL PANEL DJANGO
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW sooniverse.v_usage_daily AS
SELECT
    r.bucket_start                        AS periodo,
    r.api_key_id,
    COALESCE(k.key_alias, '(sin registro)') AS key_alias,
    k.cliente_id,
    r.model_name,
    r.request_count,
    r.prompt_tokens,
    r.completion_tokens,
    r.total_tokens,
    r.spend_usd,
    r.error_count
FROM sooniverse.token_usage_rollup r
LEFT JOIN sooniverse.api_key_registry k ON k.id = r.api_key_id
WHERE r.granularity = 'daily';

CREATE OR REPLACE VIEW sooniverse.v_usage_weekly AS
SELECT
    r.bucket_start                        AS periodo,
    r.api_key_id,
    COALESCE(k.key_alias, '(sin registro)') AS key_alias,
    k.cliente_id,
    r.model_name,
    r.request_count,
    r.prompt_tokens,
    r.completion_tokens,
    r.total_tokens,
    r.spend_usd,
    r.error_count
FROM sooniverse.token_usage_rollup r
LEFT JOIN sooniverse.api_key_registry k ON k.id = r.api_key_id
WHERE r.granularity = 'weekly';

CREATE OR REPLACE VIEW sooniverse.v_usage_monthly AS
SELECT
    r.bucket_start                        AS periodo,
    r.api_key_id,
    COALESCE(k.key_alias, '(sin registro)') AS key_alias,
    k.cliente_id,
    r.model_name,
    r.request_count,
    r.prompt_tokens,
    r.completion_tokens,
    r.total_tokens,
    r.spend_usd,
    r.error_count
FROM sooniverse.token_usage_rollup r
LEFT JOIN sooniverse.api_key_registry k ON k.id = r.api_key_id
WHERE r.granularity = 'monthly';

-- Resumen por API Key para las tarjetas del panel
CREATE OR REPLACE VIEW sooniverse.v_apikey_summary AS
SELECT
    k.id,
    k.key_alias,
    k.key_prefix,
    k.cliente_id,
    k.entorno,
    k.is_active,
    k.max_budget_usd,
    k.created_at,
    COALESCE(SUM(e.total_tokens), 0)       AS total_tokens,
    COALESCE(SUM(e.prompt_tokens), 0)      AS prompt_tokens,
    COALESCE(SUM(e.completion_tokens), 0)  AS completion_tokens,
    COALESCE(SUM(e.spend_usd), 0)          AS spend_usd,
    COUNT(e.id)                            AS request_count,
    MAX(e.event_ts)                        AS last_used_at
FROM sooniverse.api_key_registry k
LEFT JOIN sooniverse.token_usage_event e ON e.api_key_id = k.id
GROUP BY k.id;

-- =============================================================================
-- FIN DEL ESQUEMA
-- =============================================================================
