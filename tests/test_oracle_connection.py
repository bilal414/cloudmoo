from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.test import TestCase
from oci.exceptions import ServiceError

from apps.console.account.models import CoreAccount, CoreAccountMembership
from apps.console.cloud.models import (
    CloudValidationTransientError,
    CoreCloud,
    CoreCloudServiceProvider,
)
from apps.console.cloud.oracle.models import (
    CoreOracleAccount,
    CoreOracleInstance,
    CoreOracleVolume,
)
from apps.console.member.models import CoreMember
from apps.console.utils.models import UtilAsset
from apps.monitoring.checks import get_check_function
from apps.monitoring.checks.oracle import (
    check_oracle_server_status,
    check_oracle_volume_status,
)

TENANCY_OCID = 'ocid1.tenancy.oc1..aaaaaaaaiz4tki3iisfoeymnll2fvyu7utf7ewscnp47eikyfsiempaiesda'
USER_OCID = 'ocid1.user.oc1..aaaaaaaayp6wtb6p3idufiefbhvk4hrfxlzwjm6dcmcakzssznlbwohfl7rq'
FINGERPRINT = '75:84:a3:1f:fc:9f:27:45:dd:73:03:81:09:77:36:26'
REGION = 'us-ashburn-1'
PRIVATE_KEY = '-----BEGIN RSA PRIVATE KEY-----\ntest-key\n-----END RSA PRIVATE KEY-----'

CREDENTIALS = {
    'tenancy_ocid': TENANCY_OCID,
    'user_ocid': USER_OCID,
    'fingerprint': FINGERPRINT,
    'region': REGION,
    'private_key': PRIVATE_KEY,
}


def make_service_error(status, code='Error', message='error'):
    return ServiceError(status=status, code=code, headers={}, message=message)


class OracleAccountFixtureTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='oracle-user',
            email='oracle@example.com',
            password='testpass123',
        )
        self.account = CoreAccount.objects.create(
            name='Oracle Test Account',
            status=CoreAccount.Status.ACTIVE,
            owner=self.user,
        )
        member = CoreMember.objects.create(user=self.user, active_account=self.account)
        CoreAccountMembership.objects.create(
            account=self.account,
            member=member,
            role=CoreAccountMembership.Role.OWNER,
        )
        self.provider, _ = CoreCloudServiceProvider.objects.get_or_create(
            code='oracle',
            defaults={
                'name': 'Oracle Cloud',
                'status': CoreCloudServiceProvider.Status.ACTIVE,
            },
        )
        self.cloud = CoreCloud.objects.create(
            account=self.account,
            provider=self.provider,
            status=CoreCloud.Status.ACTIVE,
        )
        self.oracle_account = CoreOracleAccount.objects.create(
            cloud=self.cloud,
            name='Oracle Fixture Account',
            tenancy_ocid=TENANCY_OCID,
            user_ocid=USER_OCID,
            fingerprint=FINGERPRINT,
            region=REGION,
            private_key=PRIVATE_KEY,
        )

    def test_access_token_property(self):
        self.assertEqual(self.oracle_account.access_token, CREDENTIALS)

    def test_oci_config_keys(self):
        config = self.oracle_account._oci_config()
        self.assertEqual(
            config,
            {
                'tenancy': TENANCY_OCID,
                'user': USER_OCID,
                'fingerprint': FINGERPRINT,
                'region': REGION,
                'key_content': PRIVATE_KEY,
            },
        )

    @patch('apps.console.cloud.oracle.models.oci')
    def test_validate_success(self, mock_oci):
        identity_client = mock_oci.identity.IdentityClient.return_value

        result = self.oracle_account.validate()

        self.assertTrue(result)
        mock_oci.identity.IdentityClient.assert_called_once_with(self.oracle_account._oci_config())
        identity_client.get_tenancy.assert_called_once_with(TENANCY_OCID)

    @patch('apps.console.cloud.oracle.models.oci')
    def test_validate_invalid_credentials_returns_false(self, mock_oci):
        identity_client = mock_oci.identity.IdentityClient.return_value
        identity_client.get_tenancy.side_effect = make_service_error(401, 'NotAuthenticated', 'bad key')

        self.assertFalse(self.oracle_account.validate())

    @patch('apps.console.cloud.oracle.models.oci')
    def test_validate_forbidden_returns_false(self, mock_oci):
        identity_client = mock_oci.identity.IdentityClient.return_value
        identity_client.get_tenancy.side_effect = make_service_error(403, 'NotAuthorized', 'denied')

        self.assertFalse(self.oracle_account.validate())

    @patch('apps.console.cloud.oracle.models.oci')
    def test_validate_server_error_raises_transient(self, mock_oci):
        identity_client = mock_oci.identity.IdentityClient.return_value
        identity_client.get_tenancy.side_effect = make_service_error(500, 'InternalError', 'boom')

        with self.assertRaises(CloudValidationTransientError):
            self.oracle_account.validate()

    @patch('apps.console.cloud.oracle.models.oci')
    def test_validate_unexpected_error_raises_transient(self, mock_oci):
        mock_oci.identity.IdentityClient.side_effect = Exception('connection failed')

        with self.assertRaises(CloudValidationTransientError):
            self.oracle_account.validate()

    @patch('apps.console.cloud.oracle.models.oci')
    def test_sync_assets_creates_instances_and_volumes(self, mock_oci):
        compute_client = mock_oci.core.ComputeClient.return_value
        blockstorage_client = mock_oci.core.BlockstorageClient.return_value

        instance_payloads = [
            {
                'id': 'ocid1.instance.oc1.iad.instance1',
                'display_name': 'web-server',
                'lifecycle_state': 'RUNNING',
            },
            {
                'id': 'ocid1.instance.oc1.iad.instance2',
                'display_name': 'worker',
                'lifecycle_state': 'STOPPED',
            },
        ]
        volume_payloads = [
            {
                'id': 'ocid1.volume.oc1.iad.volume1',
                'display_name': 'data-volume',
                'lifecycle_state': 'AVAILABLE',
            },
        ]

        def fake_list_all(list_func, **kwargs):
            if list_func is compute_client.list_instances:
                self.assertEqual(kwargs, {'compartment_id': TENANCY_OCID})
                return Mock(data=list(instance_payloads))
            if list_func is blockstorage_client.list_volumes:
                self.assertEqual(kwargs, {'compartment_id': TENANCY_OCID})
                return Mock(data=list(volume_payloads))
            raise AssertionError(f'unexpected list call: {list_func}')

        mock_oci.pagination.list_call_get_all_results.side_effect = fake_list_all
        mock_oci.util.to_dict.side_effect = lambda value: value

        # A stale asset that the provider no longer returns must be marked as
        # gone instead of deleted or silently kept active.
        stale_instance = CoreOracleInstance.objects.create(
            owner=self.oracle_account,
            unique_id='ocid1.instance.oc1.iad.gone',
            name='gone',
            type=UtilAsset.Type.SERVER,
            metadata={},
        )

        self.oracle_account.sync_assets()
        self.oracle_account.refresh_from_db()
        self.assertIsNotNone(self.oracle_account.last_synced)

        instances = {
            instance.unique_id: instance
            for instance in CoreOracleInstance.objects.filter(owner=self.oracle_account)
        }
        self.assertEqual(instances['ocid1.instance.oc1.iad.instance1'].name, 'web-server')
        self.assertEqual(instances['ocid1.instance.oc1.iad.instance1'].type, UtilAsset.Type.SERVER)
        self.assertEqual(
            instances['ocid1.instance.oc1.iad.instance1'].metadata['lifecycle_state'],
            'RUNNING',
        )
        self.assertEqual(instances['ocid1.instance.oc1.iad.instance2'].name, 'worker')

        volumes = CoreOracleVolume.objects.filter(owner=self.oracle_account)
        self.assertEqual(volumes.count(), 1)
        self.assertEqual(volumes.first().name, 'data-volume')
        self.assertEqual(volumes.first().type, UtilAsset.Type.VOLUME)

        stale_instance.refresh_from_db()
        self.assertEqual(stale_instance.monitoring, UtilAsset.Monitoring.NO_LONGER_EXISTS)

    def test_asset_provider_urls(self):
        instance = CoreOracleInstance.objects.create(
            owner=self.oracle_account,
            unique_id='ocid1.instance.oc1.iad.instance1',
            name='web-server',
            type=UtilAsset.Type.SERVER,
            metadata={},
        )
        volume = CoreOracleVolume.objects.create(
            owner=self.oracle_account,
            unique_id='ocid1.volume.oc1.iad.volume1',
            name='data-volume',
            type=UtilAsset.Type.VOLUME,
            metadata={},
        )

        self.assertEqual(
            instance.provider_url,
            f'https://cloud.oracle.com/compute/instances/{instance.unique_id}?region={REGION}',
        )
        self.assertEqual(
            volume.provider_url,
            f'https://cloud.oracle.com/block-storage/volumes/{volume.unique_id}?region={REGION}',
        )


