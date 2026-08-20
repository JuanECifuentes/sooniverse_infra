/**
 * Página de capacidad: curva de degradación del benchmark y selector de corrida.
 *
 * La curva cruza dos magnitudes con escalas incompatibles (throughput y
 * latencia), así que usa dos ejes Y. La rodilla se marca con una línea
 * punteada, nunca con un resplandor de color.
 */

import { SIN_MOVIMIENTO, cssVar, escapeHtml, fmtInt, fmtMs, fmtPct, hexToRgba }
  from "./format.js";

const panel = document.getElementById("capacidad-panel");

if (panel && window.Chart) {
  const canvas = document.getElementById("capacidad-curva-canvas");
  const cuerpo = document.getElementById("capacidad-curva-body");
  const nota = document.getElementById("capacidad-curva-nota");
  const selector = document.getElementById("capacidad-corrida");
  const bootstrap = document.getElementById("capacidad-payload");

  let chart = null;

  function datasets(curva, rodilla) {
    const acento = cssVar("--chart-1");
    const p95 = cssVar("--chart-3");
    return [
      {
        label: "Tokens de salida / s",
        data: curva.map((n) => n.tokens_salida_por_seg),
        borderColor: acento,
        backgroundColor: hexToRgba(acento, 0.16),
        borderWidth: 2,
        pointRadius: (ctx) => (curva[ctx.dataIndex].concurrencia === rodilla ? 5 : 3),
        yAxisID: "y",
        tension: 0.25,
        fill: true,
      },
      {
        label: "Latencia p95 (ms)",
        data: curva.map((n) => n.p95_ms),
        borderColor: p95,
        borderWidth: 2,
        borderDash: [5, 4],
        pointRadius: 3,
        yAxisID: "y1",
        tension: 0.25,
        fill: false,
      },
    ];
  }

  function pintarTabla(curva, rodilla) {
    if (!cuerpo) return;
    if (!curva || !curva.length) {
      cuerpo.innerHTML = '<tr><td colspan="6" class="sv-help">Sin corridas registradas.</td></tr>';
      return;
    }
    cuerpo.innerHTML = curva.map((n) => {
      const esRodilla = n.concurrencia === rodilla;
      return `<tr>
        <td data-label="Concurrencia" class="sv-num">
          ${fmtInt(n.concurrencia)}${esRodilla ? ' <span class="sv-badge sv-badge--info">rodilla</span>' : ""}
        </td>
        <td data-label="Peticiones" class="sv-num">${fmtInt(n.peticiones)}</td>
        <td data-label="Tokens/s" class="sv-num">${fmtInt(Math.round(n.tokens_salida_por_seg))}</td>
        <td data-label="p95" class="sv-num">${escapeHtml(fmtMs(n.p95_ms))}</td>
        <td data-label="TTFT p95" class="sv-num">${escapeHtml(fmtMs(n.ttft_p95_ms))}</td>
        <td data-label="Error" class="sv-num">${escapeHtml(fmtPct(n.tasa_error_pct))}</td>
      </tr>`;
    }).join("");
  }

  function pintarMedidor(margen) {
    const relleno = panel.querySelector(".l-meter__fill");
    const etiqueta = panel.querySelector(".sv-semaforo-label");
    const medidor = panel.querySelector(".l-meter");
    if (relleno) {
      relleno.style.width = `${margen.uso_pct || 0}%`;
      relleno.dataset.semaforo = margen.semaforo;
    }
    if (etiqueta) {
      etiqueta.textContent = margen.etiqueta;
      etiqueta.dataset.semaforo = margen.semaforo;
    }
    if (medidor) {
      medidor.setAttribute("aria-valuenow", margen.uso_pct || 0);
      medidor.setAttribute("aria-valuetext", margen.etiqueta);
    }
  }

  function setCap(nombre, valor) {
    panel.querySelectorAll(`[data-cap="${nombre}"]`).forEach((el) => {
      el.textContent = valor;
    });
  }

  function render(payload) {
    const { margen, proyeccion, corrida } = payload;

    setCap("techo_rpm", margen.techo_rpm !== null ? fmtInt(Math.round(margen.techo_rpm)) : "—");
    setCap("techo_tokens", margen.techo_tokens_min !== null
      ? fmtInt(Math.round(margen.techo_tokens_min)) : "—");
    setCap("rodilla", margen.techo_concurrencia ?? "—");
    setCap("pico", fmtInt(Math.round(margen.pico_rpm)));
    setCap("usuarios", corrida && corrida.usuarios_estimados ? fmtInt(corrida.usuarios_estimados) : "—");
    setCap("semanas", proyeccion.confiable
      ? `≈ ${proyeccion.semanas_al_techo} semanas al techo`
      : "Sin tendencia clara");
    setCap("proyeccion-nota", proyeccion.explicacion);

    pintarMedidor(margen);

    const curva = (corrida && corrida.curva) || [];
    const rodilla = margen.techo_concurrencia;
    pintarTabla(curva, rodilla);

    if (nota) {
      nota.textContent = curva.length
        ? `Rodilla en concurrencia ${rodilla}. Parada: ${corrida.motivo_label.toLowerCase()}.`
        : "Sin datos de curva.";
    }

    if (!curva.length) return;
    const labels = curva.map((n) => `${n.concurrencia}`);

    if (!chart) {
      chart = new window.Chart(canvas.getContext("2d"), {
        type: "line",
        data: { labels, datasets: datasets(curva, rodilla) },
        options: {
          maintainAspectRatio: false,
          animation: SIN_MOVIMIENTO ? false : undefined,
          interaction: { mode: "index", intersect: false },
          scales: {
            x: {
              grid: { display: false },
              ticks: { color: cssVar("--text-muted") },
              title: { display: true, text: "Peticiones concurrentes", color: cssVar("--text-muted") },
            },
            y: {
              beginAtZero: true,
              position: "left",
              border: { display: false },
              grid: { color: cssVar("--border-subtle") },
              ticks: { color: cssVar("--text-muted") },
              title: { display: true, text: "Tokens/s", color: cssVar("--text-muted") },
            },
            y1: {
              beginAtZero: true,
              position: "right",
              border: { display: false },
              grid: { display: false },
              ticks: { color: cssVar("--text-muted") },
              title: { display: true, text: "p95 (ms)", color: cssVar("--text-muted") },
            },
          },
          plugins: {
            legend: { position: "top", align: "end", labels: { color: cssVar("--text-secondary") } },
          },
        },
      });
    } else {
      chart.data.labels = labels;
      chart.data.datasets = datasets(curva, rodilla);
      chart.update();
    }
  }

  async function cargar(runId) {
    const params = new URLSearchParams({
      desde: panel.dataset.desde,
      hasta: panel.dataset.hasta,
    });
    if (runId) params.set("corrida", runId);
    try {
      const resp = await fetch(panel.dataset.apiUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded",
          "X-CSRFToken": panel.dataset.csrf || "",
        },
        body: params.toString(),
      });
      if (resp.ok) render(await resp.json());
    } catch (_) { /* la vista ya está pintada por el servidor */ }
  }

  if (bootstrap) {
    try {
      render(JSON.parse(bootstrap.textContent));
    } catch (_) { /* payload ausente o corrupto: la plantilla ya muestra el aviso */ }
  }

  if (selector) {
    selector.addEventListener("change", () => cargar(selector.value));
  }
}
