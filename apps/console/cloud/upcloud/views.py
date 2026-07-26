from django.views import View
from django.shortcuts import render, redirect
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib import messages
from .models import CoreUpCloudAccount, CoreCloud
from .forms import UpCloudConnectForm
from datetime import datetime

from ..models import CoreCloudServiceProvider

class ConnectUpCloudView(LoginRequiredMixin, View):
    template_name = 'console/cloud/upcloud/connect.html'
    form_class = UpCloudConnectForm

    def get(self, request):
        form = self.form_class()
        return render(request, self.template_name, {'form': form})

    def post(self, request):
        form = self.form_class(request.POST, user=request.user)
        if form.is_valid():
            try:
                upcloud_account = self.create_account(
                    user=request.user,
                    account_name=form.cleaned_data['account_name'],
                    username=form.cleaned_data['username'],
                    password=form.cleaned_data['password']
                )
                upcloud_account.sync_assets()
                messages.success(request, 'UpCloud account connected successfully!')
                return redirect('console:cloud:list')
            except Exception as e:
                messages.error(request, f'Error connecting Hetzner account: {str(e)}')
        return render(request, self.template_name, {'form': form})

    def create_account(self, user, account_name, username, password):
        # Get or create CoreCloud
        core_cloud, _ = CoreCloud.objects.get_or_create(
            account=user.member.active_account,
            provider=CoreCloudServiceProvider.objects.get(code='upcloud'),
        )

        # Create or update CoreUpCloudAccount
        upcloud_account = CoreUpCloudAccount.objects.create(
            cloud=core_cloud,
            username=username,
            password=password,
            name=account_name,
            status='active',
            last_synced=datetime.now()
        )
        return upcloud_account