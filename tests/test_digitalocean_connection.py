from django.test import TestCase
from django.contrib.auth.models import User
from unittest.mock import patch, Mock
from apps.console.cloud.models import (
    CloudInventoryTransientError,
    CloudValidationTransientError,
    CoreCloud,
    CoreCloudServiceProvider,
)
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

        self.assertFalse(do_account.validate())

    @patch('requests.get')
    def test_connection_timeout(self, mock_get):
        mock_get.side_effect = Exception("Connection timeout")

        do_account = CoreDigitalOceanAccount.objects.create(
            cloud=self.cloud,
            name="Test DO Account",
            access_token="some_token"
        )

        with self.assertRaises(CloudValidationTransientError):
            do_account.validate()

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

        with self.assertRaises(CloudValidationTransientError):
            do_account.validate()

    @patch('apps.console.cloud.digitalocean.models.requests.get')
    def test_pagination_accepts_empty_links_when_meta_total_is_reached(self, mock_get):
        do_account = CoreDigitalOceanAccount.objects.create(
            cloud=self.cloud,
            name="Test DO Account",
            access_token="valid_token",
        )
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            'droplets': [{'id': 123, 'name': 'test-droplet'}],
            'links': {},
            'meta': {'total': 1},
        }
        mock_get.return_value = response

        self.assertEqual(
            do_account._paginate_api_call('droplets'),
            [{'id': 123, 'name': 'test-droplet'}],
        )
        mock_get.assert_called_once_with(
            'https://api.digitalocean.com/v2/droplets',
            headers={'Authorization': 'Bearer valid_token'},
            timeout=15,
        )

    @patch('apps.console.cloud.digitalocean.models.requests.get')
    def test_pagination_rejects_empty_links_when_meta_total_is_incomplete(self, mock_get):
        do_account = CoreDigitalOceanAccount.objects.create(
            cloud=self.cloud,
            name="Test DO Account",
            access_token="valid_token",
        )
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            'droplets': [{'id': 123, 'name': 'test-droplet'}],
            'links': {},
            'meta': {'total': 2},
        }
        mock_get.return_value = response

        with self.assertRaises(CloudInventoryTransientError):
            do_account._paginate_api_call('droplets')

    @patch('apps.console.cloud.digitalocean.models.requests.get')
    def test_unpaginated_nested_collection_can_explicitly_omit_meta(self, mock_get):
        do_account = CoreDigitalOceanAccount.objects.create(
            cloud=self.cloud,
            name="Test DO Account",
            access_token="valid_token",
        )
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            'node_pools': [{'id': 'pool-1', 'name': 'default'}],
        }
        mock_get.return_value = response

        self.assertEqual(
            do_account._paginate_api_call(
                'kubernetes/clusters/cluster-1/node_pools',
                collection_key='node_pools',
                allow_unpaginated=True,
            ),
            [{'id': 'pool-1', 'name': 'default'}],
        )

    @patch('apps.console.cloud.digitalocean.models.requests.get')
    def test_nat_gateway_collection_accepts_provider_null_when_total_is_zero(self, mock_get):
        do_account = CoreDigitalOceanAccount.objects.create(
            cloud=self.cloud,
            name="Test DO Account",
            access_token="valid_token",
        )
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            'vpc_nat_gateways': None,
            'links': {},
            'meta': {'total': 0},
        }
        mock_get.return_value = response

        self.assertEqual(
            do_account._paginate_api_call(
                'vpc_nat_gateways',
                collection_key='vpc_nat_gateways',
                allow_null_empty=True,
            ),
            [],
        )

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
