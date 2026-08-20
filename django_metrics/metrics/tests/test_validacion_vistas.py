"""
Validación de los filtros en `metrics_api` y `lente_api`.

Se ejercita `_parse_filtros` directamente con RequestFactory: sin BD, sin
sesión y sin decoradores. El objetivo es que los mensajes de error sigan siendo
accionables y en español, que es lo que ve el operador cuando el panel rechaza
una combinación de filtros.
"""

from datetime import date

from django.test import RequestFactory, SimpleTestCase

from metrics import filtros as ft, views

rf = RequestFactory()


def parsea(**datos):
    listas = {}
    for k, v in datos.items():
        listas[k] = v if isinstance(v, list) else [v]
    return views._parse_filtros(rf.post("/x/", listas))


def error_de(**datos):
    f, err = parsea(**datos)
    assert f is None, "se esperaba un error de validación"
    return err


class ValidacionDeFechas(SimpleTestCase):
    def test_rango_valido(self):
        f, err = parsea(desde="2026-08-01", hasta="2026-08-19")
        self.assertIsNone(err)
        self.assertEqual((f.desde, f.hasta), (date(2026, 8, 1), date(2026, 8, 19)))

    def test_fecha_desde_invalida(self):
        err = error_de(desde="ayer", hasta="2026-08-19")
        self.assertEqual(err.status_code, 400)
        self.assertIn("AAAA-MM-DD", err.content.decode())

    def test_fecha_hasta_invalida(self):
        self.assertEqual(error_de(desde="2026-08-01", hasta="32/13/2026").status_code, 400)

    def test_desde_posterior_a_hasta(self):
        err = error_de(desde="2026-08-19", hasta="2026-08-01")
        self.assertIn("posterior", err.content.decode())

    def test_sin_fechas_usa_el_rango_por_defecto(self):
        f, err = parsea()
        self.assertIsNone(err)
        self.assertLess(f.desde, f.hasta)


class ValidacionDeRitmo(SimpleTestCase):
    def test_dias_de_la_semana_validos(self):
        f, err = parsea(desde="2026-08-01", hasta="2026-08-19", dow=["1", "7"])
        self.assertIsNone(err)
        self.assertEqual(f.dias_semana, (1, 7))

    def test_dia_fuera_de_rango(self):
        err = error_de(desde="2026-08-01", hasta="2026-08-19", dow=["0"])
        self.assertIn("1 (lunes)", err.content.decode())
        self.assertEqual(error_de(desde="2026-08-01", hasta="2026-08-19",
                                  dow=["8"]).status_code, 400)

    def test_dia_no_numerico(self):
        self.assertEqual(error_de(desde="2026-08-01", hasta="2026-08-19",
                                  dow=["lunes"]).status_code, 400)

    def test_dias_repetidos_se_deduplican_y_ordenan(self):
        f, _ = parsea(desde="2026-08-01", hasta="2026-08-19", dow=["7", "1", "7"])
        self.assertEqual(f.dias_semana, (1, 7))

    def test_franja_valida(self):
        f, err = parsea(desde="2026-08-01", hasta="2026-08-19",
                        hora_desde="8", hora_hasta="18")
        self.assertIsNone(err)
        self.assertEqual((f.hora_desde, f.hora_hasta), (8, 18))

    def test_franja_fuera_de_rango(self):
        err = error_de(desde="2026-08-01", hasta="2026-08-19", hora_desde="24")
        self.assertIn("entre 0 y 23", err.content.decode())

    def test_franja_invertida(self):
        err = error_de(desde="2026-08-01", hasta="2026-08-19",
                       hora_desde="20", hora_hasta="5")
        self.assertIn("posterior", err.content.decode())

    def test_estado_invalido(self):
        err = error_de(desde="2026-08-01", hasta="2026-08-19", estado="regular")
        self.assertIn("'todas' o 'errores'", err.content.decode())

    def test_estado_valido(self):
        f, err = parsea(desde="2026-08-01", hasta="2026-08-19", estado="errores")
        self.assertIsNone(err)
        self.assertEqual(f.estado, ft.ESTADO_ERRORES)


class ExclusionDeBenchmark(SimpleTestCase):
    def test_por_defecto_se_excluye(self):
        f, _ = parsea(desde="2026-08-01", hasta="2026-08-19")
        self.assertFalse(f.incluir_benchmark)

    def test_se_puede_incluir_explicitamente(self):
        for valor in ("1", "true", "on"):
            f, _ = parsea(desde="2026-08-01", hasta="2026-08-19", incluir_benchmark=valor)
            self.assertTrue(f.incluir_benchmark, f"'{valor}' debería activarlo")


class TopesDeCoste(SimpleTestCase):
    """Las guardas que impiden que un filtro dispare la consulta más cara del
    panel sobre un rango absurdo."""

    def test_hourly_tiene_tope_de_dias(self):
        self.assertEqual(ft.HOURLY_MAX_DIAS, 14)

    def test_p95_tiene_tope_de_dias(self):
        self.assertEqual(ft.P95_MAX_DIAS, 90)

    def test_hourly_no_esta_en_las_granularidades_del_rollup(self):
        """`hourly` la sirve usage_hourly, no token_usage_rollup: validarla
        contra el modelo equivocado la rechazaría siempre."""
        from metrics.models import TokenUsageRollup
        self.assertNotIn(ft.HOURLY, dict(TokenUsageRollup.GRANULARITIES))
        self.assertIn(ft.HOURLY, dict(ft.GRANULARIDADES_PANEL))
