/* ==============================================================================
 * SOONIVERSE :: Navegación Chat -> Métricas y Logout con Confirmación
 * ==============================================================================
 * Inyectado como <script> en index.html (ver Dockerfile). Complementa el
 * overlay de CSS (sooniverse.css), donde viven los estilos .sv-nav-actions,
 * .sv-iconbtn, .sv-tooltip y .sv-dialog.
 *
 * Ubicación preferente: ARRIBA A LA DERECHA, integrado (no flotante) en el
 * contenedor derecho del topbar del chat (Navbar.svelte de Open WebUI,
 * div.flex-none.items-center.self-center).
 *
 * Fallback: en páginas sin topbar del chat (workspace, admin, ...) los botones
 * aparecen FLOTANTES abajo a la derecha tras un periodo de gracia.
 *
 * Destinos:
 * - Métricas: window.__SOONIVERSE_PANEL_URL__ (default '/panel/').
 * - Logout: window.__SOONIVERSE_LOGOUT_URL__ (default '/panel/metrics/logout/').
 *
 * Al pulsar "Cerrar sesión", se abre un modal de confirmación antes de destruir
 * la sesión unificada de Django y redirigir al login del clúster.
 */
(function () {
  'use strict';

  var PANEL_URL =
    (typeof window.__SOONIVERSE_PANEL_URL__ === 'string' &&
      window.__SOONIVERSE_PANEL_URL__) ||
    '/panel/';

  var LOGOUT_URL =
    (typeof window.__SOONIVERSE_LOGOUT_URL__ === 'string' &&
      window.__SOONIVERSE_LOGOUT_URL__) ||
    ((PANEL_URL.replace(/\/?$/, '')) + '/metrics/logout/');

  var CONTAINER_ID = 'sooniverse-nav-actions';
  var BTN_METRICS_ID = 'sooniverse-navbtn-metrics';
  var BTN_LOGOUT_ID = 'sooniverse-navbtn-logout';
  var DIALOG_ID = 'sooniverse-logout-dialog';

  var GRACE_MS = 4000;
  var START = Date.now();
  var scheduled = false;

  /* Icono de métricas: gráfico de columnas (idéntico a panel) */
  var ICON_METRICS =
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"' +
    ' stroke="currentColor" stroke-width="1.8" stroke-linecap="round"' +
    ' stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M5 20v-6"/><path d="M12 20V4"/><path d="M19 20V10"/>' +
    '</svg>';

  /* Icono de logout: puerta con flecha de salida (idéntico a base.html) */
  var ICON_LOGOUT =
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"' +
    ' stroke="currentColor" stroke-width="1.8" stroke-linecap="round"' +
    ' stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>' +
    '<path d="m16 17 5-5-5-5"/>' +
    '<path d="M21 12H9"/>' +
    '</svg>';

  function getCookie(name) {
    var value = '; ' + document.cookie;
    var parts = value.split('; ' + name + '=');
    if (parts.length === 2) return parts.pop().split(';').shift();
    return null;
  }

  function performLogout() {
    try {
      localStorage.removeItem('token');
      localStorage.clear();
      sessionStorage.clear();
      document.cookie = 'token=; Path=/; Expires=Thu, 01 Jan 1970 00:00:01 GMT;';
    } catch (err) {
      console.warn('[sooniverse] Error limpiando estado local:', err);
    }

    var form = document.createElement('form');
    form.method = 'POST';
    form.action = LOGOUT_URL;
    form.style.display = 'none';

    var csrf = getCookie('csrftoken');
    if (csrf) {
      var csrfInput = document.createElement('input');
      csrfInput.type = 'hidden';
      csrfInput.name = 'csrfmiddlewaretoken';
      csrfInput.value = csrf;
      form.appendChild(csrfInput);
    }

    document.body.appendChild(form);
    form.submit();
  }

  function ensureLogoutDialog() {
    var dialog = document.getElementById(DIALOG_ID);
    if (dialog) return dialog;

    dialog = document.createElement('dialog');
    dialog.id = DIALOG_ID;
    dialog.className = 'sv-dialog sv-dialog--center';

    dialog.innerHTML =
      '<form method="dialog" class="sv-dialog__content">' +
        '<div class="sv-dialog__stack">' +
          '<span class="sv-eyebrow">Sesión</span>' +
          '<h3 class="sv-dialog__title">Cerrar sesión</h3>' +
          '<p class="sv-dialog__body">¿Estás seguro de que deseas cerrar sesión?</p>' +
        '</div>' +
        '<div class="sv-dialog__actions">' +
          '<button type="button" class="sv-btn sv-btn--ghost sv-btn--sm" id="sooniverse-logout-cancel">Cancelar</button>' +
          '<button type="button" class="sv-btn sv-btn--danger sv-btn--sm" id="sooniverse-logout-confirm">Cerrar sesión</button>' +
        '</div>' +
      '</form>';

    document.body.appendChild(dialog);

    var cancelBtn = dialog.querySelector('#sooniverse-logout-cancel');
    var confirmBtn = dialog.querySelector('#sooniverse-logout-confirm');

    cancelBtn.addEventListener('click', function () {
      dialog.close();
    });

    dialog.addEventListener('click', function (e) {
      if (e.target === dialog) dialog.close();
    });

    confirmBtn.addEventListener('click', function () {
      confirmBtn.disabled = true;
      confirmBtn.textContent = 'Cerrando sesión...';
      performLogout();
    });

    return dialog;
  }

  function openLogoutModal() {
    var dialog = ensureLogoutDialog();
    if (typeof dialog.showModal === 'function') {
      dialog.showModal();
    } else {
      if (window.confirm('¿Estás seguro de que deseas cerrar sesión?')) {
        performLogout();
      }
    }
  }

  function buildNavGroup(isFloat) {
    var group = document.createElement('div');
    group.id = isFloat ? CONTAINER_ID + '-float' : CONTAINER_ID;
    group.className = 'sv-nav-actions' + (isFloat ? ' sv-nav-actions--float' : '');

    // 1. Botón Métricas
    var metricsWrap = document.createElement('span');
    metricsWrap.className = 'sv-tooltip sv-tooltip--bottom-end sv-tooltip--nav';

    var metricsBtn = document.createElement('a');
    metricsBtn.id = BTN_METRICS_ID + (isFloat ? '-float' : '');
    metricsBtn.className = 'sv-iconbtn sv-navbtn';
    metricsBtn.href = PANEL_URL;
    metricsBtn.setAttribute('aria-label', 'Ir al panel de métricas');
    metricsBtn.innerHTML = ICON_METRICS;

    var metricsTip = document.createElement('span');
    metricsTip.className = 'sv-tooltip__bubble';
    metricsTip.setAttribute('role', 'tooltip');
    metricsTip.textContent = 'Ir al panel de métricas';

    metricsWrap.appendChild(metricsBtn);
    metricsWrap.appendChild(metricsTip);

    // 2. Botón Logout
    var logoutWrap = document.createElement('span');
    logoutWrap.className = 'sv-tooltip sv-tooltip--bottom-end sv-tooltip--nav';

    var logoutBtn = document.createElement('button');
    logoutBtn.type = 'button';
    logoutBtn.id = BTN_LOGOUT_ID + (isFloat ? '-float' : '');
    logoutBtn.className = 'sv-iconbtn sv-iconbtn--ghost sv-navbtn sv-navbtn--ghost';
    logoutBtn.setAttribute('aria-label', 'Cerrar sesión');
    logoutBtn.innerHTML = ICON_LOGOUT;
    logoutBtn.addEventListener('click', function (e) {
      e.preventDefault();
      openLogoutModal();
    });

    var logoutTip = document.createElement('span');
    logoutTip.className = 'sv-tooltip__bubble';
    logoutTip.setAttribute('role', 'tooltip');
    logoutTip.textContent = 'Cerrar sesión';

    logoutWrap.appendChild(logoutBtn);
    logoutWrap.appendChild(logoutTip);

    group.appendChild(metricsWrap);
    group.appendChild(logoutWrap);

    return group;
  }

  /* Contenedor derecho del topbar del chat (Navbar.svelte) */
  function findHost() {
    var nav = document.querySelector('nav.drag-region');
    if (!nav) return null;
    return nav.querySelector('.flex-none.items-center.self-center');
  }

  function ensure() {
    var container = document.getElementById(CONTAINER_ID);
    var host = findHost();

    if (host) {
      if (!container || container.parentElement !== host) {
        if (container) container.remove();
        host.appendChild(buildNavGroup(false));
        var float = document.getElementById(CONTAINER_ID + '-float');
        if (float) float.remove();
      }
      return;
    }

    /* Sin topbar del chat: fallback flotante abajo-derecha (tras la gracia) */
    if (Date.now() - START < GRACE_MS) return;
    var existing = document.getElementById(CONTAINER_ID + '-float');
    if (!container && !existing) {
      var fl = buildNavGroup(true);
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
    ensureLogoutDialog();
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
