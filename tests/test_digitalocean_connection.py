from django.test import TestCase
from django.contrib.auth.models import User
from unittest.mock import patch, Mock
from apps.console.cloud.models import CoreCloud, CoreCloudServiceProvider
from apps.console.cloud.digitalocean.models import CoreDigitalOceanAccount
from apps.console.account.models import CoreAccount
from tests.utils import CloudTestMixin, TestAccountManager, skip_if_no_real_credentials


class DigitalOceanConnectionTestCase(CloudTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        # Create test user first
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        self.account = CoreAccount.objects.create(
            name="Test Account",
            status=CoreAccount.Status.ACTIVE,
            owner=self.user
        )
        self.provider, _ = CoreCloudServiceProvider.objects.get_or_create(
            code="digitalocean",
            defaults={
                "name": "DigitalOcean",
                "status": CoreCloudServiceProvider.Status.ACTIVE
            }
        )
        self.cloud = CoreCloud.objects.create(
            account=self.account,
            provider=self.provider,
            status=CoreCloud.Status.ACTIVE
        )

    @patch('requests.get')
    def test_valid_token_connection(self, mock_get):
        # Get test account configuration
        account_config = self.get_test_account('digitalocean', 'valid')
        mock_get.return_value = self.create_mock_response('digitalocean', 'account_success')

        do_account = CoreDigitalOceanAccount.objects.create(
            cloud=self.cloud,
            name=account_config['name'],
            access_token=account_config['access_token']
        )

        result = do_account.validate()
        self.assertTrue(result)
        self.assert_api_called_with_credentials(mock_get, 'digitalocean', account_config)

    @patch('requests.get')
    def test_invalid_token_connection(self, mock_get):
        # Get invalid test account configuration
        account_config = self.get_test_account('digitalocean', 'invalid')
        mock_get.return_value = self.create_mock_response('digitalocean', 'account_unauthorized')

        do_account = CoreDigitalOceanAccount.objects.create(
            cloud=self.cloud,
            name=account_config['name'],
            access_token=account_config['access_token']
        )

        result = do_account.validate()
        self.assertFalse(result)

    @patch('requests.get')
    def test_connection_timeout(self, mock_get):
        mock_get.side_effect = Exception("Connection timeout")

        do_account = CoreDigitalOceanAccount.objects.create(
            cloud=self.cloud,
            name="Test DO Account",
            access_token="some_token"
        )

        result = do_account.validate()
        self.assertFalse(result)

    @patch('requests.get')
    def test_malformed_token(self, mock_get):
        mock_response = Mock()
        mock_response.status_code = 400
        mock_get.return_value = mock_response

        do_account = CoreDigitalOceanAccount.objects.create(
            cloud=self.cloud,
            name="Test DO Account",
            access_token=""
        )

        result = do_account.validate()
        self.assertFalse(result)

    @patch('requests.get')
    def test_rate_limited_connection(self, mock_get):
        mock_response = Mock()
        mock_response.status_code = 429
        mock_get.return_value = mock_response

        do_account = CoreDigitalOceanAccount.objects.create(
            cloud=self.cloud,
            name="Test DO Account",
            access_token="valid_token"
        )

        result = do_account.validate()
        self.assertFalse(result)

    def test_access_token_property(self):
        account_config = self.get_test_account('digitalocean', 'valid')
        do_account = CoreDigitalOceanAccount.objects.create(
            cloud=self.cloud,
            name=account_config['name'],
            access_token=account_config['access_token']
        )

        self.assertEqual(do_account.access_token, account_config['access_token'])

    @skip_if_no_real_credentials('digitalocean')
    def test_real_api_connection(self):
        """
        Integration test with real DigitalOcean API (only runs if real credentials provided)
        """
        account_config = self.get_test_account('digitalocean', 'valid')
        
        do_account = CoreDigitalOceanAccount.objects.create(
            cloud=self.cloud,
            name=account_config['name'],
            access_token=account_config['access_token']
        )

        # This will make actual API call if real credentials are provided
        result = do_account.validate()
        self.assertTrue(result, "Real API validation should succeed with valid credentials")