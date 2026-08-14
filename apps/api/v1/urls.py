from django.conf.urls import include
from django.urls import path

app_name = "v1"

urlpatterns = [
    path(r'v1/', include([
        path(r'', include('apps.api.v1.webhook.urls')),
        path(r'mobile/', include('apps.api.v1.mobile.urls')),
    ])),
]
