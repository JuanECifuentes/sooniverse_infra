-- =============================================================================
-- SOONIVERSE INFRA :: FASE 1 :: CAPACIDADES REALES POR MODELO
-- =============================================================================
-- Idempotente. Ejecutable N veces sin efectos colaterales.
--
-- `config_global.yaml` declara qué capacidades tiene cada workload
-- ('capacidades: {vision, tool_calling, tool_call_parser}'), pero eso es una
-- DECLARACIÓN del operador, no una medición. scripts/test_model_capabilities.py
-- sondea el modelo YA DESPLEGADO a través del mismo camino que un cliente real
-- (Gateway público -> LiteLLM -> vLLM) y escribe aquí lo que observó.
--
-- Política fail-closed (decisión explícita del proyecto): una capacidad solo
-- queda ENCENDIDA para el cliente (Open WebUI, LiteLLM) si (a) el operador la
-- declaró Y (b) el sondeo la confirmó como TRUE. Un sondeo inconcluso
-- (`probed_* IS NULL`, p.ej. timeout o worker aún arrancando) se trata como NO
-- soportada -nunca se ofrece al usuario algo que no se pudo confirmar-. Por
-- eso `effective_*` son columnas GENERATED: la política vive en el motor, no
-- repetida en cada uno de los consumidores (bootstrap de Open WebUI,
-- render_litellm_config.py, render_gateway_stack.py).
--
-- No reemplaza ni modifica 001_init_schema.sql/002_infra_state.sql; se aplica
-- después en el mismo esquema `sooniverse`. db_setup.py aplica database/*.sql
-- en orden lexicográfico.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS sooniverse;

SET search_path TO sooniverse;

-- -----------------------------------------------------------------------------
-- 1. CAPACIDADES declaradas + sondeadas + efectivas, por modelo público
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sooniverse.model_capability (
    id                     BIGSERIAL PRIMARY KEY,
    client_id              TEXT        NOT NULL,
    environment            TEXT        NOT NULL,
    -- nombre público expuesto por LiteLLM (config_global.yaml: workloads[].nombre_publico)
    model_public_name      TEXT        NOT NULL,
    workload_id            TEXT        NOT NULL,
    deployment_id          UUID,

    -- --- Declarado (config_global.yaml: workloads[].capacidades) ---
    declared_vision        BOOLEAN     NOT NULL DEFAULT TRUE,
    declared_tool_calling  BOOLEAN     NOT NULL DEFAULT FALSE,
    tool_call_parser       TEXT,

    -- --- Sondeado (scripts/test_model_capabilities.py, vía el Gateway público) ---
    -- NULL = inconcluso (error de red, timeout, worker aún arrancando). Nunca
    -- se confunde con "false": ver effective_* más abajo.
    probed_vision          BOOLEAN,
    probed_tool_calling    BOOLEAN,
    probed_json_object     BOOLEAN,
    probed_streaming       BOOLEAN,

    -- --- Efectivo (lo que de verdad se le anuncia al cliente) ---
    -- Fail-closed: declarado Y sondeo=TRUE. Un sondeo inconcluso (NULL) o que
    -- vino en FALSE deja la capacidad apagada.
    effective_vision       BOOLEAN GENERATED ALWAYS AS
                               (declared_vision AND probed_vision IS TRUE) STORED,
    effective_tool_calling BOOLEAN GENERATED ALWAYS AS
                               (declared_tool_calling AND probed_tool_calling IS TRUE) STORED,
    -- json_object no se declara en el contrato (es puramente una capacidad del
    -- runtime, no una promesa del checkpoint); fail-closed = exigir sondeo TRUE.
    effective_json_object  BOOLEAN GENERATED ALWAYS AS
                               (probed_json_object IS TRUE) STORED,

    -- --- Contexto del modelo (para LiteLLM: enable_pre_call_checks) ---
    max_model_len          INT,
    max_output_tokens      INT,

    -- --- Auditoría del sondeo ---
    probe_detail           JSONB       NOT NULL DEFAULT '{}'::JSONB,
    probe_attempts         INT         NOT NULL DEFAULT 0,
    probed_at              TIMESTAMPTZ,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (client_id, environment, model_public_name)
);

CREATE INDEX IF NOT EXISTS idx_model_capability_lookup
    ON sooniverse.model_capability (client_id, environment);

COMMENT ON TABLE sooniverse.model_capability IS
    'Verdad observada por modelo (fail-closed): un consumidor (Open WebUI, LiteLLM) solo debe leer las columnas effective_*, nunca declared_*/probed_* directamente.';

DROP TRIGGER IF EXISTS trg_model_capability_touch ON sooniverse.model_capability;
CREATE TRIGGER trg_model_capability_touch
    BEFORE UPDATE ON sooniverse.model_capability
    FOR EACH ROW EXECUTE FUNCTION sooniverse.fn_touch_updated_at();

-- -----------------------------------------------------------------------------
-- 2. VISTA DE LECTURA
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW sooniverse.v_model_capability_effective AS
SELECT
    client_id,
    environment,
    model_public_name,
    workload_id,
    declared_vision,       probed_vision,       effective_vision,
    declared_tool_calling, probed_tool_calling, effective_tool_calling,
                           probed_json_object,  effective_json_object,
                           probed_streaming,
    max_model_len,
    max_output_tokens,
    probe_attempts,
    probed_at,
    updated_at
FROM sooniverse.model_capability
ORDER BY client_id, environment, model_public_name;

COMMENT ON VIEW sooniverse.v_model_capability_effective IS
    'Inspección rápida de qué capacidad quedó realmente encendida por modelo, para diagnóstico manual (psql) sin tener que razonar las columnas generadas.';
