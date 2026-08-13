from django import forms
from django.core.exceptions import ValidationError
import requests
import base64

from apps.console.cloud.upcloud.models import CoreUpCloudAccount


class UpCloudConnectForm(forms.Form):
    account_name = forms.CharField(
        widget=forms.TextInput(attrs={
            'class': 'w-full px-3 py-2 text-gray-700 border rounded-lg focus:outline-none',
            'placeholder': 'Enter your UpCloud account name'
        }),
        max_length=255,
        required=True
    )

    username = forms.CharField(
        widget=forms.TextInput(attrs={
            'class': 'w-full px-3 py-2 text-gray-700 border rounded-lg focus:outline-none',
            'placeholder': 'Enter your UpCloud username'
        }),
        max_length=255,
        required=False
    )

    password = forms.CharField(
        widget=forms.PasswordInput(attrs={
            'class': 'w-full px-3 py-2 text-gray-700 border rounded-lg focus:outline-none',
            'placeholder': 'Enter your UpCloud password'
        }),
        max_length=255,
        required=False
    )

    api_token = forms.CharField(
        widget=forms.PasswordInput(attrs={
            'class': 'w-full px-3 py-2 text-gray-700 border rounded-lg focus:outline-none',
            'placeholder': 'Optional: UpCloud API token (ucat_...)'
        }),
        max_length=255,
        required=False,
        help_text='Optional: preferred over username/password. Generate one in the UpCloud hub.'
    )

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)

    def clean_account_name(self):
        account_name = self.cleaned_data['account_name']

        # Check if account name already exists (case-insensitive)
        if CoreUpCloudAccount.objects.filter(
                cloud__account=self.user.member.active_account,
                name__iexact=account_name
        ).exists():
            raise ValidationError("An account with this name already exists.")

        return account_name

    def clean_username(self):
        username = self.cleaned_data.get('username', '')

        # Check if username already exists (only relevant for Basic auth)
        if username and CoreUpCloudAccount.objects.filter(
                cloud__account=self.user.member.active_account,
                username=username
        ).exists():
            raise ValidationError("This username is already in use.")

        return username

    def clean(self):
        cleaned_data = super().clean()
        username = cleaned_data.get('username')
        password = cleaned_data.get('password')
        api_token = cleaned_data.get('api_token')

        if not api_token and not (username and password):
            raise ValidationError(
                "Enter an UpCloud API token, or both a username and a password."
            )
        if api_token:
            headers = {
                'Authorization': f'Bearer {api_token}',
                'Content-Type': 'application/json',
            }
        else:
            basic = base64.b64encode(f"{username}:{password}".encode()).decode()
            headers = {
                'Authorization': f'Basic {basic}',
                'Content-Type': 'application/json',
            }
        try:
            response = requests.get('https://api.upcloud.com/1.3/account', headers=headers, timeout=10)
        except requests.RequestException:
            raise ValidationError(
                "Could not validate UpCloud credentials. Please check your internet connection."
            )
        if response.status_code != 200:
            raise ValidationError("Invalid UpCloud credentials. Please check and try again.")

        return cleaned_data
