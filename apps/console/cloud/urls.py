from django.urls import path, include
from .views import CloudConnect, CloudListView, CloudEditView, CloudSyncView, CloudDetailView

app_name = "cloud"

urlpatterns = [
    path(
        r"cloud/",
        include(
            [
                path('connect/', CloudConnect.as_view(), name='connect'),
                path('', CloudListView.as_view(), name='list'),
                path('edit/<int:cloud_id>/', CloudEditView.as_view(), name='edit'),
                path('sync/<int:cloud_id>/', CloudSyncView.as_view(), name='sync'),
                path('detail/<int:cloud_id>/', CloudDetailView.as_view(), name='detail'),
                path(
                    r"connect/",
                    include(
                        [
                            path(r"", include("apps.console.cloud.digitalocean.urls")),
                            path(r"", include("apps.console.cloud.hetzner.urls")),
                            path(r"", include("apps.console.cloud.vultr.urls")),
                            path(r"", include("apps.console.cloud.aws.urls")),
                            path(r"", include("apps.console.cloud.upcloud.urls")),
                            path(r"", include("apps.console.cloud.linode.urls")),
                            path(r"", include("apps.console.cloud.oracle.urls")),
                        ]
                    ),
                ),
            ]
        ),
    ),
]
