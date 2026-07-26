from django.urls import path
from apps.console.logout.views import CustomLogoutView

urlpatterns = [
    path('logout/', CustomLogoutView.as_view(), name='logout'),
]