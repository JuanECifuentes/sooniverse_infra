-- =============================================================================
-- SOONIVERSE INFRA :: ANALÍTICA DE USO (latencia real, hora local, ocio)
-- =============================================================================
-- Idempotente. Ejecutable N veces sin efectos colaterales.
--
-- PRIVACIDAD: se mantiene la regla dura de 001_init_schema.sql. Ninguna columna
--   nueva almacena prompts, mensajes ni respuestas. Solo contadores, marcas de
--   tiempo, identificadores de enrutado y códigos de estado.
--
-- POR QUÉ EXISTE ESTE ARCHIVO
--   El panel podía responder "cuánto se consumió", pero no "cuándo se usa" ni
--   "cuánto aguanta la máquina". La causa no era la interfaz, eran seis huecos
--   de datos verificados en el propio esquema:
--
--     1) El ETL leía 8 columnas de litellm."LiteLLM_SpendLogs" y NO leía
--        "endTime" ni "completionStartTime" -> token_usage_event.latency_ms era
--        NULL SIEMPRE. No había ni una latencia medida en todo el sistema.
--     2) Tampoco leía "status" -> token_usage_event.status se quedaba en su
--        DEFAULT 'success' para todo, así que token_usage_rollup.error_count
--        era 0 siempre y la tarjeta "Tasa de error" del panel era decorativa.
--     3) Tampoco leía "api_base" -> worker_endpoint NULL: imposible atribuir
--        carga a un worker concreto.
--     4) La agregación mínima era DIARIA (token_usage_rollup.bucket_start es
--        DATE), así que "¿a qué hora del sábado está parada la máquina?" era
--        literalmente incontestable.
--     5) DATE_TRUNC usa la zona horaria DE LA SESIÓN. Django fija la conexión
--        en UTC (USE_TZ=True) mientras el panel renderiza en America/Bogota:
--        5 h de desfase entre el bucket y el día que ve el usuario. Y el corte
--        cambiaba según quién disparara el ETL (Django, db_setup.py o psql).
--     6) En refresh_usage_rollups, el grupo `api_key_id IS NULL` (eventos cuya
--        key no está en api_key_registry) DUPLICABA filas en cada refresco: en
--        PostgreSQL un UNIQUE con columna nullable no deduplica NULLs, así que
--        el ON CONFLICT nunca disparaba para ese grupo.
--
--   Este archivo cierra los seis y añade la agregación horaria en hora local.
--
-- ORDEN DE CARGA: después de 001/002/003 (db_setup.py los aplica en orden
--   lexicográfico). No modifica ninguno de los tres.
-- =============================================================================

SET search_path TO sooniverse;

-- -----------------------------------------------------------------------------
-- 0. AVISO DE ARIDAD: por qué hay un DROP antes de redefinir la función
-- -----------------------------------------------------------------------------
-- Añadir un parámetro con DEFAULT a refresh_usage_rollups(INTEGER) NO reemplaza
-- la función: crea una SOBRECARGA. La llamada existente
-- `SELECT sooniverse.refresh_usage_rollups(90)` pasaría a resolver contra dos
-- candidatas y PostgreSQL respondería `function ... is not unique`, rompiendo
-- scripts/db_setup.py::refresh_metrics y metrics/services.py::refrescar_metricas.
-- ingest_litellm_spendlogs SÍ conserva su firma (INTEGER) RETURNS INTEGER por el
-- mismo motivo: ambos consumidores hacen cur.fetchone()[0] sobre un escalar.
-- -----------------------------------------------------------------------------
DROP FUNCTION IF EXISTS sooniverse.refresh_usage_rollups(INTEGER);

