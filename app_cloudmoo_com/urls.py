"""
URL configuration for the CloudMoo project.
"""
from django.contrib import admin
from django.urls import path
from django.conf.urls import include
from django.conf.urls.static import static
from django.conf import settings

from app_cloudmoo_com.views import healthz

urlpatterns = [
    path("django-admin/", admin.site.urls),
    path("api-auth/", include("rest_framework.urls", namespace="rest_framework")),
    # Liveness probe — must stay before the catch-all includes below.
    path("healthz/", healthz),
    path("", include("apps.console.urls")),
    path("", include("apps.api.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
