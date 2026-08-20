#!/usr/bin/env python3
"""
==============================================================================
Sooniverse Infra - PostgreSQL Schema Bootstrapper
==============================================================================
Lee las credenciales de PostgreSQL desde `.env` e ingesta, en orden lexicográfico,
todos los archivos `.sql` de `database/` (001_init_schema.sql, 002_infra_state.sql,
...) en la base de datos existente. Cada archivo es idempotente por diseño
(`CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`, etc.), así que aplicar
el directorio completo en cada despliegue es seguro y no destruye datos.

Uso:
    python scripts/db_setup.py                       # aplica database/*.sql en orden
    python scripts/db_setup.py --check               # solo verifica conexión y estado
    python scripts/db_setup.py --refresh             # aplica + corre ETL y rollups
    python scripts/db_setup.py --sql-dir database    # equivalente al default, explícito
    python scripts/db_setup.py --sql database/001_init_schema.sql   # un solo archivo (legado)

Mantenimiento (operaciones puntuales, no parte del despliegue):
    python scripts/db_setup.py --recompute-rollups 3650
        Recalcula TODOS los rollups con la zona horaria de reporte actual. Necesario
        una única vez tras instalar 004 (los buckets antiguos se cortaron en UTC) y
        cada vez que cambie TIME_ZONE en el .env.
    python scripts/db_setup.py --backfill 3650
        Reingesta el histórico de litellm."LiteLLM_SpendLogs" por lotes para rellenar
        latency_ms / ttft_ms / status / worker_endpoint en filas ya ingeridas.
"""

import argparse
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV = REPO_ROOT / ".env"
DEFAULT_SQL_DIR = REPO_ROOT / "database"

REQUIRED_KEYS = ("DB_NAME", "DB_USER", "DB_PASSWORD", "DB_HOST", "DB_PORT")

# Zona horaria con la que se cortan los buckets de agregación. NO va en
# REQUIRED_KEYS: es opcional y tiene un default sensato. Debe coincidir con
# django_metrics/sooniverse_panel/settings.py::TIME_ZONE, o el panel mostraría
# los días desplazados respecto a como se agregaron.
DEFAULT_TIMEZONE = "America/Bogota"

# Objetos que deben existir tras una ingesta correcta del esquema.
EXPECTED_TABLES = (
    "api_key_registry",
    "token_usage_event",
    "token_usage_rollup",
    "api_key_audit",
    "worker_node",
    "infra_deployment",
    "infra_resource",
    "infra_event",
    "model_capability",
    "app_setting",
    "usage_hourly",
    "capacity_benchmark",
)


class DbSetupError(Exception):
    """Error recuperable durante la inicialización de la base de datos."""


def parse_env_file(env_path: Path) -> Dict[str, str]:
    """Parser minimalista de .env (sin dependencias externas)."""
    if not env_path.exists():
        raise DbSetupError(f"No se encontró el archivo de entorno: {env_path}")

    values: Dict[str, str] = {}
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        values[key.strip()] = value
    return values


def resolve_db_config(env_path: Path) -> Dict[str, str]:
    """
    Construye la configuración de conexión.

    El archivo `.env` es la fuente autoritativa: es el contrato que el operador
    edita y el que se monta en el Nodo Gateway. Las variables del entorno del
    proceso solo cubren las claves que el archivo no define (o si no existe),
    de modo que un shell con valores obsoletos no secuestre el despliegue.
    """
    from_file = parse_env_file(env_path) if env_path.exists() else {}

    config = {}
    missing = []
    for key in REQUIRED_KEYS:
        value = from_file.get(key) or os.environ.get(key)
        if not value:
            missing.append(key)
        config[key] = value

    if missing:
        raise DbSetupError(
            f"Faltan variables de conexión ({', '.join(missing)}) tanto en {env_path} como en el entorno"
        )

    return config


def resolve_timezone(env_path: Path) -> str:
    """Zona horaria de reporte, con la misma precedencia que `resolve_db_config`
    (el archivo manda sobre el entorno del proceso)."""
    from_file = parse_env_file(env_path) if env_path.exists() else {}
    return from_file.get("TIME_ZONE") or os.environ.get("TIME_ZONE") or DEFAULT_TIMEZONE


def sync_reporting_timezone(conn, timezone: str) -> None:
    """Persiste la zona en `sooniverse.app_setting` para que el corte de buckets
    sea el mismo lo dispare quien lo dispare (Django, este script o un cron con
    psql). Ver el bloque 1 de database/004_usage_analytics.sql."""
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM sooniverse.app_setting WHERE key = 'reporting_timezone'")
        row = cur.fetchone()
        anterior = row[0] if row else None

        cur.execute(
            "INSERT INTO sooniverse.app_setting (key, value) VALUES ('reporting_timezone', %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()",
            (timezone,),
        )
    conn.commit()

    if anterior and anterior != timezone:
        print(f"[WARNING] La zona de reporte cambió ({anterior} -> {timezone}). Los buckets "
              f"históricos siguen cortados con la anterior; realinéalos con:\n"
              f"          python scripts/db_setup.py --recompute-rollups 3650")


