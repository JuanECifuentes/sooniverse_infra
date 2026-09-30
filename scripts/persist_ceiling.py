#!/usr/bin/env python3
"""
Persiste en sooniverse.capacity_benchmark el resultado de scripts/benchmark_ceiling.py
(el JSON que este ultimo deja en --out), para que el barrido de techo quede en la
misma tabla que la rampa acotada de benchmark_capacity.py y el panel lo pueda leer.

  python scripts/persist_ceiling.py --config clients/bench-l4/config_global.yaml \
      --json ceiling_l4_direct.json --deployment-id <uuid> [--extra-json otro.json ...]

- La "rodilla" se define como el nivel de concurrencia de MAYOR tokens_salida/s del
  perfil 'decode' (en el barrido de techo no hay condicion de parada por latencia).
- El JSON completo (todos los perfiles, prediccion 8k/1k) va en `notas`.
- Idempotente: run_id = uuid5 de (label, inicio_epoch).
- Nunca guarda prompts ni respuestas (invariante de privacidad del proyecto): solo contadores.
"""
import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def read_env(path: Path) -> dict:
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k] = v.strip().strip('"').strip("'")
    return env


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--json", required=True, help="JSON principal de benchmark_ceiling.py")
    ap.add_argument("--extra-json", nargs="*", default=[], help="JSON adicionales (p.ej. via gateway) que se anexan a notas")
    ap.add_argument("--deployment-id", default=None)
    ap.add_argument("--origen", default="gateway", choices=["gateway", "operador"])
    args = ap.parse_args()

    import psycopg2
    from psycopg2.extras import Json

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    res = json.loads(Path(args.json).read_text(encoding="utf-8"))
    extras = {Path(p).stem: json.loads(Path(p).read_text(encoding="utf-8")) for p in args.extra_json}
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from benchmark_ceiling import prediccion  # recalcula con la mediana del prefill
    res["prediccion_8k_1k"] = prediccion(res) or res.get("prediccion_8k_1k")
    wl = cfg["workloads"][0]
    frac, conc = wl.get("asignacion_fraccional", {}), wl.get("concurrencia", {})
    decode = res.get("decode") or []
    best = max(decode, key=lambda f: f["out_tok_s"]) if decode else {}
    base = decode[0] if decode else {}
    tok_out_min = best.get("out_tok_s", 0) * 60
    tok_in_min = best.get("in_tok_s", 0) * 60
    run_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{res['label']}-{res['inicio_epoch']}")
    notas = {"tipo": "benchmark_ceiling", "ceiling": res, "extras": extras,
             "prediccion_8k_1k": res.get("prediccion_8k_1k")}

    env = read_env(REPO_ROOT / ".env")
    conn = psycopg2.connect(host=env["DB_HOST"], dbname=env["DB_NAME"], user=env["DB_USER"],
                            password=env["DB_PASSWORD"], port=env.get("DB_PORT", "5432"), connect_timeout=15)
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sooniverse.capacity_benchmark (
              run_id, client_id, environment, deployment_id, workload_id, model_public_name,
              instance_type, accelerator, gpu_count, replicas, max_num_seqs, max_num_batched_tokens,
              max_model_len, gpu_memory_utilization, enforce_eager, quantization, lb_strategy,
              niveles_concurrencia, prompt_tokens_objetivo, max_tokens, segundos_por_nivel,
              warmup_segundos, streaming, origen, concurrencia_rodilla, rpm_sostenido,
              tokens_salida_por_min, tokens_totales_por_min, p50_base_ms, p95_base_ms,
              ttft_p50_base_ms, ttft_p95_base_ms, itl_medio_rodilla_ms, tasa_error_pct,
              motivo_parada, usuarios_estimados, curva, notas, duracion_total_seg, started_at, finished_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (run_id) DO NOTHING
            """,
            (str(run_id), cfg["cliente"]["id"], cfg["cliente"]["entorno"], args.deployment_id,
             wl["id"], wl.get("nombre_publico", wl["id"]),
             wl.get("tipo_instancia"), wl["accelerator"], wl.get("cantidad_gpus", 1), wl.get("replicas", 1),
             conc.get("max_num_seqs"), conc.get("max_num_batched_tokens"), frac.get("max_model_len"),
             frac.get("gpu_memory_utilization"), False, "awq", None,
             [f["concurrencia"] for f in decode] or [1], 128, 512, base.get("ventana_seg", 45),
             15, True, args.origen, best.get("concurrencia"), round(best.get("cliente_rps", 0) * 60, 2),
             round(tok_out_min, 2), round(tok_out_min + tok_in_min, 2), base.get("lat_p50_ms"),
             base.get("lat_p95_ms"), base.get("ttft_p50_ms"), base.get("ttft_p95_ms"),
             best.get("itl_medio_ms"), 0, "saturacion_throughput", None, Json(decode), Json(notas),
             round(res["fin_epoch"] - res["inicio_epoch"], 2) if res.get("fin_epoch") else None,
             datetime.fromtimestamp(res["inicio_epoch"], timezone.utc),
             datetime.fromtimestamp(res.get("fin_epoch", res["inicio_epoch"]), timezone.utc)))
        print(f"[OK] run_id={run_id} persistido en sooniverse.capacity_benchmark "
              f"(techo decode={best.get('out_tok_s')} tok/s a concurrencia {best.get('concurrencia')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
