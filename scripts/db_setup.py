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


def refresh_metrics(conn, since_hours: int = 48, since_days: int = 90) -> None:
    """Corre el ETL desde LiteLLM y recalcula las agregaciones daily/weekly/monthly."""
    with conn.cursor() as cur:
        cur.execute("SELECT sooniverse.ingest_litellm_spendlogs(%s)", (since_hours,))
        ingested = cur.fetchone()[0]
        cur.execute("SELECT sooniverse.refresh_usage_rollups(%s)", (since_days,))
        rolled = cur.fetchone()[0]
    conn.commit()
    print(f"[OK] ETL: {ingested} eventos nuevos | Rollups recalculados: {rolled} filas")


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
    parser.add_argument("--quiet", action="store_true", help="Reduce la salida a errores")
    args = parser.parse_args()

    env_path = Path(args.env_file)

    try:
        config = resolve_db_config(env_path)
        if not args.quiet:
            print(f"[SOONIVERSE DB] Objetivo: {config['DB_USER']}@{config['DB_HOST']}:"
                  f"{config['DB_PORT']}/{config['DB_NAME']}")

        conn = connect(config)
        try:
            if args.check:
                healthy = verify_schema(conn)
                return 0 if healthy else 2

            if args.sql:
                apply_schema(conn, Path(args.sql))
            else:
                applied = apply_schema_dir(conn, Path(args.sql_dir))
                if not args.quiet:
                    print(f"[OK] {len(applied)} archivo(s) aplicado(s): {', '.join(applied)}")

            verify_schema(conn)

            if args.refresh:
                refresh_metrics(conn)
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
