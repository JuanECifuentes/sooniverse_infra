"""
Pruebas del inventario único de API Keys (LiteLLM + Open WebUI, ver
database/006_workers_y_login.sql): la propiedad ApiKeyRegistry.gestionable, el
guard en services.desactivar_api_key/reactivar_api_key, y el fix del bug real
de crear_api_key (expires_at siempre quedaba en NULL aunque el operador
eligiera una vigencia).

Mismo enfoque que el resto del proyecto: mocks sobre el ORM, sin BD real.
"""

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from django.utils import timezone

from metrics import services
from metrics.models import ApiKeyRegistry


def _fake_registro(**overrides):
    defaults = dict(id=1, key_alias="mi-key", origen="litellm", litellm_token_hash="hash123", is_active=True)
    defaults.update(overrides)
    return ApiKeyRegistry(**defaults)


class GestionablePropertyTests(SimpleTestCase):
    def test_litellm_es_gestionable(self):
        self.assertTrue(_fake_registro(origen="litellm").gestionable)

    def test_openwebui_no_es_gestionable(self):
        self.assertFalse(_fake_registro(origen="openwebui").gestionable)


class DesactivarReactivarGuardTests(SimpleTestCase):
    """desactivar_api_key/reactivar_api_key llevan @transaction.atomic, que
    exige una conexión real (BEGIN/COMMIT) incluso con todo lo de dentro
    mockeado -SimpleTestCase la bloquea, y declarar `databases` hace que
    `manage.py test` intente crear una BD de pruebas completa, que falla
    porque el esquema 'sooniverse' (fijado a nivel de conexión) no existe ahí
    (ver el resto del proyecto: por eso NINGÚN test usa TestCase). Se llama a
    `.__wrapped__` -la función SIN decorar, que Django preserva vía
    functools.wraps- para probar el guard sin esa conexión."""

    def test_desactivar_key_openwebui_lanza_error_sin_llamar_a_litellm(self):
        registro = _fake_registro(origen="openwebui")
        with patch.object(ApiKeyRegistry.objects, "get", return_value=registro), \
             patch("metrics.services.LiteLLMClient") as client_cls:
            with self.assertRaises(services.ApiKeyNoGestionableError):
                services.desactivar_api_key.__wrapped__(1, actor="tester")
        client_cls.assert_not_called()

    def test_reactivar_key_openwebui_lanza_error_sin_llamar_a_litellm(self):
        registro = _fake_registro(origen="openwebui", is_active=False)
        with patch.object(ApiKeyRegistry.objects, "get", return_value=registro), \
             patch("metrics.services.LiteLLMClient") as client_cls:
            with self.assertRaises(services.ApiKeyNoGestionableError):
                services.reactivar_api_key.__wrapped__(1, actor="tester")
        client_cls.assert_not_called()

    def test_desactivar_key_litellm_sigue_funcionando(self):
        registro = _fake_registro(origen="litellm")
        cliente = MagicMock()
        with patch.object(ApiKeyRegistry.objects, "get", return_value=registro), \
             patch("metrics.services.LiteLLMClient", return_value=cliente), \
             patch.object(ApiKeyRegistry, "save"), \
             patch("metrics.services._audit"):
            resultado = services.desactivar_api_key.__wrapped__(1, actor="tester")

        cliente.block_key.assert_called_once_with("hash123")
        self.assertFalse(resultado.is_active)


class CrearApiKeyExpiresAtTests(SimpleTestCase):
    """crear_api_key también lleva @transaction.atomic -mismo motivo que
    DesactivarReactivarGuardTests para usar .__wrapped__."""

    def test_expires_at_se_guarda_cuando_se_elige_vigencia(self):
        vigencia = date.today() + timedelta(days=30)
        cliente = MagicMock()
        cliente.generate_key.return_value = {"key": "sk-1234567890abcdef", "token": "tok1"}
        creado = _fake_registro(id=2)

        with patch("metrics.services.LiteLLMClient", return_value=cliente), \
             patch.object(ApiKeyRegistry.objects, "create", return_value=creado) as create, \
             patch("metrics.services._audit"):
            services.crear_api_key.__wrapped__(alias="test", duration="30d", expires_at=vigencia, actor="tester")

        kwargs = create.call_args.kwargs
        self.assertIsNotNone(kwargs["expires_at"])
        self.assertEqual(kwargs["expires_at"].date(), vigencia)
        self.assertTrue(timezone.is_aware(kwargs["expires_at"]))

    def test_expires_at_es_none_sin_vigencia(self):
        cliente = MagicMock()
        cliente.generate_key.return_value = {"key": "sk-1234567890abcdef", "token": "tok1"}
        creado = _fake_registro(id=2)

        with patch("metrics.services.LiteLLMClient", return_value=cliente), \
             patch.object(ApiKeyRegistry.objects, "create", return_value=creado) as create, \
             patch("metrics.services._audit"):
            services.crear_api_key.__wrapped__(alias="test", actor="tester")

        self.assertIsNone(create.call_args.kwargs["expires_at"])
