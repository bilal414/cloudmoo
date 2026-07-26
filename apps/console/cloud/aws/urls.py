from django.urls import path
from .views import ConnectAWSView

app_name = "aws"

urlpatterns = [
    path('aws/', ConnectAWSView.as_view(), name='connect'),
]