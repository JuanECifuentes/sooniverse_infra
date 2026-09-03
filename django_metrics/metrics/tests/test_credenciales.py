"""
Pruebas del módulo Credenciales (CRUD de usuarios del clúster, solo staff) y
del blindaje de sus endpoints: redirect a login para no-staff, creación de
usuarios vía create_user, y guardrails (superusers intocables, autobloqueo
imposible desde la propia sesión).

Mismo enfoque que test_login.py: RequestFactory + mocks sobre get_user_model,
sin BD real (la conexión fija search_path=sooniverse y una BD de pruebas nueva
no tiene ese esquema -ver la explicación en test_login.py).

Detalle de mocking: las vistas obtienen usuarios por DOS vías que hay que
configurar por separado:
  · get_object_or_404(User, pk=...) usa User._default_manager.all().get(...)
  · listado/filtros del CRUD usan User.objects.order_by/filter/create_user
"""

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, SimpleTestCase, override_settings

from metrics import views

rf = RequestFactory()


def _fake_user(**overrides):
    defaults = dict(
        id=7,
        pk=7,
        username="operador",
        email="operador@acme.com",
        password="hash",
        is_active=True,
        is_staff=True,
        is_superuser=False,
        first_name="",
        last_name="",
    )
    defaults.update(overrides)
    if "pk" not in overrides:  # pk e id van siempre sincronizados
        defaults["pk"] = defaults["id"]

    class _FakeUser:
        def __init__(self, **kw):
            self.__dict__.update(kw)
            self.is_authenticated = True

        def get_full_name(self):
            return f"{self.first_name} {self.last_name}".strip() or None

        def set_password(self, raw):  # noqa: ARG002 - solo marca la llamada
            self.password = "hasheada"

        def save(self):
            pass

        def delete(self):
            self.deleted = True

    return _FakeUser(**defaults)


def _request(method="get", path="/panel/metrics/credenciales/", user=None, **data):
    req = getattr(rf, method)(path, data)
    req.user = user or AnonymousUser()
    req.session = {}
    req._messages = FallbackStorage(req)
    return req


def _user_mock(target=None, total=3, admins=1):
    """Reemplazo de get_user_model(): cubre objects (CRUD/listado) y
    _default_manager (get_object_or_404) con `exists()` en False para que las
    validaciones de unicidad no vean fantasmas."""
    user_cls = MagicMock()

    # objects (listado + formularios)
    qs = MagicMock()
    qs.count.return_value = total
    qs.filter.return_value.count.return_value = admins
    qs.filter.return_value.exists.return_value = False
    qs.filter.return_value.exclude.return_value.exists.return_value = False
    user_cls.objects.order_by.return_value = qs
    user_cls.objects.filter.return_value = qs.filter.return_value
    if target is not None:
        user_cls.objects.create_user.return_value = target

    # _default_manager (get_object_or_404)
    user_cls._default_manager.all.return_value.get.return_value = target
    return user_cls


def _con_usuarios(user_cls):
    """Parchea get_user_model en TODOS los módulos que lo importan por nombre:
    views (listado/crear/eliminar) y forms (validaciones de unicidad)."""
    stack = ExitStack()
    stack.enter_context(patch("metrics.views.get_user_model", return_value=user_cls))
    stack.enter_context(patch("metrics.forms.get_user_model", return_value=user_cls))
    return stack


@override_settings(ALLOWED_HOSTS=["testserver"])
class AccesoCredencialesTests(SimpleTestCase):
    """El módulo entero exige staff (panel_login_required ya lo garantiza)."""

    def test_no_staff_redirige_al_login(self):
        req = _request(user=_fake_user(is_staff=False))
        resp = views.credenciales(req)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("login", resp.url)

    def test_anonimo_redirige_al_login(self):
        resp = views.credenciales(_request())
        self.assertEqual(resp.status_code, 302)

    def test_staff_ve_el_listado(self):
        with _con_usuarios(_user_mock()):
            resp = views.credenciales(_request(user=_fake_user()))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Crear usuario", resp.content.decode())


