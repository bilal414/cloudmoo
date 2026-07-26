from django.conf.urls import include
from django.urls import path
from django.views.generic import RedirectView

app_name = "console"

urlpatterns = [
    path('', RedirectView.as_view(url='/console', permanent=False), name='root'),
    path('', include('apps.console.login.urls')),
    path('', include('apps.console.signup.urls')),
    path('', include('apps.console.logout.urls')),
    path(
        r"console/",
        include(
            [
                path('', include('apps.console.home.urls')),
                path('', include('apps.console.account.urls')),
                path('', include('apps.console.cloud.urls')),
                path('', include('apps.console.asset.urls')),
                path('', include('apps.console.error.urls')),
                path('', include('apps.console.security.urls')),
                path('', include('apps.console.notifications.urls')),
            ]
        ),
    ),
]
