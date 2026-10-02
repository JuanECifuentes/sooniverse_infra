-- ==============================================================================
-- 007. Corrige litellm_token_hash guardado EN CLARO en api_key_registry
-- ==============================================================================
-- Bug: scripts/ensure_openwebui_key.py, metrics/services.py::crear_api_key y
-- scripts/benchmark_capacity.py guardaban el campo 'token' de la respuesta de
-- '/key/generate' de LiteLLM como litellm_token_hash. Ese campo es la key EN
-- CLARO ('sk-...'), no el hash. El ETL (004_usage_analytics.sql) une
-- api_key_registry.litellm_token_hash = LiteLLM_SpendLogs.api_key, y
-- LiteLLM guarda ahí sha256(key) -> nunca había match, api_key_id quedaba
-- NULL y el panel mostraba todo ese consumo como "(sin registro)" (incluido
-- el de la interfaz de chat, que debía verse como "Interfaz Web").
--
-- Idempotente: solo toca filas cuyo hash todavía empieza por 'sk-' (un sha256
-- hex nunca lo hace), así que reaplicarlo en cada despliegue no hace nada.
-- sha256()/encode() son nativas de PostgreSQL 11+ (mismo patrón que 006).
-- ==============================================================================

SET search_path TO sooniverse;

-- 1. Si la versión hasheada YA existe en otra fila (p.ej. ensure_openwebui_key.py
--    corregido re-registró la key antes de este archivo), la fila vieja solo
--    conserva la key en claro: se le quita (UNIQUE admite varios NULL) en vez
--    de dejar un secreto persistido en la BD.
UPDATE sooniverse.api_key_registry r
SET litellm_token_hash = NULL,
    updated_at = NOW()
WHERE r.origen = 'litellm'
  AND r.litellm_token_hash LIKE 'sk-%'
  AND EXISTS (
      SELECT 1 FROM sooniverse.api_key_registry o
      WHERE o.id <> r.id
        AND o.litellm_token_hash = encode(sha256(convert_to(r.litellm_token_hash, 'UTF8')), 'hex')
  );

-- 2. El resto: reemplaza la key en claro por su sha256 (lo que LiteLLM usa).
UPDATE sooniverse.api_key_registry
SET litellm_token_hash = encode(sha256(convert_to(litellm_token_hash, 'UTF8')), 'hex'),
    updated_at = NOW()
WHERE origen = 'litellm'
  AND litellm_token_hash LIKE 'sk-%';

-- 3. Re-atribuye TODO el consumo huérfano ya ingerido (el ETL periódico solo
--    re-une las últimas 48 h). token_usage_event.litellm_token_hash es copia
--    directa de SpendLogs.api_key, así que ahora sí casa con el registro.
--    Los rollups se recalculan solos en el siguiente ciclo de sync_metrics
--    del panel (o con 'python scripts/db_setup.py --refresh').
UPDATE sooniverse.token_usage_event e
SET api_key_id = reg.id
FROM sooniverse.api_key_registry reg
WHERE e.api_key_id IS NULL
  AND e.litellm_token_hash IS NOT NULL
  AND e.litellm_token_hash = reg.litellm_token_hash;
