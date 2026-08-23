-- ==============================================================================
-- 006. Acciones sobre workers + login/API keys unificados
-- ==============================================================================
-- Este archivo se reaplica en cada despliegue (AUTO_INIT_DB): toda columna y
-- tabla nueva es idempotente (IF NOT EXISTS), y los CHECK se recrean con
-- DROP+ADD para poder endurecerlos sin romper una segunda corrida.
--
-- Sección 1: cierra las brechas encontradas en sooniverse.worker_node para
-- poder actuar sobre un nodo desde el panel (comprobar salud, reiniciar vLLM,
-- apagar/arrancar la instancia EC2) con manejo explícito de desincronización.
-- ==============================================================================

SET search_path TO sooniverse;

-- -----------------------------------------------------------------------------
-- 1.1 worker_node: instance_id (necesario para stop/start-instances de EC2 -
--     hasta ahora solo se guardaba la IP privada, nunca el id de la instancia)
--     y un CHECK real sobre health_status (antes texto libre sin restricción,
--     ver 002_infra_state.sql).
-- -----------------------------------------------------------------------------
ALTER TABLE sooniverse.worker_node
    ADD COLUMN IF NOT EXISTS instance_id VARCHAR(32);

COMMENT ON COLUMN sooniverse.worker_node.instance_id IS
    'Id de instancia EC2 (i-xxxx). Lo puebla scripts/sync_endpoints.py::register_in_db '
    'desde describe_instances; sin esto, apagar/arrancar el nodo desde el panel '
    'no tendría a qué instancia apuntar.';

ALTER TABLE sooniverse.worker_node DROP CONSTRAINT IF EXISTS worker_node_health_status_chk;
ALTER TABLE sooniverse.worker_node ADD CONSTRAINT worker_node_health_status_chk
    CHECK (health_status IN ('healthy', 'unhealthy', 'unknown'));

-- -----------------------------------------------------------------------------
-- 1.2 estado_operativo: el estado que de verdad pinta el panel. NO es lo mismo
--     que is_healthy/health_status -esos los escribe sync_endpoints.py en cada
--     resincronización, pero un clúster que desaparece POR COMPLETO de una
--     corrida no se resetea (bug real en register_in_db, corregido en esta
--     misma iteración) y un nodo puede seguir 'is_healthy=true' con datos
--     obsoletos. estado_operativo lo deriva Django (services.estado_pool)
--     cruzando is_healthy/health_status con la frescura de last_seen_at y con
--     LiteLLMClient().health() -nunca se confía solo en is_healthy.
-- -----------------------------------------------------------------------------
ALTER TABLE sooniverse.worker_node
    ADD COLUMN IF NOT EXISTS estado_operativo VARCHAR(16) NOT NULL DEFAULT 'desconocido';

ALTER TABLE sooniverse.worker_node DROP CONSTRAINT IF EXISTS worker_node_estado_operativo_chk;
ALTER TABLE sooniverse.worker_node ADD CONSTRAINT worker_node_estado_operativo_chk
    CHECK (estado_operativo IN ('sano', 'degradado', 'desincronizado', 'apagado', 'reiniciando', 'desconocido'));

COMMENT ON COLUMN sooniverse.worker_node.estado_operativo IS
    'Estado mostrado en la card Pool vLLM del panel. Derivado, no escrito '
    'directamente por sync_endpoints.py -ver metrics/services.py::estado_pool.';

