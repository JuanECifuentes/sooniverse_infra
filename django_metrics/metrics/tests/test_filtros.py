"""
`FiltrosTemporales`: límites aware, rejilla horaria y periodo anterior.

Es el único sitio donde se resuelve la zona horaria del panel; estos tests son
lo que impide que vuelva a colarse un `__date__gte` o un corte en UTC.
"""

from datetime import date, timedelta

from django.test import SimpleTestCase, override_settings

from metrics import filtros as ft


class LimitesTemporales(SimpleTestCase):
    @override_settings(TIME_ZONE="America/Bogota")
    def test_los_limites_son_aware_y_estan_en_hora_local(self):
        f = ft.FiltrosTemporales(desde=date(2026, 8, 1), hasta=date(2026, 8, 1))
        self.assertIsNotNone(f.inicio.tzinfo)
        self.assertEqual(f.inicio.hour, 0)
        # Bogotá es UTC-5 sin horario de verano.
        self.assertEqual(f.inicio.utcoffset(), timedelta(hours=-5))

    def test_el_intervalo_es_semiabierto(self):
        """`fin` es el instante en que empieza el día SIGUIENTE al último: así
        se usa siempre con `__lt` y ninguna petición del último día se pierde
        ni se duplica."""
        f = ft.FiltrosTemporales(desde=date(2026, 8, 1), hasta=date(2026, 8, 3))
        self.assertEqual(f.fin.date(), date(2026, 8, 4))
        self.assertEqual(f.fin.hour, 0)

    def test_un_solo_dia_cuenta_como_un_dia(self):
        f = ft.FiltrosTemporales(desde=date(2026, 8, 1), hasta=date(2026, 8, 1))
        self.assertEqual(f.dias, 1)

    @override_settings(TIME_ZONE="UTC")
    def test_la_zona_sale_de_settings(self):
        f = ft.FiltrosTemporales(desde=date(2026, 8, 1), hasta=date(2026, 8, 1))
        self.assertEqual(f.inicio.utcoffset(), timedelta(0))


class DimensionHoraria(SimpleTestCase):
    def test_sin_filtros_de_ritmo_no_hace_falta_la_tabla_horaria(self):
        f = ft.FiltrosTemporales(desde=date(2026, 8, 1), hasta=date(2026, 8, 5))
        self.assertFalse(f.usa_dimension_horaria)

    def test_filtrar_por_dia_de_la_semana_si_la_necesita(self):
        f = ft.FiltrosTemporales(desde=date(2026, 8, 1), hasta=date(2026, 8, 5),
                                 dias_semana=(6, 7))
        self.assertTrue(f.usa_dimension_horaria)

    def test_filtrar_por_franja_si_la_necesita(self):
        f = ft.FiltrosTemporales(desde=date(2026, 8, 1), hasta=date(2026, 8, 5),
                                 hora_desde=8, hora_hasta=18)
        self.assertTrue(f.usa_dimension_horaria)

    def test_franja_completa_no_cuenta_como_filtro(self):
        f = ft.FiltrosTemporales(desde=date(2026, 8, 1), hasta=date(2026, 8, 5),
                                 hora_desde=0, hora_hasta=23)
        self.assertFalse(f.usa_dimension_horaria)

    def test_dias_seleccionados_por_defecto_son_los_siete(self):
        f = ft.FiltrosTemporales(desde=date(2026, 8, 1), hasta=date(2026, 8, 5))
        self.assertEqual(set(f.dias_seleccionados), {1, 2, 3, 4, 5, 6, 7})


class PeriodoAnterior(SimpleTestCase):
    def test_misma_longitud_inmediatamente_antes(self):
        f = ft.FiltrosTemporales(desde=date(2026, 8, 11), hasta=date(2026, 8, 17))
        prev = f.periodo_anterior()
        self.assertEqual(prev.hasta, date(2026, 8, 10))
        self.assertEqual(prev.desde, date(2026, 8, 4))
        self.assertEqual(prev.dias, f.dias)

    def test_conserva_el_resto_de_filtros(self):
        f = ft.FiltrosTemporales(desde=date(2026, 8, 11), hasta=date(2026, 8, 17),
                                 modelos=("m1",), dias_semana=(6, 7), estado="errores")
        prev = f.periodo_anterior()
        self.assertEqual(prev.modelos, ("m1",))
        self.assertEqual(prev.dias_semana, (6, 7))
        self.assertEqual(prev.estado, "errores")


class RejillaHoraria(SimpleTestCase):
    @override_settings(TIME_ZONE="America/Bogota")
    def test_un_dia_completo_son_24_horas(self):
        f = ft.FiltrosTemporales(desde=date(2026, 8, 17), hasta=date(2026, 8, 17))
        self.assertEqual(len(ft.horas_de_la_rejilla(f)), 24)

    @override_settings(TIME_ZONE="America/Bogota")
    def test_la_franja_recorta_la_rejilla(self):
        f = ft.FiltrosTemporales(desde=date(2026, 8, 17), hasta=date(2026, 8, 17),
                                 hora_desde=8, hora_hasta=17)
        self.assertEqual(len(ft.horas_de_la_rejilla(f)), 10)

    @override_settings(TIME_ZONE="America/Bogota")
    def test_el_dia_de_la_semana_recorta_la_rejilla(self):
        # 17/08/2026 es lunes; la semana entera son 7 días.
        f = ft.FiltrosTemporales(desde=date(2026, 8, 17), hasta=date(2026, 8, 23),
                                 dias_semana=(6, 7))
        self.assertEqual(len(ft.horas_de_la_rejilla(f)), 48)

    @override_settings(TIME_ZONE="America/Bogota")
    def test_las_horas_de_la_rejilla_estan_en_hora_local(self):
        f = ft.FiltrosTemporales(desde=date(2026, 8, 17), hasta=date(2026, 8, 17),
                                 hora_desde=13, hora_hasta=13)
        horas = ft.horas_de_la_rejilla(f)
        self.assertEqual(len(horas), 1)
        # Las 13:00 en Bogotá son las 18:00 UTC.
        self.assertEqual(horas[0].astimezone(ft.tz()).hour, 13)


class Eco(SimpleTestCase):
    def test_la_exclusion_del_benchmark_se_declara_siempre(self):
        """Un filtro activo que no viaja al cliente es un filtro que el usuario
        no puede ver ni quitar."""
        f = ft.FiltrosTemporales(desde=date(2026, 8, 1), hasta=date(2026, 8, 5))
        self.assertTrue(f.eco()["benchmark_excluida"])
        f2 = ft.FiltrosTemporales(desde=date(2026, 8, 1), hasta=date(2026, 8, 5),
                                  incluir_benchmark=True)
        self.assertFalse(f2.eco()["benchmark_excluida"])
