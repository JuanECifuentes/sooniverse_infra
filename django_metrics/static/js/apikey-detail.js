/**
 * Filtrado asíncrono de la página de detalle de una API Key: agrupación
 * (Diario/Semanal/Mensual) + rango Desde/Hasta, antes recargaban la página
 * completa vía querystring. Ahora disparan un fetch POST a metrics_api (el
 * mismo endpoint del dashboard) fijando `api_key` a esta credencial, y
 * actualizan KPIs + tabla in-place; el gráfico lo actualiza metrics-charts.js
 * escuchando el mismo evento "metrics:data" que usa el dashboard (comparten
 * módulo: solo necesitan los mismos ids de elementos, que este template
 * replica). El filtrado nunca queda reflejado en la URL: por eso metrics_api
 * es POST y no GET.
 */
const panel = document.getElementById("metrics-panel");

if (panel && panel.dataset.apiKeyId) {
  const API_URL = panel.dataset.apiUrl;
  const API_KEY_ID = panel.dataset.apiKeyId;

  const els = {
    granularity: document.getElementById("akd-granularity"),
    granularityLabel: document.getElementById("akd-granularity-label"),
    desde: document.getElementById("akd-desde"),
    hasta: document.getElementById("akd-hasta"),
    dateError: document.getElementById("akd-date-error"),
    rango: document.getElementById("akd-rango"),
    loading: document.getElementById("akd-loading"),
    errorBanner: document.getElementById("akd-error-banner"),
    errorMessage: document.getElementById("akd-error-message"),
    retryBtn: document.getElementById("akd-retry"),
    live: document.getElementById("akd-live"),
    tablaModelo: document.getElementById("akd-tabla-modelo-body"),
  };

  function getCsrfToken() {
    const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  const fmtInt = (n) => Number(n || 0).toLocaleString("es-ES");

  function fmtCompacto(n, divisor, sufijo) {
    let texto = (n / divisor).toFixed(1);
    if (texto.endsWith(".0")) texto = texto.slice(0, -2);
    return `${texto.replace(".", ",")}${sufijo}`;
  }
  function fmtTok(n) {
    n = Number(n || 0);
    if (Math.abs(n) >= 1_000_000) return fmtCompacto(n, 1_000_000, "M");
    if (Math.abs(n) >= 100_000) return fmtCompacto(n, 1_000, "K");
    return fmtInt(n);
  }

  function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  const state = {
    granularity: els.granularity.querySelector(".sv-listbox__option--active")?.dataset.value || "daily",
    desde: els.desde.value,
    hasta: els.hasta.value,
  };
  let requestCounter = 0;
  let activeController = null;

  function setField(name, value) {
    document.querySelectorAll(`[data-field="${name}"]`).forEach((el) => {
      el.textContent = value;
    });
  }

  function updateGranularityHighlight() {
    const etiqueta = els.granularity.querySelector(`[data-value="${state.granularity}"]`)?.textContent.trim();
    els.granularity.querySelectorAll("[data-value]").forEach((btn) => {
      const activo = btn.dataset.value === state.granularity;
      btn.classList.toggle("sv-listbox__option--active", activo);
      btn.setAttribute("aria-selected", activo ? "true" : "false");
    });
    const label = els.granularity.querySelector("[data-listbox-label]");
    if (label && etiqueta) label.textContent = etiqueta;
  }

  function renderTablaModelo(rows) {
    if (!rows.length) {
      els.tablaModelo.innerHTML = '<tr><td colspan="5" class="sv-help">Sin consumo registrado.</td></tr>';
      return;
    }
    els.tablaModelo.innerHTML = rows
      .map(
        (r) => `<tr>
          <td class="sv-mono l-cell--modelo">${escapeHtml(r.model_name)}</td>
          <td class="sv-num">${fmtInt(r.request_count)}</td>
          <td class="sv-num">${fmtTok(r.prompt_tokens)}</td>
          <td class="sv-num">${fmtTok(r.completion_tokens)}</td>
          <td class="sv-num sv-num--accent">${fmtTok(r.total_tokens)}</td>
        </tr>`
      )
      .join("");
  }

  function render(data) {
    panel.dispatchEvent(new CustomEvent("metrics:data", { detail: data }));

    setField("total_tokens", fmtTok(data.summary.total_tokens));
    setField("prompt_tokens", fmtTok(data.summary.prompt_tokens));
    setField("completion_tokens", fmtTok(data.summary.completion_tokens));
    setField("ratio_completion", data.summary.ratio_completion);
    setField("request_count", fmtInt(data.summary.request_count));
    setField("tokens_por_request", fmtTok(data.summary.tokens_por_request));
    setField("tasa_error", data.summary.tasa_error);
    els.rango.textContent = `${data.desde} → ${data.hasta}`;
    els.granularityLabel.textContent = `Serie ${data.granularity_label.toLowerCase()}`;
    renderTablaModelo(data.por_modelo);
  }

  async function cargar() {
    if (state.desde && state.hasta && state.desde > state.hasta) {
      els.dateError.hidden = false;
      els.dateError.textContent = 'La fecha "desde" no puede ser posterior a "hasta".';
      els.hasta.setAttribute("aria-invalid", "true");
      return;
    }
    els.dateError.hidden = true;
    els.hasta.removeAttribute("aria-invalid");

    updateGranularityHighlight();

    if (activeController) activeController.abort();
    const controller = new AbortController();
    activeController = controller;
    const myRequestId = ++requestCounter;

    let overlayShown = false;
    const showTimer = setTimeout(() => {
      overlayShown = true;
      els.loading.hidden = false;
      panel.setAttribute("aria-busy", "true");
      panel.inert = true;
      els.live.textContent = "Cargando datos";
    }, 150);
    const startedAt = performance.now();

    const params = new URLSearchParams({ granularity: state.granularity, api_key: API_KEY_ID });
    if (state.desde) params.set("desde", state.desde);
    if (state.hasta) params.set("hasta", state.hasta);

    try {
      const res = await fetch(API_URL, {
        method: "POST",
        headers: { "X-CSRFToken": getCsrfToken() },
        body: params,
        signal: controller.signal,
      });
      const body = await res.json().catch(() => null);
      if (myRequestId !== requestCounter) return;
      if (!res.ok) throw new Error((body && body.error) || `Error ${res.status} al cargar métricas.`);
      render(body);
      els.errorBanner.hidden = true;
      els.live.textContent = "Datos actualizados";
    } catch (err) {
      if (err.name === "AbortError" || myRequestId !== requestCounter) return;
      els.errorBanner.hidden = false;
      els.errorMessage.textContent = err.message || "No se pudo cargar la serie.";
      els.live.textContent = "No se pudieron cargar los datos";
    } finally {
      if (myRequestId === requestCounter) {
        clearTimeout(showTimer);
        const finish = () => {
          els.loading.hidden = true;
          panel.setAttribute("aria-busy", "false");
          panel.inert = false;
        };
        if (overlayShown) {
          setTimeout(finish, Math.max(0, 300 - (performance.now() - startedAt)));
        } else {
          finish();
        }
      }
    }
  }

  els.granularity.querySelector(".sv-listbox__panel").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-value]");
    if (!btn) return;
    state.granularity = btn.dataset.value;
    els.granularity.removeAttribute("open");
    cargar();
  });

  [els.desde, els.hasta].forEach((input) => {
    input.addEventListener("change", () => {
      state.desde = els.desde.value;
      state.hasta = els.hasta.value;
      cargar();
    });
  });

  els.retryBtn.addEventListener("click", cargar);
}
