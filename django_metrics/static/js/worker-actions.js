/**
 * Confirmación para las acciones destructivas de la card "Pool vLLM"
 * (reiniciar/apagar un worker): un <dialog> compartido, NUNCA
 * window.confirm() -bloquea el hilo principal y rompe cualquier
 * automatización del navegador.
 *
 * Estos formularios NO llevan data-spinner-submit (spinner.js dispara con el
 * primer submit real; aquí el primer submit se intercepta para mostrar el
 * diálogo, así que el spinner de página se activa a mano tras confirmar).
 */
document.addEventListener("DOMContentLoaded", () => {
  const dialog = document.getElementById("worker-confirm-dialog");
  if (!dialog) return;

  const message = document.getElementById("worker-confirm-message");
  const acceptBtn = dialog.querySelector("[data-worker-confirm-accept]");
  const cancelBtn = dialog.querySelector("[data-worker-confirm-cancel]");
  let pendingForm = null;

  function showPageOverlay() {
    let overlay = document.querySelector(".l-loading-overlay--page");
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

  document.querySelectorAll("form[data-worker-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (form.dataset.workerConfirmed === "true") return;
      event.preventDefault();
      pendingForm = form;
      if (message) message.textContent = form.dataset.workerConfirm || "¿Confirmas esta acción?";
      dialog.showModal();
    });
  });

  if (acceptBtn) {
    acceptBtn.addEventListener("click", () => {
      if (!pendingForm) return;
      pendingForm.dataset.workerConfirmed = "true";
      showPageOverlay();
      pendingForm.requestSubmit();
      pendingForm = null;
    });
  }

  if (cancelBtn) {
    cancelBtn.addEventListener("click", () => {
      pendingForm = null;
      dialog.close();
    });
  }
});
