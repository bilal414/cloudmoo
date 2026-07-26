from django.test import TestCase
from django.contrib.auth.models import User
from apps.console.cloud.models import CoreCloud, CoreCloudServiceProvider
from apps.console.cloud.digitalocean.models import CoreDigitalOceanAccount
from apps.console.cloud.aws.models import CoreAWSAccount
from apps.console.cloud.vultr.models import CoreVultrAccount
from apps.console.cloud.hetzner.models import CoreHetznerAccount
from apps.console.cloud.linode.models import CoreLinodeAccount
from apps.console.cloud.upcloud.models import CoreUpCloudAccount
from apps.console.account.models import CoreAccount
from tests.utils import get_integration_test_data


class CloudIntegrationTestCase(TestCase):
    """
    Integration tests that actually call cloud provider APIs.
    Only runs if real credentials are provided in test_accounts.json and use_real_api is set to true.
    """

    def setUp(self):
        self.test_data = get_integration_test_data()
        if not self.test_data:
            self.skipTest("No real API credentials configured or use_real_api is disabled")

        # Create test user first
        self.user = User.objects.create_user(
            username='integrationtestuser',
            email='integration@example.com',
            password='testpass123'
        )
        self.account = CoreAccount.objects.create(
            name="Integration Test Account",
            status=CoreAccount.Status.ACTIVE,
            owner=self.user
        )

    def create_provider_and_cloud(self, provider_code, provider_name):
        """Helper to create provider and cloud objects"""
        provider, _ = CoreCloudServiceProvider.objects.get_or_create(
            code=provider_code,
            defaults={
                "name": provider_name,
                "status": CoreCloudServiceProvider.Status.ACTIVE
            }
        )
        cloud = CoreCloud.objects.create(
            account=self.account,
            provider=provider,
            status=CoreCloud.Status.ACTIVE
        )
        return provider, cloud

    def test_digitalocean_real_connection(self):
        if 'digitalocean' not in self.test_data:
            self.skipTest("No DigitalOcean credentials for integration testing")

        provider, cloud = self.create_provider_and_cloud('digitalocean', 'DigitalOcean')
        account_config = self.test_data['digitalocean']

        do_account = CoreDigitalOceanAccount.objects.create(
            cloud=cloud,
            name=account_config['name'],
            access_token=account_config['access_token']
        )

        result = do_account.validate()
        self.assertTrue(result, f"DigitalOcean API validation failed with real credentials")

    def test_aws_real_connection(self):
        if 'aws' not in self.test_data:
            self.skipTest("No AWS credentials for integration testing")

        provider, cloud = self.create_provider_and_cloud('aws', 'Amazon Web Services')
        account_config = self.test_data['aws']

        aws_account = CoreAWSAccount.objects.create(
            cloud=cloud,
            name=account_config['name'],
            access_key=account_config['access_key'],
            secret_key=account_config['secret_key'],
            region=account_config['region']
        )

        result = aws_account.validate()
        self.assertTrue(result, f"AWS API validation failed with real credentials")

    def test_vultr_real_connection(self):
        if 'vultr' not in self.test_data:
            self.skipTest("No Vultr credentials for integration testing")

        provider, cloud = self.create_provider_and_cloud('vultr', 'Vultr')
        account_config = self.test_data['vultr']

        vultr_account = CoreVultrAccount.objects.create(
            cloud=cloud,
            name=account_config['name'],
            access_token=account_config['access_token']
        )

        result = vultr_account.validate()
        self.assertTrue(result, f"Vultr API validation failed with real credentials")

    def test_hetzner_real_connection(self):
        if 'hetzner' not in self.test_data:
            self.skipTest("No Hetzner credentials for integration testing")

        provider, cloud = self.create_provider_and_cloud('hetzner', 'Hetzner')
        account_config = self.test_data['hetzner']

        hetzner_account = CoreHetznerAccount.objects.create(
            cloud=cloud,
            name=account_config['name'],
            access_token=account_config['access_token']
        )

        result = hetzner_account.validate()
        self.assertTrue(result, f"Hetzner API validation failed with real credentials")

    def test_linode_real_connection(self):
        if 'linode' not in self.test_data:
            self.skipTest("No Linode credentials for integration testing")

        provider, cloud = self.create_provider_and_cloud('linode', 'Linode')
        account_config = self.test_data['linode']

        linode_account = CoreLinodeAccount.objects.create(
            cloud=cloud,
            name=account_config['name'],
            access_token=account_config['access_token']
        )

        result = linode_account.validate()
        self.assertTrue(result, f"Linode API validation failed with real credentials")

    def test_upcloud_real_connection(self):
        if 'upcloud' not in self.test_data:
            self.skipTest("No UpCloud credentials for integration testing")

        provider, cloud = self.create_provider_and_cloud('upcloud', 'UpCloud')
        account_config = self.test_data['upcloud']

        upcloud_account = CoreUpCloudAccount.objects.create(
            cloud=cloud,
            name=account_config['name'],
            username=account_config['username'],
            password=account_config['password']
        )

        result = upcloud_account.validate()
        self.assertTrue(result, f"UpCloud API validation failed with real credentials")

    def test_all_configured_providers(self):
        """Test all providers that have real credentials configured"""
        results = {}
        
        for provider_code, account_config in self.test_data.items():
            with self.subTest(provider=provider_code):
                provider_name_map = {
                    'digitalocean': 'DigitalOcean',
                    'aws': 'Amazon Web Services',
                    'vultr': 'Vultr',
                    'hetzner': 'Hetzner',
                    'linode': 'Linode',
                    'upcloud': 'UpCloud'
                }
                
                provider, cloud = self.create_provider_and_cloud(
                    provider_code,
                    provider_name_map[provider_code]
                )

                # Create account based on provider type
                if provider_code == 'aws':
                    account = CoreAWSAccount.objects.create(
                        cloud=cloud,
                        name=account_config['name'],
                        access_key=account_config['access_key'],
                        secret_key=account_config['secret_key'],
                        region=account_config['region']
                    )
                elif provider_code == 'upcloud':
                    account = CoreUpCloudAccount.objects.create(
                        cloud=cloud,
                        name=account_config['name'],
                        username=account_config['username'],
                        password=account_config['password']
                    )
                else:
                    model_map = {
                        'digitalocean': CoreDigitalOceanAccount,
                        'vultr': CoreVultrAccount,
                        'hetzner': CoreHetznerAccount,
                        'linode': CoreLinodeAccount,
                    }
                    model_class = model_map[provider_code]
                    account = model_class.objects.create(
                        cloud=cloud,
                        name=account_config['name'],
                        access_token=account_config['access_token']
                    )

                result = account.validate()
                results[provider_code] = result
                self.assertTrue(result, f"{provider_code} API validation failed")

        # Print summary
        print(f"\nIntegration test results:")
        for provider, result in results.items():
            status = "✓ PASS" if result else "✗ FAIL"
            print(f"  {provider.upper()}: {status}")
        
        self.assertTrue(all(results.values()), "Some integration tests failed")