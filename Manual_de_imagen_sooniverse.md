# 📘 MANUAL DE IDENTIDAD DE MARCA: SOONIVERSE
## Versión: 1.0.0 (B2B Tech Optimization)
## Target: Parseable por IA / Desarrolladores / Diseñadores UI-UX

---

## 1. ESPECIFICACIONES DE COLOR (PALETA CROMÁTICA RESTRIGIDA)

El uso de colores es estricto. Queda prohibido el uso de fondos claros (#FFFFFF o similares)[cite: 1]. La interfaz debe mantener una estética oscura profunda con acentos neón de alto contraste[cite: 1].

| Rol de Color | Nombre Técnico | Código HEX | Uso en Interfaz (UI) |
| :--- | :--- | :--- | :--- |
| Background Principal | Deep Space | `#070A12` | Fondos de página, secciones y contenedores maestros[cite: 1]. |
| Background Secundario | Cyber Surface | `#0F172A` | Tarjetas (Cards), modales, dropdowns y bordes de control. |
| Texto Principal | Pure White | `#FFFFFF` | Títulos principales, copy destacado y cuerpo de texto legible[cite: 1]. |
| Texto Secundario | Slate Gray | `#94A3B8` | Subtítulos, textos explicativos, microcopy y deshabilitados. |
| Acento Primario (AI) | Neon Green | `#00FF87` | CTAs principales, badges de éxito, métricas y texto destacado[cite: 1]. |
| Acento Secundario (Infra) | Electric Cyan | `#60EFFF` | Bordes activos, enlaces hover, nodos e iluminación decorativa[cite: 1]. |
| Acento Terciario (Secure) | Cosmic Violet | `#8A2BE2` | Degradados secundarios, sombras de neón y estados especiales[cite: 1]. |

### 1.1 CONFIGURACIÓN DE DEGRADADOS OFICIALES
Cualquier gradiente utilizado en texto o elementos gráficos debe configurarse exactamente bajo este vector lineal:
*   **Nombre:** `cosmic-gradient`
*   **Tipo:** Linear Gradient (`linear-gradient`)
*   **Ángulo de inclinación:** 135 grados (`135deg`)
*   **Paradas de color (Color Stops):**
    *   `0%` -> `#00FF87` (Neon Green)[cite: 1]
    *   `50%` -> `#60EFFF` (Electric Cyan)[cite: 1]
    *   `100%` -> `#8A2BE2` (Cosmic Violet)[cite: 1]

---

## 2. SISTEMA TIPOGRÁFICO Y JERARQUÍA

El emparejamiento tipográfico combina la legibilidad moderna B2B con la estética cruda del código técnico[cite: 1].

### 2.1 Fuentes del Sistema
1.  **Fuente Principal (Prosa y Títulos):** `Inter` o `Plus Jakarta Sans` (Sans-serif limpia de alta legibilidad).
2.  **Fuente Secundaria (Código, Datos, Métricas y Subtítulos de Marca):** `JetBrains Mono` o `Fira Code` (Monospace de precisión técnica)[cite: 1].

### 2.2 Jerarquía de Texto (Escala UI)
*   **H1 (Títulos Hero / Apertura):** `Inter`, 56px, ExtraBold (800), Tracking: -1.5px. Color: `#FFFFFF`[cite: 1].
*   **H2 (Títulos de Sección):** `Inter`, 36px, Bold (700), Tracking: -1px. Color: `#FFFFFF`.
*   **H3 (Títulos de Componentes/Cards):** `Inter`, 22px, SemiBold (600). Color: `#FFFFFF`.
*   **Body Text (Cuerpo de copia):** `Inter`, 16px, Regular (400), Line-Height: 1.6. Color: `#94A3B8` (Slate Gray).
*   **Taglines / Badges / Métricas:** `JetBrains Mono`, 12px a 14px, SemiBold (600), Text-Transform: UPPERCASE, Letter-Spacing: 6px. Color: `#00FF87`[cite: 1].

---

## 3. IDENTIDAD VERBAL DE MARCA (LOGOTIPO)

### 3.1 Naming Estricto
*   **Escritura Correcta:** `Sooniverse` (Capitalización en la letra S únicamente).
*   **Estilo del Texto del Logo:** La palabra se divide visualmente mediante color, manteniendo una sola tipografía unificada (`Inter` ExtraBold 800)[cite: 1]:
    *   `Sooni` -> Renderizado en color `#FFFFFF` (Pure White)[cite: 1].
    *   `verse` -> Renderizado aplicando el `cosmic-gradient`[cite: 1].

### 3.2 Tagline Oficial Unificado
Queda prohibido el uso de la frase "PRIVATE AI PLATFORM" como descriptor general[cite: 1]. El nuevo subtítulo mandatorio que engloba todos los servicios de la empresa es:
*   **Texto:** `ADVANCED TECH UNIVERSE`[cite: 2]
*   **Estilo:** `JetBrains Mono`, UPPERCASE, Bold (700), Letter-Spacing: 6.5px[cite: 1]. Color: `#00FF87`[cite: 1].

---

## 4. COMPONENTES VISUALES Y REGLAS DE DISEÑO DE INTERFAZ (UI)

Para evitar la apariencia de plantillas web genéricas, la IA debe implementar las siguientes especificaciones técnicas de diseño[cite: 1]:

### 4.1 Contenedores y Tarjetas (Cards)
*   **Fondo:** `#0F172A` (Cyber Surface) con una opacidad del 80% si se requiere traslucidez (`rgba(15, 23, 42, 0.8)`).
*   **Bordes:** 1px sólido, color `#1E293B` (Gris oscuro estructural).
*   **Radio de Esquina (Border Radius):** `12px` (Bordes suavizados modernos, no agresivos).
*   **Efecto de Fondo:** Filtro de desenfoque de fondo (`backdrop-filter: blur(12px)`) para paneles flotantes.

### 4.2 Botones y Elementos de Acción (CTAs)
*   **Botón Principal (Primary CTA):**
    *   **Fondo:** `cosmic-gradient`[cite: 1].
    *   **Texto:** `#070A12` (Deep Space), `Inter` Bold (700), UPPERCASE.
    *   **Comportamiento Hover:** Micro-interacción obligatoria de desplazamiento hacia arriba de 2px (`transform: translateY(-2px)`) con una transición suave de 0.3 segundos (`transition: all 0.3s ease`) y un aumento ligero del resplandor de sombra[cite: 1].
*   **Botón Secundario (Secondary CTA):**
    *   **Fondo:** Transparente (`transparent`).
    *   **Borde:** 1.5px sólido con color `#60EFFF`[cite: 1].
    *   **Texto:** `#FFFFFF`[cite: 1].

### 4.3 Efectos de Resplandor Neón (Glow Effects)
Los elementos interactivos clave o estados activos (como los nodos del logotipo o botones seleccionados) deben simular luz activa mediante sombras paralelas (`box-shadow` o `filter: drop-shadow`)[cite: 1]:
*   **Fórmula del Brillo:** `drop-shadow(0px 0px 12px rgba(96, 239, 255, 0.6))` (Usa el color del acento correspondiente).

### 4.4 Materialidad de la Interfaz
*   **Textura:** Se debe aplicar un efecto de grano/ruido digital estático extremadamente sutil (opacidad entre 1.5% y 2%) sobre todo el fondo de la pantalla para otorgar materialidad analógica y romper la planitud digital[cite: 1].

---

## 5. DIRECTRICES DE PROMPT ENGINEERING PARA LA IA (INSTRUCCIONES DIRECTAS)

```text
[INSTRUCCIÓN MAESTRA DE DISEÑO GENERATIVO: SOONIVERSE]
Usted es un diseñador frontend e ingeniero UI experto. Al generar cualquier componente visual, código CSS/Tailwind, o imágenes para la marca 'Sooniverse', usted debe obedecer ciegamente la paleta cromática '#070A12' como fondo absoluto. No use colores pastel, no use fondos blancos, no use bordes redondeados excesivos estilo burbuja (máximo 12px). Los textos del logotipo deben respetar la división exacta de color: 'Sooni' en blanco puro, 'verse' en gradiente neón. El tono de la comunicación visual debe ser tecnológico, sofisticado, de alta ciberseguridad y con alto contraste interactivo. Si el diseño generado parece una plantilla de negocio estándar o un sitio web corporativo tradicional, deséchelo y vuelva a escribir aplicando los principios de contraste oscuro y brillo neón controlados.