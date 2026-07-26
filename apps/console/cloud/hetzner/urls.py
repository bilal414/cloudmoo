from django.urls import path
from .views import ConnectHetznerView

app_name = "hetzner"

urlpatterns = [
    path('hetzner/', ConnectHetznerView.as_view(), name='connect'),
]