import json
import os
from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
from apps.console.cloud.models import CoreCloud, CoreCloudServiceProvider
from apps.console.cloud.digitalocean.models import CoreDigitalOceanAccount
from apps.console.cloud.aws.models import CoreAWSAccount
from apps.console.cloud.vultr.models import CoreVultrAccount
from apps.console.cloud.hetzner.models import CoreHetznerAccount
from apps.console.cloud.linode.models import CoreLinodeAccount
from apps.console.cloud.upcloud.models import CoreUpCloudAccount
from apps.console.account.models import CoreAccount


class Command(BaseCommand):
    help = 'Setup test accounts for all cloud providers from JSON configuration'

    def add_arguments(self, parser):
        parser.add_argument(
            '--config',
            type=str,
            default='tests/test_accounts.json',
            help='Path to test accounts JSON configuration file'
        )
        parser.add_argument(
            '--account-name',
            type=str,
            default='Test Account for Cloud Testing',
            help='Name for the test CoreAccount'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be created without actually creating'
        )
        parser.add_argument(
            '--provider',
            type=str,
            choices=['digitalocean', 'aws', 'vultr', 'hetzner', 'linode', 'upcloud'],
            help='Only setup accounts for specific provider'
        )
        parser.add_argument(
            '--overwrite',
            action='store_true',
            help='Overwrite existing test accounts'
        )

    def handle(self, *args, **options):
        config_path = options['config']
        
        # Resolve relative path from project root
        if not os.path.isabs(config_path):
            config_path = os.path.join(settings.BASE_DIR, config_path)
        
        if not os.path.exists(config_path):
            raise CommandError(f'Configuration file not found: {config_path}')

        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
        except json.JSONDecodeError as e:
            raise CommandError(f'Invalid JSON in configuration file: {e}')

        if 'test_accounts' not in config:
            raise CommandError('Configuration file must contain "test_accounts" section')

        # Get or create test account
        test_account, created = CoreAccount.objects.get_or_create(
            name=options['account_name'],
            defaults={
                'status': CoreAccount.Status.ACTIVE
            }
        )
        
        if created:
            self.stdout.write(
                self.style.SUCCESS(f'Created test account: {test_account.name}')
            )
        else:
            self.stdout.write(f'Using existing test account: {test_account.name}')

        providers_to_setup = [options['provider']] if options['provider'] else config['test_accounts'].keys()
        
        for provider_code in providers_to_setup:
            if provider_code not in config['test_accounts']:
                self.stdout.write(
                    self.style.WARNING(f'No configuration found for provider: {provider_code}')
                )
                continue
                
            self.setup_provider_accounts(
                provider_code, 
                config['test_accounts'][provider_code],
                test_account,
                options
            )

    def setup_provider_accounts(self, provider_code, provider_config, test_account, options):
        self.stdout.write(f'\nSetting up {provider_code.upper()} test accounts...')
        
        # Get or create provider
        try:
            provider = CoreCloudServiceProvider.objects.get(code=provider_code)
        except CoreCloudServiceProvider.DoesNotExist:
            provider_names = {
                'digitalocean': 'DigitalOcean',
                'aws': 'Amazon Web Services',
                'vultr': 'Vultr',
                'hetzner': 'Hetzner',
                'linode': 'Linode',
                'upcloud': 'UpCloud'
            }
            
            if options['dry_run']:
                self.stdout.write(
                    self.style.WARNING(f'Would create provider: {provider_names.get(provider_code, provider_code)}')
                )
                return
                
            provider = CoreCloudServiceProvider.objects.create(
                code=provider_code,
                name=provider_names.get(provider_code, provider_code.title()),
                status=CoreCloudServiceProvider.Status.ACTIVE
            )
            self.stdout.write(f'Created provider: {provider.name}')

        # Setup each account variant for this provider
        for account_type, account_config in provider_config.items():
            self.create_cloud_account(
                provider_code, 
                provider, 
                test_account, 
                account_type, 
                account_config, 
                options
            )

    def create_cloud_account(self, provider_code, provider, test_account, account_type, account_config, options):
        account_name = f"{account_config['name']} ({account_type})"
        
        if options['dry_run']:
            self.stdout.write(f'  Would create: {account_name}')
            return

        # Get or create CoreCloud
        cloud_name = f"{provider.name} Test Cloud ({account_type})"
        cloud, cloud_created = CoreCloud.objects.get_or_create(
            account=test_account,
            provider=provider,
            defaults={
                'status': CoreCloud.Status.ACTIVE
            }
        )

        # Model mapping
        model_map = {
            'digitalocean': CoreDigitalOceanAccount,
            'aws': CoreAWSAccount,
            'vultr': CoreVultrAccount,
            'hetzner': CoreHetznerAccount,
            'linode': CoreLinodeAccount,
            'upcloud': CoreUpCloudAccount
        }

        model_class = model_map.get(provider_code)
        if not model_class:
            self.stdout.write(
                self.style.ERROR(f'No model found for provider: {provider_code}')
            )
            return

        # Check if account already exists
        existing_account = model_class.objects.filter(
            cloud=cloud,
            name=account_name
        ).first()

        if existing_account and not options['overwrite']:
            self.stdout.write(f'  Skipping existing: {account_name}')
            return

        if existing_account and options['overwrite']:
            existing_account.delete()
            self.stdout.write(f'  Deleted existing: {account_name}')

        # Create account with provider-specific fields
        try:
            if provider_code == 'aws':
                account = model_class.objects.create(
                    cloud=cloud,
                    name=account_name,
                    access_key=account_config['access_key'],
                    secret_key=account_config['secret_key'],
                    region=account_config['region']
                )
            elif provider_code == 'upcloud':
                account = model_class.objects.create(
                    cloud=cloud,
                    name=account_name,
                    username=account_config['username'],
                    password=account_config['password']
                )
            else:  # digitalocean, vultr, hetzner, linode
                account = model_class.objects.create(
                    cloud=cloud,
                    name=account_name,
                    access_token=account_config['access_token']
                )

            self.stdout.write(
                self.style.SUCCESS(f'  Created: {account_name}')
            )
            
            # Add notes if description exists
            if 'description' in account_config:
                account.notes = account_config['description']
                account.save()

        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f'  Failed to create {account_name}: {str(e)}')
            )