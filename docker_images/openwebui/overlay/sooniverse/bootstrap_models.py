#!/usr/bin/env python3
"""
==============================================================================
SOONIVERSE :: Bootstrap de modelos + capacidades en Open WebUI
==============================================================================
Traduce `sooniverse.model_capability` (la verdad OBSERVADA por
scripts/test_model_capabilities.py, ver database/003_model_capabilities.sql)
en filas `model` de Open WebUI, para que la interfaz solo ofrezca lo que el
modelo desplegado de verdad soporta (p.ej. no mostrar el botón de subir
imagen si `effective_vision` es false).

Corre DENTRO de la imagen de Open WebUI (mismo contenedor, entrypoint
distinto: ver el servicio `openwebui-bootstrap` en el docker-compose
generado por scripts/render_gateway_stack.py), pero habla con Open WebUI
por su API HTTP pública -nunca importa su ORM interno- para no acoplarse a
detalles internos (motor async, esquema de sesiones) que cambian entre
versiones. La única superficie que si es "interna" es PostgreSQL, y ahí solo
tocamos NUESTRA tabla (`sooniverse.model_capability`), nunca las tablas de
Open WebUI.

Autenticación: usa una cuenta técnica (OPENWEBUI_BOOTSTRAP_EMAIL/PASSWORD).
En una instancia nueva es la PRIMERA cuenta creada -Open WebUI la asciende a
admin automáticamente incluso con ENABLE_SIGNUP=false, ver
backend/open_webui/routers/auths.py::signup- así que el signup solo tiene
efecto una vez; en corridas posteriores (idempotentes) cae al signin.

Convención de modelo asumida (a verificar/ajustar en el despliegue de prueba,
ver docker_images/openwebui/patches/README.md si hiciera falta un ajuste):
el `id` de la fila `model` es el MISMO `model_public_name` que expone LiteLLM
-así la fila "sombrea" (añade capacidades/params a) el modelo de conexión ya
listado, en vez de crear un duplicado en el selector-.

Uso (normalmente invocado por el propio compose, no a mano):
    python3 -m sooniverse.bootstrap_models
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

OPENWEBUI_BASE_URL = os.environ.get("OPENWEBUI_BASE_URL", "http://open-webui:8080")
LITELLM_BASE_URL = os.environ.get("LITELLM_BASE_URL", "http://litellm:4000")
LITELLM_MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
CLIENTE_ID = os.environ.get("CLIENTE_ID", "default")
ENTORNO = os.environ.get("ENTORNO", "prod")

BOOTSTRAP_EMAIL = os.environ.get("OPENWEBUI_BOOTSTRAP_EMAIL", "bootstrap@sooniverse.internal")
BOOTSTRAP_PASSWORD = os.environ.get("OPENWEBUI_BOOTSTRAP_PASSWORD", "")
BOOTSTRAP_NAME = "Sooniverse Bootstrap"

READY_TIMEOUT_SECONDS = 120
READY_POLL_INTERVAL_SECONDS = 5


class BootstrapError(Exception):
    """Error irrecuperable de esta corrida (ver mensaje para diagnóstico)."""


def _http(method: str, url: str, body: Optional[Dict[str, Any]] = None,
          token: Optional[str] = None, timeout: int = 20) -> Dict[str, Any]:
    """'token' se manda como Authorization: Bearer <token> sea cual sea su
    origen (sesión de Open WebUI o la master key de LiteLLM) -son dos APIs
    HTTP distintas, pero el mismo esquema de auth."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            if not raw:
                return {"status": resp.status, "json": {}}
            try:
                return {"status": resp.status, "json": json.loads(raw)}
            except json.JSONDecodeError:
                # p.ej. GET "/" de Open WebUI devuelve el index.html de la SPA,
                # no JSON -usado solo para el ping de wait_for_openwebui().
                return {"status": resp.status, "text": raw}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return {"status": exc.code, "json": json.loads(raw)}
        except json.JSONDecodeError:
            return {"status": exc.code, "text": raw}
    except (urllib.error.URLError, TimeoutError) as exc:
        return {"error": str(exc)}


