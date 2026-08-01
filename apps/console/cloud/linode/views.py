from django.views import View
from django.shortcuts import render, redirect
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib import messages
from .models import CoreLinodeAccount, CoreCloud
from .forms import LinodeConnectForm
from django.utils import timezone

from ..models import CoreCloudServiceProvider


class ConnectLinodeView(LoginRequiredMixin, View):
    template_name = 'console/cloud/linode/connect.html'
    form_class = LinodeConnectForm

    def get(self, request):
        form = self.form_class(user=request.user)
        return render(request, self.template_name, {'form': form})

    def post(self, request):
        form = self.form_class(request.POST, user=request.user)
        if form.is_valid():
            try:
                linode_account = self.create_account(
                    user=request.user,
                    account_name=form.cleaned_data['account_name'],
                    access_token=form.cleaned_data['access_token']
                )
                linode_account.sync_assets()
                messages.success(request, 'Linode account connected successfully!')
                return redirect('console:cloud:list')
            except Exception as e:
                messages.error(request, f'Error connecting Linode account: {str(e)}')
        return render(request, self.template_name, {'form': form})

    def create_account(self, user, account_name, access_token):
        # Create CoreCloud
        core_cloud = CoreCloud.objects.create(
            account=user.member.active_account,
            provider=CoreCloudServiceProvider.objects.get(code='linode'),
        )

        # Create CoreLinodeAccount
        linode_account = CoreLinodeAccount.objects.create(
            cloud=core_cloud,
            access_token=access_token,
            name=account_name,
            status='active',
            last_synced=timezone.now()
        )
        return linode_account
