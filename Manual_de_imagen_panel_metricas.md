# 📘 MANUAL DE IDENTIDAD DE MARCA: PANEL DE MÉTRICAS SOONIVERSE
## Versión: 2.0.0 (Sober / Data-Dense UI)
## Target: Parseable por IA / Desarrolladores / Diseñadores UI-UX
## Alcance: `django_metrics` (panel de métricas + gestor de API Keys). No sustituye a `Manual_de_imagen_sooniverse.md` (identidad de marca general): este documento describe la re-tematización específica de este panel, alineada con la estética de Open WebUI.

---

## 0. POR QUÉ ESTE MANUAL EXISTE POR SEPARADO

El panel se re-tematizó (2026-08) para dejar de parecer una landing B2B genérica y parecerse a la interfaz de IA con la que convive en producción (Open WebUI, vía LiteLLM). La paleta neón/degradados del manual original quedó reservada para material de marketing y logotipo; **la interfaz de trabajo diaria (dashboard, gestor de API Keys) usa la paleta sobria descrita aquí.**

La paleta de este documento no se aproximó de memoria: se extrajo inspeccionando `getComputedStyle()` sobre la instancia real de Open WebUI en producción, sesión autenticada. Cada valor de la tabla de la sección 1 indica de qué elemento se extrajo.

---

## 1. ESPECIFICACIONES DE COLOR

Fondo casi negro real, superficies diferenciadas por luminancia (no por bordes gruesos), un único acento saturado usado con restricción.

| Rol | Variable CSS | HEX / valor | Origen | Uso |
| :--- | :--- | :--- | :--- | :--- |
| Superficie 0 (página) | `--surface-0` | `#0d0d0e` | `body { background-color }` de Open WebUI (`#171717`), llevado al extremo inferior del rango `#0a0a0a–#141414` pedido | Fondo de página |
| Superficie 1 (cards) | `--surface-1` | `#141415` | Derivado del anterior, mismo patrón de "diferencia mínima de luminancia" que Open WebUI (`#171717` → `#161616`) | Cards, inputs, thead |
| Superficie 2 | `--surface-2` | `#1a1a1c` | Escalado desde surface-1 | Hover de filas, chips, checkboxes |
| Superficie 3 | `--surface-3` | `#212124` | Escalado desde surface-2 | Paneles flotantes (multiselect, tooltip) |
| Borde sutil | `--border-subtle` | `rgba(255,255,255,.08)` | Borde del contenedor de chat de Open WebUI (`#242828` @ 30%), reexpresado como overlay blanco de baja opacidad | Separadores, bordes de card |
| Borde fuerte | `--border-strong` | `rgba(255,255,255,.16)` | El doble de opacidad del anterior | Bordes en hover/focus, tarjetas en modo móvil |
| Texto primario | `--text-primary` | `#ececec` | Color de texto de body/botones de Open WebUI (`#ebebeb`) | Títulos, valores numéricos destacados |
| Texto secundario | `--text-secondary` | `#a8a8ac` | Texto secundario L85 de Open WebUI (`#cecece`, ajustado a AA sobre `#0d0d0e`) | Copy, ejes de gráfica |
| Texto muted | `--text-muted` | `#6f6f76` | Derivado, para el nivel más bajo de énfasis | Labels, placeholders, ticks |
| Acento único | `--accent` / `-hover` / `-pressed` | `#7c86ff` / `#919aff` / `#615fff` | Único color saturado (índigo) usado por Open WebUI para estados activos | Foco, botón primario, serie principal de gráfica |
| Positivo | `--positive` | `#3fb968` | Punto de estado "online" de Open WebUI (`#00c950`), desaturado para uso extendido en UI | Badges de éxito |
| Advertencia | `--warning` | `#d99a3d` | Convención estándar, sin equivalente directo en Open WebUI | Badges de advertencia |
| Negativo | `--negative` | `#e0616f` | Convención estándar | Errores, acción destructiva |

**Reglas duras:**
- Ningún color va suelto fuera del bloque `:root` de `theme-sooniverse.css`. Si necesitas una variante de opacidad, defínela como token (`--x-soft`, `--x-border`) en el mismo bloque.
- Prohibido reintroducir gradientes decorativos o `box-shadow`/`filter` con resplandor de color. Las únicas sombras permitidas son `--shadow-1`/`--shadow-2` (negras, discretas).
- El logotipo (`static/img/logo_sin_fondo.svg`, `static/img/icon-192x192.png`) conserva su gradiente neón original: es la única excepción intencional, igual que otros productos mantienen un ísotipo de color aunque el resto de la UI sea monocromática.

### 1.1 Paleta de series (gráficas)

`--chart-1: #7c86ff` (= `--accent`) y `--chart-2: #a8a8ac` (= `--text-secondary`) son las dos series activas hoy (Prompt / Completion, barras apiladas). `--chart-3` a `--chart-6` quedan reservados para series futuras y deben mantenerse desaturados y con luminancia decreciente para seguir siendo legibles en escala de grises.

