import logging

from django.views import View
from django.shortcuts import render, redirect
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib import messages
from .models import CoreHetznerAccount, CoreCloud
from apps.console.account.models import CoreAccount
from .forms import HetznerConnectForm
import requests
from django.utils import timezone

from ..models import CoreCloudServiceProvider


logger = logging.getLogger(__name__)


class ConnectHetznerView(LoginRequiredMixin, View):
    template_name = 'console/cloud/hetzner/connect.html'
    form_class = HetznerConnectForm

    def get(self, request):
        form = self.form_class(user=request.user)
        return render(request, self.template_name, {'form': form})

    def post(self, request):
        form = self.form_class(request.POST, user=request.user)
        if form.is_valid():
            try:
                hetzner_account = self.create_account(
                    user=request.user,
                    account_name=form.cleaned_data['account_name'],
                    access_token=form.cleaned_data['access_token'],
                    object_storage_access_key=form.cleaned_data.get('object_storage_access_key', ''),
                    object_storage_secret_key=form.cleaned_data.get('object_storage_secret_key', ''),
                    object_storage_region=form.cleaned_data.get('object_storage_region', ''),
                )
                hetzner_account.cloud.sync_assets()
                messages.success(request, 'Hetzner account connected successfully!')
                return redirect('console:cloud:list')
            except Exception:
                # Provider exceptions can contain request URLs or credential
                # material. Log only the exception type and show a stable
                # message to the user.
                logger.exception('Hetzner account connection failed')
                messages.error(request, 'Could not connect the Hetzner account. Please try again.')
        return render(request, self.template_name, {'form': form})

    def create_account(
        self,
        user,
        account_name,
        access_token,
        object_storage_access_key='',
        object_storage_secret_key='',
        object_storage_region='',
    ):
        # Create CoreCloud
        core_cloud = CoreCloud.objects.create(
            account=user.member.active_account,
            provider=CoreCloudServiceProvider.objects.get(code='hetzner'),
        )

        # Create CoreHetznerAccount
        hetzner_account = CoreHetznerAccount.objects.create(
            cloud=core_cloud,
            access_token=access_token,
            name=account_name,
            status='active',
            object_storage_access_key=object_storage_access_key,
            object_storage_secret_key=object_storage_secret_key,
            object_storage_region=object_storage_region or 'fsn1',
            last_synced=timezone.now()
        )
        return hetzner_account
