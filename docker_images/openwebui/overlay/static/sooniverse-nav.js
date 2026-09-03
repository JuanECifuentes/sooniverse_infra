/* ==============================================================================
 * SOONIVERSE :: Botón de navegación chat -> panel de métricas
 * ==============================================================================
 * Inyectado como <script> en index.html (ver Dockerfile). Complementa el
 * overlay de CSS (sooniverse.css), donde vive la piel .sv-navbtn.
 *
 * Ubicación preferente: ARRIBA A LA DERECHA, integrado (no flotante) en el
 * contenedor derecho del topbar del chat (Navbar.svelte de Open WebUI,
 * div.mr-1.flex.flex-none.items-center.gap-2.self-center, donde viven
 * "Temporary Chat" y "Controls"). El topbar es DOM gestionado por Svelte: un
 * MutationObserver re-inyecta el botón cuando un cambio de ruta lo elimina.
 *
 * Fallback: en páginas sin topbar del chat (workspace, admin, ...) el botón
 * aparece FLOTANTE abajo a la derecha tras un periodo de gracia.
 *
 * Destino: window.__SOONIVERSE_PANEL_URL__ (escrito en runtime por
 * entrypoint.sh desde la env SOONIVERSE_PANEL_URL; default '/panel/', la ruta
 * de nginx en producción, mismo origen).
 */
(function () {
  'use strict';

  var PANEL_URL =
    (typeof window.__SOONIVERSE_PANEL_URL__ === 'string' &&
      window.__SOONIVERSE_PANEL_URL__) ||
    '/panel/';
  var BTN_ID = 'sooniverse-navbtn';
  var GRACE_MS = 4000;
  var START = Date.now();
  var scheduled = false;

  /* Icono: gráfico de columnas (equivale al panel de métricas). Stroke currentColor
     para heredar el color del botón; misma geometría que el icono del panel. */
  var ICON =
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"' +
    ' stroke="currentColor" stroke-width="1.8" stroke-linecap="round"' +
    ' stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M5 20v-6"/><path d="M12 20V4"/><path d="M19 20V10"/>' +
    '</svg>';

  function buildButton(idSuffix, extraClass) {
    var a = document.createElement('a');
    a.id = BTN_ID + (idSuffix || '');
    a.className = 'sv-navbtn' + (extraClass ? ' ' + extraClass : '');
    a.href = PANEL_URL;
    a.setAttribute('aria-label', 'Ir al panel de métricas');
    a.setAttribute('data-tip', 'Ir al panel de métricas');
    a.innerHTML = ICON;
    return a;
  }

  /* Contenedor derecho del topbar del chat (Navbar.svelte, tag v0.11.0). */
  function findHost() {
    var nav = document.querySelector('nav.drag-region');
    if (!nav) return null;
    return nav.querySelector('.flex-none.items-center.self-center');
  }

  function ensure() {
    var btn = document.getElementById(BTN_ID);
    var host = findHost();

    if (host) {
      if (!btn || btn.parentElement !== host) {
        if (btn) btn.remove();
        host.appendChild(buildButton());
        var float = document.getElementById(BTN_ID + '-float');
        if (float) float.remove();
      }
      return;
    }

    /* Sin topbar del chat: fallback flotante abajo-derecha (tras la gracia). */
    if (Date.now() - START < GRACE_MS) return;
    var existing = document.getElementById(BTN_ID + '-float');
    if (!btn && !existing) {
      var fl = buildButton('-float', 'sv-navbtn--float');
      document.body.appendChild(fl);
    }
  }

  function schedule() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(function () {
      scheduled = false;
      ensure();
    });
  }

  function start() {
    ensure();
    var mo = new MutationObserver(schedule);
    mo.observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
