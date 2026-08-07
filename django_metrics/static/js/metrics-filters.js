/**
 * Filtrado asíncrono del panel de métricas. Sin dependencias, sin build step.
 * Dueño único del estado de filtros; el DOM se deriva de `state`.
 */

const panel = document.getElementById("metrics-panel");
if (panel) {
  const API_URL = panel.dataset.apiUrl;
  const DASHBOARD_URL = panel.dataset.dashboardUrl;

  const els = {
    granularity: document.getElementById("id_granularity"),
    desde: document.getElementById("id_desde"),
    hasta: document.getElementById("id_hasta"),
    dateError: document.getElementById("metrics-date-error"),
    chips: document.getElementById("metrics-chips"),
    loading: document.getElementById("metrics-loading"),
    errorBanner: document.getElementById("metrics-error-banner"),
    errorMessage: document.getElementById("metrics-error-message"),
    retryBtn: document.getElementById("metrics-retry"),
    emptyBanner: document.getElementById("metrics-empty-banner"),
    expandBtn: document.getElementById("metrics-expand-range"),
    content: document.getElementById("metrics-content"),
    live: document.getElementById("metrics-live"),
    chartWrap: document.getElementById("metrics-chart-wrap"),
    chartGranularityLabel: document.getElementById("chart-granularity-label"),
    tablaModelo: document.getElementById("tabla-modelo-body"),
    tablaApiKey: document.getElementById("tabla-apikey-body"),
    cardApiKey: document.getElementById("card-desglose-apikey"),
    presets: document.getElementById("metrics-presets"),
    badgeConError: document.getElementById("badge-con-error"),
    badgeSinError: document.getElementById("badge-sin-error"),
  };

  const multiselects = {
    modelo: document.querySelector('[data-filter-group="modelo"]'),
    api_key: document.querySelector('[data-filter-group="api_key"]'),
  };

  const fmtInt = (n) => Number(n || 0).toLocaleString("es-ES");
  const fmtUsd = (n) =>
    Number(n || 0).toLocaleString("es-ES", { minimumFractionDigits: 4, maximumFractionDigits: 4 });

  function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function readCheckedValues(group) {
    return [...multiselects[group].querySelectorAll("input[type=checkbox]:checked")].map((cb) => cb.value);
  }

  function findCheckbox(group, value) {
    return [...multiselects[group].querySelectorAll("input[type=checkbox]")].find((cb) => cb.value === value);
  }

  function labelFor(group, value) {
    const cb = findCheckbox(group, value);
    const label = cb && cb.closest("label");
    return label ? label.textContent.trim() : value;
  }

  const state = {
    granularity: els.granularity.value,
    modelo: readCheckedValues("modelo"),
    api_key: readCheckedValues("api_key"),
    desde: els.desde.value,
    hasta: els.hasta.value,
  };

  let requestCounter = 0;
  let activeController = null;

  function buildParams(s) {
    const p = new URLSearchParams();
    p.set("granularity", s.granularity);
    if (s.desde) p.set("desde", s.desde);
    if (s.hasta) p.set("hasta", s.hasta);
    s.modelo.forEach((m) => p.append("modelo", m));
    s.api_key.forEach((k) => p.append("api_key", k));
    return p;
  }

  function syncUrl(s) {
    history.replaceState(null, "", `${DASHBOARD_URL}?${buildParams(s).toString()}`);
  }

  function announce(msg) {
    els.live.textContent = msg;
  }

  function setField(name, value) {
    document.querySelectorAll(`[data-field="${name}"]`).forEach((el) => {
      el.textContent = value;
    });
  }

  function updateChips() {
    const items = [
      ...state.modelo.map((v) => ({ group: "modelo", value: v, label: labelFor("modelo", v) })),
      ...state.api_key.map((v) => ({ group: "api_key", value: v, label: labelFor("api_key", v) })),
    ];
    els.chips.innerHTML = items
      .map(
        (it) => `<span class="sv-chip">${escapeHtml(it.label)}
          <button type="button" class="sv-chip__remove" data-remove-group="${it.group}"
                  data-remove-value="${escapeHtml(it.value)}" aria-label="Quitar ${escapeHtml(it.label)}">×</button>
        </span>`
      )
      .join("");

    document.querySelectorAll("[data-count-for]").forEach((el) => {
      const count = state[el.dataset.countFor].length;
      el.hidden = count === 0;
      el.textContent = count || "";
    });
  }

  function addDays(date, days) {
    const d = new Date(date);
    d.setDate(d.getDate() + days);
    return d;
  }
  const isoDate = (d) => d.toISOString().slice(0, 10);

  function computePreset(preset) {
    const today = new Date();
    if (preset === "7d") return { desde: isoDate(addDays(today, -7)), hasta: isoDate(today) };
    if (preset === "30d") return { desde: isoDate(addDays(today, -30)), hasta: isoDate(today) };
    if (preset === "90d") return { desde: isoDate(addDays(today, -90)), hasta: isoDate(today) };
    if (preset === "mes_actual") {
      const first = new Date(today.getFullYear(), today.getMonth(), 1);
      return { desde: isoDate(first), hasta: isoDate(today) };
    }
    return null;
  }

  function matchPreset(desde, hasta) {
    if (hasta !== isoDate(new Date())) return null;
    for (const p of ["7d", "30d", "90d", "mes_actual"]) {
      if (computePreset(p).desde === desde) return p;
    }
    return null;
  }

  function updatePresetHighlight() {
    const active = matchPreset(state.desde, state.hasta);
    els.presets.querySelectorAll("[data-preset]").forEach((btn) => {
      btn.classList.toggle("sv-segment__item--active", btn.dataset.preset === active);
    });
  }

  function renderChart(data) {
    const { labels, total_tokens, prompt_tokens, request_count, altura_pct } = data.series;
    if (!labels.length) {
      els.chartWrap.innerHTML = `
        <div class="l-empty">
          <span class="sv-eyebrow">Sin datos</span>
          <p class="sv-body">No hay consumo registrado en esta ventana.<br>
             Ejecuta el refresco de métricas o genera tráfico a través del gateway.</p>
        </div>`;
      return;
    }

    const cols = labels
      .map((etiqueta, i) => {
        const totalTok = total_tokens[i];
        const promptTok = prompt_tokens[i];
        const promptPct = totalTok ? Math.round((promptTok / totalTok) * 10000) / 100 : 0;
        const title = `${escapeHtml(etiqueta)} — ${fmtInt(totalTok)} tokens (${fmtInt(request_count[i])} peticiones)`;
        return `
          <div class="l-chart-col">
            <div class="l-bar-track">
              <span class="sv-bar${totalTok ? "" : " sv-bar--empty"}" style="height: ${altura_pct[i]}%" title="${title}">
                ${totalTok ? `<span class="sv-bar__prompt" style="height: ${promptPct}%"></span>` : ""}
              </span>
            </div>
            <span class="sv-axis">${escapeHtml(etiqueta)}</span>
          </div>`;
      })
      .join("");

    els.chartWrap.innerHTML = `
      <div class="sv-chart" role="img"
           aria-label="Consumo de tokens ${escapeHtml(data.granularity_label.toLowerCase())} entre ${data.desde} y ${data.hasta}">
        ${cols}
      </div>
      <div class="l-legend l-mt-lg">
        <span class="l-legend__item">
          <span class="sv-legend__swatch sv-legend__swatch--total"></span>
          <span class="sv-mono">Tokens totales</span>
        </span>
        <span class="l-legend__item">
          <span class="sv-legend__swatch sv-legend__swatch--prompt"></span>
          <span class="sv-mono">Prompt (entrada)</span>
        </span>
        <span class="l-spacer"></span>
        <span class="sv-mono sv-muted">${labels.length} periodo(s) · ${data.desde} → ${data.hasta}</span>
      </div>`;
  }

  function renderTable(tbody, rows, kind) {
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="4" class="sv-help">Sin datos en la ventana seleccionada.</td></tr>';
      return;
    }
    tbody.innerHTML = rows
      .map((r) => {
        if (kind === "modelo") {
          return `<tr>
            <td class="sv-mono">${escapeHtml(r.model_name)}</td>
            <td class="sv-num">${fmtInt(r.request_count)}</td>
            <td class="sv-num sv-num--accent">${fmtInt(r.total_tokens)}</td>
            <td class="sv-num">$${fmtUsd(r.spend_usd)}</td>
          </tr>`;
        }
        const alias = r.api_key__key_alias ? escapeHtml(r.api_key__key_alias) : "(sin registro)";
        const inactiva = r.api_key__is_active === false
          ? ' <span class="sv-badge sv-badge--muted">inactiva</span>' : "";
        return `<tr>
          <td><span class="sv-strong">${alias}</span>${inactiva}</td>
          <td class="sv-num">${fmtInt(r.request_count)}</td>
          <td class="sv-num sv-num--accent">${fmtInt(r.total_tokens)}</td>
          <td class="sv-num">$${fmtUsd(r.spend_usd)}</td>
        </tr>`;
      })
      .join("");
  }

  function render(data) {
    const isEmpty = data.summary.request_count === 0 && data.series.labels.length === 0;
    els.emptyBanner.hidden = !isEmpty;
    els.content.hidden = isEmpty;
    if (isEmpty) return;

    setField("total_tokens", fmtInt(data.summary.total_tokens));
    setField("tokens_por_request", fmtInt(data.summary.tokens_por_request));
    setField("prompt_tokens", fmtInt(data.summary.prompt_tokens));
    setField("completion_tokens", fmtInt(data.summary.completion_tokens));
    setField("ratio_completion", data.summary.ratio_completion);
    setField("request_count", fmtInt(data.summary.request_count));
    setField("error_count", fmtInt(data.summary.error_count));
    setField("spend_usd", fmtUsd(data.summary.spend_usd));
    els.badgeConError.hidden = data.summary.error_count === 0;
    els.badgeSinError.hidden = data.summary.error_count !== 0;

    els.chartGranularityLabel.textContent = `Serie ${data.granularity_label.toLowerCase()}`;
    renderChart(data);

    renderTable(els.tablaModelo, data.por_modelo, "modelo");
    const mostrarApiKey = data.mostrar_desglose_api_key && data.por_api_key.length > 0;
    els.cardApiKey.hidden = !mostrarApiKey;
    if (mostrarApiKey) renderTable(els.tablaApiKey, data.por_api_key, "api_key");
  }

  async function applyFilters() {
    if (state.desde && state.hasta && state.desde > state.hasta) {
      els.dateError.hidden = false;
      els.dateError.textContent = 'La fecha "desde" no puede ser posterior a "hasta".';
      els.hasta.setAttribute("aria-invalid", "true");
      return;
    }
    els.dateError.hidden = true;
    els.hasta.removeAttribute("aria-invalid");

    syncUrl(state);
    updateChips();
    updatePresetHighlight();

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
      announce("Cargando datos");
    }, 150);
    const startedAt = performance.now();

    try {
      const res = await fetch(`${API_URL}?${buildParams(state).toString()}`, { signal: controller.signal });
      const body = await res.json().catch(() => null);
      if (myRequestId !== requestCounter) return;
      if (!res.ok) throw new Error((body && body.error) || `Error ${res.status} al cargar métricas.`);
      render(body);
      els.errorBanner.hidden = true;
      announce("Datos actualizados");
    } catch (err) {
      if (err.name === "AbortError" || myRequestId !== requestCounter) return;
      els.errorBanner.hidden = false;
      els.errorMessage.textContent = err.message || "No se pudo cargar la serie.";
      announce("No se pudieron cargar los datos");
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

  els.granularity.addEventListener("change", () => {
    state.granularity = els.granularity.value;
    applyFilters();
  });

  [els.desde, els.hasta].forEach((input) => {
    input.addEventListener("change", () => {
      state.desde = els.desde.value;
      state.hasta = els.hasta.value;
      applyFilters();
    });
  });

  Object.entries(multiselects).forEach(([group, details]) => {
    details.querySelector(".sv-multiselect__panel").addEventListener("change", (e) => {
      if (e.target.matches('input[type="checkbox"]')) {
        state[group] = readCheckedValues(group);
        applyFilters();
      }
    });
  });

  els.chips.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-remove-group]");
    if (!btn) return;
    const { removeGroup: group, removeValue: value } = btn.dataset;
    const cb = findCheckbox(group, value);
    if (cb) cb.checked = false;
    state[group] = state[group].filter((v) => v !== value);
    applyFilters();
  });

  els.presets.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-preset]");
    if (!btn) return;
    const range = computePreset(btn.dataset.preset);
    if (!range) return;
    state.desde = range.desde;
    state.hasta = range.hasta;
    els.desde.value = range.desde;
    els.hasta.value = range.hasta;
    applyFilters();
  });

  els.retryBtn.addEventListener("click", applyFilters);

  els.expandBtn.addEventListener("click", () => {
    const today = new Date();
    state.desde = isoDate(addDays(today, -90));
    state.hasta = isoDate(today);
    els.desde.value = state.desde;
    els.hasta.value = state.hasta;
    applyFilters();
  });

  document.addEventListener("click", (e) => {
    document.querySelectorAll(".sv-multiselect[open]").forEach((details) => {
      if (!details.contains(e.target)) details.removeAttribute("open");
    });
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      document.querySelectorAll(".sv-multiselect[open]").forEach((d) => d.removeAttribute("open"));
    }
  });

  updateChips();
  updatePresetHighlight();
}
