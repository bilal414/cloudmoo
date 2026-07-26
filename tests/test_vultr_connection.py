from django.test import TestCase
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from unittest.mock import patch, Mock
from apps.console.cloud.models import CoreCloud, CoreCloudServiceProvider
from apps.console.cloud.vultr.models import CoreVultrAccount
from apps.console.account.models import CoreAccount
from tests.utils import CloudTestMixin, TestAccountManager, skip_if_no_real_credentials


class VultrConnectionTestCase(CloudTestMixin, TestCase):
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
            code="vultr",
            defaults={
                "name": "Vultr",
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
        account_config = self.get_test_account('vultr', 'valid')
        mock_get.return_value = self.create_mock_response('vultr', 'account_success')

        vultr_account = CoreVultrAccount.objects.create(
            cloud=self.cloud,
            name=account_config['name'],
            access_token=account_config['access_token']
        )

        result = vultr_account.validate()
        self.assertTrue(result)
        self.assert_api_called_with_credentials(mock_get, 'vultr', account_config)

    @patch('requests.get')
    def test_invalid_token_connection(self, mock_get):
        # Get invalid test account configuration
        account_config = self.get_test_account('vultr', 'invalid')
        mock_get.return_value = self.create_mock_response('vultr', 'account_unauthorized')

        vultr_account = CoreVultrAccount.objects.create(
            cloud=self.cloud,
            name=account_config['name'],
            access_token=account_config['access_token']
        )

        with self.assertRaises(ValidationError) as context:
            vultr_account.validate()
        
        self.assertIn("Vultr API validation failed", str(context.exception))

    @patch('requests.get')
    def test_forbidden_access_connection(self, mock_get):
        mock_response = Mock()
        mock_response.status_code = 403
        mock_response.json.return_value = {
            "error": "Forbidden access to this resource"
        }
        mock_get.return_value = mock_response

        vultr_account = CoreVultrAccount.objects.create(
            cloud=self.cloud,
            name="Test Vultr Account",
            access_token="restricted_token"
        )

        with self.assertRaises(ValidationError) as context:
            vultr_account.validate()
        
        self.assertIn("Vultr API validation failed", str(context.exception))

    @patch('requests.get')
    def test_rate_limited_connection(self, mock_get):
        # Get rate limited test account configuration
        account_config = self.get_test_account('vultr', 'rate_limited')
        mock_get.return_value = self.create_mock_response('vultr', 'account_rate_limited')

        vultr_account = CoreVultrAccount.objects.create(
            cloud=self.cloud,
            name=account_config['name'],
            access_token=account_config['access_token']
        )

        with self.assertRaises(ValidationError) as context:
            vultr_account.validate()
        
        self.assertIn("Vultr API validation failed", str(context.exception))

    @patch('requests.get')
    def test_server_error_connection(self, mock_get):
        mock_response = Mock()
        mock_response.status_code = 500
        mock_response.json.return_value = {
            "error": "Internal server error"
        }
        mock_get.return_value = mock_response

        vultr_account = CoreVultrAccount.objects.create(
            cloud=self.cloud,
            name="Test Vultr Account",
            access_token="some_token"
        )

        with self.assertRaises(ValidationError) as context:
            vultr_account.validate()
        
        self.assertIn("Vultr API validation failed", str(context.exception))

    @patch('requests.get')
    def test_connection_timeout(self, mock_get):
        mock_get.side_effect = Exception("Connection timeout")

        vultr_account = CoreVultrAccount.objects.create(
            cloud=self.cloud,
            name="Test Vultr Account",
            access_token="some_token"
        )

        with self.assertRaises(Exception):
            vultr_account.validate()

    @patch('requests.get')
    def test_malformed_response(self, mock_get):
        mock_response = Mock()
        mock_response.status_code = 400
        mock_response.json.return_value = {}  # No error field
        mock_get.return_value = mock_response

        vultr_account = CoreVultrAccount.objects.create(
            cloud=self.cloud,
            name="Test Vultr Account",
            access_token="bad_token"
        )

        with self.assertRaises(ValidationError) as context:
            vultr_account.validate()
        
        self.assertIn("Unknown error occurred", str(context.exception))

    def test_access_token_property(self):
        account_config = self.get_test_account('vultr', 'valid')
        
        vultr_account = CoreVultrAccount.objects.create(
            cloud=self.cloud,
            name=account_config['name'],
            access_token=account_config['access_token']
        )

        self.assertEqual(vultr_account.access_token, account_config['access_token'])

    @skip_if_no_real_credentials('vultr')
    def test_real_api_connection(self):
        """
        Integration test with real Vultr API (only runs if real credentials provided)
        """
        account_config = self.get_test_account('vultr', 'valid')
        
        vultr_account = CoreVultrAccount.objects.create(
            cloud=self.cloud,
            name=account_config['name'],
            access_token=account_config['access_token']
        )

        # This will make actual API call if real credentials are provided
        result = vultr_account.validate()
        self.assertTrue(result, "Real Vultr API validation should succeed with valid credentials")