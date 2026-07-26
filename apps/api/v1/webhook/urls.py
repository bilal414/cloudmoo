from django.urls import re_path

from .views import *

urlpatterns = [
    re_path('webhook/cloud/sync_assets/', CloudSyncAPIView.as_view(), name='cloud-sync-api'),

]
