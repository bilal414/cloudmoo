from django.urls import path
from .views import ConnectVultrView

app_name = "vultr"

urlpatterns = [
    path('vultr/', ConnectVultrView.as_view(), name='connect'),
]