from django.urls import path
from .views import AccountListView, SwitchAccountView

app_name = "account"

urlpatterns = [
    path('accounts/', AccountListView.as_view(), name='list'),
    path('switch/<int:account_id>/', SwitchAccountView.as_view(), name='switch'),
]