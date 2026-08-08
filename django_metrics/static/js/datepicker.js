/**
 * Inicializa flatpickr (vendorizado, sin CDN) sobre cualquier
 * input[data-flatpickr]. Progressive enhancement: el input real se queda
 * como <input type="date"> (funciona sin JS); flatpickr lo oculta y muestra
 * en su lugar un input alterno (altInput) con el mismo estilo `.sv-input`,
 * formateado en es-ES, mientras el input real conserva el valor ISO
 * (Y-m-d) que ya esperan metrics-filters.js y los formularios de Django.
 */
document.addEventListener("DOMContentLoaded", () => {
  if (!window.flatpickr) return;

  const locale = {
    weekdays: {
      shorthand: ["Do", "Lu", "Ma", "Mi", "Ju", "Vi", "Sá"],
      longhand: ["Domingo", "Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado"],
    },
    months: {
      shorthand: ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"],
      longhand: [
        "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
        "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
      ],
    },
    firstDayOfWeek: 1,
    rangeSeparator: " a ",
  };

  document.querySelectorAll("input[data-flatpickr]").forEach((el) => {
    window.flatpickr(el, {
      dateFormat: "Y-m-d",
      altInput: true,
      altFormat: "d/m/Y",
      altInputClass: "sv-input",
      locale,
      disableMobile: true,
      onChange: () => {
        el.dispatchEvent(new Event("change", { bubbles: true }));
      },
    });
  });
});
