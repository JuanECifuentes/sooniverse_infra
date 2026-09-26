/* ==============================================================================
 * SOONIVERSE :: Auto-login SSO (cabecera de confianza)
 * ==============================================================================
 * Inyectado como el PRIMER <script> del <head> (ver Dockerfile) -sin 'defer'
 * ni 'type=module', a propósito: debe ejecutarse de forma SÍNCRONA y BLOQUEAR
 * el resto del parseo del documento (incluido el bundle principal de la SPA)
 * hasta terminar, para que Open WebUI arranque YA con el token guardado.
 *
 * Por qué existe: el backend de Open WebUI confía en la cabecera
 * WEBUI_AUTH_TRUSTED_EMAIL_HEADER (nginx la inyecta vía 'auth_request' contra
 * la sesión de Django -ver scripts/render_gateway_stack.py) para autenticar
 * /api/v1/auths/signin sin validar contraseña real. Pero eso solo cubre
 * llamadas SERVER-SIDE (scripts/docker_images/openwebui/overlay/sooniverse/
 * bootstrap_models.py, que corre dentro de la red docker). El NAVEGADOR del
 * usuario real nunca hacía ese signin por su cuenta: entraba a "/" con sesión
 * Django válida, pero el frontend de Open WebUI (SvelteKit) solo revisa su
 * propio localStorage['token'] -inexistente la primera vez- y muestra su
 * pantalla de login pidiendo credenciales que el usuario nunca configuró.
 * Confirmado en un despliegue real: GET /api/models sin este login devolvía
 * 401 "Not authenticated" pese a la sesión Django activa.
 *
 * Este script cierra ese hueco: si no hay token guardado, intenta el mismo
 * signin (con valores dummy -el backend los ignora cuando la cabecera de
 * confianza gana la autenticación) y lo guarda. Si falla (401/400: acceso
 * directo por IP sin pasar por el dominio, o SSO no configurado), no hace
 * nada -el flujo de login manual de Open WebUI sigue disponible sin cambios.
 */
(function () {
  "use strict";
  try {
    if (localStorage.getItem("token")) {
      return; // ya hay una sesión guardada de una visita anterior.
    }
    var xhr = new XMLHttpRequest();
    // Síncrono a propósito (tercer argumento 'false'): debe resolver antes de
    // que el bundle de la SPA (cargado después en el documento) se ejecute.
    xhr.open("POST", "/api/v1/auths/signin", false);
    xhr.setRequestHeader("Content-Type", "application/json");
    xhr.send(
      JSON.stringify({
        email: "sooniverse-sso@sooniverse.internal",
        password: "sooniverse-sso-trusted-header-unused",
      })
    );
    if (xhr.status === 200) {
      var data = JSON.parse(xhr.responseText);
      if (data && data.token) {
        localStorage.setItem("token", data.token);
      }
    }
    // Cualquier otro status (401 sin cabecera de confianza, 5xx transitorio):
    // silencioso, cae al login manual normal de Open WebUI.
  } catch (e) {
    // Nunca debe bloquear la carga del chat si algo falla aquí.
  }
})();
