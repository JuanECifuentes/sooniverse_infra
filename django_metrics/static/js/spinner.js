/**
 * Componente de spinner en botones de formulario clásico (POST + navegación
 * completa, sin fetch). Marca un <form data-spinner-submit> y, al enviarlo:
 *   1. su botón de submit se deshabilita y muestra el spinner con el texto
 *      de `data-loading-text` (o el texto actual del botón);
 *   2. un overlay de página completa bloquea el resto de la interfaz
 *      (filtros, cards, otros formularios) hasta que la navegación real
 *      ocurra — igual que el overlay de carga asíncrona del dashboard, pero
 *      a nivel de página entera, porque aquí no hay fetch que "termine":
 *      el propio cambio de página lo hace desaparecer.
 * No intercepta el submit: el formulario sigue su curso normal.
 */
document.addEventListener("DOMContentLoaded", () => {
  let overlay = null;

  function showPageOverlay() {
    if (overlay) {
      overlay.hidden = false;
      return;
    }
    overlay = document.createElement("div");
    overlay.className = "l-loading-overlay l-loading-overlay--page";
    overlay.setAttribute("aria-hidden", "true");
    overlay.innerHTML = '<span class="sv-spinner sv-spinner--lg" aria-hidden="true"></span>';
    document.body.appendChild(overlay);
  }

  document.querySelectorAll("form[data-spinner-submit]").forEach((form) => {
    form.addEventListener("submit", () => {
      const btn = form.querySelector('button[type="submit"]');
      if (btn && !btn.disabled) {
        const texto = btn.dataset.loadingText || btn.textContent.trim();
        btn.disabled = true;
        btn.setAttribute("aria-busy", "true");
        btn.innerHTML = `<span class="sv-spinner" aria-hidden="true"></span> ${texto}`;
      }
      showPageOverlay();
    });
  });
});
