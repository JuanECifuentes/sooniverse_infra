"""
Rampa de color y densificación del mapa de calor.

La intensidad de cada celda y los cortes de la leyenda los calcula el SERVIDOR
(no el JS) precisamente para poder probarlos aquí: el cliente queda como render
tonto y no hace falta montar un runner de JavaScript.
"""

from django.test import SimpleTestCase

from metrics.analytics import NIVELES_RAMPA, _cortes_por_cuantiles, _intensidad


class CortesPorCuantiles(SimpleTestCase):
    def test_sin_valores_no_hay_cortes(self):
        self.assertEqual(_cortes_por_cuantiles([]), [])

    def test_solo_ceros_no_hay_cortes(self):
        self.assertEqual(_cortes_por_cuantiles([0, 0, 0]), [])

    def test_pocos_valores_se_usan_tal_cual(self):
        self.assertEqual(_cortes_por_cuantiles([3, 1, 2]), [1, 2, 3])

    def test_cortes_son_crecientes(self):
        valores = [i for i in range(1, 200)]
        cortes = _cortes_por_cuantiles(valores)
        self.assertEqual(len(cortes), NIVELES_RAMPA)
        self.assertEqual(cortes, sorted(cortes))

    def test_los_cortes_son_por_cuantiles_y_no_lineales(self):
        """Con una hora pico aislada, una escala lineal sobre el máximo dejaría
        todo lo demás en el primer paso y el mapa saldría plano. Por cuantiles,
        las celdas normales siguen repartidas por la rampa."""
        valores = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 1000]
        cortes = _cortes_por_cuantiles(valores)
        self.assertLess(cortes[0], 100, "el primer corte no puede depender del outlier")
        self.assertEqual(cortes[-1], 1000)

    def test_los_ceros_no_participan_en_los_cuantiles(self):
        con_ceros = _cortes_por_cuantiles([0] * 100 + [10, 20, 30, 40, 50, 60])
        sin_ceros = _cortes_por_cuantiles([10, 20, 30, 40, 50, 60])
        self.assertEqual(con_ceros, sin_ceros)


class Intensidad(SimpleTestCase):
    def test_cero_siempre_es_el_paso_cero(self):
        """El paso 0 es "sin tráfico", no "poco tráfico": se pinta con
        superficie, no con acento."""
        self.assertEqual(_intensidad(0, [1, 2, 3, 4, 5]), 0)

    def test_sin_cortes_todo_es_paso_cero(self):
        self.assertEqual(_intensidad(99, []), 0)

    def test_el_valor_maximo_llega_al_ultimo_paso(self):
        cortes = [10, 20, 30, 40, 50]
        self.assertEqual(_intensidad(50, cortes), 5)
        self.assertEqual(_intensidad(999, cortes), 5)

    def test_reparto_por_tramos(self):
        cortes = [10, 20, 30, 40, 50]
        self.assertEqual(_intensidad(1, cortes), 1)
        self.assertEqual(_intensidad(10, cortes), 1)
        self.assertEqual(_intensidad(11, cortes), 2)
        self.assertEqual(_intensidad(35, cortes), 4)

    def test_la_intensidad_nunca_excede_la_rampa(self):
        cortes = [1, 2, 3, 4, 5]
        for v in (0, 1, 3, 5, 10, 10_000):
            self.assertLessEqual(_intensidad(v, cortes), NIVELES_RAMPA)
