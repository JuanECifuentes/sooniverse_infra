#!/usr/bin/env python3
"""
==============================================================================
Sooniverse Infra - API Key dedicada para Open WebUI
==============================================================================
Sin esto, Open WebUI habla con LiteLLM usando la MASTER KEY directamente
(`OPENAI_API_KEY` en docker-compose): la master key no es una key virtual,
así que LiteLLM no la asocia a ninguna fila de `sooniverse.api_key_registry`
y TODO el consumo del chat queda huérfano -el panel lo muestra como
"(sin registro)" en vez de poder rastrearlo.

Genera (UNA sola vez, idempotente) una key virtual de LiteLLM dedicada a la
interfaz de chat, la registra en `sooniverse.api_key_registry`
(`proposito='cliente'`: es tráfico real de negocio, no debe excluirse de
ningún reporte) y la persiste en `.env` como `OPENWEBUI_LITELLM_API_KEY` -el
docker-compose generado usa esa variable como `OPENAI_API_KEY` del servicio
`open-webui` en vez de la master key (ver render_gateway_stack.py).

Corre en el propio Gateway (GATEWAY_RUN_SCRIPT, justo tras el primer
'docker compose up'): necesita `.env` como archivo real (no symlink) para
poder hacerle append.

Habla con LiteLLM A TRAVÉS DE NGINX (http://localhost:80/key/...), NUNCA
directo al puerto interno de litellm (4000): con
`gateway.exponer_puertos_directos: false` (el default recomendado) litellm
solo usa `expose:` en docker-compose -alcanzable desde OTROS CONTENEDORES de
la misma red, no desde el host- así que 'http://localhost:4000' está
inalcanzable desde este script, que corre directo en el host (confirmado en
un despliegue real: timeout total, nunca un error de conexión). nginx SÍ
publica el puerto 80 al host siempre, y ya proxia '/key/...' a litellm sin
pasar por el 'auth_request' de SSO (ver
scripts/render_gateway_stack.py::_nginx_locations_block) -services 'ports:'
directos solo si el operador activa exponer_puertos_directos, este camino no
depende de ese flag.

Best-effort por diseño: cualquier fallo se reporta como [WARNING]/[ERROR] y
Open WebUI sigue funcionando con la master key mientras tanto -nunca debe
abortar el despliegue completo por esto.

Hash de correlación: `LiteLLM_SpendLogs.api_key` guarda sha256(key en claro),
y el ETL (database/004_usage_analytics.sql) une contra
`api_key_registry.litellm_token_hash`. El campo `token` de la respuesta de
'/key/generate' es la key EN CLARO (no el hash), así que se calcula aquí el
mismo sha256 que LiteLLM (`hash_token`) en vez de fiarse de la respuesta
-guardar `token` dejaba todo el chat como "(sin registro)" en el panel.

Idempotente y auto-reparable: si la key ya existe en `.env`, igual se
re-sincroniza su fila del registro (corrige registros viejos con el hash
equivocado o que nunca llegaron a escribirse).

Uso:
    python3 scripts/ensure_openwebui_key.py --env-file .env
    python3 scripts/ensure_openwebui_key.py --env-file demo/.env.demo --base-url http://proxy

Salida relevante para quien invoca (generate_infra.py, vía GATEWAY_RUN_SCRIPT):
    imprime la línea literal 'SOONIVERSE_OPENWEBUI_KEY_CREATED=1' SOLO cuando
    esta corrida creó una key nueva -señal para recrear 'open-webui' con el
    valor recién escrito en .env (el contenedor ya arrancó con el .env viejo).
"""

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

READY_TIMEOUT_SECONDS = 120
READY_POLL_INTERVAL_SECONDS = 5


def _http_json(method: str, url: str, body: Optional[Dict[str, Any]] = None,
               token: Optional[str] = None, timeout: int = 15) -> Dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return {"status": resp.status, "json": json.loads(raw) if raw else {}}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return {"status": exc.code, "json": json.loads(raw)}
        except json.JSONDecodeError:
            return {"status": exc.code, "text": raw}
    except (urllib.error.URLError, TimeoutError) as exc:
        return {"error": str(exc)}


# Default: nginx publicado en el host del Gateway. El demo (demo/deploy_demo.sh)
# corre este script DENTRO de la red Docker y lo sobreescribe con
# '--base-url http://proxy' (o SOONIVERSE_NGINX_BASE_URL).
NGINX_BASE_URL = os.environ.get("SOONIVERSE_NGINX_BASE_URL", "http://localhost").rstrip("/")


