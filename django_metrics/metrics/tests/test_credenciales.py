"""
Pruebas del módulo Credenciales (solo rol Administrador) y del blindaje de sus
endpoints: gate por grupo, filtros/orden/paginación de la tabla, alta/edición
con separación de checkboxes (acceso al panel vs administrador), estado
(deshabilitar/habilitar, sin borrado) y validaciones de formulario (sin ñ,
regex de correo).

Mismo enfoque que test_login.py: RequestFactory + mocks sobre get_user_model,
sin BD real (la conexión fija search_path=sooniverse y una BD de pruebas nueva
no tiene ese esquema -ver la explicación en test_login.py).

Detalle de mocking: get_object_or_404 usa User._default_manager.all().get(...)
mientras el CRUD usa User.objects.*; los formularios importan get_user_model
por su cuenta (por eso se parchea también metrics.forms).
"""

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, SimpleTestCase, override_settings

from metrics import views
from metrics.forms import CredencialCreateForm

rf = RequestFactory()

CLAVE = "Clave-Larga-2024-segura"


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
        last_login=None,
    )
    defaults.update(overrides)
    if "pk" not in overrides:  # pk e id van siempre sincronizados
        defaults["pk"] = defaults["id"]

    class _FakeUser:
        def __init__(self, **kw):
            self.__dict__.update(kw)
            self.is_authenticated = True
            # groups: MagicMock con exists() configurable por test
            self.groups = MagicMock()
            self.groups.filter.return_value.exists.return_value = False

        def get_full_name(self):
            return f"{self.first_name} {self.last_name}".strip() or None

        def set_password(self, raw):  # noqa: ARG002 - solo marca la llamada
            self.password = "hasheada"

        def save(self, *args, **kwargs):
            self.saved = True
            self.saved_update_fields = kwargs.get("update_fields")

        def delete(self):
            self.deleted = True

    return _FakeUser(**defaults)


def _request(method="get", path="/panel/metrics/credenciales/", user=None, **data):
    req = getattr(rf, method)(path, data)
    req.user = user or AnonymousUser()
    req.session = {}
    req._messages = FallbackStorage(req)
    return req


def _user_mock(usuarios=None, target=None, creado=None):
    """Reemplazo de get_user_model(): `usuarios` es la LISTA que devuelve
    objects.order_by() (la vista pagina en Python sobre esa lista)."""
    user_cls = MagicMock()

    user_cls.objects.order_by.return_value = list(usuarios or [])
    user_cls.objects.filter.return_value.exists.return_value = False
    user_cls.objects.filter.return_value.exclude.return_value.exists.return_value = (
        False
    )
    if creado is not None:
        user_cls.objects.create_user.return_value = creado
    user_cls._default_manager.all.return_value.get.return_value = target
    return user_cls


def _con_usuarios(user_cls):
    """Parchea get_user_model en views y forms, y el grupo de administradores
    (para no tocar la BD real al sincronizar el rol)."""
    stack = ExitStack()
    stack.enter_context(patch("metrics.views.get_user_model", return_value=user_cls))
    stack.enter_context(patch("metrics.forms.get_user_model", return_value=user_cls))
    stack.enter_context(
        patch("metrics.credenciales.grupo_administradores", return_value=MagicMock())
    )
    return stack


def _admin(**overrides):
    """Usuario de sesión con el rol Administrador (grupo presente)."""
    user = _fake_user(**overrides)
    user.groups.filter.return_value.exists.return_value = True
    return user


def _panel(**overrides):
    """Usuario con acceso al panel pero SIN rol de administrador."""
    user = _fake_user(**overrides)
    user.groups.filter.return_value.exists.return_value = False
    return user


