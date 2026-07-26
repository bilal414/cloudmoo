from django.urls import path
from .views import ConnectLinodeView

app_name = "linode"

urlpatterns = [
    path('linode/', ConnectLinodeView.as_view(), name='connect'),
]