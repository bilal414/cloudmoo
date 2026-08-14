from django.views import View
from django.shortcuts import render, redirect
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib import messages
from apps.monitoring.tasks import queue_cloud_sync
from .models import CoreOracleAccount, CoreCloud
from .forms import OracleConnectForm
from django.utils import timezone

from ..models import CoreCloudServiceProvider

class ConnectOracleView(LoginRequiredMixin, View):
    template_name = 'console/cloud/oracle/connect.html'
    form_class = OracleConnectForm

    def get(self, request):
        form = self.form_class()
        return render(request, self.template_name, {'form': form})

    def post(self, request):
        form = self.form_class(request.POST, user=request.user)
        if form.is_valid():
            try:
                oracle_account = self.create_account(
                    user=request.user,
                    account_name=form.cleaned_data['account_name'],
                    tenancy_ocid=form.cleaned_data['tenancy_ocid'],
                    user_ocid=form.cleaned_data['user_ocid'],
                    fingerprint=form.cleaned_data['fingerprint'],
                    region=form.cleaned_data['region'],
                    private_key=form.cleaned_data['private_key'],
                )
                queue_cloud_sync(oracle_account.cloud)
                messages.success(request, 'Oracle Cloud account connected successfully! Asset sync is running in the background.')
                return redirect('console:cloud:list')
            except Exception as e:
                messages.error(request, f'Error connecting Oracle Cloud account: {str(e)}')
        return render(request, self.template_name, {'form': form})

    def create_account(self, user, account_name, tenancy_ocid, user_ocid, fingerprint, region, private_key):
        # Get or create CoreCloud
        core_cloud, _ = CoreCloud.objects.get_or_create(
            account=user.member.active_account,
            provider=CoreCloudServiceProvider.objects.get(code='oracle'),
        )

        # Create or update CoreOracleAccount
        oracle_account = CoreOracleAccount.objects.create(
            cloud=core_cloud,
            tenancy_ocid=tenancy_ocid,
            user_ocid=user_ocid,
            fingerprint=fingerprint,
            region=region,
            private_key=private_key,
            name=account_name,
            status='active',
            last_synced=timezone.now()
        )
        return oracle_account
