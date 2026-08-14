"""Distributed cloud inventory sync pipeline (CloudSyncRun fan-out)."""
from datetime import timedelta
from unittest.mock import Mock, patch

from celery.exceptions import Retry
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django_celery_beat.models import PeriodicTask

from apps.console.account.models import CoreAccount, CoreAccountMembership
from apps.console.cloud.aws.models import CoreAWSAccount
from apps.console.cloud.digitalocean.models import (
    CoreDigitalOceanAccount,
    CoreDigitalOceanServer,
)
from apps.console.cloud.models import (
    CloudInventoryTransientError,
    CoreCloud,
    CoreCloudServiceProvider,
)
from apps.console.member.models import CoreMember
from apps.console.utils.models import UtilAsset
from apps.monitoring.models import (
    CLOUD_SYNC_RUN_TIMEOUT_SECONDS,
    CloudSyncRun,
)
from apps.monitoring.tasks import (
    CloudSyncFailed,
    _complete_sync_family,
    finalize_cloud_sync_run,
    get_active_sync_run,
    queue_cloud_sync,
    run_cloud_sync,
    start_distributed_cloud_sync,
    sync_cloud_asset_family,
    sync_cloud_assets,
)


class SyncPipelineFixtureTestCase(TestCase):
    """user -> account -> provider -> cloud -> DO account -> server."""

    provider_code = 'digitalocean'
    provider_name = 'DigitalOcean'

    def setUp(self):
        self.user = User.objects.create_user(
            username='syncuser',
            email='sync@example.com',
            password='testpass123',
        )
        self.account = CoreAccount.objects.create(
            name='Sync Account',
            status=CoreAccount.Status.ACTIVE,
            owner=self.user,
        )
        self.member = CoreMember.objects.create(user=self.user, active_account=self.account)
        CoreAccountMembership.objects.create(
            account=self.account,
            member=self.member,
            role=CoreAccountMembership.Role.OWNER,
        )
        self.provider, _ = CoreCloudServiceProvider.objects.get_or_create(
            code=self.provider_code,
            defaults={
                'name': self.provider_name,
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
            access_token='token',
            name='do',
            status='active',
        )
        self.server = CoreDigitalOceanServer.objects.create(
            owner=self.do_account,
            unique_id='droplet-1',
            name='web-1',
            monitoring=UtilAsset.Monitoring.ACTIVE,
            type=UtilAsset.Type.SERVER,
            metadata={},
        )

    def create_run(self, families_total=2, **kwargs):
        defaults = {
            'cloud_uuid': self.cloud.uuid,
            'cloud_id': self.cloud.pk,
            'account_id': self.account.pk,
            'provider': self.provider_code,
            'families_total': families_total,
        }
        defaults.update(kwargs)
        return CloudSyncRun.objects.create(**defaults)


class DefaultSyncFamiliesTestCase(SyncPipelineFixtureTestCase):
    def test_default_family_is_monolithic(self):
        self.assertEqual(self.do_account.sync_asset_families(), [('all', None)])

    def test_default_dispatch_runs_full_sync(self):
        with patch.object(CoreDigitalOceanAccount, 'sync_assets') as mock_sync:
            self.do_account.sync_asset_family('all')
        mock_sync.assert_called_once_with()

    def test_default_dispatch_rejects_unknown_family(self):
        with self.assertRaises(ValueError):
            self.do_account.sync_asset_family('servers')


class AWSSyncFamiliesTestCase(SyncPipelineFixtureTestCase):
    provider_code = 'aws'
    provider_name = 'AWS'

    def setUp(self):
        super().setUp()
        self.aws_account = CoreAWSAccount.objects.create(
            cloud=self.cloud,
            access_key='AKIAEXAMPLE',
            secret_key='secret',
            region='us-east-1',
            name='aws',
            status='active',
        )

    def test_families_shard_metrics_per_region(self):
        with patch(
            'apps.console.cloud.aws.discovery.get_enabled_regions',
            return_value=['eu-west-1', 'us-east-1'],
        ):
            families = self.aws_account.sync_asset_families()

        expected = [(key, None) for key in CoreAWSAccount.AWS_SYNC_FAMILIES]
        expected += [
            ('observability.metrics', 'eu-west-1'),
            ('observability.metrics', 'us-east-1'),
        ]
        self.assertEqual(families, expected)

    def test_families_fall_back_when_region_discovery_fails(self):
        with patch(
            'apps.console.cloud.aws.discovery.get_enabled_regions',
            side_effect=RuntimeError('discovery down'),
        ):
            families = self.aws_account.sync_asset_families()

        expected = [(key, None) for key in CoreAWSAccount.AWS_SYNC_FAMILIES]
        expected.append(('observability.metrics', None))
        self.assertEqual(families, expected)

    def test_dispatch_to_account_method(self):
        with patch.object(CoreAWSAccount, 'sync_servers') as mock_sync:
            self.aws_account.sync_asset_family('servers')
        mock_sync.assert_called_once_with()

    def test_dispatch_to_module_function(self):
        with patch(
            'apps.console.cloud.aws.network.sync_aws_network_assets'
        ) as mock_network:
            self.aws_account.sync_asset_family('network')
        mock_network.assert_called_once_with(self.aws_account)

    def test_dispatch_observability_shard(self):
        with patch(
            'apps.console.cloud.aws.observability.sync_aws_observability_collection'
        ) as mock_collection:
            self.aws_account.sync_asset_family('observability.metrics', 'eu-west-1')
        mock_collection.assert_called_once_with(
            self.aws_account, 'aws_cloudwatch_metric', region='eu-west-1'
        )

    def test_dispatch_rejects_unknown_family(self):
        with self.assertRaises(ValueError):
            self.aws_account.sync_asset_family('nope')

    def test_metrics_shard_requires_valid_region(self):
        with self.assertRaises(ValueError):
            self.aws_account.sync_asset_family('observability.metrics', 'not a region')


class OrchestratorTestCase(SyncPipelineFixtureTestCase):
    @patch.object(CoreCloud, 'validate', return_value=True)
    def test_creates_run_and_enqueues_family_tasks(self, _mock_validate):
        with patch('apps.monitoring.tasks.sync_cloud_asset_family.delay') as mock_delay:
            result = start_distributed_cloud_sync(self.cloud)

        self.assertTrue(result['success'])
        self.assertTrue(result['queued'])
        run = CloudSyncRun.objects.get(cloud_uuid=self.cloud.uuid)
        self.assertEqual(run.status, CloudSyncRun.Status.RUNNING)
        self.assertEqual(run.families_total, 1)
        self.assertEqual(run.provider, 'digitalocean')
        mock_delay.assert_called_once_with(
            str(self.cloud.uuid), str(run.uuid), 'all', None
        )

    @patch.object(CoreCloud, 'validate', return_value=True)
    def test_skips_while_another_run_is_active(self, mock_validate):
        self.create_run(families_total=3)

        result = start_distributed_cloud_sync(self.cloud)

        self.assertTrue(result['success'])
        self.assertTrue(result['skipped'])
        mock_validate.assert_not_called()
        self.assertEqual(CloudSyncRun.objects.count(), 1)

    @patch.object(CoreCloud, 'validate', return_value=False)
    def test_invalid_credentials_mark_invalid_and_skip_fanout(self, _mock_validate):
        from apps.monitoring.schedules import asset_schedule_create
        asset_schedule_create(self.server)

        result = start_distributed_cloud_sync(self.cloud)

        self.assertTrue(result['success'])
        self.assertEqual(result['current_status'], CoreCloud.Status.INVALID_AUTH)
        self.cloud.refresh_from_db()
        self.assertEqual(self.cloud.status, CoreCloud.Status.INVALID_AUTH)
        self.assertFalse(CloudSyncRun.objects.exists())
        self.assertFalse(
            PeriodicTask.objects.filter(name=f'asset-{self.server.uuid}').exists()
        )

    def test_paused_cloud_is_skipped(self):
        self.cloud.status = CoreCloud.Status.PAUSED
        self.cloud.save()

        result = start_distributed_cloud_sync(self.cloud)

        self.assertTrue(result['success'])
        self.assertTrue(result['skipped'])
        self.assertFalse(CloudSyncRun.objects.exists())

    @patch.object(CoreCloud, 'validate', side_effect=NotImplementedError('nope'))
    def test_not_implemented_is_reported(self, _mock_validate):
        result = start_distributed_cloud_sync(self.cloud)

        self.assertFalse(result['success'])
        self.assertTrue(result['not_implemented'])
        self.assertFalse(CloudSyncRun.objects.exists())

    @patch.object(CoreCloud, 'validate', return_value=True)
    def test_task_raises_retryable_failure(self, _mock_validate):
        with patch(
            'apps.monitoring.tasks.start_distributed_cloud_sync',
            return_value={'success': False, 'message': 'boom'},
        ):
            with self.assertRaises(CloudSyncFailed):
                sync_cloud_assets(str(self.cloud.uuid))

    def test_task_ignores_deleted_cloud(self):
        self.assertIsNone(sync_cloud_assets('00000000-0000-0000-0000-000000000000'))


class FamilyTaskTestCase(SyncPipelineFixtureTestCase):
    def test_runs_dispatch_and_finalizes_last_family(self):
        run = self.create_run(families_total=1)
        with patch.object(
            CoreDigitalOceanAccount, 'sync_asset_family'
        ) as mock_dispatch:
            sync_cloud_asset_family(str(self.cloud.uuid), str(run.uuid), 'all', None)

        mock_dispatch.assert_called_once_with('all', None)
        run.refresh_from_db()
        self.assertEqual(run.status, CloudSyncRun.Status.SUCCESS)
        self.assertIsNotNone(run.finished_at)
        self.cloud.refresh_from_db()
        self.assertIsNotNone(self.cloud.last_synced)
        self.do_account.refresh_from_db()
        self.assertIsNotNone(self.do_account.last_synced)
        # The finalizer reconciled schedules for the active server.
        self.assertTrue(
            PeriodicTask.objects.filter(name=f'asset-{self.server.uuid}').exists()
        )

    def test_records_permanent_error_and_completes_marker(self):
        run = self.create_run(families_total=2)
        with patch.object(
            CoreDigitalOceanAccount,
            'sync_asset_family',
            side_effect=RuntimeError('provider exploded'),
        ):
            sync_cloud_asset_family(str(self.cloud.uuid), str(run.uuid), 'all', None)

        run.refresh_from_db()
        self.assertEqual(run.status, CloudSyncRun.Status.RUNNING)
        self.assertEqual(run.families_done, ['all|'])
        self.assertEqual(len(run.family_errors), 1)
        self.assertEqual(run.family_errors[0]['family'], 'all')

    def test_retries_transient_errors_without_completing_marker(self):
        run = self.create_run(families_total=2)
        with patch.object(
            CoreDigitalOceanAccount,
            'sync_asset_family',
            side_effect=CloudInventoryTransientError('throttled'),
        ), patch.object(
            sync_cloud_asset_family, 'retry', side_effect=Retry('retrying')
        ):
            with self.assertRaises(Retry):
                sync_cloud_asset_family(str(self.cloud.uuid), str(run.uuid), 'all', None)

        run.refresh_from_db()
        self.assertEqual(run.families_done, [])
        self.assertEqual(run.family_errors, [])

    def test_completion_is_idempotent_for_redelivered_tasks(self):
        run = self.create_run(families_total=2)

        _complete_sync_family(str(run.uuid), 'servers', None)
        _complete_sync_family(str(run.uuid), 'servers', None)

        run.refresh_from_db()
        self.assertEqual(run.families_done, ['servers|'])
        self.assertEqual(run.status, CloudSyncRun.Status.RUNNING)

    def test_skips_when_run_is_no_longer_active(self):
        run = self.create_run(
            families_total=1, status=CloudSyncRun.Status.FINALIZING
        )
        with patch.object(
            CoreDigitalOceanAccount, 'sync_asset_family'
        ) as mock_dispatch:
            sync_cloud_asset_family(str(self.cloud.uuid), str(run.uuid), 'all', None)

        mock_dispatch.assert_not_called()

    def test_deleted_cloud_still_completes_marker(self):
        run = self.create_run(families_total=1, cloud_uuid='00000000-0000-0000-0000-000000000001')
        sync_cloud_asset_family(
            '00000000-0000-0000-0000-000000000001', str(run.uuid), 'all', None
        )

        run.refresh_from_db()
        self.assertEqual(run.status, CloudSyncRun.Status.FAILED)
        self.assertIn('removed', run.error)


class FinalizerTestCase(SyncPipelineFixtureTestCase):
    def test_recovers_invalid_auth_and_removes_tombstone_schedules(self):
        from apps.monitoring.schedules import asset_schedule_create

        self.cloud.status = CoreCloud.Status.INVALID_AUTH
        self.cloud.save()
        tombstone = CoreDigitalOceanServer.objects.create(
            owner=self.do_account,
            unique_id='droplet-gone',
            name='gone',
            monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS,
            type=UtilAsset.Type.SERVER,
            metadata={},
        )
        # Leftover schedule that the old monolithic pass never reconciled.
        asset_schedule_create(tombstone)
        asset_schedule_create(self.server)
        run = self.create_run(
            families_total=1,
            families_done=['all|'],
            status=CloudSyncRun.Status.FINALIZING,
        )

        finalize_cloud_sync_run(str(run.uuid))

        self.cloud.refresh_from_db()
        self.assertEqual(self.cloud.status, CoreCloud.Status.ACTIVE)
        self.assertFalse(
            PeriodicTask.objects.filter(name=f'asset-{tombstone.uuid}').exists()
        )
        self.assertTrue(
            PeriodicTask.objects.filter(name=f'asset-{self.server.uuid}').exists()
        )
        run.refresh_from_db()
        self.assertEqual(run.status, CloudSyncRun.Status.SUCCESS)
        self.assertIsNotNone(run.finished_at)

    def test_family_errors_mark_run_partial(self):
        run = self.create_run(
            families_total=1,
            families_done=['all|'],
            family_errors=[{'family': 'all', 'region': '', 'error': 'boom'}],
            status=CloudSyncRun.Status.FINALIZING,
        )

        finalize_cloud_sync_run(str(run.uuid))

        run.refresh_from_db()
        self.assertEqual(run.status, CloudSyncRun.Status.PARTIAL)

    def test_deleted_cloud_marks_run_failed(self):
        run = self.create_run(
            families_total=0,
            cloud_uuid='00000000-0000-0000-0000-000000000002',
            status=CloudSyncRun.Status.FINALIZING,
        )

        finalize_cloud_sync_run(str(run.uuid))

        run.refresh_from_db()
        self.assertEqual(run.status, CloudSyncRun.Status.FAILED)
        self.assertIn('removed', run.error)


class ActiveRunTrackingTestCase(SyncPipelineFixtureTestCase):
    def test_fresh_run_is_active(self):
        run = self.create_run()
        self.assertEqual(get_active_sync_run(self.cloud.uuid), run)
        self.assertTrue(self.cloud.sync_in_progress)

    def test_stale_running_run_expires(self):
        run = self.create_run()
        CloudSyncRun.objects.filter(pk=run.pk).update(
            started_at=timezone.now()
            - timedelta(seconds=CLOUD_SYNC_RUN_TIMEOUT_SECONDS + 60)
        )

        self.assertIsNone(get_active_sync_run(self.cloud.uuid))
        run.refresh_from_db()
        self.assertEqual(run.status, CloudSyncRun.Status.FAILED)
        self.assertFalse(self.cloud.sync_in_progress)

    def test_stale_finalizing_run_is_finalized_inline(self):
        run = self.create_run(families_total=1, families_done=['all|'])
        CloudSyncRun.objects.filter(pk=run.pk).update(
            status=CloudSyncRun.Status.FINALIZING,
            started_at=timezone.now()
            - timedelta(seconds=CLOUD_SYNC_RUN_TIMEOUT_SECONDS + 60),
        )

        self.assertIsNone(get_active_sync_run(self.cloud.uuid))
        run.refresh_from_db()
        self.assertEqual(run.status, CloudSyncRun.Status.SUCCESS)
        self.assertIsNotNone(run.finished_at)
        self.cloud.refresh_from_db()
        self.assertIsNotNone(self.cloud.last_synced)

    def test_run_cloud_sync_respects_active_run(self):
        self.create_run()
        with patch.object(CoreCloud, 'validate') as mock_validate:
            result = run_cloud_sync(self.cloud)

        self.assertTrue(result['success'])
        self.assertTrue(result['skipped'])
        mock_validate.assert_not_called()


class QueueCloudSyncTestCase(SyncPipelineFixtureTestCase):
    def test_enqueues_distributed_sync(self):
        with patch('apps.monitoring.tasks.sync_cloud_assets.delay') as mock_delay:
            result = queue_cloud_sync(self.cloud)

        self.assertTrue(result['success'])
        self.assertTrue(result['queued'])
        mock_delay.assert_called_once_with(str(self.cloud.uuid))

    def test_falls_back_to_inline_sync_without_broker(self):
        with patch(
            'apps.monitoring.tasks.sync_cloud_assets.delay',
            side_effect=ConnectionError('broker down'),
        ), patch(
            'apps.monitoring.tasks.run_cloud_sync',
            return_value={'success': True, 'message': 'inline'},
        ) as mock_inline:
            result = queue_cloud_sync(self.cloud)

        self.assertEqual(result['message'], 'inline')
        mock_inline.assert_called_once_with(self.cloud)


class CloudSyncViewTestCase(SyncPipelineFixtureTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def test_sync_view_queues_background_sync(self):
        with patch(
            'apps.console.cloud.views.queue_cloud_sync',
            return_value={'success': True, 'queued': True},
        ) as mock_queue:
            response = self.client.post(
                reverse('console:cloud:sync', kwargs={'cloud_id': self.cloud.pk})
            )

        self.assertEqual(response.status_code, 302)
        mock_queue.assert_called_once()
        queued_cloud = mock_queue.call_args[0][0]
        self.assertEqual(queued_cloud.pk, self.cloud.pk)

    def test_sync_view_surfaces_inline_failure(self):
        with patch(
            'apps.console.cloud.views.queue_cloud_sync',
            return_value={'success': False, 'message': 'nope'},
        ):
            response = self.client.post(
                reverse('console:cloud:sync', kwargs={'cloud_id': self.cloud.pk}),
                follow=True,
            )

        self.assertContains(response, 'nope', status_code=200)


class ConnectViewQueuingTestCase(SyncPipelineFixtureTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def test_digitalocean_connect_queues_sync(self):
        mock_response = Mock()
        mock_response.status_code = 200
        with patch(
            'apps.console.cloud.forms.requests.get', return_value=mock_response
        ), patch(
            'apps.console.cloud.digitalocean.views.queue_cloud_sync'
        ) as mock_queue:
            response = self.client.post(reverse('console:cloud:digitalocean:connect'), {
                'account_name': 'do-prod',
                'access_token': 'dop_v1_test',
                'spaces_access_key': '',
                'spaces_secret_key': '',
                'spaces_region': '',
            })

        self.assertEqual(response.status_code, 302)
        account = CoreDigitalOceanAccount.objects.get(name='do-prod')
        mock_queue.assert_called_once()
        self.assertEqual(mock_queue.call_args[0][0].pk, account.cloud_id)
