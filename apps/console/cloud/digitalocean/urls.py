from django.urls import path
from .views import ConnectDigitalOceanView

app_name = "digitalocean"

urlpatterns = [
    path('digitalocean/', ConnectDigitalOceanView.as_view(), name='connect'),
]