class OracleCheckTestCase(TestCase):
    def test_check_function_resolution(self):
        self.assertIs(get_check_function('oracle', 'server'), check_oracle_server_status)
        self.assertIs(get_check_function('oracle', 'volume'), check_oracle_volume_status)

    @patch('apps.monitoring.checks.oracle.oci')
    def test_check_oracle_server_status(self, mock_oci):
        compute_client = mock_oci.core.ComputeClient.return_value
        instance = Mock()
        instance.lifecycle_state = 'RUNNING'
        compute_client.get_instance.return_value = Mock(data=instance)
        mock_oci.util.to_dict.return_value = {'id': 'instance-1', 'lifecycle_state': 'RUNNING'}

        status, metadata = check_oracle_server_status('ocid1.instance.oc1.iad.instance1', CREDENTIALS)

        self.assertEqual(status, 'RUNNING')
        self.assertEqual(metadata, {'server': {'id': 'instance-1', 'lifecycle_state': 'RUNNING'}})
        mock_oci.core.ComputeClient.assert_called_once_with({
            'tenancy': TENANCY_OCID,
            'user': USER_OCID,
            'fingerprint': FINGERPRINT,
            'region': REGION,
            'key_content': PRIVATE_KEY,
        })
        compute_client.get_instance.assert_called_once_with('ocid1.instance.oc1.iad.instance1')

    @patch('apps.monitoring.checks.oracle.oci')
    def test_check_oracle_server_status_not_found(self, mock_oci):
        compute_client = mock_oci.core.ComputeClient.return_value
        compute_client.get_instance.side_effect = make_service_error(404, 'NotFound', 'missing')

        status, _message = check_oracle_server_status('ocid1.instance.oc1.iad.gone', CREDENTIALS)

        self.assertEqual(status, 'not_found')

    @patch('apps.monitoring.checks.oracle.oci')
    def test_check_oracle_server_status_invalid_token(self, mock_oci):
        compute_client = mock_oci.core.ComputeClient.return_value
        compute_client.get_instance.side_effect = make_service_error(401, 'NotAuthenticated', 'bad key')

        status, _message = check_oracle_server_status('ocid1.instance.oc1.iad.instance1', CREDENTIALS)

        self.assertEqual(status, 'invalid_access_token')

    @patch('apps.monitoring.checks.oracle.oci')
    def test_check_oracle_server_status_forbidden(self, mock_oci):
        compute_client = mock_oci.core.ComputeClient.return_value
        compute_client.get_instance.side_effect = make_service_error(403, 'NotAuthorized', 'denied')

        status, _message = check_oracle_server_status('ocid1.instance.oc1.iad.instance1', CREDENTIALS)

        self.assertEqual(status, 'invalid_access_token')

    @patch('apps.monitoring.checks.oracle.oci')
    def test_check_oracle_server_status_unexpected_error(self, mock_oci):
        mock_oci.core.ComputeClient.side_effect = Exception('network down')

        status, _message = check_oracle_server_status('ocid1.instance.oc1.iad.instance1', CREDENTIALS)

        self.assertEqual(status, 'error')

    @patch('apps.monitoring.checks.oracle.oci')
    def test_check_oracle_volume_status(self, mock_oci):
        blockstorage_client = mock_oci.core.BlockstorageClient.return_value
        volume = Mock()
        volume.lifecycle_state = 'AVAILABLE'
        blockstorage_client.get_volume.return_value = Mock(data=volume)
        mock_oci.util.to_dict.return_value = {'id': 'volume-1', 'lifecycle_state': 'AVAILABLE'}

        status, metadata = check_oracle_volume_status('ocid1.volume.oc1.iad.volume1', CREDENTIALS)

        self.assertEqual(status, 'AVAILABLE')
        self.assertEqual(metadata, {'volume': {'id': 'volume-1', 'lifecycle_state': 'AVAILABLE'}})
        mock_oci.core.BlockstorageClient.assert_called_once_with({
            'tenancy': TENANCY_OCID,
            'user': USER_OCID,
            'fingerprint': FINGERPRINT,
            'region': REGION,
            'key_content': PRIVATE_KEY,
        })
        blockstorage_client.get_volume.assert_called_once_with('ocid1.volume.oc1.iad.volume1')

    @patch('apps.monitoring.checks.oracle.oci')
    def test_check_oracle_volume_status_not_found(self, mock_oci):
        blockstorage_client = mock_oci.core.BlockstorageClient.return_value
        blockstorage_client.get_volume.side_effect = make_service_error(404, 'NotFound', 'missing')

        status, _message = check_oracle_volume_status('ocid1.volume.oc1.iad.gone', CREDENTIALS)

        self.assertEqual(status, 'not_found')

    @patch('apps.monitoring.checks.oracle.oci')
    def test_check_oracle_volume_status_invalid_token(self, mock_oci):
        blockstorage_client = mock_oci.core.BlockstorageClient.return_value
        blockstorage_client.get_volume.side_effect = make_service_error(403, 'NotAuthorized', 'denied')

        status, _message = check_oracle_volume_status('ocid1.volume.oc1.iad.volume1', CREDENTIALS)

        self.assertEqual(status, 'invalid_access_token')
