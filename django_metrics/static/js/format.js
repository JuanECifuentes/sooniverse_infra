/**
 * Helpers de formato compartidos por todo el JS del panel.
 *
 * Existe para que `fmtTok` deje de estar copiado en metrics-filters.js,
 * metrics-charts.js y apikey-detail.js: tres copias que había que mantener
 * sincronizadas a mano y con el helper de servidor.
 *
 * ESPEJO DE `metrics/templatetags/metrics_extras.py::human_tokens`.
 * Las dos implementaciones tienen que dar el mismo resultado y no pueden
 * compartir código (una es Python de servidor y otra JS de cliente). Si cambias
 * los umbrales en una, cambia la otra: la tabla de casos canónica está en
 * `metrics/tests/test_formato.py`, que además vigila este archivo como texto.
 */

const LOCALE = "es-ES";

export const fmtInt = (n) => Number(n || 0).toLocaleString(LOCALE);

function fmtCompacto(n, divisor, sufijo) {
  let texto = (n / divisor).toFixed(1);
  if (texto.endsWith(".0")) texto = texto.slice(0, -2);
  return `${texto.replace(".", ",")}${sufijo}`;
}

/**
 * Notación compacta SOLO para contadores de tokens. Los conteos de peticiones y
 * los costes en USD nunca la usan (regla del manual de imagen, §2.1).
 * >= 1.000.000 -> "2,5M"; >= 100.000 -> "125K"; por debajo, separador de miles.
 */
export function fmtTok(n) {
  n = Number(n || 0);
  if (Math.abs(n) >= 1_000_000) return fmtCompacto(n, 1_000_000, "M");
  if (Math.abs(n) >= 100_000) return fmtCompacto(n, 1_000, "K");
  return fmtInt(n);
}

/** USD siempre con 4 decimales (manual §2.1). */
export function fmtUsd(n) {
  return `$${Number(n || 0).toLocaleString(LOCALE, {
    minimumFractionDigits: 4, maximumFractionDigits: 4,
  })}`;
}

/** Milisegundos: por encima de 10 s se pasa a segundos para que quepa. */
export function fmtMs(n) {
  if (n === null || n === undefined) return "—";
  const v = Number(n);
  if (v >= 10_000) return `${(v / 1000).toFixed(1).replace(".", ",")} s`;
  return `${fmtInt(Math.round(v))} ms`;
}

export function fmtPct(n, decimales = 1) {
  if (n === null || n === undefined) return "—";
  return `${Number(n).toFixed(decimales).replace(".", ",")} %`;
}

export function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/** Lee un token de color del tema en tiempo de ejecución.
 *  Regla dura del manual: ningún color hardcodeado en JS. */
export const cssVar = (name) =>
  getComputedStyle(document.documentElement).getPropertyValue(name).trim();

/** Chart.js necesita rgba() para transparencias; los tokens son hex sólidos. */
export function hexToRgba(hex, alpha) {
  const limpio = String(hex).replace("#", "").trim();
  const valor = parseInt(
    limpio.length === 3 ? limpio.split("").map((c) => c + c).join("") : limpio,
    16
  );
  const r = (valor >> 16) & 255, g = (valor >> 8) & 255, b = valor & 255;
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

/**
 * La regla `prefers-reduced-motion` de theme-sooniverse.css neutraliza las
 * transiciones CSS, pero NO alcanza a Chart.js, que anima por JS. Toda gráfica
 * del panel tiene que pasar `animation: SIN_MOVIMIENTO ? false : undefined`.
 */
export const SIN_MOVIMIENTO =
  window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

export const DIAS_ISO = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"];

export const horaLabel = (h) => `${String(h).padStart(2, "0")}h`;