-- -----------------------------------------------------------------------------
-- 1.3 Auditoría de acciones sobre workers -mismo papel que api_key_audit para
--     las API keys.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sooniverse.worker_action (
    id              BIGSERIAL PRIMARY KEY,
    worker_node_id  BIGINT REFERENCES sooniverse.worker_node(id) ON DELETE CASCADE,
    accion          VARCHAR(16) NOT NULL CHECK (accion IN ('health', 'restart', 'stop', 'start')),
    estado          VARCHAR(16) NOT NULL DEFAULT 'solicitada' CHECK (estado IN ('solicitada', 'ok', 'error')),
    actor           VARCHAR(254) NOT NULL DEFAULT 'system',
    source_ip       INET,
    mensaje         VARCHAR(1000),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_worker_action_node ON sooniverse.worker_action (worker_node_id, created_at DESC);

COMMENT ON TABLE sooniverse.worker_action IS
    'Bitácora de acciones ejecutadas desde el panel sobre un worker '
    '(comprobar salud, reiniciar vLLM, apagar/arrancar la instancia EC2).';

-- ==============================================================================
-- Sección 2: inventario único de API Keys (LiteLLM + Open WebUI)
-- ==============================================================================
-- Hasta ahora convivían dos inventarios sin relación: sooniverse.api_key_registry
-- (espejo administrativo de LiteLLM, gestionable desde el panel) y las keys
-- propias de Open WebUI (tabla sooniverse.api_key, creada por sus propias
-- migraciones Alembic -nunca por este archivo-, en claro). 'origen' distingue
-- ambas SIN mezclar sus ciclos de vida: las de LiteLLM se siguen creando/
-- revocando desde el panel; las de Open WebUI se ingestan en SOLO LECTURA
-- (nunca se guarda la clave en claro, solo un hash + los últimos 4 caracteres,
-- igual que ya hace crear_api_key() para las de LiteLLM) y nunca se pueden
-- desactivar desde aquí.
-- ------------------------------------------------------------------------------
ALTER TABLE sooniverse.api_key_registry
    ADD COLUMN IF NOT EXISTS origen VARCHAR(16) NOT NULL DEFAULT 'litellm';

ALTER TABLE sooniverse.api_key_registry DROP CONSTRAINT IF EXISTS api_key_registry_origen_chk;
ALTER TABLE sooniverse.api_key_registry ADD CONSTRAINT api_key_registry_origen_chk
    CHECK (origen IN ('litellm', 'openwebui'));

ALTER TABLE sooniverse.api_key_registry
    ADD COLUMN IF NOT EXISTS openwebui_user_id VARCHAR(64);

CREATE UNIQUE INDEX IF NOT EXISTS ux_apikey_openwebui_user
    ON sooniverse.api_key_registry (openwebui_user_id) WHERE origen = 'openwebui';

COMMENT ON COLUMN sooniverse.api_key_registry.origen IS
    'litellm = key real de LiteLLM, gestionable desde el panel. openwebui = espejo '
    'de solo lectura de sooniverse."user"/api_key -nunca se crea/revoca desde aquí.';
COMMENT ON COLUMN sooniverse.api_key_registry.openwebui_user_id IS
    'FK lógica a sooniverse."user".id (Open WebUI, tabla ajena). Solo tiene '
    'sentido con origen=''openwebui''.';

-- ------------------------------------------------------------------------------
-- ingest_openwebui_apikeys(): ETL de solo lectura. Fail-soft si Open WebUI
-- todavía no aplicó sus migraciones en este despliegue (tablas ausentes).
-- sha256()/encode() son funciones nativas de PostgreSQL 11+, sin extensión.
-- ------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION sooniverse.ingest_openwebui_apikeys()
RETURNS INTEGER AS $$
DECLARE
    v_count INTEGER := 0;
BEGIN
    IF to_regclass('sooniverse."user"') IS NULL OR to_regclass('sooniverse.api_key') IS NULL THEN
        RETURN 0;
    END IF;

    WITH ingesta AS (
        INSERT INTO sooniverse.api_key_registry
            (key_alias, litellm_token_hash, key_prefix, owner_email, is_active,
             origen, openwebui_user_id, created_at, updated_at, expires_at)
        SELECT
            'openwebui-' || COALESCE(NULLIF(u.name, ''), u.email),
            encode(sha256(ak.key::bytea), 'hex'),
            left(ak.key, 8) || '…' || right(ak.key, 4),
            u.email,
            TRUE,
            'openwebui',
            u.id,
            to_timestamp(ak.created_at),
            to_timestamp(ak.updated_at),
            CASE WHEN ak.expires_at IS NOT NULL THEN to_timestamp(ak.expires_at) END
        FROM sooniverse.api_key ak
        JOIN sooniverse."user" u ON u.id = ak.user_id
        ON CONFLICT (openwebui_user_id) WHERE origen = 'openwebui' DO UPDATE SET
            litellm_token_hash = EXCLUDED.litellm_token_hash,
            key_prefix         = EXCLUDED.key_prefix,
            key_alias          = EXCLUDED.key_alias,
            owner_email        = EXCLUDED.owner_email,
            updated_at         = EXCLUDED.updated_at,
            expires_at         = EXCLUDED.expires_at
        RETURNING 1
    )
    SELECT count(*) INTO v_count FROM ingesta;

    -- Una key de Open WebUI que el usuario borró desaparece también del
    -- inventario -a diferencia de las de LiteLLM (nunca se borran, solo se
    -- desactivan), estas son un espejo de solo lectura de una tabla ajena.
    DELETE FROM sooniverse.api_key_registry r
    WHERE r.origen = 'openwebui'
      AND NOT EXISTS (SELECT 1 FROM sooniverse.api_key ak WHERE ak.user_id = r.openwebui_user_id);

    RETURN v_count;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION sooniverse.ingest_openwebui_apikeys() IS
    'Ingesta de solo lectura: sooniverse.api_key (Open WebUI, en claro) -> '
    'api_key_registry (solo hash + prefijo, origen=openwebui). Nunca escribe '
    'en sooniverse.api_key. Fail-soft si Open WebUI aún no migró su esquema.';
