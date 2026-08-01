from datetime import timedelta
from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone
from django_celery_beat.models import PeriodicTask

from apps.console.account.models import CoreAccount, CoreAccountMembership
from apps.console.cloud.digitalocean.models import CoreDigitalOceanAccount, CoreDigitalOceanServer
from apps.console.cloud.models import CoreCloud, CoreCloudServiceProvider
from apps.console.member.models import CoreMember
from apps.console.utils.models import UtilAsset
from apps.monitoring.metadata import compare_metadata
from apps.monitoring.models import AssetStatusEmail, AssetStatusLog
from apps.monitoring.schedules import (
    asset_schedule_create,
    asset_schedule_delete,
    asset_schedule_update,
    cloud_schedule_create,
    cloud_schedule_delete,
    cloud_schedule_update,
)
from apps.monitoring.tasks import prune_status_logs, run_status_check


def make_droplet_metadata(status='active'):
    """DigitalOcean droplet payload as returned by the provider API."""
    return {
        'droplet': {
            'name': 'test-server',
            'status': status,
            'memory': 1024,
            'vcpus': 1,
            'disk': 25,
            'size_slug': 's-1vcpu-1gb',
            'locked': False,
            'networks': {'v4': [{'ip_address': '1.2.3.4'}]},
            'features': ['backups'],
            'region': {'name': 'NYC3'},
        }
    }


