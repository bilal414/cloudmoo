from django.urls import path
from .views import ConnectUpCloudView

app_name = "upcloud"

urlpatterns = [
    path('upcloud/', ConnectUpCloudView.as_view(), name='connect'),
]