from django.views import View
from django.shortcuts import render, redirect
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib import messages
from django.utils import timezone

from .forms import AWSConnectForm
from .models import CoreAWSAccount
from ..models import CoreCloudServiceProvider, CoreCloud


class ConnectAWSView(LoginRequiredMixin, View):
    template_name = 'console/cloud/aws/connect.html'
    form_class = AWSConnectForm

    def get(self, request):
        form = self.form_class(user=request.user)
        return render(request, self.template_name, {'form': form})

    def post(self, request):
        form = self.form_class(request.POST, user=request.user)
        if form.is_valid():
            try:
                aws_account = self.create_account(
                    user=request.user,
                    account_name=form.cleaned_data['account_name'],
                    access_key=form.cleaned_data['access_key'],
                    secret_key=form.cleaned_data['secret_key'],
                    region=form.cleaned_data['region']
                )
                aws_account.cloud.sync_assets()
                messages.success(request, 'AWS account connected successfully!')
                return redirect('console:cloud:list')
            except Exception as e:
                messages.error(request, f'Error connecting AWS account: {str(e)}')
        return render(request, self.template_name, {'form': form})

    def create_account(self, user, account_name, access_key, secret_key, region):
        core_cloud = CoreCloud.objects.create(
            account=user.member.active_account,
            provider=CoreCloudServiceProvider.objects.get(code='aws'),
        )

        aws_account = CoreAWSAccount.objects.create(
            cloud=core_cloud,
            access_key=access_key,
            secret_key=secret_key,
            region=region,
            name=account_name,
            status='active',
            last_synced=timezone.now()
        )
        return aws_account
