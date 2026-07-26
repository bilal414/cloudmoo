from django import forms
from django.core.exceptions import ValidationError
from apps.console.cloud.models import CoreCloud
from apps.console.cloud.hetzner.models import CoreHetznerAccount
import requests


class HetznerConnectForm(forms.Form):
    account_name = forms.CharField(
        widget=forms.TextInput(attrs={
            'class': 'w-full px-3 py-2 text-gray-700 border rounded-lg focus:outline-none',
            'placeholder': 'Enter your Hetzner account name'
        }),
        max_length=255,
        required=True,
        help_text="Enter a name for this Hetzner account (e.g., 'My Hetzner Account')"
    )
    access_token = forms.CharField(
        widget=forms.TextInput(attrs={
            'class': 'w-full px-3 py-2 text-gray-700 border rounded-lg focus:outline-none',
            'placeholder': 'Enter your Hetzner API token'
        }),
        max_length=255,
        required=True,
        help_text="You can generate an API token in your Hetzner account settings."
    )

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)

    def clean_account_name(self):
        account_name = self.cleaned_data['account_name']

        # Check if account name already exists (case-insensitive)
        if CoreHetznerAccount.objects.filter(
                cloud__account=self.user.member.active_account,
                name__iexact=account_name
        ).exists():
            raise ValidationError("An account with this name already exists.")

        return account_name

    def clean_access_token(self):
        access_token = self.cleaned_data['access_token']

        # Check if access token already exists
        if CoreHetznerAccount.objects.filter(
                cloud__account=self.user.member.active_account,
                access_token=access_token
        ).exists():
            raise ValidationError("This access token is already in use.")

        # Validate token with Hetzner API
        headers = {'Authorization': f'Bearer {access_token}'}
        try:
            response = requests.get('https://api.hetzner.cloud/v1/servers', headers=headers)
            if response.status_code != 200:
                raise ValidationError("Invalid Hetzner API token. Please check and try again.")
        except requests.RequestException:
            raise ValidationError("Could not validate Hetzner API token. Please check your internet connection.")

        return access_token
