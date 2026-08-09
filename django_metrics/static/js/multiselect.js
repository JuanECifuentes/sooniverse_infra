/**
 * Comportamiento genérico de cierre para cualquier disclosure `.sv-disclosure`
 * (<details>/<summary>): incluye tanto `.sv-multiselect` (Modelo/API Key) como
 * `.sv-listbox` (Agrupación). Clic fuera o Escape cierran cualquier instancia
 * abierta. La apertura/cierre en sí es nativa del elemento <details>; este
 * script solo añade el cierre "fuera de foco" que <details> no ofrece.
 * Cargado globalmente desde base.html para estar disponible en cualquier
 * página que use estos componentes (dashboard, gestor de API Keys, etc.).
 */
document.addEventListener("click", (e) => {
  document.querySelectorAll(".sv-disclosure[open]").forEach((details) => {
    if (!details.contains(e.target)) details.removeAttribute("open");
  });
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    document.querySelectorAll(".sv-disclosure[open]").forEach((d) => d.removeAttribute("open"));
  }
});
