from django.urls import path, include
from .views import AssetsListView, AssetDetailView

app_name = "asset"

urlpatterns = [
    path(
        r"assets/",
        include(
            [
                path('', AssetsListView.as_view(), name='list'),
                path('<str:provider_code>/<str:asset_type>/<int:asset_id>/', AssetDetailView.as_view(), name='detail'),
                path('<str:provider_code>/<str:asset_type>/<int:asset_id>/update-monitoring/',
                     AssetDetailView.as_view(), {'action': 'update_monitoring'}, name='update_monitoring'),
                path('<str:provider_code>/<str:asset_type>/<int:asset_id>/update-email-list/',
                     AssetDetailView.as_view(), {'action': 'update_email_list'}, name='update_emails'),
                path('<str:provider_code>/<str:asset_type>/<int:asset_id>/check-status/',
                     AssetDetailView.as_view(), {'action': 'check_status'}, name='check_status'),
            ]
        ),
    ),
]