@override_settings(ALLOWED_HOSTS=["testserver"])
class AccesoCredencialesTests(SimpleTestCase):
    """El módulo exige sesión + staff + rol Administrador."""

    def test_anonimo_redirige_al_login(self):
        resp = views.credenciales(_request())
        self.assertEqual(resp.status_code, 302)
        self.assertIn("login", resp.url)

    def test_no_staff_redirige_al_login(self):
        resp = views.credenciales(_request(user=_panel(is_staff=False)))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("login", resp.url)

    def test_staff_sin_rol_admin_cae_al_dashboard(self):
        """Un usuario de panel NO puede entrar a credenciales 'por URL/API'."""
        from django.urls import reverse

        resp = views.credenciales(_request(user=_panel()))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse("metrics:dashboard"))

    def test_admin_ve_el_listado(self):
        with _con_usuarios(_user_mock(usuarios=[_admin(username="a")])):
            resp = views.credenciales(_request(user=_admin()))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Crear usuario", resp.content.decode())

    def test_superuser_ve_el_listado(self):
        with _con_usuarios(_user_mock(usuarios=[])):
            resp = views.credenciales(_request(user=_fake_user(is_superuser=True)))
        self.assertEqual(resp.status_code, 200)


@override_settings(ALLOWED_HOSTS=["testserver"])
class ListadoFiltrosTests(SimpleTestCase):
    def _usuarios(self, n):
        usuarios = []
        for i in range(n):
            usuarios.append(
                _fake_user(
                    id=i + 1,
                    username=f"cuenta{i:03d}",
                    email=f"cuenta{i:03d}@acme.com",
                    is_staff=False,
                    is_active=(i % 4 != 0),  # 1 de cada 4 deshabilitada
                )
            )
        usuarios[0].is_staff = True
        return usuarios

    def test_paginacion_de_30_en_30(self):
        usuarios = self._usuarios(35)
        req = _request("get", user=_admin())
        with _con_usuarios(_user_mock(usuarios=usuarios)):
            resp = views.credenciales(req)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("Página 1 de 2", html)
        self.assertIn("35 cuenta(s)", html)

    def test_filtro_por_usuario(self):
        usuarios = self._usuarios(3)
        req = _request("get", user=_admin(), usuario="cuenta001")
        with _con_usuarios(_user_mock(usuarios=usuarios)):
            resp = views.credenciales(req)
        html = resp.content.decode()
        self.assertIn("1 coincidencia(s)", html)
        self.assertNotIn("cuenta000</td>", html)

    def test_filtro_por_rol(self):
        usuarios = self._usuarios(4)
        req = _request("get", user=_admin(), rol="panel")
        with _con_usuarios(_user_mock(usuarios=usuarios)):
            resp = views.credenciales(req)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("1 coincidencia(s)", resp.content.decode())

    def test_filtro_por_rol_superuser(self):
        """El filtro usa los MISMOS textos que las badges: Superuser incluido."""
        usuarios = self._usuarios(3)
        usuarios[1].is_superuser = True
        req = _request("get", user=_admin(), rol="superuser")
        with _con_usuarios(_user_mock(usuarios=usuarios)):
            resp = views.credenciales(req)
        html = resp.content.decode()
        self.assertIn("1 coincidencia(s)", html)
        self.assertIn("Superuser", html)

    def test_orden_no_usa_queryparams(self):
        """El orden es 100% cliente (credenciales.js reordena las filas
        visibles): el servidor ignora orden/dir y no genera enlaces de orden
        (los th son botones data-sort). 'next' puede conservar la URL actual
        con sus params, pero no existen <a href="?orden=..."> en la página."""
        usuarios = self._usuarios(3)
        req = _request("get", user=_admin(), orden="usuario", dir="desc")
        with _con_usuarios(_user_mock(usuarios=usuarios)):
            resp = views.credenciales(req)
        html = resp.content.decode()
        self.assertIn('data-sort="usuario"', html)
        self.assertNotIn('href="?orden=', html)
        # El orden base del servidor no se altera por esos params.
        self.assertTrue(html.index("cuenta000") < html.index("cuenta001"))


