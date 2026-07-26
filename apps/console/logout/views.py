from django.contrib.auth.views import LogoutView
from django.urls import reverse_lazy


class CustomLogoutView(LogoutView):
    next_page = reverse_lazy('console:login')
