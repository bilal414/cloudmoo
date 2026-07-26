from django.urls import path, include

from apps.console.security.views import PasswordChangeView, ProfileView, AppTwoFactorView, SetupAppTwoFactorView, DisableAppTwoFactorView

app_name = 'security'

urlpatterns = [
    path(
        r"security/",
        include(
            [
                path('password/change/', PasswordChangeView.as_view(), name='password_change'),
                path('profile/', ProfileView.as_view(), name='profile'),
                path('app-two-factor/', AppTwoFactorView.as_view(), name='app_two_factor'),
                path('app-two-factor/setup/', SetupAppTwoFactorView.as_view(), name='setup_app_two_factor'),
                path('app-two-factor/disable/', DisableAppTwoFactorView.as_view(), name='disable_app_two_factor'),
            ]
        ),
    ),
]

