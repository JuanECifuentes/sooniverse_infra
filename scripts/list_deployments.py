#!/usr/bin/env python3
"""
==============================================================================
Sooniverse Infra - Inventario de despliegues (Fase 6, multi-cliente)
==============================================================================
Lista todos los despliegues registrados en `sooniverse.infra_deployment`
(cualquier cliente, entorno y región), usando la vista
`v_infra_deployment_summary` (database/002_infra_state.sql) para el conteo de
recursos y el coste estimado por hora.

Uso:
    python scripts/list_deployments.py
    python scripts/list_deployments.py --env-file .env.prod
    python scripts/list_deployments.py --json
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from db_setup import DbSetupError, connect, resolve_db_config  # noqa: E402


def fetch_deployments(conn) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT deployment_id, client_id, environment, region, status,
                   recursos_totales, recursos_activos, nat_gateways, elastic_ips,
                   costo_estimado_usd_hora, edad_horas, created_at, destroyed_at
            FROM sooniverse.v_infra_deployment_summary
            ORDER BY client_id, environment, created_at DESC
            """
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _fmt_edad(horas: float) -> str:
    if horas is None:
        return "-"
    if horas < 24:
        return f"{horas:.1f}h"
    return f"{horas / 24:.1f}d"


def print_table(rows: List[Dict[str, Any]]) -> None:
    if not rows:
        print("[OK] No hay despliegues registrados en sooniverse.infra_deployment.")
        return

    header = f"{'CLIENTE':<16} {'ENTORNO':<8} {'REGIÓN':<12} {'ESTADO':<12} {'RECURSOS':<10} {'NAT':<4} {'EIP':<4} {'USD/h':<8} {'EDAD':<8}"
    print(header)
    print("-" * len(header))
    total_cost = 0.0
    for r in rows:
        cost = float(r["costo_estimado_usd_hora"] or 0)
        total_cost += cost if r["status"] not in ("destroyed", "error") else 0
        print(
            f"{r['client_id']:<16} {r['environment']:<8} {r['region']:<12} {r['status']:<12} "
            f"{r['recursos_activos']}/{r['recursos_totales']:<8} {r['nat_gateways']:<4} "
            f"{r['elastic_ips']:<4} {cost:<8.4f} {_fmt_edad(r['edad_horas']):<8}"
        )
    print("-" * len(header))
    print(f"Coste estimado acumulado (despliegues no destruidos): ~${total_cost:.4f}/hora "
          f"(~${total_cost * 24 * 30:.2f}/mes) -- solo NAT+EIP, no incluye cómputo ni tráfico.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Inventario de despliegues Sooniverse (todos los clientes).")
    parser.add_argument("--env-file", default=str(REPO_ROOT / ".env"))
    parser.add_argument("--json", action="store_true", help="Salida en JSON en vez de tabla")
    args = parser.parse_args()

    try:
        conn = connect(resolve_db_config(Path(args.env_file)))
    except DbSetupError as exc:
        print(f"[ERROR] Sin acceso a PostgreSQL: {exc}", file=sys.stderr)
        return 1

    try:
        rows = fetch_deployments(conn)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(rows, indent=2, default=str))
    else:
        print_table(rows)

    return 0


if __name__ == "__main__":
    sys.exit(main())