def litellm_token_hash(raw_key: str) -> str:
    """Mismo hash que LiteLLM (`hash_token`): sha256 hex de la key en claro.
    Es el valor que LiteLLM escribe en `LiteLLM_SpendLogs.api_key`."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def wait_for_litellm() -> bool:
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    url = f"{NGINX_BASE_URL}/health/liveliness"
    while time.monotonic() < deadline:
        resp = _http_json("GET", url, timeout=5)
        if resp.get("status") == 200:
            return True
        time.sleep(READY_POLL_INTERVAL_SECONDS)
    return False


def _generate_key(alias: str, master_key: str) -> Dict[str, Any]:
    return _http_json(
        "POST", f"{NGINX_BASE_URL}/key/generate",
        {
            "key_alias": alias,
            "metadata": {"gestionado_por": "sooniverse", "proposito": "chat-interno"},
        },
        token=master_key,
    )


def _delete_key_by_alias(alias: str, master_key: str) -> None:
    """Best-effort: LiteLLM no borra por alias directamente en todas las
    versiones, así que primero se busca el token vía '/key/list' filtrando
    por alias. Si no se encuentra o el borrado falla, el siguiente
    '/key/generate' con el mismo alias volverá a fallar y quedará como
    [WARNING] -no es peor que el estado actual (key huérfana sin uso)."""
    listed = _http_json("GET", f"{NGINX_BASE_URL}/key/list?key_alias={alias}", token=master_key)
    keys = (listed.get("json") or {}).get("keys", [])
    # '/key/list' devuelve 'keys' como una lista de STRINGS (el token/hash
    # directo), no de objetos -confirmado contra una instancia real; asumir
    # dicts con '.get()' fallaba en silencio y dejaba 'tokens' siempre vacío.
    tokens = [k for k in keys if isinstance(k, str) and k]
    if not tokens:
        print(f"[WARNING] No se encontró el token de la key huérfana '{alias}' vía /key/list; "
              "no se puede borrar.")
        return
    resp = _http_json("POST", f"{NGINX_BASE_URL}/key/delete", {"keys": tokens}, token=master_key)
    if resp.get("status") != 200:
        print(f"[WARNING] No se pudo borrar la key huérfana '{alias}': {resp}")



def _register_key(env_path: Path, alias: str, raw_key: str,
                  cliente_id: str, entorno: str) -> bool:
    """Upsert de la fila de la key de Open WebUI en `sooniverse.api_key_registry`
    con el hash REAL (sha256) que LiteLLM escribe en SpendLogs. Best-effort:
    devuelve False (con [WARNING]) si la BD no es alcanzable, nunca lanza."""
    from db_setup import connect, resolve_db_config  # type: ignore[import-not-found]

    token_hash = litellm_token_hash(raw_key)
    try:
        conn = connect(resolve_db_config(env_path))
        try:
            with conn:
                with conn.cursor() as cur:
                    # Deja como mucho una fila ACTIVA con este alias: si
                    # esta corrida acaba de borrar y reemplazar una key
                    # huérfana, su fila vieja seguía activa en el registro
                    # -mismo alias, distinto litellm_token_hash (la columna
                    # UNIQUE real), así que el INSERT de abajo no la pisa por
                    # sí solo- (confirmado en un despliegue real: quedaban dos
                    # filas 'sooniverse-openwebui-acme-prod' activas). También
                    # desactiva la fila vieja que guardó la key EN CLARO en
                    # vez del hash (bug previo, ver docstring del módulo).
                    cur.execute(
                        """
                        UPDATE sooniverse.api_key_registry
                        SET is_active = FALSE, deactivated_at = NOW()
                        WHERE key_alias = %s AND cliente_id = %s AND entorno = %s
                          AND litellm_token_hash <> %s AND is_active
                        """,
                        (alias, cliente_id, entorno, token_hash),
                    )
                    cur.execute(
                        """
                        INSERT INTO sooniverse.api_key_registry
                            (key_alias, litellm_token_hash, key_prefix, cliente_id, entorno,
                             descripcion, is_active, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, TRUE, NOW(), NOW())
                        ON CONFLICT (litellm_token_hash) DO UPDATE SET
                            key_alias = EXCLUDED.key_alias, updated_at = NOW()
                        """,
                        (alias, token_hash, raw_key[:12], cliente_id, entorno,
                         "Key interna de la interfaz de chat (Open WebUI) hacia LiteLLM. "
                         "No repartir a clientes ni usuarios finales."),
                    )
                    # Re-atribuye el consumo ya ingerido de esta key que quedó
                    # huérfano (api_key_id NULL) mientras el registro tenía el
                    # hash equivocado -el ETL solo re-une las últimas 48 h.
                    cur.execute(
                        """
                        UPDATE sooniverse.token_usage_event e
                        SET api_key_id = reg.id
                        FROM sooniverse.api_key_registry reg
                        WHERE reg.litellm_token_hash = %s
                          AND e.litellm_token_hash = reg.litellm_token_hash
                          AND e.api_key_id IS NULL
                        """,
                        (token_hash,),
                    )
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - la key ya quedó operativa; el registro es best-effort
        print(f"[WARNING] Key de Open WebUI operativa pero no se pudo registrar en la BD: {exc}")
        return False
    return True


def main() -> int:
    global NGINX_BASE_URL

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument(
        "--base-url", default=NGINX_BASE_URL,
        help="URL de nginx del Gateway que proxia /key/* y /health/* a LiteLLM "
             "(default: $SOONIVERSE_NGINX_BASE_URL o http://localhost)",
    )
    args = parser.parse_args()
    NGINX_BASE_URL = args.base_url.rstrip("/")

    from db_setup import parse_env_file  # type: ignore[import-not-found]

    env_path = Path(args.env_file)
    env_vals = parse_env_file(env_path)

    cliente_id = env_vals.get("CLIENTE_ID", "default")
    entorno = env_vals.get("ENTORNO", "prod")
    alias = f"sooniverse-openwebui-{cliente_id}-{entorno}"

    existing_key = env_vals.get("OPENWEBUI_LITELLM_API_KEY", "").strip()
    if existing_key:
        # Nada que emitir, pero SÍ se re-sincroniza el registro: repara
        # despliegues que guardaron la key en claro como hash (o nunca
        # llegaron a escribir la fila) y por eso veían "(sin registro)".
        if _register_key(env_path, alias, existing_key, cliente_id, entorno):
            print(f"[OK] OPENWEBUI_LITELLM_API_KEY ya existe en {env_path}; registro "
                  f"verificado (alias={alias}).")
        return 0

    master_key = env_vals.get("LITELLM_MASTER_KEY", "").strip()
    if not master_key:
        print("[WARNING] LITELLM_MASTER_KEY no está definida; se omite el aprovisionamiento "
              "de la key de Open WebUI (seguirá usando la master key).")
        return 0

    if not wait_for_litellm():
        print(f"[WARNING] LiteLLM no respondió a través de nginx ({NGINX_BASE_URL}) tras "
              f"{READY_TIMEOUT_SECONDS}s; se omite el aprovisionamiento de la key de Open WebUI "
              "(reintenta en el próximo despliegue/sync).")
        return 1

    resp = _generate_key(alias, master_key)
    if resp.get("status") == 400 and "already exists" in str(resp.get("json", "")):
        # El alias sobrevivió en LiteLLM a un .env que perdió la key (p.ej.
        # _associate_gateway_eip sobrescribiendo el .env remoto antes de que
        # existiera la fusión de valores dinámicos -ver generate_infra.py-,
        # o un redeploy manual). LiteLLM nunca vuelve a devolver la key en
        # claro para un alias existente, así que "recuperarla" es imposible
        # -se borra la huérfana y se emite una nueva con el mismo alias.
        print(f"[WARNING] La key '{alias}' ya existe en LiteLLM sin rastro en .env "
              "(quedó huérfana); se borra y se emite una nueva con el mismo alias.")
        _delete_key_by_alias(alias, master_key)
        resp = _generate_key(alias, master_key)
    body = resp.get("json") or {}
    raw_key = body.get("key")
    if not raw_key:
        print(f"[WARNING] LiteLLM no emitió la key de Open WebUI (status={resp.get('status')}): "
              f"{body or resp.get('error')}. Seguirá usando la master key.")
        return 1

    with env_path.open("a", encoding="utf-8") as f:
        f.write(f"\nOPENWEBUI_LITELLM_API_KEY={raw_key}\n")

    _register_key(env_path, alias, raw_key, cliente_id, entorno)

    print(f"[OK] API Key de Open WebUI creada y persistida en {env_path} (alias={alias}).")
    print("SOONIVERSE_OPENWEBUI_KEY_CREATED=1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
