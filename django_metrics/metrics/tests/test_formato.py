"""
`human_tokens` y su espejo en JavaScript.

Hay dos implementaciones de la misma regla -una en Python de servidor
(metrics_extras) y otra en JS de cliente (static/js/format.js)- y no pueden
compartir código. Esta es la tabla de casos canónica; el último test vigila el
archivo JS como texto para atrapar la divergencia por descuido sin montar un
runner de JavaScript, que sería la primera dependencia de build del proyecto.
"""

from pathlib import Path

from django.test import SimpleTestCase

from metrics.templatetags.metrics_extras import human_tokens

FORMAT_JS = Path(__file__).resolve().parents[2] / "static" / "js" / "format.js"

# (entrada, salida esperada). Los umbrales son 100.000 y 1.000.000.
CASOS = [
    (0, "0"),
    (1, "1"),
    (999, "999"),
    (99_999, "99.999"),      # justo por debajo del umbral: separador de miles
    (100_000, "100K"),       # umbral exacto: pasa a miles
    (125_400, "125,4K"),
    (999_999, "1000K"),
    (1_000_000, "1M"),       # umbral exacto: pasa a millones
    (2_500_000, "2,5M"),
]


class HumanTokens(SimpleTestCase):
    def test_tabla_de_casos(self):
        for entrada, esperado in CASOS:
            with self.subTest(entrada=entrada):
                self.assertEqual(human_tokens(entrada), esperado)

    def test_none_no_revienta(self):
        self.assertEqual(human_tokens(None), "0")

    def test_texto_no_numerico_no_revienta(self):
        self.assertEqual(human_tokens("abc"), "abc")


class EspejoJavaScript(SimpleTestCase):
    """Guarda barata contra la divergencia entre las dos implementaciones."""

    def test_format_js_existe(self):
        self.assertTrue(FORMAT_JS.exists(), f"falta {FORMAT_JS}")

    def test_los_umbrales_coinciden(self):
        fuente = FORMAT_JS.read_text(encoding="utf-8")
        self.assertIn("1_000_000", fuente)
        self.assertIn("100_000", fuente)
        self.assertIn('"M"', fuente)
        self.assertIn('"K"', fuente)

    def test_format_js_declara_que_es_un_espejo(self):
        """Sin el comentario cruzado, el siguiente que toque los umbrales no
        sabrá que hay una segunda implementación que actualizar."""
        fuente = FORMAT_JS.read_text(encoding="utf-8")
        self.assertIn("human_tokens", fuente)

    def test_los_modulos_js_no_redefinen_fmtTok(self):
        """Las tres copias antiguas se sustituyeron por un import; si alguien
        vuelve a pegar una, este test lo caza."""
        js_dir = FORMAT_JS.parent
        for archivo in js_dir.glob("*.js"):
            if archivo.name == "format.js":
                continue
            fuente = archivo.read_text(encoding="utf-8")
            self.assertNotIn("function fmtTok", fuente,
                             f"{archivo.name} redefine fmtTok en vez de importarlo")
