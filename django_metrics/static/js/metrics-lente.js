/**
 * Orquestador de las tres lentes de la tarjeta de gráfica:
 * Serie (la de siempre) · Mapa semanal · Perfil horario.
 *
 * Se engancha como SEGUNDO oyente de "metrics:data" (el que emite
 * metrics-filters.js) y no muta `e.detail`. Consecuencia buscada:
 * metrics-charts.js sigue recibiendo su evento y funcionando exactamente igual
 * que antes, incluso con su panel oculto, y apikey-detail.js -que no carga este
 * módulo- deja la página de detalle de key intacta. Si mañana se borra este
 * archivo, el dashboard vuelve al comportamiento anterior sin tocar nada más.
 *
 * El mapa de calor y el perfil se piden a un endpoint APARTE (lente_api) y no
 * al de métricas: no dependen de page/sort/dir, que son los parámetros que más
 * se reenvían, y en modo p95 el mapa es la consulta más cara del panel.
 */

const panel = document.getElementById("metrics-panel");

if (panel) {
  const selector = document.getElementById("metrics-lente");
  const metricaSel = document.getElementById("heatmap-metrica");
  const titulo = document.getElementById("lente-titulo");
  const eyebrow = document.getElementById("chart-granularity-label");
  const overlay = document.getElementById("metrics-lente-loading");
  const errorBox = document.getElementById("metrics-lente-error");
  const paneles = Array.from(document.querySelectorAll("[data-lente-panel]"));
  const url = panel.dataset.lenteUrl;

  const TITULOS = {
    serie: "Evolución del consumo",
    heatmap: "Actividad por día y hora",
    perfil: "Perfil de carga horario",
  };
  const EYEBROWS = {
    heatmap: "Hora local · 7 días × 24 h",
    perfil: "Mediana y pico por hora del día",
  };

  let lenteActiva = "serie";
  let ultimosParams = null;
  // Caché por (filtros, lente, métrica): cambiar de lente y volver no repite
  // la consulta cara.
  const cache = new Map();
  let peticion = null;

  function claveCache() {
    const metrica = metricaSel ? metricaSel.value : "peticiones";
    return `${ultimosParams || ""}|${lenteActiva}|${metrica}`;
  }

  function emitir(lente, datos) {
    panel.dispatchEvent(new CustomEvent("metrics:lente", { detail: { lente, datos } }));
  }

  async function cargarLente() {
    if (lenteActiva === "serie" || !url) return;

    if (ultimosParams === null) {
      // El evento inicial de metrics-filters.js pudo dispararse antes de que
      // este módulo registrara su listener (el orden de los <script
      // type="module"> no lo garantiza). Respaldo directo vía
      // panel.getParamsLente, expuesto justo para este caso: sin él, la
      // primera vez que alguien entra a la página y hace clic en una lente
      // -antes de tocar cualquier filtro- no pasaba nada.
      if (typeof panel.getParamsLente !== "function") return;
      ultimosParams = panel.getParamsLente();
    }

    const clave = claveCache();
    if (cache.has(clave)) {
      emitir(lenteActiva, cache.get(clave));
      return;
    }

    const params = new URLSearchParams(ultimosParams);
    params.set("lente", lenteActiva);
    if (metricaSel) params.set("metrica", metricaSel.value);

    if (peticion) peticion.abort();
    peticion = new AbortController();
    if (overlay) overlay.hidden = false;
    if (errorBox) errorBox.hidden = true;

    try {
      const resp = await fetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded",
          "X-CSRFToken": panel.dataset.csrf || "",
        },
        body: params.toString(),
        signal: peticion.signal,
      });
      const cuerpo = await resp.json();
      if (!resp.ok) {
        if (errorBox) {
          errorBox.textContent = cuerpo.error || "No se pudo cargar la vista.";
          errorBox.hidden = false;
        }
        return;
      }
      cache.set(clave, cuerpo.datos);
      emitir(cuerpo.lente, cuerpo.datos);
    } catch (err) {
      if (err.name === "AbortError") return;
      if (errorBox) {
        errorBox.textContent = "No se pudo contactar con el servidor.";
        errorBox.hidden = false;
      }
    } finally {
      if (overlay) overlay.hidden = true;
      peticion = null;
    }
  }

  function activar(lente, desdeTeclado = false) {
    lenteActiva = lente;

    selector.querySelectorAll("[data-lente]").forEach((btn) => {
      const activo = btn.dataset.lente === lente;
      btn.classList.toggle("sv-segment__item--active", activo);
      btn.setAttribute("aria-pressed", activo ? "true" : "false");
    });

    paneles.forEach((p) => { p.hidden = p.dataset.lentePanel !== lente; });

    if (titulo) titulo.textContent = TITULOS[lente] || TITULOS.serie;
    if (eyebrow && EYEBROWS[lente]) eyebrow.textContent = EYEBROWS[lente];

    // El canvas oculto medía 0x0: hay que avisar en el mismo tick del cambio
    // de `hidden` para que la gráfica se redimensione al mostrarse.
    panel.dispatchEvent(new CustomEvent("metrics:lente-visible", { detail: { lente } }));

    if (desdeTeclado) {
      const activo = paneles.find((p) => p.dataset.lentePanel === lente);
      if (activo) activo.focus({ preventScroll: true });
    }
    cargarLente();
  }

  if (selector) {
    selector.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-lente]");
      if (btn) activar(btn.dataset.lente);
    });
    selector.addEventListener("keyup", (e) => {
      const btn = e.target.closest("[data-lente]");
      if (btn && (e.key === "Enter" || e.key === " ")) activar(btn.dataset.lente, true);
    });
  }

  if (metricaSel) {
    metricaSel.addEventListener("change", () => cargarLente());
  }

  // Los filtros cambiaron: las lentes cacheadas dejan de ser válidas.
  panel.addEventListener("metrics:data", (e) => {
    ultimosParams = e.detail && e.detail.__params ? e.detail.__params : ultimosParams;
    cache.clear();
    cargarLente();
  });

  panel.addEventListener("metrics:params", (e) => {
    ultimosParams = e.detail;
    cache.clear();
    cargarLente();
  });
}