@override_settings(ALLOWED_HOSTS=["testserver"])
class CredencialCrearTests(SimpleTestCase):
    DATOS = dict(
        username="nueva.cuenta",
        email="nueva@acme.com",
        first_name="Nueva",
        last_name="Cuenta",
        password=CLAVE,
        password2=CLAVE,
        is_staff="on",
    )

    def test_crea_usuario_y_sincroniza_grupo_si_es_admin(self):
        creado = _fake_user(username="nueva.cuenta")
        user_cls = _user_mock(creado=creado)
        datos = dict(self.DATOS, es_admin="on")
        req = _request("post", user=_admin(), **datos)
        with _con_usuarios(user_cls):
            resp = views.credencial_crear(req)
        self.assertEqual(resp.status_code, 302)
        kwargs = user_cls.objects.create_user.call_args.kwargs
        self.assertEqual(kwargs["username"], "nueva.cuenta")
        self.assertTrue(kwargs["is_staff"])
        # El rol administrador vive en el grupo, no es un argumento de create_user
        self.assertNotIn("es_admin", kwargs)
        creado.groups.add.assert_called_once()

    def test_crear_sin_admin_no_toca_el_grupo(self):
        creado = _fake_user(username="nueva.cuenta")
        user_cls = _user_mock(creado=creado)
        req = _request("post", user=_admin(), **self.DATOS)
        with _con_usuarios(user_cls):
            views.credencial_crear(req)
        creado.groups.add.assert_not_called()
        creado.groups.remove.assert_not_called()

    def test_admin_sin_acceso_al_panel_rechazado(self):
        user_cls = _user_mock()
        datos = dict(self.DATOS, es_admin="on")
        del datos["is_staff"]  # administrador sin acceso al panel
        req = _request("post", user=_admin(), **datos)
        with _con_usuarios(user_cls):
            resp = views.credencial_crear(req)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("requiere acceso al panel", resp.content.decode())
        user_cls.objects.create_user.assert_not_called()

    def test_formulario_invalido_renderiza_con_400(self):
        user_cls = _user_mock()
        datos = dict(self.DATOS, password2="distinta")
        req = _request("post", user=_admin(), **datos)
        with _con_usuarios(user_cls):
            resp = views.credencial_crear(req)
        self.assertEqual(resp.status_code, 400)
        user_cls.objects.create_user.assert_not_called()

    def test_no_se_admite_enie(self):
        datos = dict(self.DATOS, username="peñita")
        with patch("metrics.forms.get_user_model", return_value=_user_mock()):
            form = CredencialCreateForm(datos)
            self.assertFalse(form.is_valid())
        self.assertIn("ñ", str(form.errors))

    def test_correo_con_estructura_invalida(self):
        datos = dict(self.DATOS, email="sin-arroba.acme")
        with patch("metrics.forms.get_user_model", return_value=_user_mock()):
            form = CredencialCreateForm(datos)
            self.assertFalse(form.is_valid())
        self.assertTrue(
            any("correo" in e.lower() for e in form.errors.get("email", []))
        )

    def test_get_redirige_al_listado(self):
        resp = views.credencial_crear(_request(user=_admin()))
        self.assertEqual(resp.status_code, 302)


