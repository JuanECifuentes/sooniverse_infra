/**
 * Filtrado asíncrono del panel de métricas. Sin dependencias, sin build step.
 * Dueño único del estado de filtros; el DOM se deriva de `state`.
 *
 * Otros módulos (metrics-lente.js, metrics-heatmap.js) NUNCA mutan `state`:
 * emiten "metrics:filtro-sugerido" y este archivo decide. Así el estado sigue
 * teniendo un solo dueño aunque haya varios productores de intención.
 */

import { escapeHtml, fmtInt, fmtPct, fmtTok, fmtUsd } from "./format.js";

const panel = document.getElementById("metrics-panel");
if (panel) {
  const API_URL = panel.dataset.apiUrl;

  function getCsrfToken() {
    const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  const els = {
    granularity: document.getElementById("metrics-granularity"),
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
    // -- filtros de ritmo de uso --
    horaDesde: document.getElementById("id_hora_desde"),
    horaHasta: document.getElementById("id_hora_hasta"),
    horaError: document.getElementById("metrics-hora-error"),
    estado: document.getElementById("metrics-estado"),
    incluirBenchmark: document.getElementById("id_incluir_benchmark"),
    comparar: document.getElementById("id_comparar"),
    atajosDow: document.getElementById("dow-atajos"),
    // -- tiempos muertos --
    ocioVentanas: document.getElementById("ocio-ventanas-body"),
    ocioCoste: document.getElementById("ocio-coste-col"),
    cardOcio: document.getElementById("card-tiempos-muertos"),
    // -- comparativa --
    comparativaBox: document.getElementById("metrics-comparativa"),
  };

  const multiselects = {
    modelo: document.querySelector('[data-filter-group="modelo"]'),
    api_key: document.querySelector('[data-filter-group="api_key"]'),
    dow: document.querySelector('[data-filter-group="dow"]'),
  };

  // Los presets fijan la fecha programáticamente; si flatpickr ya tomó el
  // control del input (ver datepicker.js), asignar `.value` directo no
  // actualiza su calendario/altInput visible y quedan desincronizados.
  // triggerChange:false porque el llamador ya dispara su propio applyFilters().
  function setDateValue(el, value) {
    if (el._flatpickr) el._flatpickr.setDate(value, false);
    else el.value = value;
  }

  function readCheckedValues(group) {
    if (!multiselects[group]) return [];
    return [...multiselects[group].querySelectorAll("input[type=checkbox]:checked")].map((cb) => cb.value);
  }

  function findCheckbox(group, value) {
    if (!multiselects[group]) return null;
    return [...multiselects[group].querySelectorAll("input[type=checkbox]")]
      .find((cb) => cb.value === String(value));
  }

  function labelFor(group, value) {
    const cb = findCheckbox(group, value);
    const label = cb && cb.closest("label");
    return label ? label.textContent.trim() : value;
  }

  const state = {
    granularity: els.granularity.querySelector(".sv-listbox__option--active")?.dataset.value || "daily",
    modelo: readCheckedValues("modelo"),
    api_key: readCheckedValues("api_key"),
    dow: readCheckedValues("dow"),
    desde: els.desde.value,
    hasta: els.hasta.value,
    hora_desde: 0,
    hora_hasta: 23,
    estado: "todas",
    incluir_benchmark: false,
    comparar: false,
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
    s.dow.forEach((d) => p.append("dow", d));
    p.set("hora_desde", s.hora_desde);
    p.set("hora_hasta", s.hora_hasta);
    p.set("estado", s.estado);
    if (s.incluir_benchmark) p.set("incluir_benchmark", "1");
    if (s.comparar) p.set("comparar", "1");
    p.set("page", s.page);
    p.set("sort", s.sort);
    p.set("dir", s.dir);
    return p;
  }

  /** Parámetros que comparten metrics_api y lente_api: sin paginación ni orden,
   *  que son justo los que más se reenvían y no afectan a las lentes. */
  function buildParamsLente(s) {
    const p = buildParams(s);
    ["page", "sort", "dir", "granularity", "comparar"].forEach((k) => p.delete(k));
    return p.toString();
  }

  function announce(msg) {
    els.live.textContent = msg;
  }

  function setField(name, value) {
    document.querySelectorAll(`[data-field="${name}"]`).forEach((el) => {
      el.textContent = value;
    });
  }

  const HORA = (h) => `${String(h).padStart(2, "0")}h`;

  function updateChips() {
    const items = [
      ...state.modelo.map((v) => ({ group: "modelo", value: v, label: labelFor("modelo", v) })),
      ...state.api_key.map((v) => ({ group: "api_key", value: v, label: labelFor("api_key", v) })),
      ...state.dow.map((v) => ({ group: "dow", value: v, label: labelFor("dow", v) })),
    ];
    if (state.hora_desde !== 0 || state.hora_hasta !== 23) {
      items.push({ group: "franja", value: "franja",
                   label: `${HORA(state.hora_desde)}–${HORA(state.hora_hasta)}` });
    }
    if (state.estado !== "todas") {
      items.push({ group: "estado", value: "estado", label: "Solo errores" });
    }
    // Regla de honestidad: un filtro activo que no se ve en pantalla es un
    // usuario engañado. La exclusión del benchmark es el default, así que TIENE
    // que aparecer como chip, igual que cualquier otro filtro.
    if (!state.incluir_benchmark) {
      items.push({ group: "benchmark", value: "benchmark", label: "Benchmark excluido" });
    }

    els.chips.innerHTML = items
      .map(
        (it) => `<span class="sv-chip">${escapeHtml(it.label)}
          <button type="button" class="sv-chip__remove" data-remove-group="${it.group}"
                  data-remove-value="${escapeHtml(it.value)}" aria-label="Quitar ${escapeHtml(it.label)}">×</button>
        </span>`
      )
      .join("");

    document.querySelectorAll("[data-count-for]").forEach((el) => {
      const valor = state[el.dataset.countFor];
      const count = Array.isArray(valor) ? valor.length : 0;
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

  const PRESETS = ["24h", "7d", "30d", "90d", "mes_actual"];

  function computePreset(preset) {
    const today = new Date();
    // 24h fija además granularidad horaria: con la diaria devolvería un solo
    // punto y la gráfica parecería rota.
    if (preset === "24h") return { desde: isoDate(addDays(today, -1)), hasta: isoDate(today), granularity: "hourly" };
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
    for (const p of PRESETS) {
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

  function renderTable(tbody, rows, kind) {
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="5" class="sv-help">Sin datos en la ventana seleccionada.</td></tr>';
      return;
    }
    tbody.innerHTML = rows
      .map((r) => {
        if (kind === "modelo") {
          return `<tr>
            <td class="sv-mono">${escapeHtml(r.model_name)}</td>
            <td class="sv-num">${fmtInt(r.request_count)}</td>
            <td class="sv-num">${fmtTok(r.prompt_tokens)}</td>
            <td class="sv-num">${fmtTok(r.completion_tokens)}</td>
            <td class="sv-num sv-num--accent">${fmtTok(r.total_tokens)}</td>
          </tr>`;
        }
        const alias = r.api_key__key_alias ? escapeHtml(r.api_key__key_alias) : "(sin registro)";
        const inactiva = r.api_key__is_active === false
          ? ' <span class="sv-badge sv-badge--muted">inactiva</span>' : "";
        return `<tr>
          <td><span class="sv-strong">${alias}</span>${inactiva}</td>
          <td class="sv-num">${fmtInt(r.request_count)}</td>
          <td class="sv-num">${fmtTok(r.prompt_tokens)}</td>
          <td class="sv-num">${fmtTok(r.completion_tokens)}</td>
          <td class="sv-num sv-num--accent">${fmtTok(r.total_tokens)}</td>
        </tr>`;
      })
      .join("");
  }

  function renderTiemposMuertos(tm) {
    if (!tm || !els.cardOcio) return;
    setField("pct_ocioso", fmtPct(tm.pct_ocioso));
    setField("horas_ociosas", `${fmtInt(tm.horas_ociosas)} / ${fmtInt(tm.horas_totales)} h`);
    setField("coste_ocioso", fmtUsd(tm.coste_ocioso_usd));

    // Con coste/hora sin configurar (METRICS_COSTE_HORA_USD=0) se oculta la
    // columna entera en vez de llenarla de $0,0000, que no informa de nada.
    document.querySelectorAll("[data-coste-ocioso]").forEach((el) => {
      el.hidden = !tm.mostrar_coste;
    });

    if (!els.ocioVentanas) return;
    if (!tm.ventanas.length) {
      els.ocioVentanas.innerHTML =
        '<tr><td colspan="3" class="sv-help">Sin franjas ociosas en el rango.</td></tr>';
      return;
    }
    els.ocioVentanas.innerHTML = tm.ventanas.map((v) => `
      <tr>
        <td data-label="Franja">${escapeHtml(v.etiqueta)}</td>
        <td data-label="Horas" class="sv-num">${fmtInt(v.horas)}</td>
        <td data-label="Coste" class="sv-num" data-coste-ocioso ${tm.mostrar_coste ? "" : "hidden"}>
          ${fmtUsd(v.coste_usd)}</td>
      </tr>`).join("");
  }

  function renderComparativa(comp) {
    if (!els.comparativaBox) return;
    if (!comp) {
      els.comparativaBox.hidden = true;
      return;
    }
    els.comparativaBox.hidden = false;
    const delta = (v) => {
      if (v === null || v === undefined) return '<span class="sv-muted">—</span>';
      const signo = v > 0 ? "+" : "";
      const tono = v > 0 ? "up" : (v < 0 ? "down" : "flat");
      return `<span class="sv-delta" data-tono="${tono}">${signo}${fmtPct(v).replace(" %", "")} %</span>`;
    };
    els.comparativaBox.innerHTML = `
      <span class="sv-muted sv-text-xs">vs ${escapeHtml(comp.desde)} → ${escapeHtml(comp.hasta)}:</span>
      <span>Tokens ${delta(comp.delta_pct.total_tokens)}</span>
      <span>Peticiones ${delta(comp.delta_pct.request_count)}</span>
      <span>Tok/petición ${delta(comp.delta_pct.tokens_por_request)}</span>`;
  }

  function render(data) {
    panel.dispatchEvent(new CustomEvent("metrics:data", { detail: data }));

    // Las lentes (mapa de calor, perfil) usan su propio endpoint y necesitan
    // los mismos filtros pero sin paginación ni orden.
    panel.dispatchEvent(new CustomEvent("metrics:params", { detail: buildParamsLente(state) }));

    renderTiemposMuertos(data.tiempos_muertos);
    renderComparativa(data.comparativa);

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
    setField("tasa_error", data.summary.tasa_error);
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

    if (els.horaError) {
      const franjaInvalida = state.hora_desde > state.hora_hasta;
      els.horaError.hidden = !franjaInvalida;
      if (franjaInvalida) {
        els.horaError.textContent = 'La hora "desde" no puede ser posterior a "hasta".';
        return;
      }
    }

    updateChips();
    updatePresetHighlight();
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
      announce("Cargando datos");
    }, 150);
    const startedAt = performance.now();

    try {
      const res = await fetch(API_URL, {
        method: "POST",
        headers: { "X-CSRFToken": getCsrfToken() },
        body: buildParams(state),
        signal: controller.signal,
      });
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

  els.granularity.querySelector(".sv-listbox__panel").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-value]");
    if (!btn) return;
    state.granularity = btn.dataset.value;
    els.granularity.removeAttribute("open");
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

  function limpiarFranja() {
    state.hora_desde = 0;
    state.hora_hasta = 23;
    if (els.horaDesde) els.horaDesde.value = 0;
    if (els.horaHasta) els.horaHasta.value = 23;
  }

  els.chips.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-remove-group]");
    if (!btn) return;
    const { removeGroup: group, removeValue: value } = btn.dataset;

    if (group === "franja") {
      limpiarFranja();
    } else if (group === "estado") {
      state.estado = "todas";
      seleccionarListbox(els.estado, "todas");
    } else if (group === "benchmark") {
      state.incluir_benchmark = true;
      if (els.incluirBenchmark) els.incluirBenchmark.checked = true;
    } else {
      const cb = findCheckbox(group, value);
      if (cb) cb.checked = false;
      state[group] = state[group].filter((v) => String(v) !== String(value));
    }
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
    if (range.granularity) state.granularity = range.granularity;
    setDateValue(els.desde, range.desde);
    setDateValue(els.hasta, range.hasta);
    state.page = 1;
    applyFilters();
  });

  // -- filtros de ritmo de uso -------------------------------------------------
  function seleccionarListbox(contenedor, valor) {
    if (!contenedor) return;
    contenedor.querySelectorAll("[data-value]").forEach((btn) => {
      const activo = btn.dataset.value === valor;
      btn.classList.toggle("sv-listbox__option--active", activo);
      btn.setAttribute("aria-selected", activo ? "true" : "false");
      if (activo) {
        const label = contenedor.querySelector("[data-listbox-label]");
        if (label) label.textContent = btn.textContent.trim();
      }
    });
  }

  [els.horaDesde, els.horaHasta].forEach((input) => {
    if (!input) return;
    input.addEventListener("change", () => {
      state.hora_desde = Math.max(0, Math.min(23, Number(els.horaDesde.value || 0)));
      state.hora_hasta = Math.max(0, Math.min(23, Number(els.horaHasta.value ?? 23)));
      els.horaDesde.value = state.hora_desde;
      els.horaHasta.value = state.hora_hasta;
      state.page = 1;
      applyFilters();
    });
  });

  if (els.estado) {
    els.estado.querySelector(".sv-listbox__panel").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-value]");
      if (!btn) return;
      state.estado = btn.dataset.value;
      seleccionarListbox(els.estado, state.estado);
      els.estado.removeAttribute("open");
      state.page = 1;
      applyFilters();
    });
  }

  [["incluirBenchmark", "incluir_benchmark"], ["comparar", "comparar"]].forEach(([el, clave]) => {
    if (!els[el]) return;
    els[el].addEventListener("change", () => {
      state[clave] = els[el].checked;
      state.page = 1;
      applyFilters();
    });
  });

  if (els.atajosDow) {
    els.atajosDow.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-dow-preset]");
      if (!btn) return;
      const mapa = {
        laborables: ["1", "2", "3", "4", "5"],
        finde: ["6", "7"],
        todos: [],
      };
      const seleccion = mapa[btn.dataset.dowPreset] || [];
      multiselects.dow.querySelectorAll("input[type=checkbox]").forEach((cb) => {
        cb.checked = seleccion.includes(cb.value);
      });
      state.dow = seleccion;
      state.page = 1;
      applyFilters();
    });
  }

  // El mapa de calor propone un filtro (clic en celda); el estado sigue siendo
  // de este archivo, que es quien lo aplica.
  panel.addEventListener("metrics:filtro-sugerido", (e) => {
    const { dow, hora_desde, hora_hasta } = e.detail;
    state.dow = (dow || []).map(String);
    multiselects.dow?.querySelectorAll("input[type=checkbox]").forEach((cb) => {
      cb.checked = state.dow.includes(cb.value);
    });
    state.hora_desde = hora_desde ?? 0;
    state.hora_hasta = hora_hasta ?? 23;
    if (els.horaDesde) els.horaDesde.value = state.hora_desde;
    if (els.horaHasta) els.horaHasta.value = state.hora_hasta;
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
  updateGranularityHighlight();

  // Respaldo directo para metrics-lente.js: el orden de los <script
  // type="module"> NO garantiza que su listener de "metrics:params" ya esté
  // armado cuando este módulo ejecuta su código de nivel superior (este
  // archivo se carga ANTES que metrics-lente.js). Sin esto, el evento de abajo
  // se dispara al vacío y un usuario que entra a la página y hace clic en una
  // lente antes de tocar cualquier filtro no vería nunca la primera petición
  // -bug real, encontrado probando en el navegador, no solo en tests-.
  panel.getParamsLente = () => buildParamsLente(state);

  // El primer pintado lo hace el servidor con json_script; las lentes no
  // reciben ese bootstrap, así que necesitan los parámetros iniciales para
  // poder cargarse en cuanto el usuario abra una. Se dispara también por si
  // algún oyente sí llegó a tiempo (no hace daño duplicar la señal).
  panel.dispatchEvent(new CustomEvent("metrics:params", { detail: buildParamsLente(state) }));
}
