"""
Pruebas del rate limiting por IP (metrics/ratelimit.py): ventana fija sobre el
cache, conteo solo de los verbos configurados, respuesta 429 para JSON y
redirect con mensaje para páginas, y validación del formato '<N>/<unidad>'.

Mismo enfoque que el resto del proyecto: SimpleTestCase sin BD real. Cada test
usa un LOCATION de LocMemCache propio para arrancar con el contador a cero.
"""

from unittest.mock import patch

from django.http import HttpResponse
from django.test import SimpleTestCase, override_settings

from metrics.ratelimit import _parse_rate, client_ip, rate_limit

RF_PATH = "/panel/metrics/"

_RL = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def _cache_nuevo(location):
    return override_settings(
        RATELIMITS={"test": "2/m"},
        CACHES={**_RL, "default": {**_RL["default"], "LOCATION": location}},
    )


class ParseRateTests(SimpleTestCase):
    def test_formatos_validos(self):
        self.assertEqual(_parse_rate("10/m"), (10, 60))
        self.assertEqual(_parse_rate("5/s"), (5, 1))
        self.assertEqual(_parse_rate("100/h"), (100, 3600))

    def test_formato_invalido(self):
        for rate in ("10", "diez/m", "0/m", "10/x", "10/", "/m"):
            with self.assertRaises(ValueError):
                _parse_rate(rate)


class ClientIpTests(SimpleTestCase):
    def test_prefiere_x_real_ip(self):
        req = self.rf
        req.META["HTTP_X_REAL_IP"] = "1.2.3.4"
        req.META["HTTP_X_FORWARDED_FOR"] = "9.9.9.9, 10.0.0.1"
        self.assertEqual(client_ip(req), "1.2.3.4")

    def test_caen_a_remote_addr_y_luego_a_xff(self):
        req = self.rf
        req.META = {"HTTP_X_FORWARDED_FOR": "9.9.9.9, 10.0.0.1"}
        self.assertEqual(client_ip(req), "9.9.9.9")  # último recurso: primer XFF
        req.META = {"REMOTE_ADDR": "127.0.0.1", "HTTP_X_FORWARDED_FOR": "9.9.9.9"}
        self.assertEqual(client_ip(req), "127.0.0.1")

    def setUp(self):
        from django.test import RequestFactory

        self.rf = RequestFactory().get(RF_PATH)


class RateLimitTests(SimpleTestCase):
    def setUp(self):
        from django.test import RequestFactory

        self.rf = RequestFactory().get(RF_PATH)

    def _vista(self):
        def vista(request):
            return HttpResponse("ok")

        return vista

    @_cache_nuevo("rl-basico")
    def test_bloquea_al_exceder_la_ventana(self):
        vista = rate_limit("test")(self._vista())
        req = self.rf
        self.assertEqual(vista(req).status_code, 200)
        self.assertEqual(vista(req).status_code, 200)
        # Página HTML: redirect con mensaje (el GET resultante sí verá 429 si
        # el límite persiste) y SIEMPRE cabecera Retry-After.
        resp = vista(req)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.has_header("Retry-After"))

    @_cache_nuevo("rl-json")
    def test_endpoint_json_devuelve_json_429(self):
        def vista(request):
            return HttpResponse("ok")

        limitada = rate_limit("test")(vista)
        from django.test import RequestFactory

        rf = RequestFactory().get("/panel/metrics/api/lente/")
        with patch("metrics.ratelimit.client_ip", return_value="3.3.3.3"):
            limitada(rf), limitada(rf)
            resp = limitada(rf)
        self.assertEqual(resp.status_code, 429)
        self.assertIn("application/json", resp["Content-Type"])

    @_cache_nuevo("rl-metodos")
    def test_solo_cuenta_los_verbos_configurados(self):
        def vista_post(request):
            return HttpResponse("ok")

        vista = rate_limit("test", methods=("POST",))(vista_post)
        # Los GET no consumen la ventana: pueden repetirse sin límite.
        for _ in range(5):
            self.assertEqual(vista(self.rf).status_code, 200)
        from django.test import RequestFactory

        rf = RequestFactory()
        self.assertEqual(vista(rf.post(RF_PATH)).status_code, 200)
        self.assertEqual(vista(rf.post(RF_PATH)).status_code, 200)
        self.assertEqual(vista(rf.post(RF_PATH)).status_code, 302)

    @_cache_nuevo("rl-bucket-por-vista")
    def test_bucket_independiente_por_vista(self):
        def vista_a(request):
            return HttpResponse("a")

        def vista_b(request):
            return HttpResponse("b")

        limitada_a = rate_limit("test")(vista_a)
        limitada_b = rate_limit("test")(vista_b)
        req = self.rf
        self.assertEqual(limitada_a(req).status_code, 200)
        self.assertEqual(limitada_a(req).status_code, 200)
        self.assertEqual(limitada_a(req).status_code, 302)
        # Otra vista con el mismo límite tiene su propio bucket.
        self.assertEqual(limitada_b(req).status_code, 200)

    @override_settings(RATELIMITS={}, CACHES=_RL)
    def test_sin_limite_configurado_no_limita(self):
        vista = rate_limit("inexistente")(self._vista())
        req = self.rf
        for _ in range(10):
            self.assertEqual(vista(req).status_code, 200)
