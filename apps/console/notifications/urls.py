from django.urls import path
from . import views

app_name = "notifications"

urlpatterns = [
    path('notifications/', views.IndexView.as_view(), name='index'),
]
