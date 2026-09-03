"""
Pruebas del login único del clúster: backend usuario-o-correo, login_view,
logout_view y auth_check (el endpoint que consume nginx 'auth_request').

Mismo enfoque que el resto del proyecto (test_validacion_vistas.py,
test_worker_accion.py): RequestFactory + mocks, sin tocar una BD real. auth.User
es una tabla gestionada por Django, pero la conexión de este proyecto fija
`search_path=sooniverse` a nivel de conexión (settings.py DATABASES.OPTIONS)
-una BD de pruebas recién creada por `manage.py test` no tiene ese esquema
todavía, así que hasta las tablas propias de Django (auth_user incluida)
fallan al crearse ahí. Por eso ningún test de este proyecto usa TestCase.
"""

from unittest.mock import MagicMock, patch

from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.storage.fallback import FallbackStorage
from django.db.models import Q
from django.test import RequestFactory, SimpleTestCase, override_settings

from metrics import views
from metrics.auth_backends import UsernameOrEmailBackend

rf = RequestFactory()


def _fake_user(**overrides):
    defaults = dict(
        id=1,
        username="operador",
        email="operador@acme.com",
        password="hash",
        is_active=True,
        is_staff=False,
        first_name="",
        last_name="",
    )
    defaults.update(overrides)

    class _FakeUser:
        def __init__(self, **kw):
            self.__dict__.update(kw)
            self.is_authenticated = True

        def check_password(self, raw):
            return raw == "clave-correcta"

        def get_full_name(self):
            full = f"{self.first_name} {self.last_name}".strip()
            return full or None

    return _FakeUser(**defaults)


class UsernameOrEmailBackendTests(SimpleTestCase):
    def setUp(self):
        self.backend = UsernameOrEmailBackend()
        self.user = _fake_user()

    def _manager(self, get_return=None, get_side_effect=None):
        manager = MagicMock()
        if get_side_effect is not None:
            manager.get.side_effect = get_side_effect
        else:
            manager.get.return_value = get_return
        return manager

    def test_autentica_por_username_o_correo(self):
        with patch("metrics.auth_backends.get_user_model") as get_model:
            get_model.return_value.objects = self._manager(get_return=self.user)
            resultado = self.backend.authenticate(
                None, username="operador", password="clave-correcta"
            )
        self.assertEqual(resultado, self.user)

    def test_password_incorrecta_no_autentica(self):
        with patch("metrics.auth_backends.get_user_model") as get_model:
            get_model.return_value.objects = self._manager(get_return=self.user)
            resultado = self.backend.authenticate(
                None, username="operador", password="mala"
            )
        self.assertIsNone(resultado)

    def test_usuario_inexistente_no_autentica(self):
        from django.contrib.auth import get_user_model as real_get_user_model

        User = real_get_user_model()
        with patch("metrics.auth_backends.get_user_model") as get_model:
            get_model.return_value = User
            get_model.return_value.objects = self._manager(
                get_side_effect=User.DoesNotExist
            )
            resultado = self.backend.authenticate(
                None, username="fantasma", password="x"
            )
        self.assertIsNone(resultado)

    def test_email_duplicado_no_autentica_ninguna(self):
        from django.contrib.auth import get_user_model as real_get_user_model

        User = real_get_user_model()
        with patch("metrics.auth_backends.get_user_model") as get_model:
            get_model.return_value = User
            get_model.return_value.objects = self._manager(
                get_side_effect=User.MultipleObjectsReturned
            )
            resultado = self.backend.authenticate(
                None, username="operador@acme.com", password="clave-correcta"
            )
        self.assertIsNone(resultado)

    def test_consulta_por_username_o_email_case_insensitive(self):
        manager = self._manager(get_return=self.user)
        with patch("metrics.auth_backends.get_user_model") as get_model:
            get_model.return_value.objects = manager
            self.backend.authenticate(
                None, username="OPERADOR@ACME.COM", password="clave-correcta"
            )
        filtro = manager.get.call_args.args[0]
        self.assertEqual(
            filtro,
            Q(username="OPERADOR@ACME.COM") | Q(email__iexact="OPERADOR@ACME.COM"),
        )

    def test_sin_username_o_password_no_autentica(self):
        self.assertIsNone(self.backend.authenticate(None, username=None, password="x"))
        self.assertIsNone(self.backend.authenticate(None, username="x", password=None))