@override_settings(ALLOWED_HOSTS=["testserver"])
class CredencialCrearTests(SimpleTestCase):
    DATOS = dict(
        username="nueva.cuenta",
        email="nueva@acme.com",
        first_name="Nueva",
        last_name="Cuenta",
        password="Clave-Larga-2024-segura",
        password2="Clave-Larga-2024-segura",
        is_staff="on",
        is_active="on",
    )

    def test_crea_usuario_con_los_datos_del_formulario(self):
        creado = _fake_user(username="nueva.cuenta")
        user_cls = _user_mock(target=creado)
        req = _request("post", user=_fake_user(), **self.DATOS)
        with _con_usuarios(user_cls):
            resp = views.credencial_crear(req)
        self.assertEqual(resp.status_code, 302)
        kwargs = user_cls.objects.create_user.call_args.kwargs
        self.assertEqual(kwargs["username"], "nueva.cuenta")
        self.assertEqual(kwargs["email"], "nueva@acme.com")
        self.assertTrue(kwargs["is_staff"])
        self.assertTrue(kwargs["is_active"])

    def test_formulario_invalido_renderiza_con_400(self):
        user_cls = _user_mock()
        datos = dict(self.DATOS, password2="distinta")
        req = _request("post", user=_fake_user(), **datos)
        with _con_usuarios(user_cls):
            resp = views.credencial_crear(req)
        self.assertEqual(resp.status_code, 400)
        user_cls.objects.create_user.assert_not_called()

    def test_get_redirige_al_listado(self):
        resp = views.credencial_crear(_request(user=_fake_user()))
        self.assertEqual(resp.status_code, 302)


@override_settings(ALLOWED_HOSTS=["testserver"])
class GuardrailsCredencialesTests(SimpleTestCase):
    """Superusers intocables y autobloqueo imposible."""

    def test_no_puede_eliminarse_a_si_mismo(self):
        propio = _fake_user(id=7)
        req = _request("post", user=_fake_user(id=7))
        with _con_usuarios(_user_mock(target=propio)):
            resp = views.credencial_eliminar(req, user_id=7)
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(getattr(propio, "deleted", False))

    def test_no_puede_eliminar_un_superuser(self):
        superuser = _fake_user(id=2, is_superuser=True)
        req = _request("post", user=_fake_user(id=7))
        with _con_usuarios(_user_mock(target=superuser)):
            resp = views.credencial_eliminar(req, user_id=2)
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(getattr(superuser, "deleted", False))

    def test_elimina_un_usuario_normal(self):
        objetivo = _fake_user(id=3)
        req = _request("post", user=_fake_user(id=7))
        with _con_usuarios(_user_mock(target=objetivo)):
            resp = views.credencial_eliminar(req, user_id=3)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(objetivo.deleted)

    def test_superuser_no_puede_editarse_desde_el_panel(self):
        superuser = _fake_user(id=2, is_superuser=True)
        req = _request("post", user=_fake_user(id=7))
        with _con_usuarios(_user_mock(target=superuser)):
            resp = views.credencial_editar(req, user_id=2)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("credenciales", resp.url)

    def test_edicion_propia_deja_rol_y_estado_disabled(self):
        propio = _fake_user(id=7)
        req = _request("get", user=_fake_user(id=7))
        with _con_usuarios(_user_mock(target=propio)):
            resp = views.credencial_editar(req, user_id=7)
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn("disabled", content)
        self.assertIn("No puedes cambiar tu propio rol", content)


@override_settings(ALLOWED_HOSTS=["testserver"])
class CredencialEditarTests(SimpleTestCase):
    def test_guarda_cambios_basicos(self):
        objetivo = _fake_user(id=3, email="vieja@acme.com")
        datos = dict(
            email="nueva@acme.com",
            first_name="Ana",
            last_name="Lopez",
            password="",
            password2="",
            is_staff="on",
            is_active="on",
        )
        req = _request("post", user=_fake_user(id=7), **datos)
        with _con_usuarios(_user_mock(target=objetivo)):
            resp = views.credencial_editar(req, user_id=3)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(objetivo.email, "nueva@acme.com")
        self.assertEqual(objetivo.first_name, "Ana")
        self.assertNotEqual(objetivo.password, "hasheada")  # vacía = no se toca

    def test_cambia_contrasena_si_se_provee(self):
        objetivo = _fake_user(id=3)
        datos = dict(
            email="ana@acme.com",
            first_name="",
            last_name="",
            password="Otra-Clave-Larga-2024",
            password2="Otra-Clave-Larga-2024",
            is_staff="on",
            is_active="on",
        )
        req = _request("post", user=_fake_user(id=7), **datos)
        with _con_usuarios(_user_mock(target=objetivo)):
            views.credencial_editar(req, user_id=3)
        self.assertEqual(objetivo.password, "hasheada")