Las gráficas (Chart.js) **leen estas variables en tiempo de ejecución** vía `getComputedStyle(document.documentElement)` — nunca hardcodees un color de serie en JS. Si cambias `theme-sooniverse.css`, la gráfica se re-tematiza sola.

---

## 2. SISTEMA TIPOGRÁFICO

- **Fuente de interfaz:** pila nativa de sistema (`-apple-system, BlinkMacSystemFont, 'Inter', ui-sans-serif, system-ui, 'Segoe UI'...`), idéntica en orden a la de Open WebUI. Cero fuentes cargadas por red.
- **Fuente de datos:** `'JetBrains Mono', 'Fira Code', 'SFMono-Regular', Consolas, monospace` — para todo número, fecha corta, id de API Key o eyebrow. Los números tabulares deben llevar `font-variant-numeric: tabular-nums` (clase `.sv-mono`/`.sv-num`).
- **Escala:** `--text-xs` 11px … `--text-2xl` 40px (ver tokens). Nada de tamaños mágicos fuera de esta escala.
- **Pesos:** 400 (cuerpo), 600 (labels, botones, subtítulos), 700 (H1/H2 y logotipo). Sin ExtraBold 800 salvo el logotipo, que conserva su tratamiento original.

### 2.1 Formato de números

- Enteros: separador de miles `.` y decimales `,` (locale `es-ES` / `intcomma` con `LANGUAGE_CODE=es`).
- Coste en USD: siempre 4 decimales (`$0,0041`).
- **Contadores de tokens** (nunca peticiones ni costes): por debajo de 100.000, separador de miles normal; de 100.000 a menos de 1.000.000, en miles (`125K`); a partir de 1.000.000, en millones (`2,5M`). Implementado en `metrics/templatetags/metrics_extras.py::human_tokens` (servidor) y en el helper `fmtTok()` duplicado en `metrics-filters.js`/`metrics-charts.js` (cliente). Cualquier campo nuevo que muestre tokens debe usar uno de los dos, nunca `intcomma`/`toLocaleString` a secas.

---

## 3. LOGOTIPO Y MARCA EN EL PANEL

- **Ísotipo:** `static/img/logo_sin_fondo.svg` (planeta con anillos, gradiente neón original) — usado como icono de marca junto al wordmark en la cabecera (`.sv-logo__node`, 28×28px) y como favicon (`static/img/icon-192x192.png`, PNG 192×192, con `apple-touch-icon` para iOS).
- **Wordmark:** igual que el manual original — `Sooni` en `--text-primary`, `verse` en `--accent` (ya no en gradiente: un solo acento, consistente con la regla de "acento único, con restricción").
- **Crédito de pie de página:** todo template hereda de `metrics/base.html` un enlace "Creado por **Sooniverse**" hacia `https://sooniverse.co`, estilizado como chip sobrio (`.l-footer__credit`): fondo `--surface-1`, borde `--border-subtle`, sin badge de color. Es la única mención de marca fuera de la cabecera; no debe convertirse en un CTA prominente.

---

## 4. COMPONENTES

### 4.1 Cards
Fondo `--surface-1`, borde 1px `--border-subtle`, radio `--radius-lg` (14px), sombra `--shadow-1`. Hover: solo `border-color` a `--border-strong`, sin transform ni glow.

### 4.2 Botones
- Primario: fondo `--accent`, texto `--surface-0`. Hover `--accent-hover`, active `--accent-pressed`. Sin uppercase, sin transform en hover (a diferencia del manual de marca general).
- Secundario: transparente, borde `--border-strong`.
- Ghost: transparente, borde `--border-subtle`.
- Peligro: transparente, texto/borde `--negative`.
- Todos: `:disabled` con `opacity:.6` + `cursor:not-allowed`; estado `aria-busy="true"` muestra el spinner (§4.6) y `cursor:wait`.

### 4.3 Multi-select
Disclosure nativo `<details class="sv-multiselect">` + checkboxes con `appearance:none` y check propio vía `background-image` SVG (el único punto del sistema donde un color va "hardcodeado" fuera del bloque de tokens, porque `url()` no puede interpolar custom properties — documentado en el propio CSS). Cierre por click-fuera/Escape vía `static/js/multiselect.js`, cargado globalmente. El wrapper usa `width:100%; min-width:0` (nunca un `min-width` fijo mayor que su celda de grid: eso fue la causa de un bug real de solapamiento en `.l-grid--filters` a resoluciones estrechas); el panel desplegable sí puede tener su propio `min-width` porque es `position:absolute` y no participa en el cálculo de la pista del grid.