-- -----------------------------------------------------------------------------
-- 1. ZONA HORARIA DE REPORTE COMO DATO DEL MOTOR
-- -----------------------------------------------------------------------------
-- El fallo que se arregla aquí es exactamente "alguien llamó a la función sin
-- pasar la zona". Un parámetro explícito no basta como única defensa: hace
-- falta que el DEFAULT dentro del motor ya sea correcto, para que un cron con
-- psql corte los buckets igual que el panel.
-- scripts/db_setup.py reconcilia este valor desde .env:TIME_ZONE en cada
-- despliegue y avisa si cambió.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sooniverse.app_setting (
    key         TEXT        PRIMARY KEY,
    value       TEXT        NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO sooniverse.app_setting (key, value) VALUES ('reporting_timezone', 'UTC')
ON CONFLICT (key) DO NOTHING;

COMMENT ON TABLE sooniverse.app_setting IS
    'Ajustes de la aplicación que deben ser legibles DESDE SQL (no solo desde Django). '
    'Hoy solo reporting_timezone, que determina el corte de los buckets de agregación.';

CREATE OR REPLACE FUNCTION sooniverse.reporting_timezone(p_override TEXT DEFAULT NULL)
RETURNS TEXT AS $$
    SELECT COALESCE(
        NULLIF(p_override, ''),
        (SELECT value FROM sooniverse.app_setting WHERE key = 'reporting_timezone'),
        'UTC'
    );
$$ LANGUAGE sql STABLE;

-- -----------------------------------------------------------------------------
-- 2. COLUMNAS NUEVAS EN token_usage_event
-- -----------------------------------------------------------------------------
ALTER TABLE sooniverse.token_usage_event
    ADD COLUMN IF NOT EXISTS ttft_ms      INTEGER,
    ADD COLUMN IF NOT EXISTS model_group  VARCHAR(160),
    ADD COLUMN IF NOT EXISTS model_id     VARCHAR(255),
    ADD COLUMN IF NOT EXISTS call_type    VARCHAR(40),
    ADD COLUMN IF NOT EXISTS cache_hit    BOOLEAN;

COMMENT ON COLUMN sooniverse.token_usage_event.ttft_ms IS
    'Time-to-first-token en ms (completionStartTime - startTime). Sin esta columna no se '
    'puede distinguir "el modelo tarda en arrancar" de "el modelo genera lento".';
COMMENT ON COLUMN sooniverse.token_usage_event.model_group IS
    'Nombre público con el que LiteLLM enrutó (model_group), frente a model_name que es el '
    'checkpoint subyacente.';
COMMENT ON COLUMN sooniverse.token_usage_event.model_id IS
    'Identificador del deployment concreto dentro del pool de LiteLLM. Más estable que la IP '
    'privada del worker, que cambia entre despliegues.';
COMMENT ON COLUMN sooniverse.token_usage_event.cache_hit IS
    'Un acierto de caché con latency_ms=3 envenena el p95 y hace creer que la infraestructura '
    'es más rápida de lo que es. Hay que poder excluirlo de los percentiles.';

CREATE INDEX IF NOT EXISTS idx_usage_event_endpoint_ts
    ON sooniverse.token_usage_event (worker_endpoint, event_ts DESC);
-- Índice parcial: el filtro "solo errores" del panel toca una fracción diminuta
-- de la tabla y no merece un índice completo.
CREATE INDEX IF NOT EXISTS idx_usage_event_errors
    ON sooniverse.token_usage_event (event_ts DESC) WHERE status <> 'success';

-- -----------------------------------------------------------------------------
-- 3. HELPERS DE COMPATIBILIDAD CON PRISMA
-- -----------------------------------------------------------------------------
-- litellm."LiteLLM_SpendLogs" la crea Prisma y su juego de columnas cambia entre
-- versiones de LiteLLM. El ETL se construye con SQL dinámico a partir de
-- information_schema para que un upgrade (o downgrade) del proxy no lo rompa
-- entero por una columna que falte.
--
-- Se DESCARTA la alternativa "crear una vista de compatibilidad dentro del
-- esquema litellm": crear objetos propios ahí es justo lo que prohíbe el
-- comentario CONVIVENCIA CON LITELLM de 001_init_schema.sql (Prisma hace diff
-- agresivo de todo lo que encuentra y llegó a intentar DROP TABLE sobre una
-- tabla nuestra).
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION sooniverse._spendlog_has(p_col TEXT)
RETURNS BOOLEAN AS $$
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'litellm' AND table_name = 'LiteLLM_SpendLogs'
          AND column_name = p_col
    );
$$ LANGUAGE sql STABLE;

CREATE OR REPLACE FUNCTION sooniverse._spendlog_expr(p_col TEXT, p_fallback TEXT DEFAULT 'NULL::text')
RETURNS TEXT AS $$
    SELECT CASE WHEN sooniverse._spendlog_has(p_col)
                THEN format('sl.%I', p_col)
                ELSE p_fallback END;
$$ LANGUAGE sql STABLE;

-- OJO: Prisma mapea DateTime a `timestamp(3) SIN zona` en varias versiones de
-- LiteLLM. Comparar eso con NOW() o guardarlo en un TIMESTAMPTZ aplica un cast
-- implícito que usa la zona DE LA SESIÓN: es el mismo bug de corte del hueco 5,
-- pero dentro del propio ETL, desplazando event_ts según quién lo dispare.
-- Prisma persiste en UTC, así que cuando la columna es naive se la ancla ahí.
CREATE OR REPLACE FUNCTION sooniverse._spendlog_ts_expr(p_col TEXT)
RETURNS TEXT AS $$
    SELECT CASE
        WHEN NOT sooniverse._spendlog_has(p_col) THEN 'NULL::timestamptz'
        WHEN (SELECT data_type FROM information_schema.columns
              WHERE table_schema = 'litellm' AND table_name = 'LiteLLM_SpendLogs'
                AND column_name = p_col) = 'timestamp without time zone'
            THEN format('(sl.%I AT TIME ZONE ''UTC'')', p_col)
        ELSE format('sl.%I', p_col)
    END;
$$ LANGUAGE sql STABLE;

-- Diferencia en milisegundos, defensiva: descarta relojes inconsistentes y
-- valores absurdos que contaminarían los percentiles.
CREATE OR REPLACE FUNCTION sooniverse._delta_ms(p_from TIMESTAMPTZ, p_to TIMESTAMPTZ)
RETURNS INTEGER AS $$
    SELECT CASE
        WHEN p_from IS NULL OR p_to IS NULL       THEN NULL
        WHEN p_to < p_from                        THEN NULL   -- reloj inconsistente
        WHEN p_to - p_from > INTERVAL '2 hours'   THEN NULL   -- outlier: fuera del p95
        ELSE (EXTRACT(EPOCH FROM (p_to - p_from)) * 1000)::INTEGER
    END;
