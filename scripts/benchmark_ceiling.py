#!/usr/bin/env python3
"""
==============================================================================
Sooniverse Infra - Benchmark de TECHO de throughput (barrido completo)
==============================================================================
Complementa a scripts/benchmark_capacity.py (rampa corta y acotada, pensada
para correr en cada despliegue). Este script NO se detiene en la "rodilla": barre
la concurrencia hasta que el throughput se aplana, para responder "¿cuál es el
máximo de tokens/s que da esta GPU con este modelo?" y luego VERIFICA ese techo
con un perfil de peticiones normales (8k tokens de entrada / 1k de salida).

Solo usa la libreria estandar (corre tal cual en el Gateway, sin pip).

PERFILES
  decode     ~128 tokens de entrada, 512 de salida. Aisla el DECODE: el techo de
             tokens de salida por segundo (D).
  prefill    8192 de entrada, 1 de salida. Aisla el PREFILL: el techo de tokens
             de entrada por segundo (P).
  realistic  8192 de entrada, 1024 de salida (peticion "normal" de RAG/agentes).
  vision     1 imagen 640x480 + ~30 tokens de entrada, 128 de salida.

VERIFICACION DEL TECHO (modelo de tiempo de GPU)
  Una peticion 8k/1k ocupa la GPU  T = 8192/P + 1024/D  segundos (prefill a
  velocidad P y decode a velocidad agregada D, ambos medidos arriba). Por tanto
  la prediccion es  req/s = 1/T  y  tokens_salida/s = 1024/T. El perfil
  'realistic' se mide de verdad y se compara: ratio = medido/predicho. Un ratio
  ~0.8-1.0 valida el techo; mucho menor delata otro limite (KV cache, colas,
  interferencia prefill/decode) que el techo sintetico no ve.

MEDICION
  Bucle cerrado (N hilos, sin pausa). Cada nivel descarta `warm` segundos y mide
  la ventana restante. Si se da --metrics-url (endpoint /metrics de vLLM, solo
  accesible yendo directo al worker) el throughput se toma de los contadores del
  propio motor (vllm:generation_tokens_total / prompt_tokens_total): es el numero
  mas fiel. Siempre se reporta ademas el calculado en el cliente.
  Prompts con nonce unico al inicio: sin el, el prefix cache de vLLM inflaria
  el prefill varias veces.
==============================================================================
"""
import argparse
import base64
import json
import random
import re
import statistics
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
import zlib

WORDS = (
    "sistema modelo cliente factura pedido servidor reporte usuario dato analisis "
    "contrato entrega proyecto region cuenta balance auditoria proceso flujo tarea "
    "mercado precio costo ventas inventario proveedor calidad seguridad red nube "
    "acceso permiso consulta respuesta politica norma registro evento alerta metrica "
    "capacidad demanda oferta plazo pago cobro deuda credito ahorro inversion riesgo "
    "energia planta equipo turno ruta destino origen carga peso volumen tiempo fecha"
).split()

# perfil -> (tokens_entrada, max_tokens, niveles por defecto, segundos por nivel, warm)
PERFILES = {
    "decode": (128, 512, [1, 4, 8, 16, 32, 48, 64], 45, 15),
    "prefill": (8192, 1, [1, 2, 4, 8], 30, 10),
    "realistic": (8192, 1024, [1, 2, 4, 8, 16, 24, 32], 150, 45),
    "vision": (0, 128, [1, 4, 8, 16], 40, 12),
}


