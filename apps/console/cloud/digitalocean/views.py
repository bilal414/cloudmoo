from django.views import View
from django.shortcuts import render, redirect
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib import messages
from .models import CoreDigitalOceanAccount, CoreCloud
from apps.console.account.models import CoreAccount
from .forms import DigitalOceanConnectForm
import requests
from django.utils import timezone

from ..models import CoreCloudServiceProvider


class ConnectDigitalOceanView(LoginRequiredMixin, View):
    template_name = 'console/cloud/digitalocean/connect.html'
    form_class = DigitalOceanConnectForm

    def get(self, request):
        form = self.form_class(user=request.user)
        return render(request, self.template_name, {'form': form})

    def post(self, request):
        form = self.form_class(request.POST, user=request.user)
        if form.is_valid():
            try:
                do_account = self.create_account(
                    user=request.user,
                    account_name=form.cleaned_data['account_name'],
                    access_token=form.cleaned_data['access_token']
                )
                do_account.sync_assets()
                messages.success(request, 'DigitalOcean account connected successfully!')
                return redirect('console:cloud:list')
            except Exception as e:
                messages.error(request, f'Error connecting DigitalOcean account: {str(e)}')
        return render(request, self.template_name, {'form': form})

    def create_account(self, user, account_name, access_token):
        # Create CoreCloud
        core_cloud = CoreCloud.objects.create(
            account=user.member.active_account,
            provider=CoreCloudServiceProvider.objects.get(code='digitalocean'),
        )

        # Create CoreDigitalOceanAccount
        do_account = CoreDigitalOceanAccount.objects.create(
            cloud=core_cloud,
            access_token=access_token,
            name=account_name,
            status='active',
            last_synced=timezone.now()
        )
        return do_account
