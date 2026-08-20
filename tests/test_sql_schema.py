"""
Pruebas puramente TEXTUALES sobre database/*.sql. No requieren PostgreSQL.

Cubren invariantes que solo se descubren cuando ya han roto un despliegue:
idempotencia, la sobrecarga ambigua de funciones, y que ningún corte temporal
se haga sin zona horaria explícita.
"""

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from db_setup import EXPECTED_TABLES  # noqa: E402

SQL_DIR = REPO_ROOT / "database"
ARCHIVOS = sorted(SQL_DIR.glob("*.sql"))
NUEVOS = [p for p in ARCHIVOS if p.name.startswith(("004_", "005_"))]


def leer(path: Path) -> str:
    return path.read_text(encoding="utf-8")


TODO_EL_SQL = "\n".join(leer(p) for p in ARCHIVOS)


def test_hay_archivos_sql():
    assert ARCHIVOS, "database/ no contiene ningún .sql"
    assert NUEVOS, "faltan 004_/005_"


def test_toda_tabla_creada_esta_declarada_en_expected_tables():
    """Regresión que ya hacía falta antes: una tabla nueva que no esté en
    EXPECTED_TABLES no la verifica `db_setup.py --check`, así que un fallo de
    ingesta pasaría desapercibido."""
    creadas = set(re.findall(
        r"CREATE TABLE IF NOT EXISTS\s+sooniverse\.(\w+)", TODO_EL_SQL, re.IGNORECASE
    ))
    faltan = creadas - set(EXPECTED_TABLES)
    assert not faltan, f"tablas creadas pero ausentes de db_setup.EXPECTED_TABLES: {sorted(faltan)}"


def test_expected_tables_no_referencia_tablas_inexistentes():
    creadas = set(re.findall(
        r"CREATE TABLE IF NOT EXISTS\s+sooniverse\.(\w+)", TODO_EL_SQL, re.IGNORECASE
    ))
    sobran = set(EXPECTED_TABLES) - creadas
    assert not sobran, f"EXPECTED_TABLES nombra tablas que ningún .sql crea: {sorted(sobran)}"


@pytest.mark.parametrize("path", NUEVOS, ids=lambda p: p.name)
def test_alter_table_siempre_add_column_if_not_exists(path):
    """Todo el directorio se reaplica en cada despliegue: un ADD COLUMN sin
    IF NOT EXISTS rompería la segunda corrida."""
    sql = leer(path)
    for m in re.finditer(r"ADD COLUMN(?!\s+IF NOT EXISTS)", sql, re.IGNORECASE):
        contexto = sql[max(0, m.start() - 120):m.start() + 60]
        pytest.fail(f"ADD COLUMN sin IF NOT EXISTS en {path.name}:\n...{contexto}...")


def test_drop_function_antes_de_redefinir_refresh_usage_rollups():
    """Añadir un parámetro con DEFAULT no reemplaza la función: crea una
    SOBRECARGA, y la llamada existente `refresh_usage_rollups(90)` pasaría a
    fallar con 'function ... is not unique'. El DROP tiene que ir ANTES."""
    sql = leer(SQL_DIR / "004_usage_analytics.sql")
    drop = sql.find("DROP FUNCTION IF EXISTS sooniverse.refresh_usage_rollups(INTEGER)")
    crea = sql.find("CREATE OR REPLACE FUNCTION sooniverse.refresh_usage_rollups(")
    assert drop != -1, "falta el DROP FUNCTION de la firma antigua"
    assert crea != -1, "falta el CREATE OR REPLACE de refresh_usage_rollups"
    assert drop < crea, "el DROP FUNCTION debe preceder al CREATE OR REPLACE"


def test_ingest_litellm_spendlogs_conserva_su_firma_escalar():
    """db_setup.refresh_metrics y metrics/services.refrescar_metricas hacen
    cur.fetchone()[0]: si dejara de devolver un escalar, ambos romperían."""
    sql = leer(SQL_DIR / "004_usage_analytics.sql")
    assert re.search(
        r"CREATE OR REPLACE FUNCTION sooniverse\.ingest_litellm_spendlogs\(\s*"
        r"p_since_hours INTEGER DEFAULT 48\s*\)\s*RETURNS INTEGER",
        sql,
    ), "ingest_litellm_spendlogs debe seguir siendo (INTEGER) RETURNS INTEGER"