def wait_for_openwebui() -> None:
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        resp = _http("GET", f"{OPENWEBUI_BASE_URL}/", timeout=5)
        if resp.get("status") == 200:
            return
        time.sleep(READY_POLL_INTERVAL_SECONDS)
    raise BootstrapError(f"Open WebUI no respondió en {OPENWEBUI_BASE_URL}/ tras {READY_TIMEOUT_SECONDS}s")


def authenticate() -> str:
    """Devuelve un token bearer válido para la cuenta técnica de bootstrap.
    Intenta signup primero (solo tiene efecto la primera vez: cero usuarios
    en la instancia, o ENABLE_SIGNUP=true) y cae a signin si ya existe."""
    if not BOOTSTRAP_PASSWORD:
        raise BootstrapError(
            "OPENWEBUI_BOOTSTRAP_PASSWORD no está definida (ver .env.example). "
            "Sin ella no hay forma de autenticarse contra la API de Open WebUI."
        )

    signup = _http("POST", f"{OPENWEBUI_BASE_URL}/api/v1/auths/signup", {
        "email": BOOTSTRAP_EMAIL,
        "password": BOOTSTRAP_PASSWORD,
        "name": BOOTSTRAP_NAME,
    })
    body = signup.get("json", {})
    if signup.get("status") == 200 and isinstance(body, dict) and body.get("token"):
        print("[bootstrap] Cuenta técnica creada (primera cuenta -> admin automático).")
        return body["token"]

    signin = _http("POST", f"{OPENWEBUI_BASE_URL}/api/v1/auths/signin", {
        "email": BOOTSTRAP_EMAIL,
        "password": BOOTSTRAP_PASSWORD,
    })
    body = signin.get("json", {})
    if signin.get("status") == 200 and isinstance(body, dict) and body.get("token"):
        print("[bootstrap] Autenticado como cuenta técnica existente.")
        return body["token"]

    raise BootstrapError(
        f"No se pudo autenticar contra Open WebUI (signup={signup.get('status')}, "
        f"signin={signin.get('status')}). Revisa OPENWEBUI_BOOTSTRAP_EMAIL/PASSWORD."
    )


def fetch_litellm_models() -> List[str]:
    """Modelos realmente registrados en LiteLLM ahora mismo (fuente de verdad
    de 'qué existe', no lo que el contrato declaraba en el último render)."""
    resp = _http("GET", f"{LITELLM_BASE_URL}/v1/models", token=LITELLM_MASTER_KEY or None, timeout=15)
    if resp.get("status") != 200:
        raise BootstrapError(f"No se pudo leer {LITELLM_BASE_URL}/v1/models: {resp}")
    data = resp.get("json", {}).get("data", [])
    return sorted({m["id"] for m in data if "id" in m})