$$ LANGUAGE sql IMMUTABLE;

-- -----------------------------------------------------------------------------
-- 4. ETL REESCRITO :: LITELLM_SPENDLOGS -> TOKEN_USAGE_EVENT
-- -----------------------------------------------------------------------------
-- Devuelve (insertados, enriquecidos) para poder distinguir filas nuevas de
-- filas existentes a las que se les rellenó un hueco.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION sooniverse.ingest_litellm_spendlogs_range(
    p_from TIMESTAMPTZ,
    p_to   TIMESTAMPTZ DEFAULT NULL
)
RETURNS TABLE (inserted INTEGER, updated INTEGER) AS $$
DECLARE
    v_sql    TEXT;
    v_start  TEXT; v_end   TEXT; v_first TEXT;
    v_group  TEXT; v_mid   TEXT; v_base  TEXT;
    v_ctype  TEXT; v_cache TEXT; v_status TEXT;
BEGIN
    inserted := 0;
    updated  := 0;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'litellm' AND table_name = 'LiteLLM_SpendLogs'
    ) THEN
        RAISE NOTICE 'LiteLLM_SpendLogs no existe todavía; ETL omitido.';
        RETURN NEXT;
        RETURN;
    END IF;

    -- Sin estas dos no hay ni idempotencia ni eje temporal: mejor no ingerir
    -- nada que ingerir basura desde una versión de LiteLLM incompatible.
    IF NOT sooniverse._spendlog_has('request_id') OR NOT sooniverse._spendlog_has('startTime') THEN
        RAISE NOTICE 'LiteLLM_SpendLogs sin request_id/startTime; ETL omitido (versión incompatible).';
        RETURN NEXT;
        RETURN;
    END IF;

    v_start  := sooniverse._spendlog_ts_expr('startTime');
    v_end    := sooniverse._spendlog_ts_expr('endTime');
    v_first  := sooniverse._spendlog_ts_expr('completionStartTime');
    v_group  := sooniverse._spendlog_expr('model_group');
    v_mid    := sooniverse._spendlog_expr('model_id');
    v_base   := sooniverse._spendlog_expr('api_base');
    v_ctype  := sooniverse._spendlog_expr('call_type');
    v_cache  := sooniverse._spendlog_expr('cache_hit', 'NULL::boolean');
    v_status := sooniverse._spendlog_expr('status');

    v_sql := format($fmt$
        INSERT INTO sooniverse.token_usage_event AS e (
            api_key_id, litellm_token_hash, litellm_request_id, model_name, model_group,
            model_id, worker_endpoint, call_type, cache_hit,
            prompt_tokens, completion_tokens, total_tokens, spend_usd,
            latency_ms, ttft_ms, status, event_ts
        )
        SELECT
            reg.id,
            sl."api_key",
            sl."request_id",
            COALESCE(NULLIF(sl."model", ''), 'unknown'),
            LEFT(NULLIF(%1$s, ''), 160),
            LEFT(NULLIF(%2$s, ''), 255),
            -- 'http://10.0.1.23:8007/v1' -> '10.0.1.23:8007'
            LEFT(NULLIF(regexp_replace(COALESCE(%3$s, ''), '^[a-z]+://([^/]+).*$', '\1'), ''), 255),
            LEFT(NULLIF(%4$s, ''), 40),
            CASE WHEN (%5$s) IS NULL THEN NULL
                 ELSE lower((%5$s)::text) IN ('true', 't', '1') END,
            COALESCE(sl."prompt_tokens", 0),
            COALESCE(sl."completion_tokens", 0),
            COALESCE(sl."total_tokens",
                     COALESCE(sl."prompt_tokens", 0) + COALESCE(sl."completion_tokens", 0)),
            COALESCE(sl."spend", 0),
            sooniverse._delta_ms(%6$s, %7$s),
            sooniverse._delta_ms(%6$s, %8$s),
            -- 'success' cuando la columna no existe (LiteLLM viejo) o viene vacía.
            -- Cualquier otro valor cuenta como error en los rollups, que filtran
            -- por status <> 'success'.
            CASE WHEN (%9$s) IS NULL OR (%9$s)::text = '' THEN 'success'
                 ELSE LEFT(lower((%9$s)::text), 24) END,
            COALESCE(%6$s, NOW())
        FROM litellm."LiteLLM_SpendLogs" sl
        LEFT JOIN sooniverse.api_key_registry reg ON reg.litellm_token_hash = sl."api_key"
        WHERE %6$s >= $1 AND (%6$s < $2 OR $2 IS NULL)
        ON CONFLICT (litellm_request_id) DO UPDATE SET
            -- COALESCE(EXCLUDED, e): un re-ingest NUNCA borra un dato que ya
            -- teníamos (p.ej. si LiteLLM purgara endTime); solo rellena huecos.
            api_key_id      = COALESCE(EXCLUDED.api_key_id,      e.api_key_id),
            latency_ms      = COALESCE(EXCLUDED.latency_ms,      e.latency_ms),
            ttft_ms         = COALESCE(EXCLUDED.ttft_ms,         e.ttft_ms),
            worker_endpoint = COALESCE(EXCLUDED.worker_endpoint, e.worker_endpoint),
            model_group     = COALESCE(EXCLUDED.model_group,     e.model_group),
            model_id        = COALESCE(EXCLUDED.model_id,        e.model_id),
            call_type       = COALESCE(EXCLUDED.call_type,       e.call_type),
            cache_hit       = COALESCE(EXCLUDED.cache_hit,       e.cache_hit),
            status          = EXCLUDED.status,
            ingested_at     = NOW()
        -- Guarda anti-escritura. Sin ella, CADA corrida reescribiría toda la
        -- ventana (WAL, tuplas muertas, todos los índices tocados) aunque no
        -- cambiara ni un campo. Con ella, en régimen estacionario el UPDATE
        -- toca 0 filas y la corrida cuesta lo mismo que el DO NOTHING original.
        WHERE (e.api_key_id, e.latency_ms, e.ttft_ms, e.worker_endpoint,
               e.model_group, e.model_id, e.call_type, e.cache_hit, e.status)
              IS DISTINCT FROM
              (COALESCE(EXCLUDED.api_key_id,      e.api_key_id),
               COALESCE(EXCLUDED.latency_ms,      e.latency_ms),
               COALESCE(EXCLUDED.ttft_ms,         e.ttft_ms),
               COALESCE(EXCLUDED.worker_endpoint, e.worker_endpoint),
               COALESCE(EXCLUDED.model_group,     e.model_group),
               COALESCE(EXCLUDED.model_id,        e.model_id),
               COALESCE(EXCLUDED.call_type,       e.call_type),
               COALESCE(EXCLUDED.cache_hit,       e.cache_hit),
               EXCLUDED.status)
        RETURNING (xmax = 0) AS was_insert
    $fmt$, v_group, v_mid, v_base, v_ctype, v_cache, v_start, v_end, v_first, v_status);

    -- xmax = 0 distingue una inserción real de una actualización por conflicto.
    EXECUTE 'WITH w AS (' || v_sql || ') SELECT '
            ' COALESCE(COUNT(*) FILTER (WHERE was_insert), 0)::int, '
            ' COALESCE(COUNT(*) FILTER (WHERE NOT was_insert), 0)::int FROM w'
        INTO inserted, updated
        USING p_from, p_to;

    RETURN NEXT;