@pytest.mark.parametrize("path", NUEVOS, ids=lambda p: p.name)
def test_ningun_date_trunc_sin_zona_horaria(path):
    """El bug original: DATE_TRUNC usa la zona de la SESIÓN, así que el mismo día
    se cortaba distinto según lo disparara Django (UTC) o psql. Todo corte
    temporal nuevo tiene que llevar AT TIME ZONE cerca."""
    sql = leer(path)
    for m in re.finditer(r"date_trunc\s*\(", sql, re.IGNORECASE):
        ventana = sql[m.start():m.start() + 220]
        assert "AT TIME ZONE" in ventana.upper(), (
            f"date_trunc sin AT TIME ZONE en {path.name}:\n{ventana[:160]}"
        )


def test_on_conflict_de_rollups_usa_la_columna_centinela():
    """El UNIQUE con api_key_id NULLable no deduplicaba NULLs, así que el grupo
    'sin registro' insertaba una fila nueva en CADA refresco."""
    sql = leer(SQL_DIR / "004_usage_analytics.sql")
    assert "ux_rollup_bucket" in sql
    assert "ON CONFLICT (granularity, bucket_start, api_key_key, model_name)" in sql
    assert "api_key_key BIGINT" in sql and "COALESCE(api_key_id, 0)" in sql
    # La purga de duplicados previos debe ir ANTES de crear el índice único.
    purga = sql.find("DELETE FROM sooniverse.token_usage_rollup t")
    indice = sql.find("CREATE UNIQUE INDEX IF NOT EXISTS ux_rollup_bucket")
    assert purga != -1 and indice != -1 and purga < indice


def test_targets_de_on_conflict_tienen_un_indice_unico_declarado():
    """Un ON CONFLICT cuya lista de columnas no coincide con ningún índice único
    revienta en ejecución, no al crear la función."""
    declarados = set()
    for m in re.finditer(r"(?:CREATE UNIQUE INDEX[^(]*|CONSTRAINT \w+ UNIQUE\s*)\(([^)]+)\)",
                         TODO_EL_SQL, re.IGNORECASE):
        declarados.add(frozenset(c.strip() for c in m.group(1).split(",")))
    # UNIQUE o PRIMARY KEY en la propia definición de columna: ambas respaldan un
    # ON CONFLICT (p.ej. litellm_request_id UNIQUE, app_setting.key PRIMARY KEY).
    for m in re.finditer(r"^\s*(\w+)\s+[\w()\s,]*?\b(?:UNIQUE|PRIMARY KEY)\b",
                         TODO_EL_SQL, re.MULTILINE):
        declarados.add(frozenset({m.group(1)}))

    for m in re.finditer(r"ON CONFLICT\s*\(([^)]+)\)", TODO_EL_SQL, re.IGNORECASE):
        objetivo = frozenset(c.strip() for c in m.group(1).split(","))
        assert objetivo in declarados, (
            f"ON CONFLICT {sorted(objetivo)} sin índice único que lo respalde"
        )


def test_usage_hourly_documenta_que_los_percentiles_no_se_recombinan():
    """Es el error conceptual más fácil de cometer con esta tabla: promediar los
    p95 horarios NO da el p95 del día."""
    sql = leer(SQL_DIR / "004_usage_analytics.sql")
    assert "latency_percentiles" in sql
    assert re.search(r"no\s+se\s+pueden\s+recombinar|NO son recombinables", sql, re.IGNORECASE)


def test_heatmap_densifica_la_rejilla():
    """Una hora sin tráfico no tiene fila en usage_hourly. Sin generate_series,
    el mapa de calor y la detección de ocio mentirían."""
    sql = leer(SQL_DIR / "004_usage_analytics.sql")
    assert "generate_series(1, 7)" in sql and "generate_series(0, 23)" in sql
    assert "LEFT JOIN sooniverse.usage_hourly" in sql


def test_capacity_benchmark_guarda_hash_y_no_la_key():
    sql = leer(SQL_DIR / "005_capacity_benchmark.sql")
    assert "benchmark_key_hash" in sql
    assert not re.search(r"benchmark_key_plain|key_en_claro", sql, re.IGNORECASE)


def test_ningun_archivo_crea_objetos_en_el_esquema_litellm():
    """Prisma hace diff agresivo de todo lo que encuentra en su esquema y llegó a
    intentar DROP TABLE sobre una tabla nuestra (ver cabecera de 001)."""
    for path in ARCHIVOS:
        sql = leer(path)
        prohibido = re.findall(
            r"CREATE (?:TABLE|VIEW|INDEX|FUNCTION)[^;]*?\blitellm\.", sql, re.IGNORECASE
        )
        assert not prohibido, f"{path.name} crea objetos dentro del esquema 'litellm'"
