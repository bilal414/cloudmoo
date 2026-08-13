from datetime import datetime, timezone as dt_timezone
from unittest.mock import Mock, patch

import requests
from botocore.exceptions import ClientError
from django.contrib.auth.models import User
from django.test import TestCase

from apps.console.account.models import CoreAccount, CoreAccountMembership
from apps.console.cloud.digitalocean.models import (
    CoreDigitalOceanAccount,
    CoreDigitalOceanApp,
    CoreDigitalOceanBackup,
    CoreDigitalOceanCDNEndpoint,
    CoreDigitalOceanCertificate,
    CoreDigitalOceanContainerRegistry,
    CoreDigitalOceanDatabase,
    CoreDigitalOceanDNSRecord,
    CoreDigitalOceanDomain,
    CoreDigitalOceanFirewall,
    CoreDigitalOceanKubernetesCluster,
    CoreDigitalOceanKubernetesNodePool,
    CoreDigitalOceanLoadBalancer,
    CoreDigitalOceanReservedIP,
    CoreDigitalOceanSnapshot,
    CoreDigitalOceanSpace,
    CoreDigitalOceanVPC,
    CoreDigitalOceanVPCNATGateway,
    CoreDigitalOceanVPCPeering,
    CoreDigitalOceanVolume,
)
from apps.console.cloud.models import CoreCloud, CoreCloudServiceProvider
from apps.console.member.models import CoreMember
from apps.console.utils.models import UtilAsset
from apps.monitoring.checks import get_check_function
from apps.monitoring.checks.digitalocean import (
    check_digitalocean_app_platform_status,
    check_digitalocean_backup_status,
    check_digitalocean_container_registry_status,
    check_digitalocean_cdn_endpoint_status,
    check_digitalocean_certificate_status,
    check_digitalocean_database_status,
    check_digitalocean_dns_record_status,
    check_digitalocean_domain_status,
    check_digitalocean_firewall_status,
    check_digitalocean_kubernetes_cluster_status,
    check_digitalocean_kubernetes_node_pool_status,
    check_digitalocean_load_balancer_status,
    check_digitalocean_nat_gateway_status,
    check_digitalocean_object_storage_status,
    check_digitalocean_reserved_ip_status,
    check_digitalocean_snapshot_status,
    check_digitalocean_vpc_peering_status,
    check_digitalocean_vpc_status,
)
from apps.monitoring.tasks import run_status_check
from apps.console.cloud.models import CloudInventoryTransientError


class DigitalOceanResourceFixtureTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='resource-user',
            email='resource@example.com',
            password='testpass123',
        )
        self.account = CoreAccount.objects.create(
            name='Resource Test Account',
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
            code='digitalocean',
            defaults={
                'name': 'DigitalOcean',
                'status': CoreCloudServiceProvider.Status.ACTIVE,
            },
        )
        self.cloud = CoreCloud.objects.create(
            account=self.account,
            provider=self.provider,
            status=CoreCloud.Status.ACTIVE,
        )
        self.do_account = CoreDigitalOceanAccount.objects.create(
            cloud=self.cloud,
            name='Resource Test DO Account',
            access_token='test-token',
        )

    @patch('apps.monitoring.schedules.asset_schedule_create')
    def test_syncs_all_requested_control_plane_resources(self, _schedule_create):
        inventory = {
            'databases': [{'id': 'db-1', 'name': 'postgres', 'status': 'online'}],
            'volumes': [{'id': 'vol-1', 'name': 'data-volume', 'droplet_ids': []}],
            'snapshots': [{'id': 'snap-1', 'name': 'nightly', 'regions': ['nyc3']}],
            'reserved_ips': [{'ip': '203.0.113.10', 'region': {'slug': 'nyc3'}}],
            'reserved_ipv6': [{'ip': '2001:db8::1', 'region_slug': 'nyc3'}],
            'firewalls': [{'id': 'fw-1', 'name': 'web-firewall', 'status': 'succeeded'}],
            'load_balancers': [{'id': 'lb-1', 'name': 'web-lb', 'status': 'active'}],
            'apps': [{'id': 'app-1', 'spec': {'name': 'web-app', 'region': 'nyc'}}],
            'kubernetes/clusters': [{
                'id': 'cluster-1',
                'name': 'production',
                'status': {'state': 'running'},
                'region': {'slug': 'nyc3'},
            }],
            'kubernetes/clusters/cluster-1/node_pools': [{
                'id': 'pool-1',
                'name': 'default-pool',
                'size': 's-2vcpu-4gb',
                'count': 2,
                'nodes': [],
            }],
            'vpcs': [{'id': 'vpc-1', 'name': 'private-network', 'ip_range': '10.10.0.0/16'}],
            'vpc_peerings': [{'id': 'peer-1', 'name': 'shared-network', 'status': 'ACTIVE'}],
            'vpc_nat_gateways': [{'id': 'nat-1', 'name': 'egress', 'state': 'ACTIVE'}],
            'domains': [{'name': 'example.com', 'ttl': 1800}],
            'domains/example.com/records': [{
                'id': 1,
                'type': 'A',
                'name': '@',
                'data': '203.0.113.10',
                'ttl': 3600,
            }],
            'cdn/endpoints': [{
                'id': 'cdn-1',
                'origin': 'test-space.nyc3.digitaloceanspaces.com',
                'endpoint': 'cdn.example.com',
            }],
            'certificates': [{
                'id': 'cert-1',
                'name': 'example.com',
                'state': 'verified',
                'type': 'lets_encrypt',
            }],
        }

        def fake_paginate(endpoint, collection_key=None, **kwargs):
            if endpoint.endswith('/node_pools'):
                self.assertTrue(kwargs.get('allow_unpaginated'))
            if endpoint == 'vpc_nat_gateways':
                self.assertTrue(kwargs.get('allow_null_empty'))
            return inventory[endpoint]

        with patch.object(self.do_account, '_paginate_api_call', side_effect=fake_paginate), \
                patch.object(
                    self.do_account,
                    '_make_api_call',
                    return_value={'registry': {'name': 'registry-1', 'region': 'nyc3'}},
                ):
            self.do_account.sync_databases()
            self.do_account.sync_volumes()
            self.do_account.sync_snapshots()
            self.do_account.sync_reserved_ips()
            self.do_account.sync_firewalls()
            self.do_account.sync_load_balancers()
            self.do_account.sync_apps()
            self.do_account.sync_container_registries()
            self.do_account.sync_kubernetes()
            self.do_account.sync_vpcs()
            self.do_account.sync_vpc_peerings()
            self.do_account.sync_vpc_nat_gateways()
            self.do_account.sync_domains()
            self.do_account.sync_cdn_endpoints()
            self.do_account.sync_certificates()

        self.assertEqual(CoreDigitalOceanDatabase.objects.get().unique_id, 'db-1')
        self.assertEqual(CoreDigitalOceanVolume.objects.get().unique_id, 'vol-1')
        self.assertEqual(CoreDigitalOceanSnapshot.objects.get().unique_id, 'snap-1')
        self.assertEqual(
            set(CoreDigitalOceanReservedIP.objects.values_list('unique_id', flat=True)),
            {'203.0.113.10', '2001:db8::1'},
        )
        self.assertEqual(CoreDigitalOceanFirewall.objects.get().unique_id, 'fw-1')
        self.assertEqual(CoreDigitalOceanLoadBalancer.objects.get().unique_id, 'lb-1')
        self.assertEqual(CoreDigitalOceanApp.objects.get().name, 'web-app')
        self.assertEqual(CoreDigitalOceanContainerRegistry.objects.get().unique_id, 'registry-1')
        self.assertEqual(CoreDigitalOceanKubernetesCluster.objects.get().unique_id, 'cluster-1')
        self.assertEqual(CoreDigitalOceanKubernetesNodePool.objects.get().unique_id, 'pool-1')
        self.assertEqual(
            CoreDigitalOceanKubernetesNodePool.objects.get().metadata['cluster_id'],
            'cluster-1',
        )
        self.assertEqual(CoreDigitalOceanVPC.objects.get().unique_id, 'vpc-1')
        self.assertEqual(CoreDigitalOceanVPCPeering.objects.get().unique_id, 'peer-1')
        self.assertEqual(CoreDigitalOceanVPCNATGateway.objects.get().unique_id, 'nat-1')
        self.assertEqual(CoreDigitalOceanDomain.objects.get().unique_id, 'example.com')
        self.assertEqual(CoreDigitalOceanDNSRecord.objects.get().unique_id, 'example.com:1')
        self.assertEqual(CoreDigitalOceanCDNEndpoint.objects.get().unique_id, 'cdn-1')
        self.assertEqual(CoreDigitalOceanCertificate.objects.get().unique_id, 'cert-1')

    @patch('apps.monitoring.schedules.asset_schedule_create')
    def test_syncs_droplet_backups_and_marks_missing_backups_gone(self, _schedule_create):
        old_backup = CoreDigitalOceanBackup.objects.create(
            owner=self.do_account,
            unique_id='old-backup',
            name='old-backup',
            type=UtilAsset.Type.BACKUP,
            monitoring=UtilAsset.Monitoring.ACTIVE,
        )

        def fake_paginate(endpoint, collection_key=None, **kwargs):
            self.assertEqual(endpoint, 'droplets/123/backups')
            self.assertEqual(collection_key, 'backups')
            return [{'id': 'backup-1', 'created_at': '2026-08-02T00:00:00Z'}]

        with patch.object(self.do_account, '_paginate_api_call', side_effect=fake_paginate):
            self.do_account.sync_backups([{'id': 123, 'backup_ids': ['backup-1']}])

        backup = CoreDigitalOceanBackup.objects.get(unique_id='backup-1')
        self.assertEqual(backup.metadata['droplet_id'], 123)
        self.assertEqual(backup.monitoring, UtilAsset.Monitoring.ACTIVE)
        old_backup.refresh_from_db()
        self.assertEqual(old_backup.monitoring, UtilAsset.Monitoring.NO_LONGER_EXISTS)

    @patch('apps.monitoring.schedules.asset_schedule_create')
    def test_spaces_use_separate_s3_credentials(self, _schedule_create):
        self.do_account.spaces_access_key = 'spaces-key'
        self.do_account.spaces_secret_key = 'spaces-secret'
        self.do_account.spaces_region = 'nyc3'
        self.do_account.save()

        s3_client = Mock()
        s3_client.list_buckets.return_value = {
            'Buckets': [{
                'Name': 'test-space',
                'CreationDate': datetime(2026, 8, 2, tzinfo=dt_timezone.utc),
            }],
        }
        with patch('apps.console.cloud.digitalocean.models.boto3.client', return_value=s3_client) as client:
            self.do_account.sync_spaces()

        space = CoreDigitalOceanSpace.objects.get()
        self.assertEqual(space.unique_id, 'test-space')
        self.assertEqual(space.metadata['CreationDate'], '2026-08-02T00:00:00+00:00')
        self.assertEqual(space.metadata['region'], 'nyc3')
        self.assertEqual(
            client.call_args.kwargs['endpoint_url'],
            'https://nyc3.digitaloceanspaces.com',
        )
        self.assertEqual(client.call_args.kwargs['aws_access_key_id'], 'spaces-key')
        self.assertEqual(client.call_args.kwargs['aws_secret_access_key'], 'spaces-secret')

    @patch('apps.monitoring.schedules.asset_schedule_create')
    def test_spaces_without_credentials_disable_existing_checks(self, _schedule_create):
        space = CoreDigitalOceanSpace.objects.create(
            owner=self.do_account,
            unique_id='existing-space',
            name='existing-space',
            type=UtilAsset.Type.OBJECT_STORAGE,
            monitoring=UtilAsset.Monitoring.ACTIVE,
        )

        with patch('apps.console.cloud.digitalocean.models.boto3.client') as client:
            self.do_account.sync_spaces()

        space.refresh_from_db()
        self.assertEqual(space.monitoring, UtilAsset.Monitoring.DISABLED)
        client.assert_not_called()

    def test_missing_backup_collection_fails_closed(self):
        with self.assertRaises(CloudInventoryTransientError):
            self.do_account.sync_backups([{'id': 123}])

    @patch('apps.monitoring.schedules.asset_schedule_create')
    def test_sync_assets_orchestrates_every_resource_family(self, _schedule_create):
        with patch.object(self.do_account, '_paginate_api_call', return_value=[]), \
                patch.object(self.do_account, 'sync_servers') as sync_servers, \
                patch.object(self.do_account, 'sync_backups') as sync_backups, \
                patch.object(self.do_account, 'sync_databases') as sync_databases, \
                patch.object(self.do_account, 'sync_volumes') as sync_volumes, \
                patch.object(self.do_account, 'sync_snapshots') as sync_snapshots, \
                patch.object(self.do_account, 'sync_reserved_ips') as sync_reserved_ips, \
                patch.object(self.do_account, 'sync_firewalls') as sync_firewalls, \
                patch.object(self.do_account, 'sync_load_balancers') as sync_load_balancers, \
                patch.object(self.do_account, 'sync_apps') as sync_apps, \
                patch.object(self.do_account, 'sync_container_registries') as sync_registries, \
                patch.object(self.do_account, 'sync_spaces') as sync_spaces, \
                patch.object(self.do_account, 'sync_kubernetes') as sync_kubernetes, \
                patch.object(self.do_account, 'sync_vpcs') as sync_vpcs, \
                patch.object(self.do_account, 'sync_vpc_peerings') as sync_vpc_peerings, \
                patch.object(self.do_account, 'sync_vpc_nat_gateways') as sync_vpc_nat_gateways, \
                patch.object(self.do_account, 'sync_domains') as sync_domains, \
                patch.object(self.do_account, 'sync_cdn_endpoints') as sync_cdn_endpoints, \
                patch.object(self.do_account, 'sync_certificates') as sync_certificates:
            self.do_account.sync_assets()

        sync_servers.assert_called_once_with([])
        sync_backups.assert_called_once_with([])
        sync_databases.assert_called_once_with()
        sync_volumes.assert_called_once_with()
        sync_snapshots.assert_called_once_with()
        sync_reserved_ips.assert_called_once_with()
        sync_firewalls.assert_called_once_with()
        sync_load_balancers.assert_called_once_with()
        sync_apps.assert_called_once_with()
        sync_registries.assert_called_once_with()
        sync_spaces.assert_called_once_with()
        sync_kubernetes.assert_called_once_with()
        sync_vpcs.assert_called_once_with()
        sync_vpc_peerings.assert_called_once_with()
        sync_vpc_nat_gateways.assert_called_once_with()
        sync_domains.assert_called_once_with()
        sync_cdn_endpoints.assert_called_once_with()
        sync_certificates.assert_called_once_with()
        self.do_account.refresh_from_db()
        self.assertIsNotNone(self.do_account.last_synced)

    @patch('apps.monitoring.schedules.asset_schedule_create')
    @patch('apps.monitoring.tasks.get_check_function')
    def test_status_task_routes_spaces_credentials_to_provider_check(
        self,
        get_check_function,
        _schedule_create,
    ):
        self.do_account.spaces_access_key = 'spaces-key'
        self.do_account.spaces_secret_key = 'spaces-secret'
        self.do_account.spaces_region = 'nyc3'
        self.do_account.save()
        space = CoreDigitalOceanSpace.objects.create(
            owner=self.do_account,
            unique_id='test-space',
            name='test-space',
            type=UtilAsset.Type.OBJECT_STORAGE,
            monitoring=UtilAsset.Monitoring.ACTIVE,
        )
        check_status = Mock(return_value=(
            'available',
            {'bucket': {'name': 'test-space', 'region': 'nyc3'}},
        ))
        get_check_function.return_value = check_status

        result = run_status_check(space)

        self.assertEqual(result['status'], 'available')
        check_status.assert_called_once_with(
            'test-space',
            {
                'access_key': 'spaces-key',
                'secret_key': 'spaces-secret',
                'region': 'nyc3',
            },
        )

    @patch('apps.monitoring.schedules.asset_schedule_create')
    @patch('apps.monitoring.tasks.get_check_function')
    def test_status_task_routes_nested_resource_context_to_provider_check(
        self,
        get_check_function,
        _schedule_create,
    ):
        node_pool = CoreDigitalOceanKubernetesNodePool.objects.create(
            owner=self.do_account,
            unique_id='pool-1',
            name='default-pool',
            type=UtilAsset.Type.KUBERNETES_NODE_POOL,
            monitoring=UtilAsset.Monitoring.ACTIVE,
            metadata={'cluster_id': 'cluster-1', 'pool_id': 'pool-1'},
        )
        check_status = Mock(return_value=('running', {'kubernetes_node_pool': {}}))
        get_check_function.return_value = check_status

        result = run_status_check(node_pool)

        self.assertEqual(result['status'], 'running')
        check_status.assert_called_once_with(
            'pool-1',
            {
                'access_token': 'test-token',
                'cluster_id': 'cluster-1',
                'pool_id': 'pool-1',
            },
        )


