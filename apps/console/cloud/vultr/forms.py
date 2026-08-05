from django import forms
from django.core.exceptions import ValidationError
from apps.console.cloud.models import CoreCloud
from apps.console.cloud.vultr.models import CoreVultrAccount
import requests


class VultrConnectForm(forms.Form):
    account_name = forms.CharField(
        widget=forms.TextInput(attrs={
            'class': 'w-full px-3 py-2 text-gray-700 border rounded-lg focus:outline-none',
            'placeholder': 'Enter your Vultr account name'
        }),
        max_length=255,
        required=True,
        help_text="Enter a name for this Vultr account (e.g., 'My Vultr Account')"
    )

    access_token = forms.CharField(
        widget=forms.PasswordInput(attrs={
            'class': 'w-full px-3 py-2 text-gray-700 border rounded-lg focus:outline-none',
            'placeholder': 'Enter your Vultr access token'
        }),
        max_length=255,
        required=True,
        help_text="You can generate an access token in your Vultr account settings."
    )

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)

    def clean_account_name(self):
        account_name = self.cleaned_data['account_name']

        # Check if account name already exists (case-insensitive)
        if CoreVultrAccount.objects.filter(
                cloud__account=self.user.member.active_account,
                name__iexact=account_name
        ).exists():
            raise ValidationError("An account with this name already exists.")

        return account_name

    def clean_access_token(self):
        access_token = self.cleaned_data['access_token']

        # Check if access token already exists
        if CoreVultrAccount.objects.filter(
                cloud__account=self.user.member.active_account,
                access_token=access_token
        ).exists():
            raise ValidationError("This access token is already in use.")

        # Validate token with Vultr API
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }
        try:
            response = requests.get('https://api.vultr.com/v2/account', headers=headers, timeout=10)
            if response.status_code != 200:
                error_message = response.json().get('error', 'Unknown error occurred')
                raise ValidationError(f"Invalid Vultr API token: {error_message}")
        except requests.RequestException:
            raise ValidationError("Could not validate Vultr API token. Please check your internet connection.")

        return access_token
