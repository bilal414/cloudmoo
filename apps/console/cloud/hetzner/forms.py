from django import forms
from django.core.exceptions import ValidationError
from apps.console.cloud.models import CoreCloud
from apps.console.cloud.hetzner.models import CoreHetznerAccount
import requests


class HetznerConnectForm(forms.Form):
    OBJECT_STORAGE_REGION_CHOICES = (
        ('', 'Do not connect Object Storage'),
        ('fsn1', 'Falkenstein (fsn1)'),
        ('nbg1', 'Nuremberg (nbg1)'),
        ('hel1', 'Helsinki (hel1)'),
    )

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
    object_storage_access_key = forms.CharField(
        widget=forms.TextInput(attrs={
            'class': 'w-full px-3 py-2 text-gray-700 border rounded-lg focus:outline-none',
            'placeholder': 'Optional Object Storage access key'
        }),
        max_length=255,
        required=False,
        help_text='Optional: inventory and monitor Object Storage buckets.'
    )
    object_storage_secret_key = forms.CharField(
        widget=forms.PasswordInput(attrs={
            'class': 'w-full px-3 py-2 text-gray-700 border rounded-lg focus:outline-none',
            'placeholder': 'Optional Object Storage secret key'
        }),
        max_length=1024,
        required=False,
        help_text='Stored securely and used only for metadata-only S3 checks.'
    )
    object_storage_region = forms.ChoiceField(
        choices=OBJECT_STORAGE_REGION_CHOICES,
        required=False,
        initial='',
        widget=forms.Select(attrs={
            'class': 'w-full px-3 py-2 text-gray-700 border rounded-lg focus:outline-none'
        }),
        help_text='Choose the Object Storage endpoint region when credentials are provided.'
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
            response = requests.get('https://api.hetzner.cloud/v1/servers', headers=headers, timeout=10)
            if response.status_code != 200:
                raise ValidationError("Invalid Hetzner API token. Please check and try again.")
        except requests.RequestException:
            raise ValidationError("Could not validate Hetzner API token. Please check your internet connection.")

        return access_token

    def clean(self):
        cleaned_data = super().clean()
        access_key = cleaned_data.get('object_storage_access_key')
        secret_key = cleaned_data.get('object_storage_secret_key')
        region = cleaned_data.get('object_storage_region')
        if access_key or secret_key:
            if not access_key:
                self.add_error(
                    'object_storage_access_key',
                    'Object Storage access key is required when configuring Object Storage.'
                )
            if not secret_key:
                self.add_error(
                    'object_storage_secret_key',
                    'Object Storage secret key is required when configuring Object Storage.'
                )
            if not region:
                self.add_error(
                    'object_storage_region',
                    'Object Storage region is required when configuring Object Storage.'
                )
        elif region:
            self.add_error(
                'object_storage_region',
                'Object Storage credentials are required when a region is selected.'
            )
        return cleaned_data