END;
$$ LANGUAGE plpgsql;

-- Envoltorio que PRESERVA el contrato escalar que ya consumen db_setup.py y
-- metrics/services.py: devuelve solo los eventos realmente nuevos, que es lo
-- que dice el mensaje del panel ("N eventos nuevos").
CREATE OR REPLACE FUNCTION sooniverse.ingest_litellm_spendlogs(p_since_hours INTEGER DEFAULT 48)
RETURNS INTEGER AS $$
DECLARE
    r RECORD;
BEGIN
    SELECT * INTO r
    FROM sooniverse.ingest_litellm_spendlogs_range(
        NOW() - make_interval(hours => p_since_hours), NULL);

    IF r.updated > 0 THEN
        RAISE NOTICE '% evento(s) existentes enriquecidos (latency/ttft/endpoint/status).', r.updated;
    END IF;
    RETURN r.inserted;
END;
$$ LANGUAGE plpgsql;

-- Backfill del histórico, por lotes. Invocación MANUAL
-- (`python scripts/db_setup.py --backfill 3650`): reescribe filas antiguas para
-- rellenarles latencia/estado/endpoint, así que conviene un
-- `VACUUM (ANALYZE) sooniverse.token_usage_event` después.
CREATE OR REPLACE FUNCTION sooniverse.backfill_litellm_spendlogs(
    p_since_days INTEGER DEFAULT 3650,
    p_batch_days INTEGER DEFAULT 7
)
RETURNS INTEGER AS $$
DECLARE
    v_cursor TIMESTAMPTZ;
    v_stop   TIMESTAMPTZ;
    r        RECORD;
    v_total  INTEGER := 0;
BEGIN
    v_cursor := NOW() - make_interval(days => p_since_days);
    WHILE v_cursor < NOW() LOOP
        v_stop := v_cursor + make_interval(days => p_batch_days);
        SELECT * INTO r FROM sooniverse.ingest_litellm_spendlogs_range(v_cursor, v_stop);
        v_total := v_total + r.inserted + r.updated;
        RAISE NOTICE 'backfill % .. %: % nuevos, % enriquecidos',
                     v_cursor, v_stop, r.inserted, r.updated;
        v_cursor := v_stop;
    END LOOP;
    RETURN v_total;
END;
$$ LANGUAGE plpgsql;

