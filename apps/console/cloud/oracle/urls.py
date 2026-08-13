from django.urls import path
from .views import ConnectOracleView

app_name = "oracle"

urlpatterns = [
    path('oracle/', ConnectOracleView.as_view(), name='connect'),
]
