/**
 * Gráfica de consumo del panel, con Chart.js (vendorizado, sin CDN en runtime).
 * Escucha "metrics:data" (emitido por metrics-filters.js) y actualiza la
 * instancia existente en vez de destruir/recrear el canvas.
 *
 * Nota de alcance: se mantiene la métrica de tokens (prompt + completion,
 * apilados) tal como ya mostraba la gráfica CSS anterior — no se introduce
 * el desglose de coste por modelo, que requeriría una nueva agregación en
 * el backend fuera del alcance de esta fase. Se usa un único tipo de
 * gráfica (barras apiladas) para todas las granularidades: Chart.js no
 * soporta de forma fiable cambiar `type` en una instancia ya creada, y la
 * restricción de esta fase es actualizar la instancia existente, nunca
 * destruirla y recrearla.
 */

import { SIN_MOVIMIENTO, cssVar, escapeHtml, fmtInt, fmtTok, hexToRgba } from "./format.js";

const panel = document.getElementById("metrics-panel");

if (panel && window.Chart) {
  const canvas = document.getElementById("metrics-chart-canvas");
  const tableBody = document.getElementById("metrics-chart-table-body");
  const bootstrapEl = document.getElementById("metrics-initial-payload");

  let chart = null;

  function buildDatasets(series) {
    const colorPrompt = cssVar("--chart-1");
    const colorCompletion = cssVar("--chart-2");
    const colorTendencia = cssVar("--chart-5");
    return [
      {
        label: "Prompt (entrada)",
        data: series.prompt_tokens,
        backgroundColor: hexToRgba(colorPrompt, 0.32),
        borderColor: colorPrompt,
        borderWidth: 1.5,
        borderSkipped: false,
        stack: "tokens",
      },
      {
        label: "Completion (salida)",
        data: series.completion_tokens,
        backgroundColor: hexToRgba(colorCompletion, 0.32),
        borderColor: colorCompletion,
        borderWidth: 1.5,
        borderSkipped: false,
        stack: "tokens",
      },
      // Línea de tendencia del total por periodo, superpuesta a las barras
      // apiladas: no participa del stack "tokens" (si no, Chart.js la suma
      // al alto de la pila en vez de trazarla sobre ella).
      {
        type: "line",
        label: "Total (tendencia)",
        data: series.total_tokens,
        borderColor: colorTendencia,
        backgroundColor: colorTendencia,
        borderWidth: 2.5,
        tension: 0.4,
        fill: false,
        pointRadius: 3,
        pointHoverRadius: 5,
        pointBackgroundColor: colorTendencia,
        pointBorderColor: cssVar("--surface-1"),
        pointBorderWidth: 1.5,
      },
    ];
  }

  function chartConfig(data) {
    return {
      type: "bar",
      data: { labels: data.series.labels, datasets: buildDatasets(data.series) },
      options: {
        maintainAspectRatio: false,
        // La regla prefers-reduced-motion de theme-sooniverse.css neutraliza las
        // transiciones CSS pero NO alcanza a Chart.js, que anima por JS: era un
        // incumplimiento ya existente, no solo de las gráficas nuevas.
        animation: SIN_MOVIMIENTO ? false : undefined,
        scales: {
          x: {
            stacked: true,
            grid: { display: false },
            ticks: { color: cssVar("--text-muted") },
          },
          y: {
            stacked: true,
            beginAtZero: true,
            border: { display: false },
            grid: { color: cssVar("--border-subtle") },
            ticks: { color: cssVar("--text-muted"), callback: (value) => fmtTok(value) },
          },
        },
        plugins: {
          legend: {
            position: "top",
            align: "end",
            labels: { color: cssVar("--text-secondary"), boxWidth: 12, usePointStyle: true, pointStyle: "rect" },
          },
          tooltip: {
            backgroundColor: cssVar("--surface-3"),
            titleColor: cssVar("--text-primary"),
            bodyColor: cssVar("--text-secondary"),
            footerColor: cssVar("--text-primary"),
            borderColor: cssVar("--border-strong"),
            borderWidth: 1,
            cornerRadius: 8,
            padding: 10,
            boxPadding: 4,
            usePointStyle: true,
            titleFont: { family: cssVar("--font-sans"), size: 13, weight: "600" },
            bodyFont: { family: cssVar("--font-sans"), size: 12 },
            footerFont: { family: cssVar("--font-mono"), size: 12, weight: "600" },
            callbacks: {
              label: (ctx) => `${ctx.dataset.label}: ${fmtTok(ctx.parsed.y)} tokens`,
              // Solo suma las barras apiladas (stack: "tokens"); la línea de
              // tendencia repite ese mismo total y no debe contarse dos veces.
              footer: (items) => {
                const apiladas = items.filter((it) => it.dataset.stack === "tokens");
                return `Total: ${fmtTok(apiladas.reduce((sum, it) => sum + it.parsed.y, 0))} tokens`;
              },
            },
          },
        },
      },
    };
  }

  function updateAccessibleTable(data) {
    const { labels, prompt_tokens, completion_tokens, total_tokens } = data.series;
    tableBody.innerHTML =
      labels
        .map(
          (label, i) => `<tr>
            <td>${escapeHtml(label)}</td>
            <td class="sv-num">${fmtTok(prompt_tokens[i])}</td>
            <td class="sv-num">${fmtTok(completion_tokens[i])}</td>
            <td class="sv-num sv-num--accent">${fmtTok(total_tokens[i])}</td>
          </tr>`
        )
        .join("") || '<tr><td colspan="4" class="sv-help">Sin datos en la ventana seleccionada.</td></tr>';
  }

  function render(data) {
    updateAccessibleTable(data);
    canvas.setAttribute(
      "aria-label",
      `Consumo de tokens ${data.granularity_label.toLowerCase()} entre ${data.desde} y ${data.hasta}`
    );

    if (!chart) {
      chart = new Chart(canvas.getContext("2d"), chartConfig(data));
      return;
    }
    chart.data.labels = data.series.labels;
    chart.data.datasets = buildDatasets(data.series);
    chart.update();
  }

  if (bootstrapEl) {
    try {
      render(JSON.parse(bootstrapEl.textContent));
    } catch (err) {
      /* sin datos iniciales válidos: el primer cambio de filtro crea la gráfica */
    }
  }

  panel.addEventListener("metrics:data", (e) => render(e.detail));
}