-- -----------------------------------------------------------------------------
-- 5. BUG DE DUPLICADOS EN LOS ROLLUPS
-- -----------------------------------------------------------------------------
-- rollup_unique_bucket incluye api_key_id, que es NULLable. En PostgreSQL dos
-- filas con NULL en una columna del UNIQUE NO colisionan, así que para los
-- eventos cuya key no está registrada el ON CONFLICT nunca disparaba y
-- refresh_usage_rollups insertaba una fila nueva EN CADA REFRESCO.
-- Se sustituye por una columna generada con centinela 0 = "sin registro".
-- (UNIQUE NULLS NOT DISTINCT sería más limpio pero exige PG15, y la versión de
--  PostgreSQL no está fijada en el contrato del proyecto.)
-- -----------------------------------------------------------------------------
ALTER TABLE sooniverse.token_usage_rollup
    ADD COLUMN IF NOT EXISTS api_key_key BIGINT
        GENERATED ALWAYS AS (COALESCE(api_key_id, 0)) STORED;

-- Purga de los duplicados ya acumulados: se conserva el cálculo más reciente.
-- Tiene que ir ANTES de crear el índice único, o la creación falla.
DELETE FROM sooniverse.token_usage_rollup t
USING (
    SELECT id, row_number() OVER (
               PARTITION BY granularity, bucket_start, COALESCE(api_key_id, 0), model_name
               ORDER BY computed_at DESC, id DESC
           ) AS rn
    FROM sooniverse.token_usage_rollup
) d
WHERE d.id = t.id AND d.rn > 1;

ALTER TABLE sooniverse.token_usage_rollup DROP CONSTRAINT IF EXISTS rollup_unique_bucket;

CREATE UNIQUE INDEX IF NOT EXISTS ux_rollup_bucket
    ON sooniverse.token_usage_rollup (granularity, bucket_start, api_key_key, model_name);

-- -----------------------------------------------------------------------------
-- 6. RECÁLCULO DE AGREGACIONES, AHORA CON ZONA HORARIA EXPLÍCITA
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION sooniverse.refresh_usage_rollups(
    p_since_days INTEGER DEFAULT 90,
    p_timezone   TEXT    DEFAULT NULL
)
RETURNS INTEGER AS $$
DECLARE
    v_tz    TEXT := sooniverse.reporting_timezone(p_timezone);
    v_from  TIMESTAMPTZ;
    v_total INTEGER := 0;
    v_rows  INTEGER;
    v_gran  TEXT;
    v_trunc TEXT;
BEGIN
    -- El límite inferior es el INICIO DEL DÍA LOCAL hace N días. Con un
    -- `NOW() - N days` a secas, el bucket más antiguo del rango se recalculaba
    -- a partir de un día PARCIAL de eventos y se guardaba con un total menor
    -- que el real.
    v_from := (date_trunc('day', (NOW() AT TIME ZONE v_tz))
               - make_interval(days => p_since_days)) AT TIME ZONE v_tz;

    FOREACH v_gran IN ARRAY ARRAY['daily', 'weekly', 'monthly'] LOOP
        v_trunc := CASE v_gran WHEN 'daily' THEN 'day' WHEN 'weekly' THEN 'week' ELSE 'month' END;

        EXECUTE format($fmt$
            INSERT INTO sooniverse.token_usage_rollup (
                granularity, bucket_start, api_key_id, model_name,
                request_count, prompt_tokens, completion_tokens, total_tokens,
                spend_usd, avg_latency_ms, error_count, computed_at
            )
            SELECT
                %1$L,
                -- `event_ts AT TIME ZONE tz` da un timestamp NAIVE en hora local:
                -- truncarlo y castearlo a DATE ya no depende de la zona de la
                -- sesión, que era el desfase de 5 h entre Django (UTC) y el
                -- panel (America/Bogota).
                date_trunc(%2$L, e.event_ts AT TIME ZONE %3$L)::DATE,
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
            WHERE e.event_ts >= %4$L
            GROUP BY 2, 3, 4
            ON CONFLICT (granularity, bucket_start, api_key_key, model_name) DO UPDATE SET
                request_count     = EXCLUDED.request_count,
                prompt_tokens     = EXCLUDED.prompt_tokens,
                completion_tokens = EXCLUDED.completion_tokens,
                total_tokens      = EXCLUDED.total_tokens,
                spend_usd         = EXCLUDED.spend_usd,
                avg_latency_ms    = EXCLUDED.avg_latency_ms,
                error_count       = EXCLUDED.error_count,
                computed_at       = NOW()
        $fmt$, v_gran, v_trunc, v_tz, v_from);

        GET DIAGNOSTICS v_rows = ROW_COUNT;
        v_total := v_total + v_rows;
    END LOOP;

    RETURN v_total;
END;
$$ LANGUAGE plpgsql;

