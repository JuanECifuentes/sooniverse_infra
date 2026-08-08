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
    chartGranularityLabel: document.getElementById("chart-granularity-label"),
    tablaModelo: document.getElementById("tabla-modelo-body"),
    tablaApiKey: document.getElementById("tabla-apikey-body"),
    cardApiKey: document.getElementById("card-desglose-apikey"),
    presets: document.getElementById("metrics-presets"),
    badgeConError: document.getElementById("badge-con-error"),
    badgeSinError: document.getElementById("badge-sin-error"),
    tablaPeticiones: document.getElementById("tabla-peticiones-body"),
    peticionesTotal: document.querySelector('[data-field="peticiones-total"]'),
    peticionesPrev: document.getElementById("peticiones-prev"),
    peticionesNext: document.getElementById("peticiones-next"),
    peticionesPageLabel: document.getElementById("peticiones-page-label"),
    sortButtons: document.querySelectorAll(".l-sort-btn"),
  };

  const multiselects = {
    modelo: document.querySelector('[data-filter-group="modelo"]'),
    api_key: document.querySelector('[data-filter-group="api_key"]'),
  };

  const fmtInt = (n) => Number(n || 0).toLocaleString("es-ES");
  const fmtUsd = (n) =>
    Number(n || 0).toLocaleString("es-ES", { minimumFractionDigits: 4, maximumFractionDigits: 4 });

  // Los presets fijan la fecha programáticamente; si flatpickr ya tomó el
  // control del input (ver datepicker.js), asignar `.value` directo no
  // actualiza su calendario/altInput visible y quedan desincronizados.
  // triggerChange:false porque el llamador ya dispara su propio applyFilters().
  function setDateValue(el, value) {
    if (el._flatpickr) el._flatpickr.setDate(value, false);
    else el.value = value;
  }

  // Contadores de tokens (no peticiones/costes): igual que human_tokens del
  // servidor. >=1.000.000 -> "2,5M"; >=100.000 -> "125K"; si no, separador de miles.
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
    page: 1,
    sort: "fecha",
    dir: "desc",
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
    p.set("page", s.page);
    p.set("sort", s.sort);
    p.set("dir", s.dir);
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
            <td class="sv-num sv-num--accent">${fmtTok(r.prompt_tokens)}</td>
            <td class="sv-num sv-num--accent">${fmtTok(r.completion_tokens)}</td>
          </tr>`;
        }
        const alias = r.api_key__key_alias ? escapeHtml(r.api_key__key_alias) : "(sin registro)";
        const inactiva = r.api_key__is_active === false
          ? ' <span class="sv-badge sv-badge--muted">inactiva</span>' : "";
        return `<tr>
          <td><span class="sv-strong">${alias}</span>${inactiva}</td>
          <td class="sv-num">${fmtInt(r.request_count)}</td>
          <td class="sv-num sv-num--accent">${fmtTok(r.prompt_tokens)}</td>
          <td class="sv-num sv-num--accent">${fmtTok(r.completion_tokens)}</td>
        </tr>`;
      })
      .join("");
  }

  function render(data) {
    panel.dispatchEvent(new CustomEvent("metrics:data", { detail: data }));

    const isEmpty = data.summary.request_count === 0 && data.series.labels.length === 0;
    els.emptyBanner.hidden = !isEmpty;
    els.content.hidden = isEmpty;
    if (isEmpty) return;

    setField("total_tokens", fmtTok(data.summary.total_tokens));
    setField("tokens_por_request", fmtTok(data.summary.tokens_por_request));
    setField("prompt_tokens", fmtTok(data.summary.prompt_tokens));
    setField("completion_tokens", fmtTok(data.summary.completion_tokens));
    setField("ratio_completion", data.summary.ratio_completion);
    setField("request_count", fmtInt(data.summary.request_count));
    setField("error_count", fmtInt(data.summary.error_count));
    setField("spend_usd", fmtUsd(data.summary.spend_usd));
    els.badgeConError.hidden = data.summary.error_count === 0;
    els.badgeSinError.hidden = data.summary.error_count !== 0;

    els.chartGranularityLabel.textContent = `Serie ${data.granularity_label.toLowerCase()}`;

    renderTable(els.tablaModelo, data.por_modelo, "modelo");
    const mostrarApiKey = data.mostrar_desglose_api_key && data.por_api_key.length > 0;
    els.cardApiKey.hidden = !mostrarApiKey;
    if (mostrarApiKey) renderTable(els.tablaApiKey, data.por_api_key, "api_key");

    if (data.requests) renderPeticiones(data.requests);
  }

  function renderPeticiones(requests) {
    if (els.peticionesTotal) els.peticionesTotal.textContent = fmtInt(requests.total);
    if (els.peticionesPageLabel) els.peticionesPageLabel.textContent = `Página ${requests.page} de ${requests.total_pages}`;
    if (els.peticionesPrev) els.peticionesPrev.disabled = !requests.has_prev;
    if (els.peticionesNext) els.peticionesNext.disabled = !requests.has_next;

    els.sortButtons.forEach((btn) => {
      const arrow = btn.querySelector("[data-sort-arrow]");
      if (!arrow) return;
      const active = btn.dataset.sort === state.sort;
      arrow.dataset.active = active ? "true" : "false";
      if (active) arrow.dataset.dir = state.dir;
      else delete arrow.dataset.dir;
    });

    if (!els.tablaPeticiones) return;
    if (!requests.items.length) {
      els.tablaPeticiones.innerHTML =
        '<tr><td colspan="5" class="sv-help">Sin peticiones en la ventana seleccionada.</td></tr>';
      return;
    }
    els.tablaPeticiones.innerHTML = requests.items
      .map((r) => {
        const fecha = new Date(r.ts);
        const corta = fecha.toLocaleDateString("es-ES", { day: "2-digit", month: "short" }) +
          ", " + fecha.toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit" });
        const apiKey = escapeHtml(r.api_key);
        return `<tr>
          <td data-label="Fecha"><time datetime="${r.ts}">${corta}</time></td>
          <td data-label="Modelo" class="sv-mono">${escapeHtml(r.model)}</td>
          <td data-label="Entrada" class="sv-num">${fmtTok(r.input)}</td>
          <td data-label="Salida" class="sv-num">${fmtTok(r.output)}</td>
          <td data-label="Api Key">
            <span class="sv-mono">${apiKey}</span>
            <button type="button" class="l-copy-btn" data-copy="${apiKey}" aria-label="Copiar API Key">⧉</button>
          </td>
        </tr>`;
      })
      .join("");
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
    state.page = 1;
    applyFilters();
  });

  [els.desde, els.hasta].forEach((input) => {
    input.addEventListener("change", () => {
      state.desde = els.desde.value;
      state.hasta = els.hasta.value;
      state.page = 1;
      applyFilters();
    });
  });

  Object.entries(multiselects).forEach(([group, details]) => {
    details.querySelector(".sv-multiselect__panel").addEventListener("change", (e) => {
      if (e.target.matches('input[type="checkbox"]')) {
        state[group] = readCheckedValues(group);
        state.page = 1;
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
    state.page = 1;
    applyFilters();
  });

  els.presets.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-preset]");
    if (!btn) return;
    const range = computePreset(btn.dataset.preset);
    if (!range) return;
    state.desde = range.desde;
    state.hasta = range.hasta;
    setDateValue(els.desde, range.desde);
    setDateValue(els.hasta, range.hasta);
    state.page = 1;
    applyFilters();
  });

  els.retryBtn.addEventListener("click", applyFilters);

  els.expandBtn.addEventListener("click", () => {
    const today = new Date();
    state.desde = isoDate(addDays(today, -90));
    state.hasta = isoDate(today);
    setDateValue(els.desde, state.desde);
    setDateValue(els.hasta, state.hasta);
    state.page = 1;
    applyFilters();
  });

  els.sortButtons.forEach((btn) => {
    btn.addEventListener("click", () => {
      const column = btn.dataset.sort;
      state.dir = state.sort === column && state.dir === "desc" ? "asc" : "desc";
      state.sort = column;
      state.page = 1;
      applyFilters();
    });
  });

  if (els.peticionesPrev) {
    els.peticionesPrev.addEventListener("click", () => {
      if (els.peticionesPrev.disabled) return;
      state.page -= 1;
      applyFilters();
    });
  }
  if (els.peticionesNext) {
    els.peticionesNext.addEventListener("click", () => {
      if (els.peticionesNext.disabled) return;
      state.page += 1;
      applyFilters();
    });
  }

  if (els.tablaPeticiones) {
    els.tablaPeticiones.addEventListener("click", (e) => {
      const btn = e.target.closest(".l-copy-btn");
      if (!btn || !btn.dataset.copy) return;
      navigator.clipboard.writeText(btn.dataset.copy).then(
        () => {
          btn.dataset.copied = "true";
          announce("Sesión copiada");
          setTimeout(() => delete btn.dataset.copied, 1500);
        },
        () => announce("No se pudo copiar la sesión")
      );
    });
  }

  updateChips();
  updatePresetHighlight();
}