class MonitoringFixtureTestCase(TestCase):
    """Shared fixtures: user -> account -> provider -> cloud -> DO account -> server."""

    def setUp(self):
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
        self.member = CoreMember.objects.create(user=self.user, active_account=self.account)
        CoreAccountMembership.objects.create(
            account=self.account,
            member=self.member,
            role=CoreAccountMembership.Role.OWNER
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
        self.do_account = CoreDigitalOceanAccount.objects.create(
            cloud=self.cloud,
            name="Test DO Account",
            access_token="test-token"
        )
        self.server = CoreDigitalOceanServer.objects.create(
            owner=self.do_account,
            unique_id="123456",
            name="test-server",
            type=UtilAsset.Type.SERVER,
            monitoring=UtilAsset.Monitoring.ACTIVE,
            metadata=make_droplet_metadata()
        )

    def create_log(self, status, timestamp, metadata=None, metadata_changes=None):
        return AssetStatusLog.objects.create(
            asset_key=self.server.key,
            account_id=self.account.id,
            provider='digitalocean',
            asset_type='server',
            status=status,
            timestamp=timestamp,
            metadata=metadata,
            metadata_changes=metadata_changes,
        )


class MetadataCompareTestCase(TestCase):
    def test_status_flip_wording(self):
        prev = {'droplet': {'status': 'active'}}
        curr = {'droplet': {'status': 'off'}}
        changes = compare_metadata(prev, curr, 'digitalocean', 'server')
        self.assertIn("Status changed from 'active' to 'off'", changes)

    def test_identical_metadata_produces_no_changes(self):
        metadata = {
            'droplet': {
                'name': 'test-server',
                'status': 'active',
                'memory': 1024,
                'features': ['backups', 'ipv6'],
                'region': {'name': 'NYC3'},
            }
        }
        changes = compare_metadata(metadata, metadata, 'digitalocean', 'server')
        self.assertEqual(changes, [])

    def test_array_add_remove_wording(self):
        prev = {'instance': {'SecurityGroups': [{'GroupName': 'default'}, {'GroupName': 'web'}]}}
        curr = {'instance': {'SecurityGroups': [{'GroupName': 'web'}, {'GroupName': 'db'}]}}
        changes = compare_metadata(prev, curr, 'aws', 'server')
        self.assertIn("Security Groups: Added {'GroupName': 'db'}", changes)
        self.assertIn("Security Groups: Removed {'GroupName': 'default'}", changes)


class StatusTimelineTestCase(MonitoringFixtureTestCase):
    def test_collapse_error_exclusion_and_durations(self):
        now = timezone.now()
        self.create_log('active', now - timedelta(hours=3))
        # Consecutive duplicate status: must be collapsed
        self.create_log('active', now - timedelta(hours=2))
        self.create_log('off', now - timedelta(hours=1))
        # Error statuses are excluded from the timeline
        self.create_log('error', now - timedelta(minutes=30), metadata=None)
        # Entries carrying metadata changes are always kept
        self.create_log('active', now - timedelta(minutes=20),
                        metadata=make_droplet_metadata(),
                        metadata_changes=["Status changed from 'off' to 'active'"])

        timeline = self.server.get_status_timeline(days=30)

        self.assertEqual([entry['status'] for entry in timeline], ['active', 'off', 'active'])
        for entry in timeline:
            self.assertIn('timestamp', entry)
            self.assertIsNotNone(entry['timestamp'].tzinfo)
            self.assertIn('timezone', entry)
            self.assertIn('metadata_changes', entry)
            self.assertIn('duration', entry)
            self.assertIsInstance(entry['duration'], str)
            self.assertTrue(entry['duration'])
        # The newest entry kept its metadata changes
        self.assertEqual(timeline[0]['metadata_changes'], ["Status changed from 'off' to 'active'"])
        # No error status anywhere
        self.assertNotIn('error', [entry['status'] for entry in timeline])

    def test_empty_timeline(self):
        self.assertEqual(self.server.get_status_timeline(days=30), [])

    def test_paginated_contract(self):
        now = timezone.now()
        statuses = ['active', 'off', 'active', 'off']
        for i, status in enumerate(statuses):
            self.create_log(status, now - timedelta(hours=len(statuses) - i))

        page1 = self.server.get_status_timeline_paginated(page=1, page_size=2, days=30)
        self.assertEqual(
            set(page1.keys()),
            {'items', 'has_next', 'has_previous', 'total_pages', 'current_page', 'page_size'}
        )
        self.assertEqual(len(page1['items']), 2)
        self.assertTrue(page1['has_next'])
        self.assertFalse(page1['has_previous'])
        self.assertEqual(page1['total_pages'], 2)
        self.assertEqual(page1['current_page'], 1)
        self.assertEqual(page1['page_size'], 2)

        page2 = self.server.get_status_timeline_paginated(page=2, page_size=2, days=30)
        self.assertEqual(len(page2['items']), 2)
        self.assertFalse(page2['has_next'])
        self.assertTrue(page2['has_previous'])

        # No overlap between pages
        page1_timestamps = {item['timestamp'] for item in page1['items']}
        page2_timestamps = {item['timestamp'] for item in page2['items']}
        self.assertFalse(page1_timestamps & page2_timestamps)


class RunStatusCheckTestCase(MonitoringFixtureTestCase):
    def fake_check(self, status):
        return lambda unique_id, access_token: (status, make_droplet_metadata(status=status))

    @patch('apps.monitoring.tasks.send_status_change_email')
    @patch('apps.monitoring.tasks.get_check_function')
    def test_first_check_stores_log_without_email(self, mock_get_check, mock_email_task):
        mock_get_check.return_value = self.fake_check('active')

        result = run_status_check(self.server)

        self.assertEqual(result['status'], 'active')
        self.assertIsNone(result['metadata_changes'])
        self.assertEqual(AssetStatusLog.objects.count(), 1)
        log = AssetStatusLog.objects.get()
        self.assertEqual(log.status, 'active')
        self.assertEqual(log.asset_key, self.server.key)
        self.assertIsNotNone(log.metadata)
        mock_email_task.delay.assert_not_called()
        mock_email_task.run.assert_not_called()

    @patch('apps.monitoring.tasks.send_status_change_email')
    @patch('apps.monitoring.tasks.get_check_function')
    def test_identical_check_stores_nothing(self, mock_get_check, mock_email_task):
        mock_get_check.return_value = self.fake_check('active')
        run_status_check(self.server)
        self.assertEqual(AssetStatusLog.objects.count(), 1)

        result = run_status_check(self.server)

        self.assertEqual(result['status'], 'active')
        self.assertEqual(result['metadata_changes'], [])
        self.assertEqual(AssetStatusLog.objects.count(), 1)
        mock_email_task.delay.assert_not_called()
        mock_email_task.run.assert_not_called()

    @patch('apps.monitoring.tasks.send_status_change_email')
    @patch('apps.monitoring.tasks.get_check_function')
    def test_status_flip_stores_log_and_queues_email(self, mock_get_check, mock_email_task):
        mock_get_check.return_value = self.fake_check('active')
        run_status_check(self.server)

        mock_get_check.return_value = self.fake_check('off')
        result = run_status_check(self.server)

        self.assertEqual(result['status'], 'off')
        self.assertIn("Status changed from 'active' to 'off'", result['metadata_changes'])
        self.assertEqual(AssetStatusLog.objects.count(), 2)
        latest_log = AssetStatusLog.objects.order_by('-timestamp').first()
        self.assertEqual(latest_log.status, 'off')
        self.assertIn("Status changed from 'active' to 'off'", latest_log.metadata_changes)

        mock_email_task.delay.assert_called_once()
        args = mock_email_task.delay.call_args[0]
        # (content_type_id, object_id, previous_status, current_status, timeline, metadata_changes)
        self.assertEqual(args[1], self.server.pk)
        self.assertEqual(args[2], 'active')
        self.assertEqual(args[3], 'off')
        self.assertEqual(args[5], result['metadata_changes'])

    @patch('apps.monitoring.tasks.send_status_change_email')
    @patch('apps.monitoring.tasks.get_check_function')
    def test_error_status_stores_error_log_without_email(self, mock_get_check, mock_email_task):
        mock_get_check.return_value = lambda unique_id, access_token: ('error', 'boom')

        result = run_status_check(self.server)

        self.assertEqual(result['status'], 'error')
        self.assertIn('boom', result['error'])
        self.assertIsNone(result['metadata_changes'])
        self.assertIsNone(result['metadata'])
        self.assertEqual(AssetStatusLog.objects.count(), 1)
        log = AssetStatusLog.objects.get()
        self.assertEqual(log.status, 'error')
        self.assertIsNone(log.metadata)
        self.assertEqual(log.error_message, 'boom')
        mock_email_task.delay.assert_not_called()
        mock_email_task.run.assert_not_called()


class SchedulesTestCase(MonitoringFixtureTestCase):
    def test_asset_schedule_lifecycle(self):
        # The asset created in setUp already got a schedule from save()
        task_name = f'asset-{self.server.uuid}'
        task = PeriodicTask.objects.get(name=task_name)
        self.assertEqual(task.task, 'cloudmoo.check_asset_status')
        self.assertTrue(task.enabled)

        # Update follows the monitoring state
        self.server.monitoring = UtilAsset.Monitoring.DISABLED
        asset_schedule_update(self.server)
        task.refresh_from_db()
        self.assertFalse(task.enabled)

        self.server.monitoring = UtilAsset.Monitoring.ACTIVE
        asset_schedule_update(self.server)
        task.refresh_from_db()
        self.assertTrue(task.enabled)

        asset_schedule_delete(self.server)
        self.assertFalse(PeriodicTask.objects.filter(name=task_name).exists())

        # Create is idempotent
        asset_schedule_create(self.server)
        asset_schedule_create(self.server)
        self.assertEqual(PeriodicTask.objects.filter(name=task_name).count(), 1)

    def test_save_syncs_schedule_with_monitoring(self):
        task_name = f'asset-{self.server.uuid}'
        self.server.monitoring = UtilAsset.Monitoring.DISABLED
        self.server.save()
        self.assertFalse(PeriodicTask.objects.get(name=task_name).enabled)

        self.server.monitoring = UtilAsset.Monitoring.NO_LONGER_EXISTS
        self.server.save()
        self.assertFalse(PeriodicTask.objects.filter(name=task_name).exists())

    def test_cloud_schedule_lifecycle(self):
        # The cloud created in setUp already got a schedule from save()
        task_name = f'cloud-{self.cloud.uuid}'
        task = PeriodicTask.objects.get(name=task_name)
        self.assertEqual(task.task, 'cloudmoo.sync_cloud_assets')
        self.assertTrue(task.enabled)

        # Update follows the cloud status
        self.cloud.status = CoreCloud.Status.PAUSED
        cloud_schedule_update(self.cloud)
        task.refresh_from_db()
        self.assertFalse(task.enabled)

        self.cloud.status = CoreCloud.Status.ACTIVE
        cloud_schedule_update(self.cloud)
        task.refresh_from_db()
        self.assertTrue(task.enabled)

        cloud_schedule_delete(self.cloud)
        self.assertFalse(PeriodicTask.objects.filter(name=task_name).exists())

        # Create is idempotent
        cloud_schedule_create(self.cloud)
        cloud_schedule_create(self.cloud)
        self.assertEqual(PeriodicTask.objects.filter(name=task_name).count(), 1)


class PruneStatusLogsTestCase(MonitoringFixtureTestCase):
    def test_prune_deletes_old_rows_keeps_recent(self):
        now = timezone.now()
        retention_days = self.account.log_retention_days

        old_log = self.create_log('active', now - timedelta(days=retention_days + 10))
        recent_log = self.create_log('active', now - timedelta(days=1))

        old_email = AssetStatusEmail.objects.create(
            asset_key=self.server.key,
            account_id=self.account.id,
            asset_id=self.server.id,
            provider='digitalocean',
            asset_type='server',
            recipient='test@example.com',
            subject='old',
            text_body='old',
            html_body='<p>old</p>',
        )
        recent_email = AssetStatusEmail.objects.create(
            asset_key=self.server.key,
            account_id=self.account.id,
            asset_id=self.server.id,
            provider='digitalocean',
            asset_type='server',
            recipient='test@example.com',
            subject='recent',
            text_body='recent',
            html_body='<p>recent</p>',
        )
        # timestamp is auto_now_add: backdate the old row explicitly
        AssetStatusEmail.objects.filter(pk=old_email.pk).update(
            timestamp=now - timedelta(days=retention_days + 10)
        )

        prune_status_logs()

        self.assertFalse(AssetStatusLog.objects.filter(pk=old_log.pk).exists())
        self.assertTrue(AssetStatusLog.objects.filter(pk=recent_log.pk).exists())
        self.assertFalse(AssetStatusEmail.objects.filter(pk=old_email.pk).exists())
        self.assertTrue(AssetStatusEmail.objects.filter(pk=recent_email.pk).exists())


class WebhookSyncTestCase(MonitoringFixtureTestCase):
    url = '/api/v1/webhook/cloud/sync_assets/'

    @override_settings(CLOUDMOO_API_KEY='test-secret-key')
    def test_wrong_api_key_rejected(self):
        response = self.client.post(
            self.url,
            data={'uuid': str(self.cloud.uuid)},
            content_type='application/json',
            HTTP_X_API_KEY='wrong-key',
        )
        self.assertIn(response.status_code, (401, 403))

    @override_settings(CLOUDMOO_API_KEY='test-secret-key')
    def test_missing_api_key_rejected(self):
        response = self.client.post(
            self.url,
            data={'uuid': str(self.cloud.uuid)},
            content_type='application/json',
        )
        self.assertIn(response.status_code, (401, 403))

    @override_settings(CLOUDMOO_API_KEY='test-secret-key')
    @patch.object(CoreCloud, 'sync_assets')
    @patch.object(CoreCloud, 'validate')
    def test_sync_success_contract(self, mock_validate, mock_sync_assets):
        mock_validate.return_value = True

        response = self.client.post(
            self.url,
            data={'uuid': str(self.cloud.uuid)},
            content_type='application/json',
            HTTP_X_API_KEY='test-secret-key',
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            set(body.keys()),
            {'success', 'message', 'status_changed', 'current_status', 'last_synced'}
        )
        self.assertTrue(body['success'])
        self.assertFalse(body['status_changed'])
        self.assertEqual(body['current_status'], CoreCloud.Status.ACTIVE)
        mock_validate.assert_called_once()
        mock_sync_assets.assert_called_once()

    @override_settings(CLOUDMOO_API_KEY='test-secret-key')
    @patch.object(CoreCloud, 'validate')
    def test_not_implemented_returns_501(self, mock_validate):
        mock_validate.side_effect = NotImplementedError("not implemented")

        response = self.client.post(
            self.url,
            data={'uuid': str(self.cloud.uuid)},
            content_type='application/json',
            HTTP_X_API_KEY='test-secret-key',
        )

        self.assertEqual(response.status_code, 501)
        self.assertIn('error', response.json())

    @override_settings(CLOUDMOO_API_KEY='test-secret-key')
    def test_unknown_cloud_rejected(self):
        import uuid as uuid_module
        response = self.client.post(
            self.url,
            data={'uuid': str(uuid_module.uuid4())},
            content_type='application/json',
            HTTP_X_API_KEY='test-secret-key',
        )
        self.assertEqual(response.status_code, 400)