-- -----------------------------------------------------------------------------
-- 7. AGREGACIÓN HORARIA EN HORA LOCAL
-- -----------------------------------------------------------------------------
-- Tabla nueva y no `granularity='hourly'` dentro de token_usage_rollup porque
-- allí bucket_start es DATE: ensancharlo a TIMESTAMPTZ rompería el CHECK, el
-- índice único, el modelo Django (bucket_start = DateField) y todas las filas
-- existentes. Además el rollup no tiene sitio para percentiles ni para las
-- dimensiones locales que sirve el mapa de calor.
--
-- REGLA QUE HAY QUE RESPETAR EN TODOS LOS CONSUMIDORES:
--   los percentiles de esta tabla son POR HORA y NO son recombinables.
--   Promediar los p95 de 13 lunes NO da el p95 del lunes. Para un percentil
--   sobre una ventana mayor de una hora, usar sooniverse.latency_percentiles().
--   Lo que SÍ se puede recombinar es latency_sum_ms / latency_count.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sooniverse.usage_hourly (
    id                  BIGSERIAL PRIMARY KEY,
    bucket_ts           TIMESTAMPTZ  NOT NULL,   -- instante absoluto de inicio de la hora
    tz_name             TEXT         NOT NULL,   -- con qué zona se cortó (auditoría)
    -- Derivadas: sirven el mapa de calor sin recalcular EXTRACT en cada consulta.
    bucket_local_date   DATE         NOT NULL,
    bucket_local_hour   SMALLINT     NOT NULL CHECK (bucket_local_hour   BETWEEN 0 AND 23),
    bucket_local_isodow SMALLINT     NOT NULL CHECK (bucket_local_isodow BETWEEN 1 AND 7),

    api_key_id          BIGINT REFERENCES sooniverse.api_key_registry (id) ON DELETE CASCADE,
    api_key_key         BIGINT GENERATED ALWAYS AS (COALESCE(api_key_id, 0)) STORED,
    model_name          VARCHAR(160) NOT NULL DEFAULT 'unknown',

    request_count       BIGINT       NOT NULL DEFAULT 0,
    error_count         BIGINT       NOT NULL DEFAULT 0,
    cache_hit_count     BIGINT       NOT NULL DEFAULT 0,
    prompt_tokens       BIGINT       NOT NULL DEFAULT 0,
    completion_tokens   BIGINT       NOT NULL DEFAULT 0,
    total_tokens        BIGINT       NOT NULL DEFAULT 0,
    spend_usd           NUMERIC(16, 8) NOT NULL DEFAULT 0,

    latency_p50_ms      INTEGER,
    latency_p95_ms      INTEGER,
    latency_p99_ms      INTEGER,
    latency_max_ms      INTEGER,
    ttft_p50_ms         INTEGER,
    ttft_p95_ms         INTEGER,
    -- Suma + conteo SÍ son recombinables entre buckets: permiten una media
    -- ponderada honesta sin volver a token_usage_event.
    latency_sum_ms      BIGINT       NOT NULL DEFAULT 0,
    latency_count       BIGINT       NOT NULL DEFAULT 0,

    computed_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT usage_hourly_unique UNIQUE (bucket_ts, api_key_key, model_name)
);

CREATE INDEX IF NOT EXISTS idx_usage_hourly_ts   ON sooniverse.usage_hourly (bucket_ts DESC);
CREATE INDEX IF NOT EXISTS idx_usage_hourly_grid ON sooniverse.usage_hourly (bucket_local_isodow, bucket_local_hour);
CREATE INDEX IF NOT EXISTS idx_usage_hourly_key  ON sooniverse.usage_hourly (api_key_key, bucket_ts DESC);

COMMENT ON TABLE sooniverse.usage_hourly IS
    'Agregación horaria cortada en la zona de reporte (app_setting.reporting_timezone). '
    'Los percentiles son POR HORA y no se pueden recombinar entre filas: para un percentil '
    'de una ventana mayor, usar sooniverse.latency_percentiles().';

CREATE OR REPLACE FUNCTION sooniverse.refresh_usage_hourly(
    p_since_days     INTEGER DEFAULT 30,
    p_timezone       TEXT    DEFAULT NULL,
    p_retention_days INTEGER DEFAULT 400
)
RETURNS INTEGER AS $$
DECLARE
    v_tz   TEXT := sooniverse.reporting_timezone(p_timezone);
    v_from TIMESTAMPTZ;
    v_rows INTEGER;
