from django.urls import path

from . import views

app_name = "metrics"

urlpatterns = [
    # Módulo de métricas
    path("", views.dashboard, name="dashboard"),
    path("serie.json", views.serie_json, name="serie_json"),
    path("api/metrics/", views.metrics_api, name="metrics_api"),
    # Mapa de calor y perfil horario: endpoint aparte para no encarecer el
    # camino caliente de metrics_api (ver docstring de lente_api).
    path("api/lente/", views.lente_api, name="lente_api"),
    path("refrescar/", views.refrescar, name="refrescar"),
    # Capacidad de la infraestructura
    path("capacidad/", views.capacidad, name="capacidad"),
    path("api/capacidad/", views.capacidad_api, name="capacidad_api"),
    # Gestor de API Keys
    path("api-keys/", views.api_keys, name="api_keys"),
    path("api-keys/<int:key_id>/", views.api_key_detalle, name="api_key_detalle"),
    path("api-keys/<int:key_id>/estado/", views.api_key_toggle, name="api_key_toggle"),
    # Acciones sobre workers (card "Pool vLLM")
    path(
        "workers/<int:node_id>/<slug:accion>/",
        views.worker_accion,
        name="worker_accion",
    ),
    # Credenciales: CRUD de usuarios del clúster (solo staff, ver views.py).
    # Las cuentas creadas aquí sirven para el chat (SSO) y, si son staff,
    # también para el panel.
    path("credenciales/", views.credenciales, name="credenciales"),
    path("credenciales/crear/", views.credencial_crear, name="credencial_crear"),
    path(
        "credenciales/<int:user_id>/editar/",
        views.credencial_editar,
        name="credencial_editar",
    ),
    path(
        "credenciales/<int:user_id>/eliminar/",
        views.credencial_eliminar,
        name="credencial_eliminar",
    ),
    # Login único del clúster (panel + chat, ver views.py::auth_check)
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("auth-check/", views.auth_check, name="auth_check"),
]
