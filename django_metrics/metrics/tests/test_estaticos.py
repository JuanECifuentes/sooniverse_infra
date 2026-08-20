"""
Invariantes de los archivos estáticos.

Todos nacen de un fallo real: `collectstatic` abortaba porque un `.js`
vendorizado referenciaba un sourcemap que no se distribuye, el manifiesto nunca
se escribía, y WhiteNoise caía a rutas sin hash. El panel seguía funcionando, así
que el fallo pasó desapercibido durante mucho tiempo -y `entrypoint.sh` lo
silenciaba con `>/dev/null 2>&1 || true`-.
"""

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

STATIC_DIR = Path(__file__).resolve().parents[2] / "static"
JS_DIR = STATIC_DIR / "js"
VENDOR_DIR = JS_DIR / "vendor"

# `import x from "./y.js"` / `export * from "./y.js"`
IMPORT_RE = re.compile(r"""(?:^|\s)(?:import|export)\b[^;'"]*?from\s+["'](\./[^"']+)["']""")


class SourcemapsVendorizados(SimpleTestCase):
    def test_ningun_js_referencia_un_sourcemap_que_no_se_distribuye(self):
        """Un `//# sourceMappingURL=` apuntando a un .map ausente hace fallar
        el post-procesado de ManifestStaticFilesStorage y deja al panel sin
        manifiesto de estáticos."""
        for js in JS_DIR.rglob("*.js"):
            fuente = js.read_text(encoding="utf-8", errors="replace")
            for m in re.finditer(r"^//# sourceMappingURL=(.+)$", fuente, re.MULTILINE):
                destino = (js.parent / m.group(1).strip()).resolve()
                self.assertTrue(
                    destino.exists(),
                    f"{js.name} referencia el sourcemap ausente '{m.group(1)}'. "
                    "O se vendoriza el .map, o se quita el comentario.",
                )

    def test_los_vendorizados_tienen_su_archivo_de_licencia(self):
        for js in VENDOR_DIR.glob("*.min.js"):
            licencias = list(VENDOR_DIR.glob("*LICENSE.md"))
            self.assertTrue(licencias, "no hay ningún archivo de licencia en vendor/")
        # El plugin nuevo tiene que traer la suya y fijar la versión.
        matrix = VENDOR_DIR / "CHARTJS-MATRIX-LICENSE.md"
        self.assertTrue(matrix.exists(), "falta CHARTJS-MATRIX-LICENSE.md")
        texto = matrix.read_text(encoding="utf-8")
        self.assertIn("MIT", texto)
        self.assertRegex(texto, r"v\d+\.\d+\.\d+", "la licencia debe fijar la versión exacta")


class ImportsDeModulos(SimpleTestCase):
    def test_todo_import_relativo_apunta_a_un_archivo_existente(self):
        for js in JS_DIR.glob("*.js"):
            fuente = js.read_text(encoding="utf-8", errors="replace")
            for destino in IMPORT_RE.findall(fuente):
                ruta = (js.parent / destino).resolve()
                self.assertTrue(ruta.exists(),
                                f"{js.name} importa '{destino}', que no existe")

    def test_el_almacenamiento_de_produccion_reescribe_los_imports(self):
        """Sin `support_js_module_import_aggregation`, un
        `metrics-charts.<hash>.js` pediría `./format.js` sin hash y recibiría un
        404: el módulo entero dejaría de cargar y la página perdería toda su
        interactividad.

        Se comprueba la CLASE, no `storages["staticfiles"]`: en desarrollo
        (DEBUG=True) se usa a propósito el almacenamiento plano, porque
        `runserver` no sabe resolver nombres con hash.
        """
        from sooniverse_panel.storage import SooniverseStaticFilesStorage

        self.assertTrue(SooniverseStaticFilesStorage.support_js_module_import_aggregation)

    def test_el_backend_configurado_es_uno_de_los_dos_previstos(self):
        """No se compara contra `settings.DEBUG`: el runner de tests lo fuerza a
        False, pero STORAGES ya se evaluó al importar settings con el valor real
        del entorno. Lo que se comprueba es que no haya un tercer valor por un
        typo en la ruta del backend."""
        self.assertIn(
            settings.STORAGES["staticfiles"]["BACKEND"],
            {
                "django.contrib.staticfiles.storage.StaticFilesStorage",
                "sooniverse_panel.storage.SooniverseStaticFilesStorage",
            },
        )

    def test_settings_declara_el_storage_de_produccion(self):
        fuente = (Path(settings.BASE_DIR) / "sooniverse_panel" / "settings.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("sooniverse_panel.storage.SooniverseStaticFilesStorage", fuente)


class EntrypointNoSilenciaCollectstatic(SimpleTestCase):
    def test_collectstatic_no_se_ejecuta_con_la_salida_descartada(self):
        entrypoint = Path(__file__).resolve().parents[2] / "entrypoint.sh"
        linea = next(
            (l for l in entrypoint.read_text(encoding="utf-8").splitlines()
             if "collectstatic" in l and not l.strip().startswith("#")),
            "",
        )
        self.assertNotIn(">/dev/null", linea,
                         "collectstatic no puede volver a ejecutarse con la salida oculta")


class LentesNoDependenDelOrdenDeCarga(SimpleTestCase):
    """Bug real encontrado probando en el navegador (no solo en tests): el
    primer 'metrics:params' que dispara metrics-filters.js podía llegar antes
    de que metrics-lente.js registrara su listener, porque el orden de los
    <script type="module"> no lo garantiza. En una página recién cargada, sin
    tocar ningún filtro, hacer clic en 'Mapa semanal' o 'Perfil horario' no
    hacía nada -cero peticiones de red, ninguna instancia de Chart.js-.

    El arreglo es un respaldo SÍNCRONO (panel.getParamsLente), no una espera
    por temporizador: se comprueba aquí como texto para que nadie lo quite sin
    darse cuenta al tocar estos archivos."""

    JS_DIR = JS_DIR

    def test_metrics_filters_expone_el_respaldo_sincrono(self):
        fuente = (self.JS_DIR / "metrics-filters.js").read_text(encoding="utf-8")
        self.assertIn("panel.getParamsLente", fuente)

    def test_metrics_lente_usa_el_respaldo_cuando_no_hay_evento_previo(self):
        fuente = (self.JS_DIR / "metrics-lente.js").read_text(encoding="utf-8")
        self.assertIn("panel.getParamsLente", fuente)
        # La guarda ya NO debe cortar en seco solo por ultimosParams===null sin
        # intentar antes el respaldo directo.
        self.assertNotIn('!url || ultimosParams === null', fuente,
                         "la guarda de cargarLente() volvió a depender solo del evento")
