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

const panel = document.getElementById("metrics-panel");

if (panel && window.Chart) {
  const canvas = document.getElementById("metrics-chart-canvas");
  const tableBody = document.getElementById("metrics-chart-table-body");
  const bootstrapEl = document.getElementById("metrics-initial-payload");

  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const fmtInt = (n) => Number(n || 0).toLocaleString("es-ES");

  function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  let chart = null;

  function buildDatasets(series) {
    return [
      {
        label: "Prompt (entrada)",
        data: series.prompt_tokens,
        backgroundColor: cssVar("--chart-1"),
        stack: "tokens",
      },
      {
        label: "Completion (salida)",
        data: series.completion_tokens,
        backgroundColor: cssVar("--chart-2"),
        stack: "tokens",
      },
    ];
  }

  function chartConfig(data) {
    return {
      type: "bar",
      data: { labels: data.series.labels, datasets: buildDatasets(data.series) },
      options: {
        maintainAspectRatio: false,
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
            ticks: { color: cssVar("--text-muted"), callback: (value) => fmtInt(value) },
          },
        },
        plugins: {
          legend: {
            position: "top",
            align: "end",
            labels: { color: cssVar("--text-secondary"), boxWidth: 12, usePointStyle: true, pointStyle: "rect" },
          },
          tooltip: {
            callbacks: {
              label: (ctx) => `${ctx.dataset.label}: ${fmtInt(ctx.parsed.y)} tokens`,
              footer: (items) => `Total: ${fmtInt(items.reduce((sum, it) => sum + it.parsed.y, 0))} tokens`,
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
            <td class="sv-num">${fmtInt(prompt_tokens[i])}</td>
            <td class="sv-num">${fmtInt(completion_tokens[i])}</td>
            <td class="sv-num sv-num--accent">${fmtInt(total_tokens[i])}</td>
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
