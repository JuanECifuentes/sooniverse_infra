"""
Red de seguridad del contrato JSON del panel.

Es la prueba más importante de este paquete: todo lo añadido en esta iteración
es ADITIVO, y este archivo es lo que impide que deje de serlo. `serie_json` es
un contrato externo heredado y `apikey_detail.html` consume el mismo
`_metricas_payload` que el dashboard; romper cualquiera de los dos sería
silencioso hasta que alguien lo reportara desde fuera.
"""

import inspect
from datetime import date
from decimal import Decimal

from django.test import SimpleTestCase

from metrics import services, views

# Las claves exactas que el contrato ya tenía antes de esta iteración.
CLAVES_BASE = {
    "granularity", "granularity_label", "desde", "hasta", "summary", "series",
    "por_modelo", "por_api_key", "mostrar_desglose_api_key",
}
CLAVES_SUMMARY = {
    "request_count", "prompt_tokens", "completion_tokens", "total_tokens",
    "spend_usd", "error_count", "tokens_por_request", "ratio_completion", "tasa_error",
}
CLAVES_SERIES_BASE = {
    "labels", "total_tokens", "prompt_tokens", "completion_tokens",
    "request_count", "altura_pct",
}


def resumen_de_prueba():
    """Un ResumenMetricas construido a mano: sin BD, sin fixtures."""
    r = services.ResumenMetricas(
        granularity="daily",
        granularity_label="Diario",
        api_key_ids=[],
        desde=date(2026, 8, 1),
        hasta=date(2026, 8, 19),
    )
    r.serie = [
        services.SeriePunto(periodo=date(2026, 8, 1), etiqueta="01/08",
                            prompt_tokens=100, completion_tokens=50, total_tokens=150,
                            request_count=3, spend_usd=Decimal("0.0004"), altura_pct=50.0),
        services.SeriePunto(periodo=date(2026, 8, 2), etiqueta="02/08",
                            prompt_tokens=200, completion_tokens=100, total_tokens=300,
                            request_count=6, spend_usd=Decimal("0.0008"), altura_pct=100.0),
    ]
    r.total_tokens, r.prompt_tokens, r.completion_tokens = 450, 300, 150
    r.request_count, r.error_count = 9, 1
    r.spend_usd = Decimal("0.0012")
    r.por_modelo = [{"model_name": "m", "total_tokens": 450, "spend_usd": Decimal("0.0012")}]
    r.por_api_key = []
    return r


class ContratoPayload(SimpleTestCase):
    def test_todas_las_claves_previas_siguen_presentes(self):
        payload = views._metricas_payload(resumen_de_prueba(), [])
        faltan = CLAVES_BASE - set(payload)
        self.assertFalse(faltan, f"el payload perdió claves del contrato: {sorted(faltan)}")
        self.assertFalse(CLAVES_SUMMARY - set(payload["summary"]))
        self.assertFalse(CLAVES_SERIES_BASE - set(payload["series"]))

    def test_las_claves_nuevas_son_opcionales(self):
        """Sin pasar los kwargs nuevos, el payload es el de siempre: así el
        bootstrap de `api_key_detalle` no cambia de forma."""
        payload = views._metricas_payload(resumen_de_prueba(), [])
        for clave in ("tiempos_muertos", "filtros_eco", "comparativa"):
            self.assertNotIn(clave, payload)

    def test_las_claves_nuevas_aparecen_cuando_se_piden(self):
        payload = views._metricas_payload(
            resumen_de_prueba(), [], ocio={"pct_ocioso": 10.0},
            filtros_eco={"dow": []}, comparativa={"delta_pct": {}},
        )
        for clave in ("tiempos_muertos", "filtros_eco", "comparativa"):
            self.assertIn(clave, payload)

    def test_metricas_payload_sigue_aceptando_dos_posicionales(self):
        """`api_key_detalle` (views.py) llama con dos argumentos. Si los
        parámetros nuevos no fueran keyword-only con default, reventaría."""
        firma = inspect.signature(views._metricas_payload)
        posicionales = [
            p for p in firma.parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        self.assertEqual([p.name for p in posicionales], ["metricas", "api_key_ids"])
        for nombre in ("ocio", "filtros_eco", "comparativa"):
            p = firma.parameters[nombre]
            self.assertEqual(p.kind, p.KEYWORD_ONLY, f"{nombre} debe ser keyword-only")
            # Lo que importa es que TENGA default (None lo es), no cuál sea.
            self.assertIsNot(p.default, inspect.Parameter.empty,
                             f"{nombre} debe tener un valor por defecto")

    def test_series_gana_periodos_sin_perder_labels(self):
        series = views._metricas_payload(resumen_de_prueba(), [])["series"]
        self.assertIn("periodos", series)
        self.assertEqual(len(series["periodos"]), len(series["labels"]))
        self.assertEqual(series["periodos"][0], "2026-08-01")

    def test_mostrar_desglose_api_key_conserva_su_semantica(self):
        """Solo se oculta cuando hay EXACTAMENTE una key filtrada."""
        r = resumen_de_prueba()
        self.assertTrue(views._metricas_payload(r, [])["mostrar_desglose_api_key"])
        self.assertTrue(views._metricas_payload(r, [1, 2])["mostrar_desglose_api_key"])
        self.assertFalse(views._metricas_payload(r, [1])["mostrar_desglose_api_key"])


class FirmaDeServicios(SimpleTestCase):
    """`serie_json` es un contrato externo heredado que llama a
    `obtener_metricas` con parámetros posicionales. Estos tests son la promesa
    de que no se rompe."""

    def test_obtener_metricas_conserva_sus_cinco_posicionales(self):
        firma = inspect.signature(services.obtener_metricas)
        posicionales = [
            p.name for p in firma.parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        self.assertEqual(
            posicionales,
            ["granularity", "api_key_ids", "modelos", "desde", "hasta"],
        )

    def test_lo_nuevo_de_obtener_metricas_es_keyword_only_con_default(self):
        firma = inspect.signature(services.obtener_metricas)
        p = firma.parameters["incluir_benchmark"]
        self.assertEqual(p.kind, p.KEYWORD_ONLY)
        self.assertIs(p.default, False)

    def test_obtener_peticiones_conserva_sus_posicionales(self):
        firma = inspect.signature(services.obtener_peticiones)
        posicionales = [
            p.name for p in firma.parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        self.assertEqual(
            posicionales,
            ["api_key_ids", "modelos", "desde", "hasta", "page", "page_size",
             "sort_by", "sort_dir"],
        )
        for nombre in ("incluir_benchmark", "solo_errores"):
            self.assertEqual(firma.parameters[nombre].kind,
                             inspect.Parameter.KEYWORD_ONLY)

    def test_refrescar_metricas_devuelve_las_tres_cuentas(self):
        """`views.refrescar` compone su mensaje con estas tres claves."""
        fuente = inspect.getsource(services.refrescar_metricas)
        for clave in ("eventos_ingeridos", "filas_agregadas", "buckets_horarios"):
            self.assertIn(clave, fuente)
