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
          token: Optional[str] = None, timeout: int = 20,
          extra_headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """'token' se manda como Authorization: Bearer <token> sea cual sea su
    origen (sesión de Open WebUI o la master key de LiteLLM) -son dos APIs
    HTTP distintas, pero el mismo esquema de auth. 'extra_headers' es lo que
    usa authenticate() en modo SSO por cabecera de confianza."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if extra_headers:
        headers.update(extra_headers)
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


TRUSTED_EMAIL_HEADER = os.environ.get("WEBUI_AUTH_TRUSTED_EMAIL_HEADER", "")


def _authenticate_trusted_header() -> str:
    """Con SSO por cabecera de confianza activo (WEBUI_AUTH_TRUSTED_EMAIL_HEADER,
    ver docker_images/openwebui/README.md), Open WebUI BLOQUEA /signup y /signin
    por contraseña sin condiciones (backend/open_webui/routers/auths.py:
    /signin exige la cabecera y falla con 400 si no está; /signup exige
    'ui.enable_login_form', que este despliegue deja en False) -confirmado
    contra el código fuente del tag fijado en el Dockerfile, no una suposición.

    Por eso la cuenta técnica de bootstrap se autentica con la MISMA cabecera:
    este script corre DENTRO de la red interna de docker-compose, el mismo
    perímetro de confianza que nginx -nunca alcanzable desde fuera, ver
    'expose:' en el servicio open-webui-. /signin auto-aprovisiona la cuenta
    la primera vez (y la asciende a admin si es la única, igual que hacía el
    signup por contraseña antes de este modo).

    El body de /signin sigue validándose contra su esquema (SigninForm:
    email + password obligatorios) ANTES de que el router llegue a mirar la
    cabecera de confianza -comprobado empíricamente contra una instancia
    real: un body vacío devuelve 422 "Field required" para ambos campos, sin
    llegar siquiera a evaluar el SSO. Los valores en sí se ignoran cuando la
    cabecera de confianza gana la autenticación, así que basta con rellenar
    el esquema con valores dummy."""
    signin = _http(
        "POST", f"{OPENWEBUI_BASE_URL}/api/v1/auths/signin",
        {"email": BOOTSTRAP_EMAIL, "password": "sooniverse-sso-trusted-header-unused"},
        extra_headers={TRUSTED_EMAIL_HEADER: BOOTSTRAP_EMAIL},
    )
    body = signin.get("json", {})
    if signin.get("status") == 200 and isinstance(body, dict) and body.get("token"):
        print("[bootstrap] Autenticado como cuenta técnica vía SSO (cabecera de confianza).")
        return body["token"]

    raise BootstrapError(
        f"No se pudo autenticar contra Open WebUI en modo SSO (status={signin.get('status')}): {body}"
    )


def authenticate() -> str:
    """Devuelve un token bearer válido para la cuenta técnica de bootstrap.

    Sin SSO: intenta signup primero (solo tiene efecto la primera vez: cero
    usuarios en la instancia, o ENABLE_SIGNUP=true) y cae a signin si ya existe.
    Con SSO (WEBUI_AUTH_TRUSTED_EMAIL_HEADER definida), ambos endpoints por
    contraseña están bloqueados -ver _authenticate_trusted_header()."""
    if TRUSTED_EMAIL_HEADER:
        return _authenticate_trusted_header()

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


def ensure_bootstrap_is_admin() -> bool:
    """Autopromueve la cuenta técnica de bootstrap a admin en la tabla `user`
    de Open WebUI si quedó como 'user'.

    El diseño original asumía que la cuenta técnica siempre sería la PRIMERA
    en autenticarse vía SSO (ver authenticate()/docstring del módulo), pero
    eso es una carrera real: si un humano visita el chat/panel antes de que
    este bootstrap corra con éxito por primera vez, ESE humano se queda con
    el único ascenso automático a admin y la cuenta técnica recibe 401 en
    cualquier llamada de administración (crear/actualizar modelos) para
    siempre -confirmado en un despliegue real: la tabla `model` quedaba
    vacía indefinidamente y cada corrida del bootstrap reportaba 'OK' igual
    (ver el fix de exit code en main()). Corregir esto por SQL directo (en
    vez de depender del orden de visitas) hace el resultado determinista sin
    importar quién llegó primero.

    Devuelve True si promovió a alguien (quien llame debe re-autenticarse
    para obtener un token que refleje el rol nuevo)."""
    try:
        import psycopg2
    except ImportError:
        return False

    try:
        conn = psycopg2.connect(
            dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"], host=os.environ["DB_HOST"],
            port=os.environ["DB_PORT"], connect_timeout=10,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort, no debe tumbar el bootstrap
        print(f"[WARNING] No se pudo conectar a PostgreSQL para verificar el rol de la cuenta técnica: {exc}")
        return False

    # Open WebUI vive en el esquema DATABASE_SCHEMA (ver docker-compose.yml:
    # 'sooniverse', no 'public') -confirmado en un despliegue real: sin fijar
    # el search_path aquí, esta conexión psycopg2 (sin el 'options=-csearch_path'
    # que sí lleva el DATABASE_URL de Open WebUI) mira 'public.user', que no
    # existe, y la promoción falla siempre con 'relation "user" does not
    # exist' -dejando a la cuenta técnica sin admin para siempre si un humano
    # ganó la carrera del primer login (ver docstring de la función).
    schema = os.environ.get("DATABASE_SCHEMA", "public")
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(f'SET search_path TO "{schema}", public')
                cur.execute(
                    'UPDATE "user" SET role = %s WHERE email = %s AND role <> %s',
                    ("admin", BOOTSTRAP_EMAIL, "admin"),
                )
                promoted = cur.rowcount > 0
        if promoted:
            print(f"[bootstrap] Cuenta técnica '{BOOTSTRAP_EMAIL}' promovida a admin (autocorrección de carrera).")
        return promoted
    except Exception as exc:  # noqa: BLE001 - p.ej. la tabla 'user' aún no existe
        print(f"[WARNING] No se pudo verificar/corregir el rol de la cuenta técnica: {exc}")
        return False
    finally:
        conn.close()


def ensure_default_user_role_is_user(token: str) -> None:
    """Fuerza 'ui.default_user_role' = 'user' en la config de Open WebUI.

    DEFAULT_USER_ROLE (env var, ver render_gateway_stack.py) solo SIEMBRA esta
    fila la primerísima vez que arranca la instancia; en cada reinicio
    posterior Open WebUI lee el valor YA PERSISTIDO en su tabla `config`
    -ignora la env var por completo- (comprobado empíricamente en un
    despliegue real: cambiar la env var y recrear el contenedor NO cambió el
    rol asignado a un usuario nuevo). Con SSO por cabecera de confianza,
    Django YA es el único gatekeeper (login_required exige cuenta activa
    antes de que nginx deje pasar la petición), así que dejar 'pending' aquí
    -el valor de fábrica- bloquearía a cualquier usuario que no fuera el
    primero jamás creado hasta una aprobación manual dentro del propio panel
    de admin de Open WebUI. Se corrige en cada corrida del bootstrap -no solo
    en el primer despliegue- por si la instancia ya tenía el valor viejo
    persistido de antes de este fix."""
    current = _http("GET", f"{OPENWEBUI_BASE_URL}/api/v1/auths/admin/config", token=token)
    if current.get("status") != 200 or not isinstance(current.get("json"), dict):
        print(f"[WARNING] No se pudo leer la config de admin de Open WebUI: {current}")
        return

    cfg = dict(current["json"])
    if cfg.get("DEFAULT_USER_ROLE") == "user":
        return

    cfg["DEFAULT_USER_ROLE"] = "user"
    updated = _http("POST", f"{OPENWEBUI_BASE_URL}/api/v1/auths/admin/config", cfg, token=token)
    if updated.get("status") == 200:
        print("[bootstrap] ui.default_user_role corregido a 'user' (SSO ya gatekeepea en Django).")
    else:
        print(f"[WARNING] No se pudo corregir ui.default_user_role: {updated}")


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
            # Sin esto, Open WebUI cae al default 'native' (ver
            # backend/open_webui/main.py: form_data.params.function_calling
            # or model_info_params.function_calling or 'native') para TODA
            # tarea automática (autocompletado, follow-ups, consultas de
            # RAG/búsqueda), no solo para el chat -manda tool_choice="auto"
            # a LiteLLM aunque el usuario nunca toque una herramienta.
            # Contra un modelo sin --enable-auto-tool-choice/--tool-call-parser
            # eso revienta con BadRequestError incluso en un chat normal.
            # 'legacy' vuelve al camino de extracción por prompt/JSON, que sí
            # funciona sin soporte real de tool calling en vLLM.
            "function_calling": "native" if caps["effective_tool_calling"] else "legacy",
        },
        # Lista vacía, NUNCA None: update_model_by_id() reconstruye ModelForm
        # desde form_data.model_dump() (ver backend/open_webui/routers/models.py),
        # y access_grants está tipado list[...], no Optional[list] -un None
        # explícito revalida con pydantic y lanza ValidationError -> 500
        # (confirmado en despliegue real). [] = sin restricciones de acceso.
        "access_grants": [],
        "is_active": True,
    }


def upsert_model(token: str, model_id: str, form: Dict[str, Any], existing_ids: set) -> bool:
    """Devuelve True si el upsert tuvo éxito (status 200). El llamador la usa
    para decidir el código de salida del proceso -antes se ignoraba, así que
    un 401 (p.ej. la cuenta técnica sin admin, ver ensure_bootstrap_is_admin())
    quedaba como '[WARNING]' en el log pero el bootstrap completo reportaba
    éxito (exit 0) igual, dejando la tabla 'model' vacía sin que nada aguas
    arriba (sync_openwebui_models.py, la fase 'capabilities') se enterara ni
    reintentara."""
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
        return True

    print(f"[WARNING] No se pudo aplicar ({action}) '{model_id}': {resp}")
    return False


def main() -> int:
    try:
        wait_for_openwebui()
        token = authenticate()
        if ensure_bootstrap_is_admin():
            # El token que ya teníamos se emitió con el rol viejo ('user');
            # las llamadas de administración de abajo (crear/actualizar
            # modelos, leer/escribir la config de admin) necesitan uno fresco.
            token = authenticate()
        ensure_default_user_role_is_user(token)

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

        any_failed = False
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
            if not upsert_model(token, model_id, form, existing_ids):
                any_failed = True

        if any_failed:
            print("[ERROR] Uno o más modelos no se pudieron sincronizar (ver [WARNING] arriba).", file=sys.stderr)
            return 1

        print(f"[OK] Bootstrap de modelos completo ({len(litellm_models)} modelo(s)).")
        return 0
    except BootstrapError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
