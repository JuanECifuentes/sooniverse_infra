chartjs-chart-matrix v2.0.1 — plugin de matriz para Chart.js, usado por el mapa
de calor semanal del dashboard (`static/js/metrics-heatmap.js`).

Requiere Chart.js ^4.0.0 como peer; la versión vendorizada aquí es la 4.5.1
(`chart.umd.min.js`). Si se sube Chart.js a una major nueva, hay que subir
también este plugin: se auto-registra sobre el global `Chart` y quedaría roto en
silencio. Descargado de https://cdn.jsdelivr.net/npm/chartjs-chart-matrix@2.0.1/
y servido en local, sin CDN, igual que el resto de dependencias del panel.

The MIT License (MIT)

Copyright (c) 2023 Jukka Kurkela

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
