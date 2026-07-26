import boto3
from django import forms

from apps.console.cloud.aws.models import CoreAWSAccount
from apps.console.cloud.hetzner.models import CoreHetznerAccount
from apps.console.cloud.linode.models import CoreLinodeAccount
from apps.console.cloud.models import CoreCloud
from apps.console.cloud.digitalocean.models import CoreDigitalOceanAccount
from apps.console.cloud.upcloud.models import CoreUpCloudAccount
from apps.console.cloud.vultr.models import CoreVultrAccount
import requests


class CloudEditForm(forms.ModelForm):
    STATUS_CHOICES = [
        (CoreCloud.Status.ACTIVE, 'Active'),
        (CoreCloud.Status.PAUSED, 'Paused')
    ]

    # AWS Regions - comprehensive list of all AWS regions
    AWS_REGION_CHOICES = [
        ('us-east-1', 'US East (N. Virginia)'),
        ('us-east-2', 'US East (Ohio)'),
        ('us-west-1', 'US West (N. California)'),
        ('us-west-2', 'US West (Oregon)'),
        ('af-south-1', 'Africa (Cape Town)'),
        ('ap-east-1', 'Asia Pacific (Hong Kong)'),
        ('ap-northeast-1', 'Asia Pacific (Tokyo)'),
        ('ap-northeast-2', 'Asia Pacific (Seoul)'),
        ('ap-northeast-3', 'Asia Pacific (Osaka)'),
        ('ap-south-1', 'Asia Pacific (Mumbai)'),
        ('ap-south-2', 'Asia Pacific (Hyderabad)'),
        ('ap-southeast-1', 'Asia Pacific (Singapore)'),
        ('ap-southeast-2', 'Asia Pacific (Sydney)'),
        ('ap-southeast-3', 'Asia Pacific (Jakarta)'),
        ('ap-southeast-4', 'Asia Pacific (Melbourne)'),
        ('ca-central-1', 'Canada (Central)'),
        ('ca-west-1', 'Canada (Calgary)'),
        ('eu-central-1', 'Europe (Frankfurt)'),
        ('eu-central-2', 'Europe (Zurich)'),
        ('eu-north-1', 'Europe (Stockholm)'),
        ('eu-south-1', 'Europe (Milan)'),
        ('eu-south-2', 'Europe (Spain)'),
        ('eu-west-1', 'Europe (Ireland)'),
        ('eu-west-2', 'Europe (London)'),
        ('eu-west-3', 'Europe (Paris)'),
        ('il-central-1', 'Israel (Tel Aviv)'),
        ('me-central-1', 'Middle East (UAE)'),
        ('me-south-1', 'Middle East (Bahrain)'),
        ('sa-east-1', 'South America (São Paulo)'),
    ]

    name = forms.CharField(
        widget=forms.TextInput(attrs={
            'class': 'w-full px-3 py-2 text-gray-700 border rounded-lg focus:outline-none',
            'placeholder': 'Enter cloud name'
        }),
        required=True
    )

    status = forms.ChoiceField(
        choices=STATUS_CHOICES,
        widget=forms.Select(attrs={
            'class': 'w-full px-3 py-2 text-gray-700 border rounded-lg focus:outline-none'
        })
    )

    class Meta:
        model = CoreCloud
        fields = ['status']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            # Set initial name from provider account
            provider_account = self.instance.provider_account
            if provider_account:
                self.fields['name'].initial = provider_account.name

        if self.instance.provider.code == 'digitalocean':
            self.fields['access_token'] = forms.CharField(
                widget=forms.TextInput(attrs={
                    'class': 'w-full px-3 py-2 text-gray-700 border rounded-lg focus:outline-none',
                    'placeholder': 'Enter your DigitalOcean access token'
                }),
                required=False
            )
            if self.instance.digitalocean.exists():
                self.fields['access_token'].initial = self.instance.digitalocean.first().access_token
        elif self.instance.provider.code == 'hetzner':
            self.fields['access_token'] = forms.CharField(
                widget=forms.TextInput(attrs={
                    'class': 'w-full px-3 py-2 text-gray-700 border rounded-lg focus:outline-none',
                    'placeholder': 'Enter your Hetzner API token'
                }),
                required=False
            )
            if self.instance.hetzner.exists():
                self.fields['access_token'].initial = self.instance.hetzner.first().access_token
        elif self.instance.provider.code == 'vultr':
            self.fields['access_token'] = forms.CharField(
                widget=forms.TextInput(attrs={
                    'class': 'w-full px-3 py-2 text-gray-700 border rounded-lg focus:outline-none',
                    'placeholder': 'Enter your Vultr access token'
                }),
                required=False
            )
            if self.instance.vultr.exists():
                self.fields['access_token'].initial = self.instance.vultr.first().access_token
        elif self.instance.provider.code == 'aws':
            self.fields['access_key'] = forms.CharField(
                widget=forms.TextInput(attrs={
                    'class': 'w-full px-3 py-2 text-gray-700 border rounded-lg focus:outline-none',
                    'placeholder': 'Enter your AWS access key'
                }),
                required=False
            )
            self.fields['secret_key'] = forms.CharField(
                widget=forms.TextInput(attrs={
                    'class': 'w-full px-3 py-2 text-gray-700 border rounded-lg focus:outline-none',
                    'placeholder': 'Enter your AWS secret token'
                }),
                required=False
            )
            self.fields['region'] = forms.ChoiceField(
                choices=self.AWS_REGION_CHOICES,
                widget=forms.Select(attrs={
                    'class': 'w-full px-3 py-2 text-gray-700 border rounded-lg focus:outline-none'
                }),
                required=False,
                initial='us-east-1'
            )
            if self.instance.aws.exists():
                self.fields['access_key'].initial = self.instance.aws.first().access_key
                self.fields['secret_key'].initial = self.instance.aws.first().secret_key
                self.fields['region'].initial = self.instance.aws.first().region
        elif self.instance.provider.code == 'upcloud':
            self.fields['username'] = forms.CharField(
                widget=forms.TextInput(attrs={
                    'class': 'w-full px-3 py-2 text-gray-700 border rounded-lg focus:outline-none',
                    'placeholder': 'Enter your UpCloud username'
                }),
                required=False
            )
            self.fields['password'] = forms.CharField(
                widget=forms.TextInput(attrs={
                    'class': 'w-full px-3 py-2 text-gray-700 border rounded-lg focus:outline-none',
                    'placeholder': 'Enter your UpCloud password'
                }),
                required=False
            )
            if self.instance.upcloud.exists():
                self.fields['username'].initial = self.instance.upcloud.first().username
                self.fields['password'].initial = self.instance.upcloud.first().password

        elif self.instance.provider.code == 'linode':
            self.fields['access_token'] = forms.CharField(
                widget=forms.TextInput(attrs={
                    'class': 'w-full px-3 py-2 text-gray-700 border rounded-lg focus:outline-none',
                    'placeholder': 'Enter your Linode API token'
                }),
                required=False
            )
            if self.instance.linode.exists():
                self.fields['access_token'].initial = self.instance.linode.first().access_token

    def clean(self):
        cleaned_data = super().clean()
        status = cleaned_data.get('status')
        access_token = cleaned_data.get('access_token')
        access_key = cleaned_data.get('access_key')
        secret_key = cleaned_data.get('secret_key')
        region = cleaned_data.get('region')
        username = cleaned_data.get('username')
        password = cleaned_data.get('password')
        name = cleaned_data.get('name')

        if not name:
            self.add_error('name', "Name is required.")

        if self.instance.provider.code == 'digitalocean' and status == CoreCloud.Status.ACTIVE:
            if not access_token:
                if self.instance.digitalocean.exists():
                    access_token = self.instance.digitalocean.first().access_token
                else:
                    self.add_error('access_token', "Access token is required for active DigitalOcean cloud.")
                    return cleaned_data

            if not self.validate_digitalocean_token(access_token):
                self.add_error('access_token', "Invalid DigitalOcean access token.")

        elif self.instance.provider.code == 'hetzner' and status == CoreCloud.Status.ACTIVE:
            if not access_token:
                if self.instance.hetzner.exists():
                    access_token = self.instance.hetzner.first().access_token
                else:
                    self.add_error('access_token', "Access token is required for active Hetzner cloud.")
                    return cleaned_data

            if not self.validate_hetzner_token(access_token):
                self.add_error('access_token', "Invalid Hetzner access token.")

        elif self.instance.provider.code == 'vultr' and status == CoreCloud.Status.ACTIVE:
            if not access_token:
                if self.instance.vultr.exists():
                    access_token = self.instance.vultr.first().access_token
                else:
                    self.add_error('access_token', "Access token is required for active Vultr cloud.")
                    return cleaned_data

            if not self.validate_vultr_token(access_token):
                self.add_error('access_token', "Invalid Vultr access token.")

        elif self.instance.provider.code == 'aws' and status == CoreCloud.Status.ACTIVE:
            if not access_key:
                if self.instance.aws.exists():
                    access_key = self.instance.aws.first().access_key
                    secret_key = self.instance.aws.first().secret_key
                    region = self.instance.aws.first().region
                else:
                    self.add_error('access_key', "Access key is required for active AWS cloud.")
                    self.add_error('secret_key', "Secret key required for active AWS cloud.")
                    self.add_error('region', "Region required for active AWS cloud.")
                    return cleaned_data

            if not secret_key:
                if self.instance.aws.exists():
                    secret_key = self.instance.aws.first().secret_key
                else:
                    self.add_error('secret_key', "Secret key required for active AWS cloud.")
                    return cleaned_data

            if not region:
                if self.instance.aws.exists():
                    region = self.instance.aws.first().region
                else:
                    self.add_error('region', "Region required for active AWS cloud.")
                    return cleaned_data

            if not self.validate_aws(access_key, secret_key, region):
                self.add_error('access_key', "Invalid AWS access key or secret key.")
                self.add_error('secret_key', "Invalid AWS access key or secret key.")

        elif self.instance.provider.code == 'upcloud' and status == CoreCloud.Status.ACTIVE:
            if not username:
                if self.instance.aws.exists():
                    username = self.instance.upcloud.first().username
                else:
                    self.add_error('access_key', "Username is required for active UpCloud.")
                    return cleaned_data
            if not password:
                if self.instance.aws.exists():
                    password = self.instance.upcloud.first().password
                else:
                    self.add_error('password', "Password is required for active UpCloud.")
                    return cleaned_data

            if not self.validate_upcloud(username, password):
                self.add_error('username', "Invalid username or password.")
                self.add_error('password', "Invalid username or password.")

        elif self.instance.provider.code == 'linode' and status == CoreCloud.Status.ACTIVE:
            if not access_token:
                if self.instance.linode.exists():
                    access_token = self.instance.linode.first().access_token
                else:
                    self.add_error('access_token', "Access token is required for active Linode cloud.")
                    return cleaned_data

            if not self.validate_linode_token(access_token):
                self.add_error('access_token', "Invalid Linode access token.")

        return cleaned_data

    def validate_digitalocean_token(self, access_token):
        headers = {'Authorization': f'Bearer {access_token}'}
        response = requests.get('https://api.digitalocean.com/v2/account', headers=headers)
        return response.status_code == 200

    def validate_hetzner_token(self, access_token):
        headers = {'Authorization': f'Bearer {access_token}'}
        response = requests.get('https://api.hetzner.cloud/v1/servers', headers=headers)
        return response.status_code == 200

    def validate_vultr_token(self, access_token):
        headers = {'Authorization': f'Bearer {access_token}'}
        response = requests.get('https://api.vultr.com/v2/account', headers=headers)
        return response.status_code == 200

    def validate_aws(self, access_key, secret_key, region):
        try:
            session = boto3.Session(
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                region_name=region
            )
            ec2 = session.client('ec2')
            ec2.describe_instances()
            return True
        except Exception:
            return False

    def validate_upcloud(self, username, password):
        import base64

        try:
            headers = {
                'Authorization': f'Basic {base64.b64encode(f"{username}:{password}".encode()).decode()}',
                'Content-Type': 'application/json'
            }
            response = requests.get('https://api.upcloud.com/1.3/account', headers=headers)
            return response.status_code == 200
        except Exception:
            return False

    def validate_linode_token(self, access_token):
        headers = {'Authorization': f'Bearer {access_token}'}
        response = requests.get('https://api.linode.com/v4/account', headers=headers)
        return response.status_code == 200

    def save(self, commit=True):
        cloud = super().save(commit=commit)
        name = self.cleaned_data.get('name')
        access_token = self.cleaned_data.get('access_token')
        access_key = self.cleaned_data.get('access_key')
        secret_key = self.cleaned_data.get('secret_key')
        region = self.cleaned_data.get('region')
        username = self.cleaned_data.get('username')
        password = self.cleaned_data.get('password')

        if self.instance.provider.code == 'digitalocean':
            do_account, created = CoreDigitalOceanAccount.objects.get_or_create(
                cloud=cloud,
                defaults={'access_token': access_token, 'name': name}
            )
            if not created:
                do_account.name = name
                if access_token:
                    do_account.access_token = access_token
                do_account.save()

        elif self.instance.provider.code == 'hetzner':
            hetzner_account, created = CoreHetznerAccount.objects.get_or_create(
                cloud=cloud,
                defaults={'access_token': access_token, 'name': name}
            )
            if not created:
                hetzner_account.name = name
                if access_token:
                    hetzner_account.access_token = access_token
                hetzner_account.save()

        elif self.instance.provider.code == 'vultr':
            vultr_account, created = CoreVultrAccount.objects.get_or_create(
                cloud=cloud,
                defaults={'access_token': access_token, 'name': name}
            )
            if not created:
                vultr_account.name = name
                if access_token:
                    vultr_account.access_token = access_token
                vultr_account.save()

        elif self.instance.provider.code == 'aws':
            # Get defaults for creation, ensuring region is set
            defaults = {'secret_key': secret_key, 'name': name}
            if region:
                defaults['region'] = region
            elif self.instance.aws.exists():
                defaults['region'] = self.instance.aws.first().region
            else:
                defaults['region'] = 'us-east-1'  # Default region if none provided
            
            aws_account, created = CoreAWSAccount.objects.get_or_create(
                cloud=cloud,
                access_key=access_key,
                defaults=defaults
            )
            if not created:
                aws_account.name = name
                if access_key:
                    aws_account.access_key = access_key
                    aws_account.secret_key = secret_key
                if region:
                    aws_account.region = region
                aws_account.save()

        elif self.instance.provider.code == 'upcloud':
            upcloud_account, created = CoreUpCloudAccount.objects.get_or_create(
                cloud=cloud,
                defaults={'username': username, 'password': password, 'name': name}
            )
            if not created:
                upcloud_account.name = name
                if username:
                    upcloud_account.username = username
                if password:
                    upcloud_account.password = password
                upcloud_account.save()

        elif self.instance.provider.code == 'linode':
            linode_account, created = CoreLinodeAccount.objects.get_or_create(
                cloud=cloud,
                defaults={'access_token': access_token, 'name': name}
            )
            if not created:
                linode_account.name = name
                if access_token:
                    linode_account.access_token = access_token
                linode_account.save()

        return cloud