def fetch_capabilities_by_model() -> Dict[str, Dict[str, Any]]:
    """Lee sooniverse.model_capability (nuestra tabla, psycopg2 directo -no
    tiene nada que ver con el esquema interno de Open WebUI)."""
    try:
        import psycopg2
    except ImportError as exc:
        raise BootstrapError(f"psycopg2 no disponible en la imagen: {exc}")

    conn = psycopg2.connect(
        dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"], host=os.environ["DB_HOST"],
        port=os.environ["DB_PORT"], connect_timeout=10,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT model_public_name, effective_vision, effective_tool_calling,
                       effective_json_object, max_model_len, max_output_tokens
                FROM sooniverse.model_capability
                WHERE client_id = %s AND environment = %s
                """,
                (CLIENTE_ID, ENTORNO),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    return {
        r[0]: {
            "effective_vision": bool(r[1]),
            "effective_tool_calling": bool(r[2]),
            "effective_json_object": bool(r[3]),
            "max_model_len": r[4],
            "max_output_tokens": r[5],
        }
        for r in rows
    }


def build_model_form(model_id: str, caps: Dict[str, Any]) -> Dict[str, Any]:
    max_output = caps.get("max_output_tokens") or min(4096, (caps.get("max_model_len") or 16384) // 4)
    return {
        "id": model_id,
        "base_model_id": None,
        "name": model_id,
        "meta": {
            "capabilities": {
                "vision": caps["effective_vision"],
                "file_upload": caps["effective_vision"],
                "file_context": caps["effective_vision"],
                "web_search": False,
                "image_generation": False,
                "code_interpreter": caps["effective_tool_calling"],
                "terminal": False,
                "usage": True,
                "citations": True,
                "status_updates": True,
                "memory": False,
                "builtin_tools": caps["effective_tool_calling"],
            },
        },
        "params": {
            "max_tokens": max_output,
        },
        # Lista vacía, NUNCA None: update_model_by_id() reconstruye ModelForm
        # desde form_data.model_dump() (ver backend/open_webui/routers/models.py),
        # y access_grants está tipado list[...], no Optional[list] -un None
        # explícito revalida con pydantic y lanza ValidationError -> 500
        # (confirmado en despliegue real). [] = sin restricciones de acceso.
        "access_grants": [],
        "is_active": True,
    }


def upsert_model(token: str, model_id: str, form: Dict[str, Any], existing_ids: set) -> None:
    if model_id in existing_ids:
        resp = _http("POST", f"{OPENWEBUI_BASE_URL}/api/v1/models/model/update?id={model_id}",
                     form, token=token)
        action = "actualizado"
    else:
        resp = _http("POST", f"{OPENWEBUI_BASE_URL}/api/v1/models/create", form, token=token)
        action = "creado"

    if resp.get("status") == 200:
        print(f"[bootstrap] Modelo '{model_id}' {action} "
              f"(vision={form['meta']['capabilities']['vision']}, "
              f"tools={form['meta']['capabilities']['code_interpreter']})")
    else:
        print(f"[WARNING] No se pudo aplicar ({action}) '{model_id}': {resp}")


def main() -> int:
    try:
        wait_for_openwebui()
        token = authenticate()

        litellm_models = fetch_litellm_models()
        if not litellm_models:
            print("[bootstrap] LiteLLM no reporta modelos todavía; nada que sincronizar.")
            return 0

        capabilities = fetch_capabilities_by_model()

        # NOTA: '/api/v1/models/list' devuelve {"items": [...], "total": N} de
        # una búsqueda paginada -casi siempre vacía para modelos recién creados
        # sin más filtros-, NO la lista completa (confirmado en despliegue real:
        # causaba que cada corrida intentara 'crear' en vez de 'actualizar' y
        # fallara con 401 "model id ya registrado", dejando las capacidades
        # desactualizadas en silencio). '/api/v1/models/base' sí es la lista
        # completa de modelos personalizados (requiere admin, que es lo que
        # authenticate() garantiza).
        list_resp = _http("GET", f"{OPENWEBUI_BASE_URL}/api/v1/models/base", token=token)
        existing_ids = {
            m["id"] for m in list_resp.get("json", []) if isinstance(list_resp.get("json"), list) and "id" in m
        }

        for model_id in litellm_models:
            # Fail-closed por diseño (ver database/003_model_capabilities.sql):
            # sin fila de capacidades sondeadas todavía (primer despliegue, antes
            # del primer sondeo), TODO queda apagado -nunca se ofrece algo sin
            # confirmar.
            caps = capabilities.get(model_id, {
                "effective_vision": False,
                "effective_tool_calling": False,
                "effective_json_object": False,
                "max_model_len": None,
                "max_output_tokens": None,
            })
            form = build_model_form(model_id, caps)
            upsert_model(token, model_id, form, existing_ids)

        print(f"[OK] Bootstrap de modelos completo ({len(litellm_models)} modelo(s)).")
        return 0
    except BootstrapError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
