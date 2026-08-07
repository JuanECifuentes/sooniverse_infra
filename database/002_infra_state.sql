-- =============================================================================
-- SOONIVERSE INFRA :: FASE 2 :: ESTADO DE INFRAESTRUCTURA (RED AUTOGESTIONADA)
-- =============================================================================
-- Idempotente. Ejecutable N veces sin efectos colaterales.
--
-- Este es el mecanismo de propiedad de `AwsNetworkManager` (scripts/aws_network.py):
-- un recurso AWS solo se destruye si (a) aparece aquí con su `deployment_id` y
-- (b) sus tags AWS reales siguen coincidiendo con ese mismo `deployment_id`.
-- Si la BD no es alcanzable, el aprovisionamiento debe abortar ANTES de crear
-- nada en AWS (ver scripts/infra_state.py, PostgresInfraStateStore.ping()).
--
-- No reemplaza ni modifica 001_init_schema.sql; se aplica después en el mismo
-- esquema `sooniverse`. db_setup.py aplica database/*.sql en orden lexicográfico.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS sooniverse;

SET search_path TO sooniverse;

-- -----------------------------------------------------------------------------
-- 1. DESPLIEGUES (uno por cliente/entorno/región activo a la vez)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sooniverse.infra_deployment (
    id                BIGSERIAL PRIMARY KEY,
    deployment_id     UUID        NOT NULL UNIQUE,
    client_id         TEXT        NOT NULL,
    environment       TEXT        NOT NULL,
    region            TEXT        NOT NULL,
    cloud             TEXT        NOT NULL DEFAULT 'aws',
    -- planning|creating|active|degraded|destroying|destroyed|error
    status            TEXT        NOT NULL,
    managed_network   BOOLEAN     NOT NULL DEFAULT TRUE,
    config_hash       TEXT,
    -- Contrato completo (config_global.yaml efectivo) SIN secretos: ver
    -- PostgresInfraStateStore._strip_secrets() en scripts/infra_state.py.
    config_snapshot   JSONB,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    destroyed_at      TIMESTAMPTZ,
    last_error        TEXT,
    CONSTRAINT infra_deployment_status_chk CHECK (
        status IN ('planning', 'creating', 'active', 'degraded', 'destroying', 'destroyed', 'error')
    )
);

-- Solo un despliegue activo por (cliente, entorno, región): es lo que impide
-- que dos "provision" concurrentes del mismo cliente colisionen en la misma VPC.
CREATE UNIQUE INDEX IF NOT EXISTS ux_infra_deployment_active
    ON sooniverse.infra_deployment (client_id, environment, region)
    WHERE status NOT IN ('destroyed', 'error');

CREATE INDEX IF NOT EXISTS idx_infra_deployment_client ON sooniverse.infra_deployment (client_id, environment);

COMMENT ON TABLE sooniverse.infra_deployment IS
    'Un despliegue = un ciclo de vida completo de red+gateway+workers para (cliente, entorno, región). config_snapshot nunca contiene secretos.';

-- -----------------------------------------------------------------------------
-- 2. RECURSOS AWS registrados por despliegue (mecanismo de propiedad)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sooniverse.infra_resource (
    id                BIGSERIAL PRIMARY KEY,
    deployment_id     UUID        NOT NULL REFERENCES sooniverse.infra_deployment(deployment_id) ON DELETE CASCADE,
    -- vpc|subnet|igw|eip|nat|route_table|security_group|vpc_endpoint|sky_cluster
    resource_type     TEXT        NOT NULL,
    component         TEXT        NOT NULL,   -- el tag sooniverse:component (vpc, sg-gateway, nat, ...)
    aws_id            TEXT,                   -- vpc-..., subnet-..., sg-...
    aws_arn           TEXT,
    region            TEXT        NOT NULL,
    availability_zone TEXT,
    parent_aws_id     TEXT,
    delete_order      INT         NOT NULL DEFAULT 999,
    managed_by_us     BOOLEAN     NOT NULL DEFAULT TRUE,
    -- creating|active|deleting|deleted|orphan|adopted|error
    state             TEXT        NOT NULL DEFAULT 'creating',
    attributes        JSONB       NOT NULL DEFAULT '{}'::JSONB,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at        TIMESTAMPTZ,
    UNIQUE (deployment_id, resource_type, aws_id)
);

CREATE INDEX IF NOT EXISTS idx_infra_resource_deployment ON sooniverse.infra_resource (deployment_id, delete_order);
CREATE INDEX IF NOT EXISTS idx_infra_resource_aws_id     ON sooniverse.infra_resource (aws_id);

COMMENT ON TABLE sooniverse.infra_resource IS
    'Inventario de recursos AWS creados por AwsNetworkManager. destroy() solo borra filas cuyo aws_id sigue existiendo en AWS con tags que coinciden con deployment_id.';

-- -----------------------------------------------------------------------------
-- 3. AUDITORÍA de eventos por fase (network|gateway|workers|endpoints|destroy)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sooniverse.infra_event (
    id              BIGSERIAL PRIMARY KEY,
    deployment_id   UUID        NOT NULL,
    phase           TEXT        NOT NULL,
    action          TEXT        NOT NULL,
    resource_ref    TEXT,
    status          TEXT        NOT NULL,   -- started|ok|warning|error
    message         TEXT,
    duration_ms     INT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT infra_event_status_chk CHECK (status IN ('started', 'ok', 'warning', 'error'))
);

CREATE INDEX IF NOT EXISTS idx_infra_event_deployment ON sooniverse.infra_event (deployment_id, created_at DESC);

COMMENT ON TABLE sooniverse.infra_event IS
    'Bitácora de auditoría de cada operación de aprovisionamiento/destrucción. Toda escritura de infra_resource/infra_deployment va acompañada de un evento en el mismo commit.';

-- -----------------------------------------------------------------------------
-- 4. AMPLIACIÓN de sooniverse.worker_node (creada en 001_init_schema.sql) con
--    los datos de red del despliegue que la sincronizó.
-- -----------------------------------------------------------------------------
ALTER TABLE sooniverse.worker_node
    ADD COLUMN IF NOT EXISTS deployment_id      UUID,
    ADD COLUMN IF NOT EXISTS subnet_id          VARCHAR(64),
    ADD COLUMN IF NOT EXISTS security_group_id  VARCHAR(64),
    ADD COLUMN IF NOT EXISTS last_health_check  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS health_status       VARCHAR(16) NOT NULL DEFAULT 'unknown';

CREATE INDEX IF NOT EXISTS idx_worker_deployment ON sooniverse.worker_node (deployment_id);

-- -----------------------------------------------------------------------------
-- 5. VISTAS DE LECTURA
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW sooniverse.v_infra_deployment_summary AS
SELECT
    d.deployment_id,
    d.client_id,
    d.environment,
    d.region,
    d.status,
    d.created_at,
    d.destroyed_at,
    EXTRACT(EPOCH FROM (COALESCE(d.destroyed_at, now()) - d.created_at)) / 3600.0 AS edad_horas,
    COUNT(r.id)                                              AS recursos_totales,
    COUNT(r.id) FILTER (WHERE r.state = 'active')            AS recursos_activos,
    COUNT(r.id) FILTER (WHERE r.component = 'nat')            AS nat_gateways,
    COUNT(r.id) FILTER (WHERE r.component = 'eip')            AS elastic_ips,
    -- Estimación aproximada: NAT (~0.045 USD/h) + EIP asociada (~0.005 USD/h). No incluye tráfico.
    (COUNT(r.id) FILTER (WHERE r.component = 'nat') * 0.045
        + COUNT(r.id) FILTER (WHERE r.component = 'eip') * 0.005)::NUMERIC(10, 4) AS costo_estimado_usd_hora
FROM sooniverse.infra_deployment d
LEFT JOIN sooniverse.infra_resource r ON r.deployment_id = d.deployment_id
GROUP BY d.deployment_id, d.client_id, d.environment, d.region, d.status, d.created_at, d.destroyed_at;

COMMENT ON VIEW sooniverse.v_infra_deployment_summary IS
    'Un renglón por despliegue con conteo de recursos y coste estimado por hora (solo NAT+EIP; no incluye cómputo ni tráfico).';

CREATE OR REPLACE VIEW sooniverse.v_infra_orphans AS
SELECT
    r.deployment_id,
    r.resource_type,
    r.component,
    r.aws_id,
    r.region,
    r.state,
    r.created_at
FROM sooniverse.infra_resource r
JOIN sooniverse.infra_deployment d ON d.deployment_id = r.deployment_id
WHERE d.status IN ('destroyed', 'error')
  AND r.state NOT IN ('deleted');

COMMENT ON VIEW sooniverse.v_infra_orphans IS
    'Recursos registrados que pertenecen a un despliegue ya destruido/erróneo pero que no quedaron marcados deleted. Candidatos a --scan-orphans.';
