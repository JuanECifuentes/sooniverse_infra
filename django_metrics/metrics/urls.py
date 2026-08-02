from django.urls import path

from . import views

app_name = "metrics"

urlpatterns = [
    # Módulo de métricas
    path("", views.dashboard, name="dashboard"),
    path("serie.json", views.serie_json, name="serie_json"),
    path("refrescar/", views.refrescar, name="refrescar"),

    # Gestor de API Keys
    path("api-keys/", views.api_keys, name="api_keys"),
    path("api-keys/<int:key_id>/", views.api_key_detalle, name="api_key_detalle"),
    path("api-keys/<int:key_id>/estado/", views.api_key_toggle, name="api_key_toggle"),
]
