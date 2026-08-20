/**
 * Mapa de calor semanal (7 días x 24 horas) con Chart.js + chartjs-chart-matrix.
 *
 * Responde de un vistazo "¿qué días hay más interacción?" (leyendo filas) y
 * "¿en qué momento del fin de semana está parada la máquina?" (celdas vacías
 * del sábado y el domingo).
 *
 * Es un RENDER TONTO: no hace fetch ni conoce el estado de filtros. Escucha el
 * evento "metrics:lente" que emite metrics-lente.js y pinta lo que llega. Toda
 * la aritmética (intensidad de cada celda, cortes de la rampa) la calcula el
 * servidor en metrics/analytics.py, para que sea testeable sin un runner de JS.
 *
 * Accesibilidad: un canvas no es navegable por teclado y el plugin solo aporta
 * onClick. La ruta accesible para filtrar por día/hora son los controles de la
 * barra de filtros, que existen igualmente; el clic sobre el mapa es un atajo
 * de ratón, nunca la única vía. El equivalente textual va en la tabla oculta.
 */

import { DIAS_ISO, SIN_MOVIMIENTO, cssVar, escapeHtml, fmtInt, fmtMs, fmtTok, horaLabel }
  from "./format.js";

const panel = document.getElementById("metrics-panel");

if (panel && window.Chart) {
  const canvas = document.getElementById("metrics-heatmap-canvas");
  const leyenda = document.getElementById("metrics-heatmap-legend");
  const tabla = document.getElementById("metrics-heatmap-table-body");
  const vacio = document.getElementById("metrics-heatmap-vacio");
  const live = document.getElementById("metrics-live");

  let chart = null;
  let ultimo = null;
  // Celda actualmente aplicada como filtro, para que un segundo clic la quite.
  let seleccion = null;

  const HORAS = 24;
  const DIAS = 7;

  function formateaValor(v, metrica) {
    if (metrica === "tokens") return fmtTok(v);
    if (metrica === "p95") return fmtMs(v);
    return fmtInt(v);
  }

  function colorCelda(ctx) {
    const raw = ctx.raw;
    if (!raw) return cssVar("--heat-0");
    return cssVar(`--heat-${raw.i}`);
  }

  function bordeCelda(ctx) {
    const raw = ctx.raw;
    if (seleccion && raw && raw.d === seleccion.d && raw.h === seleccion.h) {
      return cssVar("--accent");
    }
    // "Sin datos agregados" se distingue de "cero tráfico" por el borde, no
    // solo por luminancia: si no, el % de horas ociosas parecería mentir
    // mientras usage_hourly todavía no tiene histórico.
    return raw && !raw.n ? cssVar("--heat-border") : "transparent";
  }

  function config(datos) {
    return {
      type: "matrix",
      data: {
        datasets: [{
          label: datos.metrica_label,
          data: datos.celdas.map((c) => ({ x: horaLabel(c.h), y: DIAS_ISO[c.d - 1], ...c })),
          backgroundColor: colorCelda,
          borderColor: bordeCelda,
          borderWidth: (ctx) => (seleccion && ctx.raw
            && ctx.raw.d === seleccion.d && ctx.raw.h === seleccion.h) ? 2 : 1,
          // El tamaño se deriva del área de dibujo para que la rejilla siempre
          // llene la tarjeta, en cualquier ancho.
          width: (ctx) => (ctx.chart.chartArea || {}).width / HORAS - 2,
          height: (ctx) => (ctx.chart.chartArea || {}).height / DIAS - 2,
        }],
      },
      options: {
        maintainAspectRatio: false,
        animation: SIN_MOVIMIENTO ? false : undefined,
        scales: {
          x: {
            type: "category",
            labels: Array.from({ length: HORAS }, (_, h) => horaLabel(h)),
            // offset:true alinea las celdas con las BANDAS del eje en vez de
            // con los ticks; sin él la rejilla queda desplazada media celda.
            offset: true,
            grid: { display: false },
            ticks: {
              color: cssVar("--text-muted"),
              autoSkip: false,
              maxRotation: 0,
              // A 24 etiquetas se solapan: se muestran las horas pares.
              callback: (v, i) => (i % 2 === 0 ? horaLabel(i) : ""),
            },
          },
          y: {
            type: "category",
            labels: DIAS_ISO,
            offset: true,
            reverse: false,
            grid: { display: false },
            ticks: { color: cssVar("--text-muted") },
          },
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            displayColors: false,
            callbacks: {
              title: (items) => {
                const r = items[0].raw;
                return `${DIAS_ISO[r.d - 1]} · ${horaLabel(r.h)}`;
              },
              label: (item) => {
                const r = item.raw;
                if (!r.n) return "Sin datos agregados";
                return `${formateaValor(r.v, ultimo.metrica)} ${ultimo.unidad}`;
              },
            },
          },
        },
        onClick: (_evt, elementos) => {
          if (!elementos.length) return;
          const r = elementos[0].element.$context.raw;
          aplicarFiltro(r.d, r.h);
        },
      },
    };
  }

  function aplicarFiltro(dow, hora) {
    // Segundo clic sobre la misma celda = quitar el filtro (toggle).
    const quitar = seleccion && seleccion.d === dow && seleccion.h === hora;
    seleccion = quitar ? null : { d: dow, h: hora };
    // El dueño único del estado de filtros sigue siendo metrics-filters.js:
    // aquí solo se sugiere, no se muta nada.
    panel.dispatchEvent(new CustomEvent("metrics:filtro-sugerido", {
      detail: quitar
        ? { dow: [], hora_desde: 0, hora_hasta: 23 }
        : { dow: [dow], hora_desde: hora, hora_hasta: hora },
    }));
  }

  function pintarLeyenda(datos) {
    if (!leyenda) return;
    if (!datos.cortes.length) {
      leyenda.innerHTML = '<span class="sv-muted sv-text-xs">Sin tráfico en el rango</span>';
      return;
    }
    // Los cortes numéricos REALES van en la leyenda: el color nunca puede ser
    // el único canal de información.
    const pasos = datos.cortes.map((corte, i) => `
      <span class="l-heatmap__step">
        <span class="l-heatmap__swatch" data-i="${i + 1}" aria-hidden="true"></span>
        <span class="sv-mono sv-num">${escapeHtml(formateaValor(corte, datos.metrica))}</span>
      </span>`).join("");
    leyenda.innerHTML = `
      <span class="l-heatmap__step">
        <span class="l-heatmap__swatch" data-i="0" aria-hidden="true"></span>
        <span class="sv-mono sv-num">0</span>
      </span>${pasos}
      <span class="sv-muted sv-text-xs">${escapeHtml(datos.unidad)}</span>`;
  }

  function pintarTabla(datos) {
    if (!tabla) return;
    const porDia = new Map();
    datos.celdas.forEach((c) => {
      if (!porDia.has(c.d)) porDia.set(c.d, []);
      porDia.get(c.d)[c.h] = c.v;
    });
    tabla.innerHTML = Array.from(porDia.entries()).map(([dia, horas]) => `
      <tr>
        <th scope="row">${DIAS_ISO[dia - 1]}</th>
        ${Array.from({ length: HORAS }, (_, h) =>
          `<td class="sv-num">${escapeHtml(formateaValor(horas[h] || 0, datos.metrica))}</td>`
        ).join("")}
      </tr>`).join("");
  }

  function render(datos) {
    ultimo = datos;
    const sinDatos = !datos.maximo;
    if (vacio) vacio.hidden = !sinDatos;

    pintarLeyenda(datos);
    pintarTabla(datos);

    if (!chart) {
      chart = new window.Chart(canvas.getContext("2d"), config(datos));
    } else {
      // Se actualiza la instancia, nunca se recrea (mismo criterio que
      // metrics-charts.js).
      chart.data.datasets[0].label = datos.metrica_label;
      chart.data.datasets[0].data = datos.celdas.map(
        (c) => ({ x: horaLabel(c.h), y: DIAS_ISO[c.d - 1], ...c })
      );
      chart.update();
    }

    if (live && datos.pico) {
      live.textContent = `Mapa de calor actualizado. Máximo: `
        + `${formateaValor(datos.pico.valor, datos.metrica)} ${datos.unidad} el `
        + `${DIAS_ISO[datos.pico.dow - 1].toLowerCase()} a las ${horaLabel(datos.pico.hora)}.`;
    }
  }

  panel.addEventListener("metrics:lente", (e) => {
    if (e.detail.lente === "heatmap") render(e.detail.datos);
  });

  // Un canvas dentro de un contenedor con [hidden] mide 0x0. Al mostrarse hay
  // que redimensionar en el mismo tick o la gráfica sale de 0 px de alto la
  // primera vez que se abre la lente.
  panel.addEventListener("metrics:lente-visible", (e) => {
    if (e.detail.lente === "heatmap" && chart) chart.resize();
  });

  // Al limpiar los filtros desde fuera, la celda marcada deja de ser válida.
  panel.addEventListener("metrics:filtros-limpiados", () => {
    seleccion = null;
    if (chart) chart.update();
  });
}
