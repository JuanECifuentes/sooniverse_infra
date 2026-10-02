"""
Etiqueta legible de la key de la interfaz de chat ("Interfaz Web") y hash con
el que crear_api_key registra las keys emitidas desde el panel: tiene que ser
sha256(key) -lo que LiteLLM escribe en LiteLLM_SpendLogs.api_key-, nunca el
campo 'token' de la respuesta (la key en claro). Con el valor equivocado el
ETL no casaba y el consumo salía como "(sin registro)".
"""

import hashlib
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from metrics import services
from metrics.models import ApiKeyRegistry, friendly_key_alias
from metrics.templatetags.metrics_extras import friendly_key_alias as friendly_key_alias_filter


class FriendlyKeyAliasTests(SimpleTestCase):
    def test_alias_de_open_webui_se_muestra_como_interfaz_web(self):
        self.assertEqual(friendly_key_alias("sooniverse-openwebui-acme-prod"), "Interfaz Web")
        self.assertEqual(friendly_key_alias_filter("sooniverse-openwebui-demo-prod"), "Interfaz Web")

    def test_otros_alias_pasan_sin_cambios(self):
        self.assertEqual(friendly_key_alias("cliente-erp"), "cliente-erp")
        self.assertIsNone(friendly_key_alias(None))
        self.assertEqual(friendly_key_alias(""), "")


class CrearApiKeyHashTests(SimpleTestCase):
    def test_registra_sha256_de_la_key_y_no_el_token_en_claro(self):
        cliente = MagicMock()
        cliente.generate_key.return_value = {"key": "sk-1234567890abcdef", "token": "sk-1234567890abcdef"}

        with patch("metrics.services.LiteLLMClient", return_value=cliente), \
             patch.object(ApiKeyRegistry.objects, "create", return_value=MagicMock(id=1)) as create, \
             patch("metrics.services._audit"):
            services.crear_api_key.__wrapped__(alias="test", actor="tester")

        self.assertEqual(
            create.call_args.kwargs["litellm_token_hash"],
            hashlib.sha256(b"sk-1234567890abcdef").hexdigest(),
        )