# --------------------------------------------------------------------------- util
def pct(vals, q):
    if not vals:
        return None
    s = sorted(vals)
    return round(s[max(0, min(len(s) - 1, int(-(-q * len(s) // 1)) - 1))], 1)


def http_json(url, payload=None, headers=None, timeout=60):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json", **(headers or {})},
        method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def scrape(metrics_url, names):
    """Suma de contadores/gauges de vLLM (todas las etiquetas). None si falla."""
    try:
        with urllib.request.urlopen(metrics_url, timeout=10) as r:
            txt = r.read().decode()
    except Exception:
        return None
    out = {}
    for n in names:
        tot = 0.0
        for m in re.finditer(r"^" + re.escape(n) + r"(?:\{[^}]*\})?\s+([0-9.eE+-]+)\s*$", txt, re.M):
            tot += float(m.group(1))
        out[n] = tot
    return out


# --------------------------------------------------------------------------- prompts
class PromptFactory:
    def __init__(self, base, headers, model):
        self.tok_per_word = 1.35  # se calibra en calibrate()
        self.base, self.headers, self.model = base, headers, model

    def text(self, target_tokens, nonce):
        n = max(1, int(target_tokens / self.tok_per_word))
        rnd = random.Random(nonce)
        return f"[{nonce}] " + " ".join(rnd.choice(WORDS) for _ in range(n))

    def calibrate(self):
        muestra = self.text(2000, "cal")
        r = http_json(f"{self.base}/chat/completions", {
            "model": self.model, "messages": [{"role": "user", "content": muestra}],
            "max_tokens": 1, "temperature": 0}, self.headers, timeout=300)
        pt = r["usage"]["prompt_tokens"]
        self.tok_per_word = max(0.5, (pt - 20) / (len(muestra.split()) or 1))
        print(f"[cal] {pt} tokens de prompt para {len(muestra.split())} palabras -> "
              f"{self.tok_per_word:.2f} tok/palabra", flush=True)


def png_bytes(w, h, seed):
    """PNG RGB sin dependencias: fondo con ruido de bloques (unico por seed) y un
    cuadrado rojo central (permite comprobar que el modelo ve la imagen)."""
    rnd = random.Random(seed)
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        fila = bytearray()
        for bx in range(w // 16):
            c = bytes((rnd.randrange(150, 256), rnd.randrange(150, 256), rnd.randrange(150, 256)))
            fila += c * 16
        raw += fila
    for y in range(h // 4, 3 * h // 4):  # cuadrado rojo
        off = y * (1 + w * 3) + 1
        for x in range(w // 4, 3 * w // 4):
            raw[off + x * 3: off + x * 3 + 3] = b"\xff\x00\x00"

    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 1)) + chunk(b"IEND", b""))


# --------------------------------------------------------------------------- una peticion
def una_peticion(base, headers, model, perfil, pf, nonce, extra):
    ent, max_tok = PERFILES[perfil][0], PERFILES[perfil][1]
    if perfil == "vision":
        b64 = base64.b64encode(png_bytes(640, 480, nonce)).decode()
        content = [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                   {"type": "text", "text": "Describe la imagen en detalle."}]
    else:
        suf = "\n\nEscribe un texto largo y detallado sobre lo anterior." if max_tok > 1 else "\n\nResponde OK."
        content = pf.text(ent, nonce) + suf
    payload = {"model": model, "messages": [{"role": "user", "content": content}],
               "max_tokens": max_tok, "temperature": 0.7, "stream": True,
               "stream_options": {"include_usage": True}, **extra}
    req = urllib.request.Request(f"{base}/chat/completions", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", **headers}, method="POST")
    t0 = time.perf_counter()
    ttft, chunks, usage = None, 0, None
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            for raw in r:
                if not raw.startswith(b"data:"):
                    continue
                if b'"completion_tokens"' in raw:
                    try:
                        usage = json.loads(raw[5:]).get("usage")
                    except ValueError:
                        pass
                    continue
                if raw.strip() == b"data: [DONE]":
                    break
                if b'"content"' in raw or b'"reasoning' in raw:
                    chunks += 1
                    if ttft is None:
                        ttft = (time.perf_counter() - t0) * 1000
        tot = (time.perf_counter() - t0) * 1000
        ct = (usage or {}).get("completion_tokens", chunks)
        pt = (usage or {}).get("prompt_tokens", 0)
        return {"ok": True, "ttft": ttft, "total": tot, "out": ct, "inp": pt, "t_end": time.monotonic()}
    except urllib.error.HTTPError as e:
        return {"ok": False, "err": f"HTTP {e.code}: {e.read()[:150]!r}", "t_end": time.monotonic()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "err": str(e)[:150], "t_end": time.monotonic()}


# --------------------------------------------------------------------------- un nivel
def correr_nivel(args, headers, pf, perfil, conc, secs, warm, extra):
    t0 = time.monotonic()
    t_meas0, t_end = t0 + warm, t0 + warm + secs
    res, lock = [], threading.Lock()
    mnames = ["vllm:generation_tokens_total", "vllm:prompt_tokens_total"]
    snap = {}

    def worker(i):
        j = 0
        while time.monotonic() < t_end:
            r = una_peticion(args.base_url, headers, args.model, perfil, pf,
                             f"{perfil[:2]}{conc}-{i}-{j}-{random.randint(0, 10**9)}", extra)
            r["t_start_ok"] = True
            with lock:
                res.append(r)
            j += 1
            if not r["ok"]:
                time.sleep(0.5)

    ths = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(conc)]
    for t in ths:
        t.start()
    gauges = []
    if args.metrics_url:
        while time.monotonic() < t_meas0:
            time.sleep(0.25)
        snap["a"] = scrape(args.metrics_url, mnames)
        snap["ta"] = time.monotonic()
        while time.monotonic() < t_end:
            time.sleep(5)
            g = scrape(args.metrics_url, ["vllm:num_requests_running", "vllm:num_requests_waiting",
                                          "vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc"])
            if g:
                gauges.append(g)
        snap["b"] = scrape(args.metrics_url, mnames)
        snap["tb"] = time.monotonic()
    for t in ths:
        t.join(timeout=secs + 900)

    ok_all = [r for r in res if r["ok"]]
    ventana = [r for r in ok_all if t_meas0 <= r["t_end"] <= t_end]
    dur = secs
    out_tok = sum(r["out"] for r in ventana)
    in_tok = sum(r["inp"] for r in ventana)
    errs = [r for r in res if not r["ok"]]
    itl = [(r["total"] - r["ttft"]) / (r["out"] - 1) for r in ok_all if r.get("ttft") and r["out"] > 1]
    row = {
        "concurrencia": conc, "ventana_seg": dur, "peticiones_ok": len(ok_all), "errores": len(errs),
        "primer_error": errs[0]["err"] if errs else None,
        "cliente_out_tok_s": round(out_tok / dur, 1), "cliente_in_tok_s": round(in_tok / dur, 1),
        "cliente_rps": round(len(ventana) / dur, 3),
        "tokens_salida_medios": round(statistics.fmean([r["out"] for r in ok_all]), 1) if ok_all else None,
        "tokens_entrada_medios": round(statistics.fmean([r["inp"] for r in ok_all]), 1) if ok_all else None,
        "ttft_p50_ms": pct([r["ttft"] for r in ok_all if r.get("ttft")], 0.5),
        "ttft_p95_ms": pct([r["ttft"] for r in ok_all if r.get("ttft")], 0.95),
        "itl_medio_ms": round(statistics.fmean(itl), 1) if itl else None,
        "lat_p50_ms": pct([r["total"] for r in ok_all], 0.5),
        "lat_p95_ms": pct([r["total"] for r in ok_all], 0.95),
        "lat_p99_ms": pct([r["total"] for r in ok_all], 0.99),
    }
    if snap.get("a") and snap.get("b"):
        dt = snap["tb"] - snap["ta"]
        row["motor_out_tok_s"] = round((snap["b"][mnames[0]] - snap["a"][mnames[0]]) / dt, 1)
        row["motor_in_tok_s"] = round((snap["b"][mnames[1]] - snap["a"][mnames[1]]) / dt, 1)
        if gauges:
            row["motor_running_medio"] = round(statistics.fmean(g["vllm:num_requests_running"] for g in gauges), 1)
            row["motor_waiting_max"] = max(g["vllm:num_requests_waiting"] for g in gauges)
            kv = [g.get("vllm:kv_cache_usage_perc") or g.get("vllm:gpu_cache_usage_perc") or 0 for g in gauges]
            row["motor_kv_uso_max_pct"] = round(max(kv) * 100, 1)
    # cifra "oficial" del nivel: la del motor si existe, si no la del cliente
    row["out_tok_s"] = row.get("motor_out_tok_s", row["cliente_out_tok_s"])
    row["in_tok_s"] = row.get("motor_in_tok_s", row["cliente_in_tok_s"])
    return row


def barrido(args, headers, pf, perfil, extra):
    ent, mt, lv, secs, warm = PERFILES[perfil]
    levels = args.levels.get(perfil, lv)
    secs = args.secs.get(perfil, secs)
    warm = args.warm.get(perfil, warm)
    filas, previos = [], []
    print(f"\n=== perfil {perfil}: niveles {levels}, {secs}s de ventana + {warm}s de warm ===", flush=True)
    for c in levels:
        f = correr_nivel(args, headers, pf, perfil, c, secs, warm, extra)
        filas.append(f)
        clave = "out_tok_s" if perfil != "prefill" else "in_tok_s"
        print(f"  c={c:<4} out={f['out_tok_s']:>8} tok/s  in={f['in_tok_s']:>8} tok/s  rps={f['cliente_rps']:<7} "
              f"ttft_p50={f['ttft_p50_ms']}ms itl={f['itl_medio_ms']}ms lat_p95={f['lat_p95_ms']}ms "
              f"err={f['errores']} running={f.get('motor_running_medio')} wait_max={f.get('motor_waiting_max')} "
              f"kv={f.get('motor_kv_uso_max_pct')}%", flush=True)
        if f["errores"] and f["errores"] > 0.05 * max(1, f["peticiones_ok"] + f["errores"]):
            print(f"  [stop] errores > 5% ({f['primer_error']})", flush=True)
            break
        previos.append(f[clave])
        # techo alcanzado: 2 niveles seguidos sin mejorar >=5% sobre el maximo previo
        if len(previos) >= 4 and max(previos[-2:]) < max(previos[:-2]) * 1.05:
            print("  [stop] throughput aplanado (2 niveles sin mejorar >=5%)", flush=True)
            break
    return filas


def sonda_vision(args, headers, pf):
    b64 = base64.b64encode(png_bytes(640, 480, 7)).decode()
    r = http_json(f"{args.base_url}/chat/completions", {
        "model": args.model, "max_tokens": 60, "temperature": 0,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            {"type": "text", "text": "There is a solid square in the middle of the image. What color is it? Answer with one word."}]}],
        **args.extra_thinking_off}, headers, timeout=300)
    txt = (r["choices"][0]["message"].get("content") or "")
    ok = "red" in txt.lower() or "rojo" in txt.lower()
    print(f"[vision] respuesta: {txt.strip()[:80]!r} -> {'OK ve la imagen' if ok else 'FALLO'} "
          f"(prompt_tokens={r['usage']['prompt_tokens']})", flush=True)
    return {"ok": ok, "respuesta": txt.strip()[:200], "prompt_tokens": r["usage"]["prompt_tokens"]}


def prediccion(res):
    try:
        D = max(f["out_tok_s"] for f in res["decode"])
        P = statistics.median(f["in_tok_s"] for f in res["prefill"])
        real = res["realistic"]
    except (KeyError, ValueError):
        return None
    if D <= 0 or P <= 0:
        return None
    ent = statistics.fmean([f["tokens_entrada_medios"] for f in real if f["tokens_entrada_medios"]]) or 8192
    sal = statistics.fmean([f["tokens_salida_medios"] for f in real if f["tokens_salida_medios"]]) or 1024
    T = ent / P + sal / D
    best = max(real, key=lambda f: f["out_tok_s"])
    pred_out = sal / T
    return {
        "D_decode_techo_tok_s": D, "P_prefill_techo_tok_s": P,
        "entrada_media": round(ent), "salida_media": round(sal),
        "seg_gpu_por_peticion": round(T, 3),
        "pred_req_s": round(1 / T, 4), "pred_out_tok_s": round(pred_out, 1),
        "pred_total_tok_s": round((ent + sal) / T, 1),
        "medido_mejor_concurrencia": best["concurrencia"],
        "medido_req_s": best["cliente_rps"], "medido_out_tok_s": best["out_tok_s"],
        "medido_in_tok_s": best["in_tok_s"],
        "ratio_medido_sobre_predicho": round(best["out_tok_s"] / pred_out, 3),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", required=True, help="p.ej. http://127.0.0.1/v1 (gateway) o http://10.x.x.x:8007/v1 (worker)")
    ap.add_argument("--model", required=True)
    ap.add_argument("--api-key", default="")
    ap.add_argument("--label", default="run")
    ap.add_argument("--metrics-url", default=None, help="http://<worker>:8007/metrics (solo yendo directo al worker)")
    ap.add_argument("--profiles", default="decode,prefill,realistic,vision")
    ap.add_argument("--levels", default="", help="p.ej. decode=1,8,32;realistic=1,4,16")
    ap.add_argument("--secs", default="", help="p.ej. decode=30;realistic=120")
    ap.add_argument("--warm", default="", help="p.ej. decode=10")
    ap.add_argument("--out", default="ceiling.json")
    ap.add_argument("--no-extra-body", action="store_true",
                    help="no enviar ignore_eos/enable_thinking (por si el gateway los rechaza)")
    args = ap.parse_args()

    def parse(kv, cast):
        d = {}
        for part in filter(None, kv.split(";")):
            k, v = part.split("=")
            d[k] = [cast(x) for x in v.split(",")] if cast is list else cast(v)
        return d
    args.levels = {k: [int(x) for x in v.split(",")] for k, v in
                   (p.split("=") for p in filter(None, args.levels.split(";")))}
    args.secs, args.warm = parse(args.secs, int), parse(args.warm, int)
    args.base_url = args.base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {args.api_key}"} if args.api_key else {}
    # Qwen3.5 razona por defecto ("thinking"): se apaga para medir salida util y
    # ignore_eos fija la longitud de salida para que todos los niveles hagan el mismo trabajo.
    args.extra_thinking_off = {} if args.no_extra_body else {"chat_template_kwargs": {"enable_thinking": False}}
    extra = {} if args.no_extra_body else {"chat_template_kwargs": {"enable_thinking": False}, "ignore_eos": True}

    pf = PromptFactory(args.base_url, headers, args.model)
    pf.calibrate()
    res = {"label": args.label, "base_url": args.base_url, "model": args.model,
           "inicio_epoch": time.time(), "tok_por_palabra": pf.tok_per_word}
    for perfil in args.profiles.split(","):
        if perfil == "vision":
            res["vision_sonda"] = sonda_vision(args, headers, pf)
        res[perfil] = barrido(args, headers, pf, perfil, extra)
        with open(args.out, "w") as fh:  # guardado incremental
            json.dump(res, fh, indent=1)
    res["prediccion_8k_1k"] = prediccion(res)
    res["fin_epoch"] = time.time()
    with open(args.out, "w") as fh:
        json.dump(res, fh, indent=1)
    print("\nRESULTADO_JSON_BEGIN")
    print(json.dumps(res))
    print("RESULTADO_JSON_END", flush=True)
    if res["prediccion_8k_1k"]:
        print("\n[prediccion 8k/1k]", json.dumps(res["prediccion_8k_1k"], indent=1))


if __name__ == "__main__":
    sys.exit(main())
