from django.test import TestCase
from django.contrib.auth.models import User
from unittest.mock import patch, Mock
from apps.console.cloud.models import CoreCloud, CoreCloudServiceProvider
from apps.console.cloud.upcloud.models import CoreUpCloudAccount
from apps.console.account.models import CoreAccount
from tests.utils import CloudTestMixin, TestAccountManager, skip_if_no_real_credentials


class UpCloudConnectionTestCase(CloudTestMixin, TestCase):
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
            code="upcloud",
            defaults={
                "name": "UpCloud",
                "status": CoreCloudServiceProvider.Status.ACTIVE
            }
        )
        self.cloud = CoreCloud.objects.create(
            account=self.account,
            provider=self.provider,
            status=CoreCloud.Status.ACTIVE
        )

    @patch('requests.get')
    def test_valid_credentials_connection(self, mock_get):
        # Get test account configuration
        account_config = self.get_test_account('upcloud', 'valid')
        mock_get.return_value = self.create_mock_response('upcloud', 'account_success')

        upcloud_account = CoreUpCloudAccount.objects.create(
            cloud=self.cloud,
            name=account_config['name'],
            username=account_config['username'],
            password=account_config['password']
        )

        result = upcloud_account.validate()
        self.assertTrue(result)
        
        # Verify the correct API endpoint was called with proper headers
        mock_get.assert_called_once_with(
            'https://api.upcloud.com/1.3/account',
            headers={
                'Authorization': f'Basic {upcloud_account._get_auth_token()}',
                'Content-Type': 'application/json'
            },
            timeout=10
        )

    @patch('requests.get')
    def test_invalid_credentials_connection(self, mock_get):
        # Get invalid test account configuration
        account_config = self.get_test_account('upcloud', 'invalid')
        mock_get.return_value = self.create_mock_response('upcloud', 'account_unauthorized')

        upcloud_account = CoreUpCloudAccount.objects.create(
            cloud=self.cloud,
            name=account_config['name'],
            username=account_config['username'],
            password=account_config['password']
        )

        result = upcloud_account.validate()
        self.assertFalse(result)

    @patch('requests.get')
    def test_forbidden_access_connection(self, mock_get):
        mock_response = Mock()
        mock_response.status_code = 403
        mock_get.return_value = mock_response

        upcloud_account = CoreUpCloudAccount.objects.create(
            cloud=self.cloud,
            name="Test UpCloud Account",
            username="restricted_user",
            password="password"
        )

        result = upcloud_account.validate()
        self.assertFalse(result)

    @patch('requests.get')
    def test_rate_limited_connection(self, mock_get):
        mock_response = Mock()
        mock_response.status_code = 429
        mock_get.return_value = mock_response

        upcloud_account = CoreUpCloudAccount.objects.create(
            cloud=self.cloud,
            name="Test UpCloud Account",
            username="testuser",
            password="testpassword"
        )

        result = upcloud_account.validate()
        self.assertFalse(result)

    @patch('requests.get')
    def test_server_error_connection(self, mock_get):
        mock_response = Mock()
        mock_response.status_code = 500
        mock_get.return_value = mock_response

        upcloud_account = CoreUpCloudAccount.objects.create(
            cloud=self.cloud,
            name="Test UpCloud Account",
            username="testuser",
            password="testpassword"
        )

        result = upcloud_account.validate()
        self.assertFalse(result)

    @patch('requests.get')
    def test_connection_timeout(self, mock_get):
        mock_get.side_effect = Exception("Connection timeout")

        upcloud_account = CoreUpCloudAccount.objects.create(
            cloud=self.cloud,
            name="Test UpCloud Account",
            username="testuser",
            password="testpassword"
        )

        result = upcloud_account.validate()
        self.assertFalse(result)

    @patch('requests.get')
    def test_empty_credentials_connection(self, mock_get):
        mock_response = Mock()
        mock_response.status_code = 400
        mock_get.return_value = mock_response

        upcloud_account = CoreUpCloudAccount.objects.create(
            cloud=self.cloud,
            name="Test UpCloud Account",
            username="",
            password=""
        )

        result = upcloud_account.validate()
        self.assertFalse(result)

    def test_access_token_property(self):
        account_config = self.get_test_account('upcloud', 'valid')
        
        upcloud_account = CoreUpCloudAccount.objects.create(
            cloud=self.cloud,
            name=account_config['name'],
            username=account_config['username'],
            password=account_config['password']
        )

        expected_token = {
            'username': account_config['username'],
            'password': account_config['password']
        }
        
        self.assertEqual(upcloud_account.access_token, expected_token)

    def test_auth_token_generation(self):
        account_config = self.get_test_account('upcloud', 'valid')
        
        upcloud_account = CoreUpCloudAccount.objects.create(
            cloud=self.cloud,
            name=account_config['name'],
            username=account_config['username'],
            password=account_config['password']
        )

        auth_token = upcloud_account._get_auth_token()
        
        # Verify the token is properly base64 encoded
        import base64
        decoded = base64.b64decode(auth_token).decode()
        expected_decoded = f"{account_config['username']}:{account_config['password']}"
        self.assertEqual(decoded, expected_decoded)

    @skip_if_no_real_credentials('upcloud')
    def test_real_api_connection(self):
        """
        Integration test with real UpCloud API (only runs if real credentials provided)
        """
        account_config = self.get_test_account('upcloud', 'valid')
        
        upcloud_account = CoreUpCloudAccount.objects.create(
            cloud=self.cloud,
            name=account_config['name'],
            username=account_config['username'],
            password=account_config['password']
        )

        # This will make actual API call if real credentials are provided
        result = upcloud_account.validate()
        self.assertTrue(result, "Real UpCloud API validation should succeed with valid credentials")

    @patch('requests.get')
    def test_malformed_json_response(self, mock_get):
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.side_effect = ValueError("Invalid JSON")
        mock_get.return_value = mock_response

        upcloud_account = CoreUpCloudAccount.objects.create(
            cloud=self.cloud,
            name="Test UpCloud Account",
            username="testuser",
            password="testpassword"
        )

        result = upcloud_account.validate()
        self.assertFalse(result)

    @patch('requests.get')
    def test_account_suspended_connection(self, mock_get):
        mock_response = Mock()
        mock_response.status_code = 402  # Payment required - common for suspended accounts
        mock_get.return_value = mock_response

        upcloud_account = CoreUpCloudAccount.objects.create(
            cloud=self.cloud,
            name="Test UpCloud Account",
            username="suspended_user",
            password="password"
        )

        result = upcloud_account.validate()
        self.assertFalse(result)