BEGIN
    -- NOTA SOBRE DST: `ts AT TIME ZONE tz` -> truncar -> `AT TIME ZONE tz` es
    -- ambiguo en la hora repetida del cambio de horario. America/Bogota no
    -- tiene DST, así que hoy es inocuo; en una zona que sí lo tenga habrá una
    -- hora al año con dos buckets colapsados en uno. La alternativa (guardar
    -- en UTC y desplazar en el panel) incumpliría el requisito de que el corte
    -- coincida con lo que ve el usuario.
    v_from := (date_trunc('hour', (NOW() AT TIME ZONE v_tz))
               - make_interval(days => p_since_days)) AT TIME ZONE v_tz;

    EXECUTE format($fmt$
        INSERT INTO sooniverse.usage_hourly (
            bucket_ts, tz_name, bucket_local_date, bucket_local_hour, bucket_local_isodow,
            api_key_id, model_name,
            request_count, error_count, cache_hit_count,
            prompt_tokens, completion_tokens, total_tokens, spend_usd,
            latency_p50_ms, latency_p95_ms, latency_p99_ms, latency_max_ms,
            ttft_p50_ms, ttft_p95_ms, latency_sum_ms, latency_count, computed_at
        )
        SELECT
            date_trunc('hour', e.event_ts AT TIME ZONE %1$L) AT TIME ZONE %1$L,
            %1$L,
            (date_trunc('hour', e.event_ts AT TIME ZONE %1$L))::DATE,
            EXTRACT(HOUR   FROM (e.event_ts AT TIME ZONE %1$L))::SMALLINT,
            EXTRACT(ISODOW FROM (e.event_ts AT TIME ZONE %1$L))::SMALLINT,
            e.api_key_id,
            e.model_name,
            COUNT(*),
            COUNT(*) FILTER (WHERE e.status <> 'success'),
            COUNT(*) FILTER (WHERE e.cache_hit IS TRUE),
            SUM(e.prompt_tokens),
            SUM(e.completion_tokens),
            SUM(e.total_tokens),
            SUM(e.spend_usd),
            -- percentile_disc devuelve un valor REALMENTE observado (no
            -- interpola) e ignora los NULL por definición de agregado ordenado.
            percentile_disc(0.50) WITHIN GROUP (ORDER BY e.latency_ms)::INTEGER,
            percentile_disc(0.95) WITHIN GROUP (ORDER BY e.latency_ms)::INTEGER,
            percentile_disc(0.99) WITHIN GROUP (ORDER BY e.latency_ms)::INTEGER,
            MAX(e.latency_ms),
            percentile_disc(0.50) WITHIN GROUP (ORDER BY e.ttft_ms)::INTEGER,
            percentile_disc(0.95) WITHIN GROUP (ORDER BY e.ttft_ms)::INTEGER,
            COALESCE(SUM(e.latency_ms), 0),
            COUNT(e.latency_ms),
            NOW()
        FROM sooniverse.token_usage_event e
        WHERE e.event_ts >= %2$L
        GROUP BY 1, 3, 4, 5, 6, 7
        ON CONFLICT (bucket_ts, api_key_key, model_name) DO UPDATE SET
            tz_name             = EXCLUDED.tz_name,
            bucket_local_date   = EXCLUDED.bucket_local_date,
            bucket_local_hour   = EXCLUDED.bucket_local_hour,
            bucket_local_isodow = EXCLUDED.bucket_local_isodow,
            request_count       = EXCLUDED.request_count,
            error_count         = EXCLUDED.error_count,
            cache_hit_count     = EXCLUDED.cache_hit_count,
            prompt_tokens       = EXCLUDED.prompt_tokens,
            completion_tokens   = EXCLUDED.completion_tokens,
            total_tokens        = EXCLUDED.total_tokens,
            spend_usd           = EXCLUDED.spend_usd,
            latency_p50_ms      = EXCLUDED.latency_p50_ms,
            latency_p95_ms      = EXCLUDED.latency_p95_ms,
            latency_p99_ms      = EXCLUDED.latency_p99_ms,
            latency_max_ms      = EXCLUDED.latency_max_ms,
            ttft_p50_ms         = EXCLUDED.ttft_p50_ms,
            ttft_p95_ms         = EXCLUDED.ttft_p95_ms,
            latency_sum_ms      = EXCLUDED.latency_sum_ms,
            latency_count       = EXCLUDED.latency_count,
            computed_at         = NOW()
    $fmt$, v_tz, v_from);

    GET DIAGNOSTICS v_rows = ROW_COUNT;

    DELETE FROM sooniverse.usage_hourly
     WHERE bucket_ts < NOW() - make_interval(days => p_retention_days);

    RETURN v_rows;
END;
$$ LANGUAGE plpgsql;

-- -----------------------------------------------------------------------------
-- 8. PERCENTILES SOBRE VENTANAS ARBITRARIAS
-- -----------------------------------------------------------------------------
-- El p95 de un día NO es el promedio de los p95 horarios. Cuando hace falta un
-- percentil sobre una ventana cualquiera hay que volver a los eventos crudos:
-- ésta es la única vía soportada para hacerlo.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION sooniverse.latency_percentiles(
    p_from          TIMESTAMPTZ,
    p_to            TIMESTAMPTZ,
    p_api_key_ids   BIGINT[] DEFAULT NULL,
    p_models        TEXT[]   DEFAULT NULL,
    p_incluir_cache BOOLEAN  DEFAULT FALSE
)
RETURNS TABLE (
    muestras    BIGINT,
    p50_ms      INTEGER,
    p95_ms      INTEGER,
    p99_ms      INTEGER,
    ttft_p50_ms INTEGER,
    ttft_p95_ms INTEGER
) AS $$
    SELECT
        COUNT(latency_ms),
        percentile_disc(0.50) WITHIN GROUP (ORDER BY latency_ms)::INTEGER,
        percentile_disc(0.95) WITHIN GROUP (ORDER BY latency_ms)::INTEGER,
        percentile_disc(0.99) WITHIN GROUP (ORDER BY latency_ms)::INTEGER,
        percentile_disc(0.50) WITHIN GROUP (ORDER BY ttft_ms)::INTEGER,
        percentile_disc(0.95) WITHIN GROUP (ORDER BY ttft_ms)::INTEGER
    FROM sooniverse.token_usage_event
    WHERE event_ts >= p_from AND event_ts < p_to
      AND (p_api_key_ids IS NULL OR api_key_id = ANY(p_api_key_ids))
      AND (p_models      IS NULL OR model_name = ANY(p_models))
      AND (p_incluir_cache OR cache_hit IS NOT TRUE);