def _request(method="post", path="/panel/metrics/login/", user=None, **data):
    req = getattr(rf, method)(path, data)
    req.user = user or AnonymousUser()
    req.session = {}
    req._messages = FallbackStorage(req)
    return req


class LoginViewTests(SimpleTestCase):
    def test_ya_autenticado_redirige_directo(self):
        req = _request("get", user=_fake_user())
        resp = views.login_view(req)
        self.assertEqual(resp.status_code, 302)

    def test_credenciales_validas_inicia_sesion_y_redirige(self):
        user = _fake_user()
        req = _request(identificador="operador", password="clave-correcta")
        with (
            patch("metrics.views.authenticate", return_value=user) as auth,
            patch("metrics.views.auth_login") as login_fn,
        ):
            resp = views.login_view(req)
        auth.assert_called_once()
        login_fn.assert_called_once_with(req, user)
        self.assertEqual(resp.status_code, 302)

    def test_credenciales_invalidas_muestra_error_sin_redirigir(self):
        req = _request(identificador="operador", password="mala")
        with patch("metrics.views.authenticate", return_value=None):
            resp = views.login_view(req)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("incorrect", resp.content.decode().lower())

    def test_respeta_next(self):
        user = _fake_user()
        req = _request(
            identificador="operador",
            password="clave-correcta",
            next="/panel/metrics/api-keys/",
        )
        with (
            patch("metrics.views.authenticate", return_value=user),
            patch("metrics.views.auth_login"),
        ):
            resp = views.login_view(req)
        self.assertEqual(resp.url, "/panel/metrics/api-keys/")


class LogoutViewTests(SimpleTestCase):
    def test_logout_cierra_sesion_y_redirige_al_login(self):
        req = _request(user=_fake_user())
        with patch("metrics.views.auth_logout") as logout_fn:
            resp = views.logout_view(req)
        logout_fn.assert_called_once_with(req)
        self.assertEqual(resp.status_code, 302)


class AuthCheckTests(SimpleTestCase):
    """El endpoint que consume nginx 'auth_request' para proteger el chat."""

    def test_sin_sesion_devuelve_401(self):
        req = _request("get", user=AnonymousUser())
        resp = views.auth_check(req)
        self.assertEqual(resp.status_code, 401)

    def test_con_sesion_activa_devuelve_200_con_cabeceras_de_identidad(self):
        user = _fake_user(email="chat@acme.com", first_name="Chat", last_name="User")
        req = _request("get", user=user)
        resp = views.auth_check(req)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["X-Sooniverse-Email"], "chat@acme.com")
        self.assertEqual(resp["X-Sooniverse-Name"], "Chat User")

    def test_usuario_inactivo_devuelve_401(self):
        user = _fake_user(is_active=False)
        req = _request("get", user=user)
        resp = views.auth_check(req)
        self.assertEqual(resp.status_code, 401)

    def test_usuario_no_staff_si_puede_pasar_auth_check(self):
        """A diferencia del panel, el chat no exige is_staff."""
        user = _fake_user(is_staff=False)
        req = _request("get", user=user)
        resp = views.auth_check(req)
        self.assertEqual(resp.status_code, 200)

    def test_email_vacio_cae_a_placeholder(self):
        user = _fake_user(username="sinemail", email="")
        req = _request("get", user=user)
        resp = views.auth_check(req)
        self.assertEqual(resp["X-Sooniverse-Email"], "sinemail@sooniverse.local")


@override_settings(ALLOWED_HOSTS=["testserver"])
class PanelLoginRequiredTests(SimpleTestCase):
    """El redirect de user_passes_test construye la URL de retorno con
    build_absolute_uri -> get_host, que exige que 'testserver' (el host de
    RequestFactory) esté en ALLOWED_HOSTS -el .env local suele traer el host
    real del despliegue y bloquearía el redirect con DisallowedHost."""

    def test_usuario_no_staff_rechazado(self):
        req = _request("get", path="/panel/metrics/", user=_fake_user(is_staff=False))

        @views.panel_login_required
        def _vista(request):
            from django.http import HttpResponse

            return HttpResponse("ok")

        resp = _vista(req)
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reversed_login_path(), resp.url)

    def test_usuario_staff_permitido(self):
        req = _request("get", path="/panel/metrics/", user=_fake_user(is_staff=True))

        @views.panel_login_required
        def _vista(request):
            from django.http import HttpResponse

            return HttpResponse("ok")

        resp = _vista(req)
        self.assertEqual(resp.status_code, 200)


def reversed_login_path():
    from django.urls import reverse

    return reverse("metrics:login")
