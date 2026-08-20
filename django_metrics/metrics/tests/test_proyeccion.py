"""
`capacidad.regresion_lineal` y la puerta de confianza de la proyección.

Este número lo va a leer una persona para decidir si gasta dinero en más
infraestructura. Con 4-8 puntos ruidosos una regresión lineal produce
resultados convincentes y falsos, así que la puerta de r² no es opcional:
"sin tendencia clara" es una respuesta mejor que "≈6 semanas" inventado.
"""

from django.test import SimpleTestCase

from metrics.capacidad import R2_MINIMO, regresion_lineal


class RegresionLineal(SimpleTestCase):
    def test_serie_perfectamente_lineal(self):
        m, b, r2 = regresion_lineal([0, 1, 2, 3], [10, 20, 30, 40])
        self.assertAlmostEqual(m, 10.0)
        self.assertAlmostEqual(b, 10.0)
        self.assertAlmostEqual(r2, 1.0)

    def test_serie_descendente_da_pendiente_negativa(self):
        m, _, r2 = regresion_lineal([0, 1, 2, 3], [40, 30, 20, 10])
        self.assertAlmostEqual(m, -10.0)
        self.assertAlmostEqual(r2, 1.0)

    def test_serie_plana_no_tiene_tendencia(self):
        m, b, _ = regresion_lineal([0, 1, 2, 3], [25, 25, 25, 25])
        self.assertAlmostEqual(m, 0.0)
        self.assertAlmostEqual(b, 25.0)

    def test_serie_ruidosa_no_pasa_la_puerta_de_confianza(self):
        _, _, r2 = regresion_lineal([0, 1, 2, 3], [10, 90, 15, 80])
        self.assertLess(r2, R2_MINIMO,
                        "una serie sin tendencia real no debe producir una proyección")

    def test_tendencia_clara_con_algo_de_ruido_si_pasa(self):
        _, _, r2 = regresion_lineal([0, 1, 2, 3, 4], [10, 21, 29, 42, 48])
        self.assertGreaterEqual(r2, R2_MINIMO)

    def test_r2_nunca_es_negativo(self):
        """Un ajuste peor que la media daría r² < 0 en la fórmula cruda; se
        satura en 0 para que la puerta compare siempre en el mismo rango."""
        _, _, r2 = regresion_lineal([0, 1, 2], [5, 5, 5])
        self.assertGreaterEqual(r2, 0.0)

    def test_un_solo_punto_no_revienta(self):
        m, b, r2 = regresion_lineal([0], [42])
        self.assertEqual((m, b, r2), (0.0, 42.0, 0.0))

    def test_lista_vacia_no_revienta(self):
        m, b, r2 = regresion_lineal([], [])
        self.assertEqual((m, b, r2), (0.0, 0.0, 0.0))

    def test_todas_las_x_iguales_no_divide_por_cero(self):
        m, _, r2 = regresion_lineal([2, 2, 2], [1, 5, 9])
        self.assertEqual(m, 0.0)
        self.assertEqual(r2, 0.0)