### 4.3.1 Datepicker (flatpickr)
Vendorizado en `static/js/vendor/flatpickr.min.js` (sin CDN, sin la hoja de estilos oficial — el tema completo vive en `layout.css`/`theme-sooniverse.css` como todo lo demás). El input real (`<input data-flatpickr>`) queda oculto (flatpickr le pone `type="hidden"`) y conserva el valor ISO `Y-m-d` que ya esperan `metrics-filters.js` y los formularios de Django; el que ve el usuario es el `altInput` (mismo `.sv-input`, formato `d/m/Y`, locale es-ES definido inline en `static/js/datepicker.js`). Regla dura de integración: **`.flatpickr-calendar` necesita `position:absolute` explícito** — flatpickr no la trae en su JS, y sin ella el calendario se inserta en el flujo normal del documento en vez de flotar sobre el input (bug real encontrado durante la integración). Cuando el estado de la app cambia una fecha por código (presets, "ampliar rango"), usa `el._flatpickr.setDate(valor, false)` en vez de `el.value = valor`, o el `altInput` visible queda desincronizado del valor real.

### 4.4 Tooltip
`templates/metrics/_tooltip.html` + clases `.sv-tooltip`/`.sv-tooltip__trigger`/`.sv-tooltip__bubble`. Se revela con `:hover`/`:focus-within`, sin JS, nunca con el atributo `title`. Úsalo para aclarar campos no autoexplicativos (p. ej. límites RPM/TPM, semántica de "vacío" en un campo opcional).

### 4.5 Tablas
`--font-mono` + `tabular-nums` en toda columna numérica, alineada a la derecha (`.sv-num`). Filas con separador 1px `--border-subtle`, hover `--surface-1`. En viewports ≤720px, las tablas marcadas `.sv-table--responsive` colapsan a lista de tarjetas (`data-label` + `::before`) para evitar scroll horizontal.

### 4.6 Spinner
Componente único: `templates/metrics/_spinner.html` (`{% include %}`, tamaño opcional `size="lg"`) + `static/js/spinner.js` (auto-wire de `form[data-spinner-submit]`: deshabilita el botón de submit, muestra el spinner, y levanta además un overlay de página completa (`.l-loading-overlay--page`, `position:fixed`) que bloquea filtros y cards mientras el navegador espera la respuesta de un POST clásico — no hay "fin de carga" que ocultarlo salvo la propia navegación). El overlay asíncrono del dashboard (`#metrics-loading`, fetch con AbortController) reutiliza el mismo partial y las mismas clases base; su spinner es `position:fixed` centrado en el viewport a propósito, porque el panel que difumina puede medir miles de píxeles de alto y centrarlo en su contenedor lo dejaría fuera de pantalla (bug real encontrado y corregido). No crear un spinner ad-hoc por página.

### 4.7 Gráficas (Chart.js)
Vendorizado en `static/js/vendor/chart.umd.min.js` (sin CDN). Barras apiladas, `maintainAspectRatio:false` sobre un contenedor de altura fija (`.l-chart-canvas-wrap`, 320px). Grid horizontal `--border-subtle`, eje Y sin línea propia, ticks `--text-muted`, leyenda arriba-derecha y clicable (nativo de Chart.js). Tooltip con separador de miles y, si el valor supera 1M, en millones (§2.1). Cada canvas va acompañado de una tabla `.sv-visually-hidden` con los mismos datos.

---

## 5. MOVIMIENTO Y ACCESIBILIDAD

- Transiciones solo en `background-color`, `border-color`, `opacity`, `transform` — nunca `transition: all`. Duraciones `--dur-fast` (120ms) a `--dur-slow` (320ms).
- `@media (prefers-reduced-motion: reduce)` desactiva toda transición/animación no esencial (incluye el spinner, que pasa a giro estático).
- `:focus-visible` con anillo `--accent` en todo elemento interactivo. Nunca `outline:none` sin reemplazo.
- Contraste mínimo AA: texto primario ≥ 4.5:1, secundario ≥ 3:1 sobre `--surface-0`/`--surface-1`.

---

## 6. DIRECTRICES DE PROMPT ENGINEERING PARA LA IA

```text
[INSTRUCCIÓN DE DISEÑO: PANEL DE MÉTRICAS SOONIVERSE]
Al generar cualquier componente para django_metrics, obedezca la paleta
casi-negra '#0d0d0e' como fondo absoluto y '#7c86ff' como único acento
saturado. No reintroduzca gradientes decorativos, resplandores de color
(box-shadow/filter con color) ni mayúsculas forzadas en botones. Todo color
vive en theme-sooniverse.css (o theme-debug.css como alternativa de prueba);
layout.css es puramente estructural. Los contadores de tokens usan notación
compacta K/M a partir de 100.000 (ver metrics_extras.human_tokens); los
conteos de peticiones y los costes en USD nunca. Si el resultado generado
se parece a una plantilla SaaS genérica con sombras y gradientes vistosos,
deséchelo: la referencia es la sobriedad de Open WebUI, no un dashboard de
marketing.
```
