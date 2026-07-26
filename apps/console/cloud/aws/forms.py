from django import forms
from django.core.exceptions import ValidationError
import boto3

class AWSConnectForm(forms.Form):
    account_name = forms.CharField(
        widget=forms.TextInput(attrs={
            'class': 'w-full px-3 py-2 text-gray-700 border rounded-lg focus:outline-none',
            'placeholder': 'Enter your AWS account name'
        }),
        max_length=255,
        required=True,
        help_text="Enter a name for this AWS account (e.g., 'My AWS Account')"
    )

    access_key = forms.CharField(
        widget=forms.TextInput(attrs={
            'class': 'w-full px-3 py-2 text-gray-700 border rounded-lg focus:outline-none',
            'placeholder': 'Enter your AWS access key'
        }),
        max_length=255,
        required=True,
        help_text="Enter your AWS access key ID"
    )

    secret_key = forms.CharField(
        widget=forms.TextInput(attrs={
            'class': 'w-full px-3 py-2 text-gray-700 border rounded-lg focus:outline-none',
            'placeholder': 'Enter your AWS secret key'
        }),
        max_length=255,
        required=True,
        help_text="Enter your AWS secret access key"
    )

    region = forms.CharField(
        widget=forms.TextInput(attrs={
            'class': 'w-full px-3 py-2 text-gray-700 border rounded-lg focus:outline-none',
            'placeholder': 'Enter your AWS region (e.g., us-east-1)'
        }),
        max_length=20,
        required=True,
        help_text="Enter the AWS region for your resources"
    )

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)

    def clean_account_name(self):
        account_name = self.cleaned_data['account_name']
        from .models import CoreAWSAccount

        if CoreAWSAccount.objects.filter(
                cloud__account=self.user.member.active_account,
                name__iexact=account_name
        ).exists():
            raise ValidationError("An account with this name already exists.")

        return account_name

    def clean_access_key(self):
        access_key = self.cleaned_data['access_key']
        from .models import CoreAWSAccount

        if CoreAWSAccount.objects.filter(
                cloud__account=self.user.member.active_account,
                access_key=access_key
        ).exists():
            raise ValidationError("An account with this access_key already exists.")

        return access_key

    def clean(self):
        cleaned_data = super().clean()
        access_key = cleaned_data.get('access_key')
        secret_key = cleaned_data.get('secret_key')
        region = cleaned_data.get('region')

        if access_key and secret_key and region:
            try:
                # Validate AWS credentials by attempting to list EC2 instances
                session = boto3.Session(
                    aws_access_key_id=access_key,
                    aws_secret_access_key=secret_key,
                    region_name=region
                )
                ec2 = session.client('ec2')
                ec2.describe_instances()
            except Exception as e:
                raise ValidationError(f"Invalid AWS credentials: {str(e)}")

        return cleaned_data