@override_settings(ALLOWED_HOSTS=["testserver"])
class GuardrailsCredencialesTests(SimpleTestCase):
    """Superusers intocables y autobloqueo imposible."""

    def test_no_puede_deshabilitarse_a_si_mismo(self):
        propio = _admin(id=7)
        req = _request("post", user=_admin(id=7), accion="deshabilitar")
        with _con_usuarios(_user_mock(target=propio)):
            views.credencial_estado(req, user_id=7)
        self.assertTrue(propio.is_active)
        self.assertNotIn("saved_update_fields", propio.__dict__)

    def test_no_puede_deshabilitar_un_superuser(self):
        superuser = _fake_user(id=2, is_superuser=True)
        req = _request("post", user=_admin(id=7), accion="deshabilitar")
        with _con_usuarios(_user_mock(target=superuser)):
            views.credencial_estado(req, user_id=2)
        self.assertTrue(superuser.is_active)

    def test_deshabilita_y_habilita_un_usuario_normal(self):
        objetivo = _fake_user(id=3)
        req = _request("post", user=_admin(id=7), accion="deshabilitar")
        with _con_usuarios(_user_mock(target=objetivo)):
            views.credencial_estado(req, user_id=3)
        self.assertFalse(objetivo.is_active)
        self.assertEqual(objetivo.saved_update_fields, ["is_active"])

        req2 = _request("post", user=_admin(id=7), accion="habilitar")
        with _con_usuarios(_user_mock(target=objetivo)):
            views.credencial_estado(req2, user_id=3)
        self.assertTrue(objetivo.is_active)

    def test_accion_desconocida_no_guarda(self):
        objetivo = _fake_user(id=3)
        req = _request("post", user=_admin(id=7), accion="borrar")
        with _con_usuarios(_user_mock(target=objetivo)):
            views.credencial_estado(req, user_id=3)
        self.assertTrue(objetivo.is_active)
        self.assertNotIn("saved_update_fields", objetivo.__dict__)

    def test_superuser_no_puede_editarse_desde_el_panel(self):
        superuser = _fake_user(id=2, is_superuser=True)
        req = _request("post", user=_admin(id=7), email="x@acme.com")
        with _con_usuarios(_user_mock(target=superuser)):
            resp = views.credencial_editar(req, user_id=2)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("credenciales", resp.url)

    def test_panel_sin_rol_no_puede_editar_ni_por_api(self):
        from django.urls import reverse

        objetivo = _fake_user(id=3)
        req = _request("post", user=_panel(id=7), email="x@acme.com")
        with _con_usuarios(_user_mock(target=objetivo)):
            resp = views.credencial_editar(req, user_id=3)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse("metrics:dashboard"))
        self.assertEqual(objetivo.email, "operador@acme.com")

    def test_panel_sin_rol_no_puede_cambiar_estado_ni_por_api(self):
        from django.urls import reverse

        objetivo = _fake_user(id=3)
        req = _request("post", user=_panel(id=7), accion="deshabilitar")
        with _con_usuarios(_user_mock(target=objetivo)):
            resp = views.credencial_estado(req, user_id=3)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse("metrics:dashboard"))
        self.assertTrue(objetivo.is_active)


@override_settings(ALLOWED_HOSTS=["testserver"])
class CredencialEditarTests(SimpleTestCase):
    DATOS = dict(
        email="nueva@acme.com",
        first_name="Ana",
        last_name="Lopez",
        password="",
        password2="",
        is_staff="on",
        es_admin="on",
    )

    def test_guarda_cambios_basicos_y_grupo(self):
        objetivo = _panel(id=3, email="vieja@acme.com")
        req = _request("post", user=_admin(id=7), **self.DATOS)
        with _con_usuarios(_user_mock(target=objetivo)):
            resp = views.credencial_editar(req, user_id=3)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(objetivo.email, "nueva@acme.com")
        self.assertEqual(objetivo.first_name, "Ana")
        self.assertNotEqual(objetivo.password, "hasheada")  # vacía = no se toca
        objetivo.groups.add.assert_called_once()  # es_admin="on"

    def test_quita_el_rol_administrador(self):
        objetivo = _admin(id=3)
        datos = dict(
            email="ana@acme.com",
            first_name="",
            last_name="",
            password="",
            password2="",
            is_staff="on",
        )  # sin es_admin
        req = _request("post", user=_admin(id=7), **datos)
        with _con_usuarios(_user_mock(target=objetivo)):
            views.credencial_editar(req, user_id=3)
        objetivo.groups.remove.assert_called_once()

    def test_cambia_contrasena_si_se_provee(self):
        objetivo = _fake_user(id=3)
        datos = dict(
            email="ana@acme.com",
            first_name="",
            last_name="",
            password="Otra-Clave-Larga-2024",
            password2="Otra-Clave-Larga-2024",
            is_staff="on",
        )
        req = _request("post", user=_admin(id=7), **datos)
        with _con_usuarios(_user_mock(target=objetivo)):
            views.credencial_editar(req, user_id=3)
        self.assertEqual(objetivo.password, "hasheada")

    def test_edicion_propia_deja_rol_disabled(self):
        propio = _admin(id=7)
        req = _request("post", user=_admin(id=7), **self.DATOS)
        with _con_usuarios(_user_mock(target=propio)):
            resp = views.credencial_editar(req, user_id=7)
        self.assertEqual(resp.status_code, 400)  # re-render con el modal abierto
        html = resp.content.decode()
        self.assertIn("disabled", html)
        self.assertIn("No puedes cambiar tu propio rol", html)

    def test_get_redirige_al_listado(self):
        resp = views.credencial_editar(_request(user=_admin()), user_id=1)
        self.assertEqual(resp.status_code, 302)