$$ LANGUAGE sql STABLE;

-- -----------------------------------------------------------------------------
-- 9. VISTAS DE DIAGNÓSTICO
-- -----------------------------------------------------------------------------
-- Rejilla COMPLETA de 7x24. Es imprescindible que incluya las celdas en cero:
-- una hora sin tráfico no tiene fila en usage_hourly, y sin densificar, tanto
-- el mapa de calor como la detección de ventanas ociosas mienten.
-- Sin ventana temporal: son vistas de inspección rápida por psql. El panel
-- agrega con filtros propios sobre usage_hourly (que ya trae isodow/hour
-- precalculados, así que no necesita EXTRACT).
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW sooniverse.v_usage_heatmap AS
SELECT
    g.isodow,
    g.hora,
    COALESCE(SUM(h.request_count), 0) AS request_count,
    COALESCE(SUM(h.total_tokens),  0) AS total_tokens,
    COALESCE(SUM(h.error_count),   0) AS error_count,
    CASE WHEN SUM(h.latency_count) > 0
         THEN (SUM(h.latency_sum_ms)::NUMERIC / SUM(h.latency_count))::NUMERIC(12, 2)
    END                               AS latency_media_ms,
    MAX(h.latency_p95_ms)             AS p95_peor_hora,
    COUNT(h.id)                       AS buckets_con_datos
FROM (
    SELECT d AS isodow, hh AS hora
    FROM generate_series(1, 7) d CROSS JOIN generate_series(0, 23) hh
) g
LEFT JOIN sooniverse.usage_hourly h
       ON h.bucket_local_isodow = g.isodow AND h.bucket_local_hour = g.hora
GROUP BY g.isodow, g.hora;

COMMENT ON VIEW sooniverse.v_usage_heatmap IS
    'Rejilla completa 7x24 (incluye las horas sin tráfico, que no tienen fila en usage_hourly). '
    'p95_peor_hora es el MÁXIMO de los p95 horarios, NO el p95 del conjunto: para eso, '
    'sooniverse.latency_percentiles().';

CREATE OR REPLACE VIEW sooniverse.v_usage_ventanas_ociosas AS
SELECT isodow, hora
FROM sooniverse.v_usage_heatmap
WHERE request_count = 0
ORDER BY isodow, hora;

-- -----------------------------------------------------------------------------
-- 10. CIERRE DE HUECOS EN worker_node Y api_key_registry
-- -----------------------------------------------------------------------------
-- worker_node solo guardaba `accelerator` ("L4"). Sin tipo de instancia ni
-- número de GPUs, un benchmark de capacidad no puede registrar bajo qué
-- hardware midió. Lo puebla scripts/sync_endpoints.py::register_in_db.
ALTER TABLE sooniverse.worker_node
    ADD COLUMN IF NOT EXISTS instance_type          VARCHAR(64),
    ADD COLUMN IF NOT EXISTS gpu_count              SMALLINT,
    ADD COLUMN IF NOT EXISTS max_num_seqs           INTEGER,
    ADD COLUMN IF NOT EXISTS max_num_batched_tokens INTEGER,
    ADD COLUMN IF NOT EXISTS max_model_len          INTEGER;

-- `proposito` como columna con CHECK y no un filtro por alias: un alias es
-- texto libre que el operador puede reutilizar; esto es un contrato. Permite
-- excluir del panel el tráfico sintético del benchmark de capacidad (y, más
-- adelante, el de openwebui-bootstrap, que hoy también contamina las métricas
-- de negocio).
ALTER TABLE sooniverse.api_key_registry
    ADD COLUMN IF NOT EXISTS proposito VARCHAR(24) NOT NULL DEFAULT 'cliente';

ALTER TABLE sooniverse.api_key_registry DROP CONSTRAINT IF EXISTS api_key_registry_proposito_chk;
ALTER TABLE sooniverse.api_key_registry ADD CONSTRAINT api_key_registry_proposito_chk
    CHECK (proposito IN ('cliente', 'benchmark', 'sistema'));

CREATE INDEX IF NOT EXISTS idx_apikey_proposito
    ON sooniverse.api_key_registry (proposito) WHERE proposito <> 'cliente';

COMMENT ON COLUMN sooniverse.api_key_registry.proposito IS
    'cliente = tráfico real de negocio. benchmark = tráfico sintético del test de capacidad '
    '(el panel lo excluye por defecto). sistema = tareas internas del stack.';

-- =============================================================================
-- FIN
-- =============================================================================
