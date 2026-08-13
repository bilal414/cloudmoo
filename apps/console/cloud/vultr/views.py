# apps/console/cloud/vultr/views.py
from django.views import View
from django.shortcuts import render, redirect
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib import messages
from .models import CoreVultrAccount, CoreCloud
from .forms import VultrConnectForm
from django.utils import timezone
from ..models import CoreCloudServiceProvider


class ConnectVultrView(LoginRequiredMixin, View):
    template_name = 'console/cloud/vultr/connect.html'
    form_class = VultrConnectForm

    def get(self, request):
        form = self.form_class(user=request.user)
        return render(request, self.template_name, {'form': form})

    def post(self, request):
        form = self.form_class(request.POST, user=request.user)
        if form.is_valid():
            try:
                vultr_account = self.create_account(
                    user=request.user,
                    account_name=form.cleaned_data['account_name'],
                    access_token=form.cleaned_data['access_token']
                )
                vultr_account.cloud.sync_assets()
                messages.success(request, 'Vultr account connected successfully!')
                return redirect('console:cloud:list')
            except Exception as e:
                messages.error(request, f'Error connecting Vultr account: {str(e)}')
        return render(request, self.template_name, {'form': form})

    def create_account(self, user, account_name, access_token):
        # Create CoreCloud
        core_cloud = CoreCloud.objects.create(
            account=user.member.active_account,
            provider=CoreCloudServiceProvider.objects.get(code='vultr'),
        )

        # Create CoreVultrAccount
        vultr_account = CoreVultrAccount.objects.create(
            cloud=core_cloud,
            access_token=access_token,
            name=account_name,
            status='active',
            last_synced=timezone.now()
        )
        return vultr_account
