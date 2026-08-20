/**
 * Perfil de carga horario: curva 0-23 h con banda mediana/pico y la línea del
 * techo de capacidad medido superpuesta.
 *
 * Es donde se ve si el pico de las 10:00 roza el límite de la infraestructura.
 * Instancia de Chart.js PROPIA en su propio canvas: nunca se cambia el `type`
 * de la gráfica de barras existente, porque Chart.js no lo soporta de forma
 * fiable (ver la cabecera de metrics-charts.js).
 *
 * Render tonto: escucha "metrics:lente" y pinta. No hace fetch.
 */

import { SIN_MOVIMIENTO, cssVar, escapeHtml, fmtInt, hexToRgba, horaLabel } from "./format.js";

const panel = document.getElementById("metrics-panel");

if (panel && window.Chart) {
  const canvas = document.getElementById("metrics-perfil-canvas");
  const tabla = document.getElementById("metrics-perfil-table-body");
  const nota = document.getElementById("metrics-perfil-nota");

  let chart = null;

  function datasets(datos) {
    const acento = cssVar("--chart-1");
    const gris = cssVar("--chart-2");
    const techo = cssVar("--knee-line");

    const series = [
      {
        label: "Pico",
        data: datos.puntos.map((p) => p.pico),
        borderColor: hexToRgba(gris, 0.55),
        backgroundColor: cssVar("--band-fill"),
        borderWidth: 1,
        pointRadius: 0,
        fill: "+1",
        tension: 0.3,
      },
      {
        label: "Mediana",
        data: datos.puntos.map((p) => p.mediana),
        borderColor: acento,
        backgroundColor: hexToRgba(acento, 0.18),
        borderWidth: 2,
        pointRadius: 0,
        fill: false,
        tension: 0.3,
      },
    ];

    if (datos.techo_pet_hora) {
      series.push({
        label: "Techo medido",
        data: datos.puntos.map(() => datos.techo_pet_hora),
        borderColor: techo,
        borderWidth: 1.5,
        borderDash: [5, 4],   // línea punteada, nunca un resplandor de color
        pointRadius: 0,
        fill: false,
      });
    }
    return series;
  }

  function render(datos) {
    if (tabla) {
      tabla.innerHTML = datos.puntos.map((p) => `
        <tr>
          <th scope="row">${escapeHtml(horaLabel(p.hora))}</th>
          <td class="sv-num">${fmtInt(p.mediana)}</td>
          <td class="sv-num">${fmtInt(p.p90)}</td>
          <td class="sv-num">${fmtInt(p.pico)}</td>
          <td class="sv-num">${fmtInt(p.dias)}</td>
        </tr>`).join("");
    }

    if (nota) {
      nota.textContent = datos.techo_pet_hora
        ? `Techo medido: ${fmtInt(datos.techo_pet_hora)} peticiones/hora.`
        : "Sin techo medido todavía: corre la fase 'capacidad' del despliegue.";
    }

    if (!chart) {
      chart = new window.Chart(canvas.getContext("2d"), {
        type: "line",
        data: { labels: datos.puntos.map((p) => horaLabel(p.hora)), datasets: datasets(datos) },
        options: {
          maintainAspectRatio: false,
          animation: SIN_MOVIMIENTO ? false : undefined,
          interaction: { mode: "index", intersect: false },
          scales: {
            x: { grid: { display: false }, ticks: { color: cssVar("--text-muted") } },
            y: {
              beginAtZero: true,
              border: { display: false },
              grid: { color: cssVar("--border-subtle") },
              ticks: { color: cssVar("--text-muted"), precision: 0 },
              title: { display: true, text: "Peticiones por hora", color: cssVar("--text-muted") },
            },
          },
          plugins: {
            legend: { position: "top", align: "end", labels: { color: cssVar("--text-secondary") } },
            tooltip: { callbacks: { label: (i) => `${i.dataset.label}: ${fmtInt(i.parsed.y)}` } },
          },
        },
      });
    } else {
      chart.data.labels = datos.puntos.map((p) => horaLabel(p.hora));
      chart.data.datasets = datasets(datos);
      chart.update();
    }
  }

  panel.addEventListener("metrics:lente", (e) => {
    if (e.detail.lente === "perfil") render(e.detail.datos);
  });

  // Igual que en el mapa: un canvas oculto mide 0x0 y saldría de 0 px de alto.
  panel.addEventListener("metrics:lente-visible", (e) => {
    if (e.detail.lente === "perfil" && chart) chart.resize();
  });
}