class DigitalOceanResourceCheckTestCase(TestCase):
    def _response(self, payload):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = payload
        return response

    @patch('apps.monitoring.checks.digitalocean.requests.get')
    def test_control_plane_resource_checks_return_provider_metadata(self, mock_get):
        cases = [
            (check_digitalocean_database_status, 'db-1', {'database': {'status': 'online'}}, 'online', 'database'),
            (check_digitalocean_load_balancer_status, 'lb-1', {'load_balancer': {'status': 'active'}}, 'active', 'load_balancer'),
            (check_digitalocean_snapshot_status, 'snap-1', {'snapshot': {'regions': ['nyc3']}}, 'available', 'snapshot'),
            (check_digitalocean_backup_status, 'backup-1', {'image': {'status': 'available'}}, 'available', 'backup'),
            (check_digitalocean_firewall_status, 'fw-1', {'firewall': {'status': 'succeeded'}}, 'succeeded', 'firewall'),
            (check_digitalocean_app_platform_status, 'app-1', {'app': {'active_deployment': {'phase': 'ACTIVE'}}}, 'ACTIVE', 'app'),
            (check_digitalocean_container_registry_status, 'registry-1', {'registry': {'name': 'registry-1'}}, 'available', 'registry'),
            (check_digitalocean_kubernetes_cluster_status, 'cluster-1', {'kubernetes_cluster': {'status': {'state': 'running'}}}, 'running', 'kubernetes_cluster'),
            (check_digitalocean_vpc_status, 'vpc-1', {'vpc': {'ip_range': '10.10.0.0/16', 'region': 'nyc3'}}, 'available', 'vpc'),
            (check_digitalocean_vpc_peering_status, 'peer-1', {'vpc_peering': {'status': 'ACTIVE'}}, 'ACTIVE', 'vpc_peering'),
            (check_digitalocean_nat_gateway_status, 'nat-1', {'vpc_nat_gateway': {'state': 'ACTIVE'}}, 'ACTIVE', 'nat_gateway'),
            (check_digitalocean_cdn_endpoint_status, 'cdn-1', {'endpoint': {'origin': 'bucket.nyc3.digitaloceanspaces.com'}}, 'available', 'endpoint'),
            (check_digitalocean_certificate_status, 'cert-1', {'certificate': {'state': 'verified'}}, 'verified', 'certificate'),
        ]

        for checker, unique_id, payload, expected_status, metadata_key in cases:
            with self.subTest(checker=checker.__name__):
                mock_get.reset_mock()
                mock_get.return_value = self._response(payload)
                status, metadata = checker(unique_id, 'token')
                self.assertEqual(status, expected_status)
                self.assertIn(metadata_key, metadata)
                self.assertIn(unique_id, mock_get.call_args.args[0])
                self.assertEqual(mock_get.call_args.kwargs['headers'], {'Authorization': 'Bearer token'})

    @patch('apps.monitoring.checks.digitalocean.requests.get')
    def test_contextual_kubernetes_and_dns_checks_use_scoped_paths(self, mock_get):
        mock_get.side_effect = [
            self._response({
                'node_pool': {
                    'nodes': [
                        {'status': {'state': 'running'}},
                        {'status': {'state': 'running'}},
                    ],
                },
            }),
            self._response({'domain': {'name': 'example.com'}}),
            self._response({'domain_record': {'id': 42, 'type': 'TXT', 'name': '@'}}),
        ]

        credentials = {
            'access_token': 'token',
            'cluster_id': 'cluster-1',
            'pool_id': 'pool-1',
        }
        status, metadata = check_digitalocean_kubernetes_node_pool_status(
            'pool-1', credentials
        )
        self.assertEqual(status, 'running')
        self.assertIn('kubernetes_node_pool', metadata)
        self.assertIn('/kubernetes/clusters/cluster-1/node_pools/pool-1', mock_get.call_args_list[0].args[0])

        domain_credentials = {'access_token': 'token', 'domain_name': 'example.com'}
        status, metadata = check_digitalocean_domain_status('example.com', domain_credentials)
        self.assertEqual(status, 'available')
        self.assertIn('domain', metadata)

        record_credentials = {
            **domain_credentials,
            'record_id': 42,
        }
        status, metadata = check_digitalocean_dns_record_status(
            'example.com:42', record_credentials
        )
        self.assertEqual(status, 'present')
        self.assertIn('domain_record', metadata)
        self.assertIn('/domains/example.com/records/42', mock_get.call_args_list[2].args[0])

    @patch('apps.monitoring.checks.digitalocean.requests.get')
    def test_reserved_ip_check_supports_ipv4_and_ipv6_endpoints(self, mock_get):
        mock_get.side_effect = [
            self._response({'reserved_ip': {'droplet': {'id': 123}}}),
            self._response({'reserved_ipv6': {'droplet': None}}),
        ]

        status_v4, _metadata_v4 = check_digitalocean_reserved_ip_status('203.0.113.10', 'token')
        status_v6, _metadata_v6 = check_digitalocean_reserved_ip_status('2001:db8::1', 'token')

        self.assertEqual(status_v4, 'assigned')
        self.assertEqual(status_v6, 'reserved')
        self.assertIn('/reserved_ips/203.0.113.10', mock_get.call_args_list[0].args[0])
        self.assertIn('/reserved_ipv6/2001%3Adb8%3A%3A1', mock_get.call_args_list[1].args[0])

    @patch('apps.monitoring.checks.digitalocean.boto3.client')
    def test_spaces_check_uses_head_bucket_and_classifies_access_errors(self, mock_client):
        client = Mock()
        mock_client.return_value = client
        credentials = {
            'access_key': 'spaces-key',
            'secret_key': 'spaces-secret',
            'region': 'nyc3',
        }

        status, metadata = check_digitalocean_object_storage_status('test-space', credentials)

        self.assertEqual(status, 'available')
        self.assertEqual(metadata['bucket']['name'], 'test-space')
        client.head_bucket.assert_called_once_with(Bucket='test-space')

        client.head_bucket.side_effect = ClientError(
            {'Error': {'Code': 'AccessDenied', 'Message': 'denied'}},
            'HeadBucket',
        )
        status, _message = check_digitalocean_object_storage_status('test-space', credentials)
        self.assertEqual(status, 'invalid_access_token')

        status, _message = check_digitalocean_object_storage_status(
            'test-space', {**credentials, 'region': 'https://internal.example'}
        )
        self.assertEqual(status, 'error')

    def test_all_new_asset_types_resolve_to_check_functions(self):
        for asset_type in (
            'database',
            'load_balancer',
            'snapshot',
            'backup',
            'reserved_ip',
            'firewall',
            'app_platform',
            'object_storage',
            'container_registry',
            'kubernetes_cluster',
            'kubernetes_node_pool',
            'vpc',
            'vpc_peering',
            'nat_gateway',
            'domain',
            'dns_record',
            'cdn_endpoint',
            'certificate',
        ):
            with self.subTest(asset_type=asset_type):
                self.assertTrue(callable(get_check_function('digitalocean', asset_type)))