def connect(config: Dict[str, str]):
    try:
        import psycopg2
    except ImportError:
        raise DbSetupError("La librería 'psycopg2-binary' no está instalada. Ejecuta: pip install psycopg2-binary")

    try:
        conn = psycopg2.connect(
            dbname=config["DB_NAME"],
            user=config["DB_USER"],
            password=config["DB_PASSWORD"],
            host=config["DB_HOST"],
            port=config["DB_PORT"],
            connect_timeout=15,
        )
    except Exception as exc:
        raise DbSetupError(f"No se pudo conectar a PostgreSQL en {config['DB_HOST']}:{config['DB_PORT']} -> {exc}")

    conn.autocommit = False
    return conn


def apply_schema(conn, sql_path: Path) -> None:
    """Ejecuta el archivo SQL completo en una única transacción."""
    if not sql_path.exists():
        raise DbSetupError(f"No se encontró el archivo SQL: {sql_path}")

    sql = sql_path.read_text(encoding="utf-8")

    with conn.cursor() as cur:
        try:
            cur.execute(sql)
        except Exception as exc:
            conn.rollback()
            raise DbSetupError(f"Falló la ingesta del esquema: {exc}")

        for notice in getattr(conn, "notices", [])[-10:]:
            print(f"   [PG] {notice.strip()}")

    conn.commit()
    print(f"[OK] Esquema aplicado desde {sql_path.name}")


def apply_schema_dir(conn, sql_dir: Path) -> List[str]:
    """Aplica, en orden lexicográfico, todos los `.sql` de `sql_dir` (cada uno en
    su propia transacción). Devuelve la lista de archivos aplicados con éxito.

    Los archivos son idempotentes (ver docstring del módulo), así que reaplicar
    el directorio completo en cada despliegue -incluyendo archivos ya aplicados
    en corridas previas- es intencional y seguro.
    """
    if not sql_dir.is_dir():
        raise DbSetupError(f"No se encontró el directorio de esquema: {sql_dir}")

    sql_files = sorted(sql_dir.glob("*.sql"))
    if not sql_files:
        raise DbSetupError(f"El directorio {sql_dir} no contiene archivos .sql")

    applied = []
    for sql_path in sql_files:
        apply_schema(conn, sql_path)
        applied.append(sql_path.name)
    return applied


