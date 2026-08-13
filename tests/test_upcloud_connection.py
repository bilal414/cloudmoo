from django.test import TestCase
from django.contrib.auth.models import User
from unittest.mock import patch, Mock
from apps.console.cloud.models import CloudValidationTransientError, CoreCloud, CoreCloudServiceProvider
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
        expected_headers = upcloud_account._auth_headers()
        mock_get.assert_called_once_with(
            'https://api.upcloud.com/1.3/account',
            headers=expected_headers,
            timeout=10
        )
        self.assertTrue(expected_headers['Authorization'].startswith('Basic '))

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

        self.assertFalse(upcloud_account.validate())

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

        self.assertFalse(upcloud_account.validate())

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

        with self.assertRaises(CloudValidationTransientError):
            upcloud_account.validate()

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

        with self.assertRaises(CloudValidationTransientError):
            upcloud_account.validate()

    @patch('requests.get')
    def test_connection_timeout(self, mock_get):
        mock_get.side_effect = Exception("Connection timeout")

        upcloud_account = CoreUpCloudAccount.objects.create(
            cloud=self.cloud,
            name="Test UpCloud Account",
            username="testuser",
            password="testpassword"
        )

        with self.assertRaises(CloudValidationTransientError):
            upcloud_account.validate()

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

        self.assertFalse(upcloud_account.validate())

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
            'password': account_config['password'],
            'api_token': '',
        }
        
        self.assertEqual(upcloud_account.access_token, expected_token)

    def test_auth_headers_basic(self):
        account_config = self.get_test_account('upcloud', 'valid')
        
        upcloud_account = CoreUpCloudAccount.objects.create(
            cloud=self.cloud,
            name=account_config['name'],
            username=account_config['username'],
            password=account_config['password']
        )

        headers = upcloud_account._auth_headers()

        # Verify the Basic credential is properly base64 encoded
        import base64
        decoded = base64.b64decode(headers['Authorization'].removeprefix('Basic ')).decode()
        expected_decoded = f"{account_config['username']}:{account_config['password']}"
        self.assertEqual(decoded, expected_decoded)

    def test_auth_headers_bearer_with_api_token(self):
        upcloud_account = CoreUpCloudAccount.objects.create(
            cloud=self.cloud,
            name='Token Account',
            api_token='ucat_test-token-123'
        )

        headers = upcloud_account._auth_headers()

        self.assertEqual(headers['Authorization'], 'Bearer ucat_test-token-123')
        self.assertEqual(
            upcloud_account.access_token,
            {'username': '', 'password': '', 'api_token': 'ucat_test-token-123'},
        )

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

        with self.assertRaises(CloudValidationTransientError):
            upcloud_account.validate()

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


class UpCloudCheckAuthTestCase(TestCase):
    """Status checks authenticate with Bearer when an API token is present."""

    @patch('apps.monitoring.checks.upcloud.requests.get')
    def test_server_check_uses_bearer_for_api_token(self, mock_get):
        from apps.monitoring.checks.upcloud import check_upcloud_server_status

        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {'server': {'state': 'started'}}
        mock_get.return_value = response

        status, _metadata = check_upcloud_server_status(
            'uuid-1',
            {'username': '', 'password': '', 'api_token': 'ucat_check-token'},
        )

        self.assertEqual(status, 'started')
        self.assertEqual(
            mock_get.call_args.kwargs['headers']['Authorization'],
            'Bearer ucat_check-token',
        )

    @patch('apps.monitoring.checks.upcloud.requests.get')
    def test_volume_check_uses_basic_without_api_token(self, mock_get):
        from apps.monitoring.checks.upcloud import check_upcloud_volume_status

        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {'storage': {'state': 'online'}}
        mock_get.return_value = response

        status, _metadata = check_upcloud_volume_status(
            'uuid-2',
            {'username': 'user', 'password': 'pass', 'api_token': ''},
        )

        self.assertEqual(status, 'online')
        self.assertTrue(
            mock_get.call_args.kwargs['headers']['Authorization'].startswith('Basic ')
        )
