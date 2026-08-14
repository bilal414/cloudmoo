from django.urls import path

from . import views

app_name = "mobile"

urlpatterns = [
    path('auth/login/', views.LoginView.as_view(), name='login'),
    path('auth/logout/', views.LogoutView.as_view(), name='logout'),
    path('auth/me/', views.MeView.as_view(), name='me'),
    path('overview/', views.OverviewView.as_view(), name='overview'),
    path('clouds/', views.CloudListView.as_view(), name='clouds'),
    path('clouds/<uuid:cloud_uuid>/', views.CloudDetailView.as_view(), name='cloud_detail'),
    path('clouds/<uuid:cloud_uuid>/sync/', views.CloudSyncView.as_view(), name='cloud_sync'),
    path('assets/', views.AssetListView.as_view(), name='assets'),
    path(
        'assets/<str:provider>/<str:asset_type>/<int:asset_id>/',
        views.AssetDetailView.as_view(),
        name='asset_detail',
    ),
    path(
        'assets/<str:provider>/<str:asset_type>/<int:asset_id>/check/',
        views.AssetCheckView.as_view(),
        name='asset_check',
    ),
    path(
        'assets/<str:provider>/<str:asset_type>/<int:asset_id>/pause/',
        views.AssetMonitoringView.as_view(),
        {'action': 'pause'},
        name='asset_pause',
    ),
    path(
        'assets/<str:provider>/<str:asset_type>/<int:asset_id>/resume/',
        views.AssetMonitoringView.as_view(),
        {'action': 'resume'},
        name='asset_resume',
    ),
    path('activity/', views.ActivityListView.as_view(), name='activity'),
    path('notifications/', views.NotificationListView.as_view(), name='notifications'),
    path('account/', views.AccountView.as_view(), name='account'),
]
