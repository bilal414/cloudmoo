from django.test import TestCase
from django.contrib.auth.models import User
from unittest.mock import patch, Mock
from apps.console.cloud.models import CoreCloud, CoreCloudServiceProvider
from apps.console.cloud.hetzner.models import CoreHetznerAccount
from apps.console.account.models import CoreAccount
from tests.utils import CloudTestMixin, TestAccountManager, skip_if_no_real_credentials


class HetznerConnectionTestCase(CloudTestMixin, TestCase):
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
            code="hetzner",
            defaults={
                "name": "Hetzner",
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
        account_config = self.get_test_account('hetzner', 'valid')
        mock_get.return_value = self.create_mock_response('hetzner', 'servers_success')

        hetzner_account = CoreHetznerAccount.objects.create(
            cloud=self.cloud,
            name=account_config['name'],
            access_token=account_config['access_token']
        )

        result = hetzner_account.validate()
        self.assertTrue(result)
        self.assert_api_called_with_credentials(mock_get, 'hetzner', account_config)

    @patch('requests.get')
    def test_invalid_token_connection(self, mock_get):
        # Get invalid test account configuration
        account_config = self.get_test_account('hetzner', 'invalid')
        mock_get.return_value = self.create_mock_response('hetzner', 'servers_unauthorized')

        hetzner_account = CoreHetznerAccount.objects.create(
            cloud=self.cloud,
            name=account_config['name'],
            access_token=account_config['access_token']
        )

        result = hetzner_account.validate()
        self.assertFalse(result)

    @patch('requests.get')
    def test_forbidden_access_connection(self, mock_get):
        mock_response = Mock()
        mock_response.status_code = 403
        mock_get.return_value = mock_response

        hetzner_account = CoreHetznerAccount.objects.create(
            cloud=self.cloud,
            name="Test Hetzner Account",
            access_token="restricted_token"
        )

        result = hetzner_account.validate()
        self.assertFalse(result)

    @patch('requests.get')
    def test_rate_limited_connection(self, mock_get):
        mock_response = Mock()
        mock_response.status_code = 429
        mock_get.return_value = mock_response

        hetzner_account = CoreHetznerAccount.objects.create(
            cloud=self.cloud,
            name="Test Hetzner Account",
            access_token="valid_token"
        )

        result = hetzner_account.validate()
        self.assertFalse(result)

    @patch('requests.get')
    def test_server_error_connection(self, mock_get):
        mock_response = Mock()
        mock_response.status_code = 500
        mock_get.return_value = mock_response

        hetzner_account = CoreHetznerAccount.objects.create(
            cloud=self.cloud,
            name="Test Hetzner Account",
            access_token="some_token"
        )

        result = hetzner_account.validate()
        self.assertFalse(result)

    @patch('requests.get')
    def test_connection_timeout(self, mock_get):
        mock_get.side_effect = Exception("Connection timeout")

        hetzner_account = CoreHetznerAccount.objects.create(
            cloud=self.cloud,
            name="Test Hetzner Account",
            access_token="some_token"
        )

        result = hetzner_account.validate()
        self.assertFalse(result)

    @patch('requests.get')
    def test_empty_token_connection(self, mock_get):
        mock_response = Mock()
        mock_response.status_code = 400
        mock_get.return_value = mock_response

        hetzner_account = CoreHetznerAccount.objects.create(
            cloud=self.cloud,
            name="Test Hetzner Account",
            access_token=""
        )

        result = hetzner_account.validate()
        self.assertFalse(result)

    def test_access_token_property(self):
        account_config = self.get_test_account('hetzner', 'valid')
        
        hetzner_account = CoreHetznerAccount.objects.create(
            cloud=self.cloud,
            name=account_config['name'],
            access_token=account_config['access_token']
        )

        self.assertEqual(hetzner_account.access_token, account_config['access_token'])

    @skip_if_no_real_credentials('hetzner')
    def test_real_api_connection(self):
        """
        Integration test with real Hetzner API (only runs if real credentials provided)
        """
        account_config = self.get_test_account('hetzner', 'valid')
        
        hetzner_account = CoreHetznerAccount.objects.create(
            cloud=self.cloud,
            name=account_config['name'],
            access_token=account_config['access_token']
        )

        # This will make actual API call if real credentials are provided
        result = hetzner_account.validate()
        self.assertTrue(result, "Real Hetzner API validation should succeed with valid credentials")

    @patch('requests.get')
    def test_malformed_json_response(self, mock_get):
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.side_effect = ValueError("Invalid JSON")
        mock_get.return_value = mock_response

        hetzner_account = CoreHetznerAccount.objects.create(
            cloud=self.cloud,
            name="Test Hetzner Account",
            access_token="valid_token"
        )

        result = hetzner_account.validate()
        self.assertFalse(result)