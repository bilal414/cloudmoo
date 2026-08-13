from django import forms
from django.core.exceptions import ValidationError

import oci
from oci.exceptions import ServiceError

from apps.console.cloud.oracle.models import CoreOracleAccount


class OracleConnectForm(forms.Form):
    account_name = forms.CharField(
        widget=forms.TextInput(attrs={
            'class': 'w-full px-3 py-2 text-gray-700 border rounded-lg focus:outline-none',
            'placeholder': 'Enter your Oracle Cloud account name'
        }),
        max_length=255,
        required=True
    )

    tenancy_ocid = forms.CharField(
        widget=forms.TextInput(attrs={
            'class': 'w-full px-3 py-2 text-gray-700 border rounded-lg focus:outline-none',
            'placeholder': 'ocid1.tenancy.oc1..aaaaaaaa...'
        }),
        max_length=255,
        required=True,
        help_text='Tenancy OCID from the OCI Console tenancy details page.'
    )

    user_ocid = forms.CharField(
        widget=forms.TextInput(attrs={
            'class': 'w-full px-3 py-2 text-gray-700 border rounded-lg focus:outline-none',
            'placeholder': 'ocid1.user.oc1..aaaaaaaa...'
        }),
        max_length=255,
        required=True,
        help_text='OCID of the OCI user that owns the API signing key.'
    )

    fingerprint = forms.CharField(
        widget=forms.TextInput(attrs={
            'class': 'w-full px-3 py-2 text-gray-700 border rounded-lg focus:outline-none',
            'placeholder': 'aa:bb:cc:dd:...'
        }),
        max_length=255,
        required=True,
        help_text='Fingerprint of the API signing key uploaded to OCI.'
    )

    region = forms.CharField(
        widget=forms.TextInput(attrs={
            'class': 'w-full px-3 py-2 text-gray-700 border rounded-lg focus:outline-none',
            'placeholder': 'us-ashburn-1'
        }),
        max_length=255,
        required=True,
        help_text='Region identifier of your tenancy, e.g. us-ashburn-1.'
    )

    private_key = forms.CharField(
        widget=forms.Textarea(attrs={
            'class': 'w-full px-3 py-2 text-gray-700 border rounded-lg focus:outline-none',
            'rows': 8,
            'placeholder': '-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----'
        }),
        required=True,
        help_text='PEM contents of the private API signing key.'
    )

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)

    def clean_account_name(self):
        account_name = self.cleaned_data['account_name']

        # Check if account name already exists (case-insensitive)
        if CoreOracleAccount.objects.filter(
                cloud__account=self.user.member.active_account,
                name__iexact=account_name
        ).exists():
            raise ValidationError("An account with this name already exists.")

        return account_name

    def clean(self):
        cleaned_data = super().clean()
        tenancy_ocid = cleaned_data.get('tenancy_ocid')
        user_ocid = cleaned_data.get('user_ocid')
        fingerprint = cleaned_data.get('fingerprint')
        region = cleaned_data.get('region')
        private_key = cleaned_data.get('private_key')

        if not all([tenancy_ocid, user_ocid, fingerprint, region, private_key]):
            # Field-level errors already describe what is missing.
            return cleaned_data

        config = {
            'tenancy': tenancy_ocid,
            'user': user_ocid,
            'fingerprint': fingerprint,
            'region': region,
            'key_content': private_key,
        }
        try:
            identity_client = oci.identity.IdentityClient(config)
            identity_client.get_tenancy(tenancy_ocid)
        except ServiceError:
            raise ValidationError("Invalid Oracle Cloud credentials. Please check and try again.")
        except Exception:
            raise ValidationError(
                "Could not validate Oracle Cloud credentials. Please check your internet connection."
            )

        return cleaned_data
