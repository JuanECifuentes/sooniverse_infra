"""
`analytics.rachas_vacias`: tramos contiguos sin tráfico.

Es la función con más casos borde de todo el módulo -huecos al principio y al
final, rejilla fragmentada por los filtros de día y franja- y es pura, así que
se prueba entera sin PostgreSQL. Es lo que responde "¿en qué momento del fin de
semana está sin uso la máquina?".
"""

from datetime import datetime, timedelta, timezone

from django.test import SimpleTestCase

from metrics.analytics import rachas_vacias

UTC = timezone.utc


def rejilla(inicio_h, n):
    base = datetime(2026, 8, 17, inicio_h, tzinfo=UTC)
    return [base + timedelta(hours=i) for i in range(n)]


class RachasVacias(SimpleTestCase):
    def test_rejilla_vacia_no_produce_rachas(self):
        self.assertEqual(rachas_vacias([], set()), [])

    def test_todo_activo_no_produce_rachas(self):
        r = rejilla(0, 5)
        self.assertEqual(rachas_vacias(r, set(r)), [])

    def test_todo_vacio_produce_una_sola_racha(self):
        r = rejilla(0, 5)
        rachas = rachas_vacias(r, set())
        self.assertEqual(len(rachas), 1)
        inicio, fin, horas = rachas[0]
        self.assertEqual(inicio, r[0])
        self.assertEqual(horas, 5)
        # El fin es EXCLUSIVO: la última hora vacía es r[4], así que la ventana
        # termina cuando empieza r[5].
        self.assertEqual(fin, r[4] + timedelta(hours=1))

    def test_hueco_en_medio(self):
        r = rejilla(0, 6)
        activas = {r[0], r[1], r[5]}
        rachas = rachas_vacias(r, activas)
        self.assertEqual(len(rachas), 1)
        inicio, fin, horas = rachas[0]
        self.assertEqual((inicio, horas), (r[2], 3))
        self.assertEqual(fin, r[5])

    def test_dos_huecos_separados(self):
        r = rejilla(0, 7)
        activas = {r[2], r[5]}
        rachas = rachas_vacias(r, activas)
        self.assertEqual([h for _, _, h in rachas], [2, 2, 1])

    def test_hueco_al_final_se_cierra(self):
        """El bug clásico: si la rejilla termina en hueco y no se cierra la
        racha abierta, la última franja ociosa no se reporta nunca."""
        r = rejilla(0, 4)
        rachas = rachas_vacias(r, {r[0]})
        self.assertEqual(len(rachas), 1)
        self.assertEqual(rachas[0][2], 3)

    def test_hueco_al_principio_se_reporta(self):
        r = rejilla(0, 4)
        rachas = rachas_vacias(r, {r[3]})
        self.assertEqual(rachas[0][0], r[0])
        self.assertEqual(rachas[0][2], 3)

    def test_rejilla_fragmentada_no_une_tramos_no_contiguos(self):
        """Con la franja horaria filtrada, la rejilla salta. Dos horas no
        adyacentes NO forman una ventana continua: unirlas reportaría una
        franja de 15 h de ocio que en realidad son dos de 2 h con actividad
        (fuera de la franja) en medio."""
        base = datetime(2026, 8, 17, 8, tzinfo=UTC)
        r = [base, base + timedelta(hours=1),                       # 08, 09
             base + timedelta(hours=24), base + timedelta(hours=25)]  # 08, 09 del día siguiente
        rachas = rachas_vacias(r, set())
        self.assertEqual(len(rachas), 2, "un salto en la rejilla debe cortar la racha")
        self.assertEqual([h for _, _, h in rachas], [2, 2])

    def test_una_sola_hora_vacia(self):
        r = rejilla(0, 3)
        rachas = rachas_vacias(r, {r[0], r[2]})
        self.assertEqual(len(rachas), 1)
        self.assertEqual(rachas[0][2], 1)

    def test_fin_de_semana_completo(self):
        """El caso de negocio: viernes 18:00 a lunes 07:00 sin una petición."""
        viernes_18 = datetime(2026, 8, 21, 18, tzinfo=UTC)
        r = [viernes_18 + timedelta(hours=i) for i in range(62)]
        activas = {r[-1]}
        rachas = rachas_vacias(r, activas)
        self.assertEqual(len(rachas), 1)
        self.assertEqual(rachas[0][2], 61)