class DigitalOceanEmptyAccountSyncTestCase(TestCase):
    """Empty-account response shapes seen against the live DigitalOcean API.

    The databases endpoint answers a bare {"databases": null}, App Platform
    omits the collection and answers only meta total=0, and /v2/registry is
    404 when the account has no registry. None of these may abort the sync.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username='empty-account-user',
            email='empty-account@example.com',
            password='testpass123',
        )
        self.account = CoreAccount.objects.create(
            name='Empty Account Test',
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
            code='digitalocean',
            defaults={
                'name': 'DigitalOcean',
                'status': CoreCloudServiceProvider.Status.ACTIVE,
            },
        )
        self.cloud = CoreCloud.objects.create(
            account=self.account,
            provider=self.provider,
            status=CoreCloud.Status.ACTIVE,
        )
        self.do_account = CoreDigitalOceanAccount.objects.create(
            cloud=self.cloud,
            name='Empty DO Account',
            access_token='test-token',
        )

    @staticmethod
    def _response(payload):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = payload
        return response

    @patch('apps.monitoring.schedules.asset_schedule_create')
    @patch('apps.console.cloud.digitalocean.models.requests.get')
    def test_databases_bare_null_syncs_empty(self, mock_get, _schedule_create):
        stale = CoreDigitalOceanDatabase.objects.create(
            owner=self.do_account,
            unique_id='stale-db',
            name='stale-db',
            type=UtilAsset.Type.DATABASE,
            monitoring=UtilAsset.Monitoring.ACTIVE,
        )
        mock_get.return_value = self._response({'databases': None})

        self.do_account.sync_databases()

        stale.refresh_from_db()
        self.assertEqual(stale.monitoring, UtilAsset.Monitoring.NO_LONGER_EXISTS)

    @patch('apps.monitoring.schedules.asset_schedule_create')
    @patch('apps.console.cloud.digitalocean.models.requests.get')
    def test_apps_missing_collection_with_zero_total_syncs_empty(self, mock_get, _schedule_create):
        mock_get.return_value = self._response({'meta': {'total': 0}})

        self.do_account.sync_apps()

        self.assertEqual(CoreDigitalOceanApp.objects.count(), 0)

    @patch('apps.monitoring.schedules.asset_schedule_create')
    @patch('apps.console.cloud.digitalocean.models.requests.get')
    def test_unexplained_null_still_raises_without_opt_in(self, mock_get, _schedule_create):
        mock_get.return_value = self._response({'databases': None})

        with self.assertRaises(CloudInventoryTransientError):
            self.do_account._paginate_api_call('databases', collection_key='databases')

    @patch('apps.monitoring.schedules.asset_schedule_create')
    @patch('apps.console.cloud.digitalocean.models.requests.get')
    def test_registry_404_syncs_empty(self, mock_get, _schedule_create):
        error = requests.HTTPError('not found')
        error.response = Mock(status_code=404)
        response = Mock()
        response.raise_for_status.side_effect = error
        mock_get.return_value = response

        self.do_account.sync_container_registries()

        self.assertEqual(CoreDigitalOceanContainerRegistry.objects.count(), 0)

    @patch('apps.monitoring.schedules.asset_schedule_create')
    @patch('apps.console.cloud.digitalocean.models.requests.get')
    def test_registry_single_object_is_wrapped(self, mock_get, _schedule_create):
        mock_get.return_value = self._response({'registry': {'name': 'registry-1', 'region': 'nyc3'}})

        self.do_account.sync_container_registries()

        self.assertEqual(CoreDigitalOceanContainerRegistry.objects.get().unique_id, 'registry-1')
