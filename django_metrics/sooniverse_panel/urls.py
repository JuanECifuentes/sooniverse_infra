from django.contrib import admin
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import include, path

admin.site.site_header = "Sooniverse · Administración"
admin.site.site_title = "Sooniverse"
admin.site.index_title = "Panel de infraestructura"


def healthz(_request):
    return JsonResponse({"status": "ok", "service": "sooniverse-metrics"})


urlpatterns = [
    path("", lambda r: redirect("metrics:dashboard"), name="root"),
    path("healthz/", healthz, name="healthz"),
    path("metrics/", include("metrics.urls")),
    path("admin/", admin.site.urls),
]