def verify_schema(conn) -> bool:
    """Confirma que el esquema `sooniverse` y sus tablas base existen."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name = 'sooniverse'")
        if cur.fetchone() is None:
            print("[FAIL] El esquema 'sooniverse' no existe.")
            return False

        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'sooniverse'"
        )
        found = {row[0] for row in cur.fetchall()}

    ok = True
    for table in EXPECTED_TABLES:
        marker = "OK  " if table in found else "MISS"
        if table not in found:
            ok = False
        print(f"   [{marker}] sooniverse.{table}")

    with conn.cursor() as cur:
        # LiteLLM vive en su PROPIO esquema 'litellm', no en 'sooniverse' (ver
        # el comentario "CONVIVENCIA CON LITELLM" en database/001_init_schema.sql
        # para el porqué: su motor de migraciones Prisma no convive bien
        # compartiendo esquema con tablas/vistas ajenas).
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'litellm' AND table_name = 'LiteLLM_SpendLogs'"
        )
        litellm_ready = cur.fetchone() is not None
    print(f"   [{'OK  ' if litellm_ready else 'WAIT'}] litellm.LiteLLM_SpendLogs "
          f"({'detectada' if litellm_ready else 'pendiente del primer arranque de LiteLLM'})")

    return ok


def refresh_metrics(conn, since_hours: int = 48, since_days: int = 90,
                    timezone: Optional[str] = None) -> None:
    """Corre el ETL desde LiteLLM y recalcula agregaciones diarias/semanales/
    mensuales y horarias. La zona se pasa explícita para no depender del
    `TimeZone` de la sesión (ver database/004_usage_analytics.sql)."""
    # La ventana horaria se acota a 30 días: es la que alimenta el mapa de calor
    # y recalcular percentiles sobre eventos crudos de 90 días en cada refresco
    # periódico no compensa.
    hourly_days = min(since_days, 30)

    with conn.cursor() as cur:
        cur.execute("SELECT sooniverse.ingest_litellm_spendlogs(%s)", (since_hours,))
        ingested = cur.fetchone()[0]
        cur.execute("SELECT sooniverse.refresh_usage_rollups(%s, %s)", (since_days, timezone))
        rolled = cur.fetchone()[0]
        cur.execute("SELECT sooniverse.refresh_usage_hourly(%s, %s)", (hourly_days, timezone))
        hourly = cur.fetchone()[0]
    conn.commit()
    print(f"[OK] ETL: {ingested} eventos nuevos | Rollups: {rolled} filas | "
          f"Horario: {hourly} buckets")


def recompute_rollups(conn, since_days: int, timezone: Optional[str] = None) -> None:
    """Recalcula TODO el histórico con la zona actual. Operación puntual: los
    buckets creados antes de 004 se cortaron en UTC mientras el panel renderiza
    en hora local, así que hasta esta pasada los días de la frontera están
    desplazados."""
    with conn.cursor() as cur:
        cur.execute("SELECT sooniverse.refresh_usage_rollups(%s, %s)", (since_days, timezone))
        rolled = cur.fetchone()[0]
        cur.execute("SELECT sooniverse.refresh_usage_hourly(%s, %s)", (since_days, timezone))
        hourly = cur.fetchone()[0]
    conn.commit()
    print(f"[OK] Recalculado con zona '{timezone or 'la de app_setting'}': "
          f"{rolled} filas de rollup | {hourly} buckets horarios")


def backfill_events(conn, since_days: int) -> None:
    """Reingesta el histórico de SpendLogs por lotes para rellenar los campos que
    el ETL antiguo nunca escribió (latencia, TTFT, estado, worker)."""
    with conn.cursor() as cur:
        cur.execute("SELECT sooniverse.backfill_litellm_spendlogs(%s)", (since_days,))
        total = cur.fetchone()[0]
        for notice in getattr(conn, "notices", [])[-20:]:
            print(f"   [PG] {notice.strip()}")
    conn.commit()
    print(f"[OK] Backfill: {total} fila(s) insertada(s) o enriquecida(s).")
    print("[INFO] El backfill reescribe filas antiguas. Recomendado a continuación:")
    print("       VACUUM (ANALYZE) sooniverse.token_usage_event;")


def main() -> int:
    parser = argparse.ArgumentParser(description="Inicializador del esquema PostgreSQL de Sooniverse.")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV), help="Ruta al archivo .env")
    parser.add_argument(
        "--sql", default=None,
        help="Ruta a UN solo archivo SQL a ingestar (comportamiento legado; ignora --sql-dir).",
    )
    parser.add_argument(
        "--sql-dir", default=str(DEFAULT_SQL_DIR),
        help="Directorio con .sql a aplicar en orden lexicográfico (por defecto: database/).",
    )
    parser.add_argument("--check", action="store_true", help="Solo verificar conexión y estado del esquema")
    parser.add_argument("--refresh", action="store_true", help="Tras aplicar, corre ETL de LiteLLM y rollups")
    parser.add_argument(
        "--recompute-rollups", nargs="?", type=int, const=3650, default=None, metavar="DIAS",
        help="Recalcula los rollups y la agregación horaria de los últimos DIAS días (def. 3650) "
             "con la zona de reporte actual. Operación puntual, no parte del despliegue.",
    )
    parser.add_argument(
        "--backfill", nargs="?", type=int, const=3650, default=None, metavar="DIAS",
        help="Reingesta por lotes el histórico de SpendLogs para rellenar latencia/estado/worker "
             "en filas ya ingeridas.",
    )
    parser.add_argument("--quiet", action="store_true", help="Reduce la salida a errores")
    args = parser.parse_args()

    env_path = Path(args.env_file)

    try:
        config = resolve_db_config(env_path)
        timezone = resolve_timezone(env_path)
        if not args.quiet:
            print(f"[SOONIVERSE DB] Objetivo: {config['DB_USER']}@{config['DB_HOST']}:"
                  f"{config['DB_PORT']}/{config['DB_NAME']} | zona de reporte: {timezone}")

        conn = connect(config)
        try:
            if args.check:
                healthy = verify_schema(conn)
                return 0 if healthy else 2

            # Mantenimiento: opera sobre un esquema ya aplicado, no lo reaplica.
            if args.recompute_rollups is not None or args.backfill is not None:
                if args.backfill is not None:
                    backfill_events(conn, args.backfill)
                if args.recompute_rollups is not None:
                    recompute_rollups(conn, args.recompute_rollups, timezone)
                return 0

            if args.sql:
                apply_schema(conn, Path(args.sql))
            else:
                applied = apply_schema_dir(conn, Path(args.sql_dir))
                if not args.quiet:
                    print(f"[OK] {len(applied)} archivo(s) aplicado(s): {', '.join(applied)}")

            sync_reporting_timezone(conn, timezone)
            verify_schema(conn)

            if args.refresh:
                refresh_metrics(conn, timezone=timezone)
        finally:
            conn.close()

    except DbSetupError as exc:
        print(f"\n[ERROR DB] {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - frontera del CLI
        print(f"\n[ERROR INESPERADO] {exc}", file=sys.stderr)
        return 1

    if not args.quiet:
        print("[SUCCESS] Base de datos lista para la Fase 1 (Gateway + Métricas).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
