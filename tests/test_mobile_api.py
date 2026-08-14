"""Mobile API tests: auth, scoping, endpoints, and health math."""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from apps.console.account.models import CoreAccount, CoreAccountMembership
from apps.console.cloud.digitalocean.models import (
    CoreDigitalOceanAccount,
    CoreDigitalOceanServer,
)
from apps.console.cloud.models import CoreCloud, CoreCloudServiceProvider
from apps.console.member.models import CoreMember
from apps.console.utils.models import UtilAsset
from apps.monitoring.health import calculate_uptime, classify_health
from apps.monitoring.models import (
    AssetMonitoringState,
    AssetStatusEmail,
    AssetStatusLog,
)


class HealthClassificationTestCase(APITestCase):
    def test_up_statuses_are_healthy(self):
        for status in ('active', 'running', 'available', 'RUNNING', 'started'):
            self.assertEqual(classify_health(status), 'healthy')

    def test_down_statuses(self):
        for status in ('off', 'stopped', 'terminated', 'down'):
            self.assertEqual(classify_health(status), 'down')

    def test_warning_statuses(self):
        self.assertEqual(classify_health('degraded'), 'warning')
        self.assertEqual(classify_health('provisioning'), 'warning')

    def test_monitoring_state_wins(self):
        self.assertEqual(classify_health('active', monitoring='disabled'), 'paused')
        self.assertEqual(
            classify_health('active', monitoring='no_longer_exists'), 'gone'
        )

    def test_diagnostic_and_stale_statuses_are_unknown(self):
        self.assertEqual(classify_health('error'), 'unknown')
        self.assertEqual(classify_health('invalid_access_token'), 'unknown')
        self.assertEqual(classify_health('active', stale=True), 'unknown')
        self.assertEqual(classify_health(None), 'unknown')
        self.assertEqual(classify_health(''), 'unknown')
        self.assertEqual(classify_health('mystery-state'), 'unknown')


class UptimeTestCase(APITestCase):
    def test_no_data_is_none_not_zero(self):
        self.assertIsNone(calculate_uptime('cm__1__digitalocean__nothing'))

    def test_seed_before_window_counts_full_window(self):
        key = 'cm__1__digitalocean__seeded'
        AssetStatusLog.objects.create(
            asset_key=key, account_id=1, provider='digitalocean',
            asset_type='server', status='active',
            timestamp=timezone.now() - timedelta(days=40),
        )
        self.assertEqual(calculate_uptime(key, days=30), 100.0)

    def test_down_segment_reduces_uptime(self):
        key = 'cm__1__digitalocean__flapping'
        now = timezone.now()
        AssetStatusLog.objects.create(
            asset_key=key, account_id=1, provider='digitalocean',
            asset_type='server', status='active', timestamp=now - timedelta(days=20),
        )
        AssetStatusLog.objects.create(
            asset_key=key, account_id=1, provider='digitalocean',
            asset_type='server', status='off', timestamp=now - timedelta(days=10),
        )
        # active 10d (seed start->off) + off 10d; window 30d opens unknown? no:
        # no seed before window; 10d unknown + 10d active + 10d off => 33.33%
        self.assertEqual(calculate_uptime(key, days=30), 33.33)

    def test_error_logs_do_not_affect_uptime(self):
        key = 'cm__1__digitalocean__noisy'
        now = timezone.now()
        AssetStatusLog.objects.create(
            asset_key=key, account_id=1, provider='digitalocean',
            asset_type='server', status='active', timestamp=now - timedelta(days=40),
        )
        AssetStatusLog.objects.create(
            asset_key=key, account_id=1, provider='digitalocean',
            asset_type='server', status='error', timestamp=now - timedelta(days=5),
            error_message='provider timeout',
        )
        self.assertEqual(calculate_uptime(key, days=30), 100.0)


class MobileAPIFixtureTestCase(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='mobileuser',
            email='mobile@example.com',
            password='testpass123',
            first_name='Mo',
            last_name='Bile',
        )
        self.account = CoreAccount.objects.create(
            name='Mobile Account',
            status=CoreAccount.Status.ACTIVE,
            owner=self.user,
        )
        self.member = CoreMember.objects.create(
            user=self.user, active_account=self.account, email_verified=True,
        )
        CoreAccountMembership.objects.create(
            account=self.account,
            member=self.member,
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
            cloud=self.cloud, name='DO Prod', access_token='token',
        )
        self.server = CoreDigitalOceanServer.objects.create(
            owner=self.do_account,
            unique_id='4242',
            name='api-01',
            type=UtilAsset.Type.SERVER,
            monitoring=UtilAsset.Monitoring.ACTIVE,
            metadata={'region': {'name': 'nyc3'}, 'size_slug': 's-1vcpu-1gb'},
        )
        self.token = Token.objects.create(user=self.user)

        # A second account that must never bleed into responses.
        self.other_user = User.objects.create_user(
            username='other', email='other@example.com', password='pass12345',
        )
        self.other_account = CoreAccount.objects.create(
            name='Other Account', status=CoreAccount.Status.ACTIVE,
            owner=self.other_user,
        )
        self.other_member = CoreMember.objects.create(
            user=self.other_user, active_account=self.other_account,
            email_verified=True,
        )
        CoreAccountMembership.objects.create(
            account=self.other_account, member=self.other_member,
            role=CoreAccountMembership.Role.OWNER,
        )
        other_cloud = CoreCloud.objects.create(
            account=self.other_account, provider=self.provider,
            status=CoreCloud.Status.ACTIVE,
        )
        other_do = CoreDigitalOceanAccount.objects.create(
            cloud=other_cloud, name='Other DO', access_token='token',
        )
        self.other_server = CoreDigitalOceanServer.objects.create(
            owner=other_do, unique_id='9999', name='other-server',
            type=UtilAsset.Type.SERVER, monitoring=UtilAsset.Monitoring.ACTIVE,
            metadata={},
        )
        self.other_cloud = other_cloud

    def authenticate(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')


class MobileAuthTestCase(MobileAPIFixtureTestCase):
    login_url = '/api/v1/mobile/auth/login/'

    def test_login_returns_token_user_installation(self):
        response = self.client.post(self.login_url, {
            'email': 'mobile@example.com',
            'password': 'testpass123',
        }, format='json')

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn('token', body)
        self.assertEqual(body['user']['email'], 'mobile@example.com')
        self.assertEqual(body['user']['role'], 'Owner')
        self.assertEqual(body['user']['account'], 'Mobile Account')
        self.assertIn('name', body['installation'])
        self.assertIn('version', body['installation'])

    def test_login_is_case_insensitive_for_email(self):
        response = self.client.post(self.login_url, {
            'email': 'MOBILE@example.com',
            'password': 'testpass123',
        }, format='json')
        self.assertEqual(response.status_code, 200)

    def test_login_rejects_wrong_password(self):
        response = self.client.post(self.login_url, {
            'email': 'mobile@example.com',
            'password': 'wrong',
        }, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['code'], 'invalid_credentials')

    def test_login_does_not_enumerate_unknown_emails(self):
        response = self.client.post(self.login_url, {
            'email': 'ghost@example.com',
            'password': 'whatever123',
        }, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['code'], 'invalid_credentials')

    def test_login_requires_verified_email(self):
        self.member.email_verified = False
        self.member.save()
        response = self.client.post(self.login_url, {
            'email': 'mobile@example.com',
            'password': 'testpass123',
        }, format='json')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['code'], 'email_not_verified')

    def test_login_never_bypasses_two_factor(self):
        from django.contrib.auth.models import Group
        group = Group.objects.create(name='two-factor-app')
        self.user.groups.add(group)
        response = self.client.post(self.login_url, {
            'email': 'mobile@example.com',
            'password': 'testpass123',
        }, format='json')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['code'], 'two_factor_required')

    def test_me_and_logout(self):
        self.authenticate()
        response = self.client.get('/api/v1/mobile/auth/me/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['user']['email'], 'mobile@example.com')

        response = self.client.post('/api/v1/mobile/auth/logout/')
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Token.objects.filter(user=self.user).exists())

        response = self.client.get('/api/v1/mobile/auth/me/')
        self.assertIn(response.status_code, (401, 403))

    def test_endpoints_require_authentication(self):
        for url in (
            '/api/v1/mobile/overview/',
            '/api/v1/mobile/clouds/',
            '/api/v1/mobile/assets/',
            '/api/v1/mobile/activity/',
            '/api/v1/mobile/notifications/',
            '/api/v1/mobile/account/',
        ):
            self.assertIn(
                self.client.get(url).status_code, (401, 403), url
            )


class MobileOverviewTestCase(MobileAPIFixtureTestCase):
    def test_overview_shape_and_counts(self):
        self.authenticate()
        AssetMonitoringState.objects.create(
            asset_key=self.server.key, account_id=self.account.id,
            provider='digitalocean', asset_type='server',
            last_checked_at=timezone.now(), last_status='active',
        )
        AssetStatusLog.objects.create(
            asset_key=self.server.key, account_id=self.account.id,
            provider='digitalocean', asset_type='server', status='off',
            timestamp=timezone.now() - timedelta(hours=2),
        )

        response = self.client.get('/api/v1/mobile/overview/')
        self.assertEqual(response.status_code, 200)
        body = response.json()

        self.assertEqual(body['clouds']['total'], 1)
        self.assertEqual(body['clouds']['active'], 1)
        self.assertEqual(body['assets']['monitored'], 1)
        self.assertEqual(body['health']['healthy'], 1)
        self.assertEqual(body['incidents_last_24h'], 1)
        self.assertEqual(body['uptime_percentage'], 100.0)
        self.assertEqual(body['providers'][0]['provider'], 'digitalocean')

    def test_overview_excludes_other_accounts(self):
        self.authenticate()
        AssetMonitoringState.objects.create(
            asset_key=self.other_server.key, account_id=self.other_account.id,
            provider='digitalocean', asset_type='server',
            last_checked_at=timezone.now(), last_status='off',
        )
        response = self.client.get('/api/v1/mobile/overview/')
        body = response.json()
        self.assertEqual(body['clouds']['total'], 1)
        self.assertEqual(body['assets']['monitored'], 1)


class MobileCloudsTestCase(MobileAPIFixtureTestCase):
    def test_cloud_list_and_detail(self):
        self.authenticate()
        response = self.client.get('/api/v1/mobile/clouds/')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['count'], 1)
        cloud = body['results'][0]
        self.assertEqual(cloud['uuid'], str(self.cloud.uuid))
        self.assertEqual(cloud['health'], 'connected')
        self.assertEqual(cloud['asset_counts']['monitored'], 1)
        self.assertFalse(cloud['syncing'])

        response = self.client.get(f"/api/v1/mobile/clouds/{self.cloud.uuid}/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['asset_counts_by_type'], {'server': 1})
        self.assertEqual(body['recent_sync_runs'], [])

    def test_cloud_patch_rename_pause_resume(self):
        self.authenticate()
        url = f"/api/v1/mobile/clouds/{self.cloud.uuid}/"

        response = self.client.patch(url, {'name': 'Renamed DO'},
                                     format='json')
        self.assertEqual(response.status_code, 200)
        self.do_account.refresh_from_db()
        self.assertEqual(self.do_account.name, 'Renamed DO')

        response = self.client.patch(url, {'action': 'pause'},
                                     format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['health'], 'disconnected')
        self.assertFalse(
            __import__('django_celery_beat').models.PeriodicTask.objects.filter(
                name=f'asset-{self.server.uuid}', enabled=True
            ).exists()
        )

        response = self.client.patch(url, {'action': 'resume'},
                                     format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['health'], 'connected')

    def test_cloud_sync_queues(self):
        self.authenticate()
        with patch(
            'apps.api.v1.mobile.views.queue_cloud_sync',
            return_value={'success': True, 'queued': True,
                          'message': 'Started cloud sync for DO Prod'},
        ) as mock_queue:
            response = self.client.post(
                f"/api/v1/mobile/clouds/{self.cloud.uuid}/sync/"
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['queued'])
        mock_queue.assert_called_once()

    def test_other_accounts_cloud_is_invisible(self):
        self.authenticate()
        response = self.client.get(f"/api/v1/mobile/clouds/{self.other_cloud.uuid}/")
        self.assertEqual(response.status_code, 404)
        response = self.client.post(
            f"/api/v1/mobile/clouds/{self.other_cloud.uuid}/sync/"
        )
        self.assertEqual(response.status_code, 404)


class MobileAssetsTestCase(MobileAPIFixtureTestCase):
    def test_asset_list_shape_filters_pagination(self):
        self.authenticate()
        response = self.client.get('/api/v1/mobile/assets/')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['count'], 1)
        asset = body['results'][0]
        self.assertEqual(asset['name'], 'api-01')
        self.assertEqual(asset['identifier'], '4242')
        self.assertEqual(asset['type'], 'server')
        self.assertEqual(asset['provider'], 'digitalocean')
        self.assertEqual(asset['region'], 'nyc3')
        self.assertIn('health', asset)
        self.assertIn('last_checked_at', asset)

        self.assertEqual(
            self.client.get('/api/v1/mobile/assets/?q=nomatch').json()['count'], 0
        )
        self.assertEqual(
            self.client.get('/api/v1/mobile/assets/?q=api').json()['count'], 1
        )
        self.assertEqual(
            self.client.get('/api/v1/mobile/assets/?provider=aws').json()['count'], 0
        )
        self.assertEqual(
            self.client.get('/api/v1/mobile/assets/?type=volume').json()['count'], 0
        )

    def test_asset_list_excludes_other_accounts(self):
        self.authenticate()
        names = [
            row['name'] for row in self.client.get('/api/v1/mobile/assets/').json()['results']
        ]
        self.assertNotIn('other-server', names)

    def test_asset_detail_redacts_metadata(self):
        self.authenticate()
        self.server.metadata = {'secret_key': 'super-secret-value', 'region': 'nyc3'}
        self.server.save()
        response = self.client.get(
            f'/api/v1/mobile/assets/digitalocean/server/{self.server.pk}/'
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertNotIn('super-secret-value', str(body['metadata']))
        self.assertIn('timeline', body)
        self.assertIn('uptime_30d', body)
        self.assertEqual(body['cloud']['uuid'], str(self.cloud.uuid))
        self.assertEqual(body['notification_emails'], ['mobile@example.com'])

    def test_asset_detail_unknown_type_404(self):
        self.authenticate()
        response = self.client.get('/api/v1/mobile/assets/digitalocean/nope/1/')
        self.assertEqual(response.status_code, 404)

    def test_asset_patch_monitoring_and_emails(self):
        self.authenticate()
        url = f'/api/v1/mobile/assets/digitalocean/server/{self.server.pk}/'

        response = self.client.patch(url, {'monitoring': 'disabled'},
                                     format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['monitoring'], 'disabled')
        self.assertEqual(response.json()['health'], 'paused')

        response = self.client.patch(url, {
            'notification_emails': ['alerts@example.com', 'ops@example.com'],
        }, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()['notification_emails'],
            ['alerts@example.com', 'ops@example.com'],
        )

        response = self.client.patch(url, {
            'notification_emails': ['not-an-email'],
        }, format='json')
        self.assertEqual(response.status_code, 400)

    def test_asset_pause_resume_check(self):
        self.authenticate()
        base = f'/api/v1/mobile/assets/digitalocean/server/{self.server.pk}'

        response = self.client.post(f'{base}/pause/')
        self.assertEqual(response.json()['monitoring'], 'disabled')
        response = self.client.post(f'{base}/resume/')
        self.assertEqual(response.json()['monitoring'], 'active')

        with patch(
            'apps.api.v1.mobile.views.check_asset_status_now',
            return_value={'status': 'active', 'timestamp': '2026-08-14T13:00:00+00:00',
                          'metadata_changes': [], 'error': None},
        ) as mock_check:
            response = self.client.post(f'{base}/check/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'active')
        self.assertEqual(response.json()['health'], 'healthy')
        mock_check.assert_called_once()

    def test_asset_actions_scope_to_account(self):
        self.authenticate()
        url = f'/api/v1/mobile/assets/digitalocean/server/{self.other_server.pk}/'
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(f'{url}pause/').status_code, 404)


class MobileActivityTestCase(MobileAPIFixtureTestCase):
    def test_activity_feed_and_filters(self):
        self.authenticate()
        AssetStatusLog.objects.create(
            asset_key=self.server.key, account_id=self.account.id,
            provider='digitalocean', asset_type='server', status='off',
            timestamp=timezone.now() - timedelta(hours=1),
        )
        AssetStatusLog.objects.create(
            asset_key=self.other_server.key, account_id=self.other_account.id,
            provider='digitalocean', asset_type='server', status='off',
            timestamp=timezone.now() - timedelta(hours=1),
        )

        response = self.client.get('/api/v1/mobile/activity/')
        body = response.json()
        self.assertEqual(body['count'], 1)
        row = body['results'][0]
        self.assertEqual(row['asset_key'], self.server.key)
        self.assertEqual(row['health'], 'down')

        self.assertEqual(
            self.client.get('/api/v1/mobile/activity/?status=active').json()['count'], 0
        )
        self.assertEqual(
            self.client.get('/api/v1/mobile/activity/?status=off').json()['count'], 1
        )

    def test_notifications_feed(self):
        self.authenticate()
        AssetStatusEmail.objects.create(
            asset_key=self.server.key, account_id=self.account.id,
            asset_id=self.server.id, provider='digitalocean', asset_type='server',
            recipient='mobile@example.com', subject='Down alert',
            text_body='body', html_body='<p>body</p>',
            status_previous='active', status_current='off',
        )
        response = self.client.get('/api/v1/mobile/notifications/')
        body = response.json()
        self.assertEqual(body['count'], 1)
        row = body['results'][0]
        self.assertEqual(row['subject'], 'Down alert')
        self.assertEqual(row['health'], 'down')


class MobileAccountTestCase(MobileAPIFixtureTestCase):
    def test_account_payload(self):
        self.authenticate()
        response = self.client.get('/api/v1/mobile/account/')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['name'], 'Mobile Account')
        self.assertEqual(body['role'], 'Owner')
        self.assertEqual(body['members_count'], 1)
        self.assertEqual(body['clouds_count'], 1)
        self.assertGreaterEqual(body['monitoring_interval'], 1)

    def test_account_rename_owner_only(self):
        self.authenticate()
        response = self.client.patch('/api/v1/mobile/account/', {'name': 'Renamed'},
                                     format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['name'], 'Renamed')

        # A non-owner member cannot rename.
        other_user = User.objects.create_user(
            username='member2', email='member2@example.com', password='pass12345',
        )
        member2 = CoreMember.objects.create(
            user=other_user, active_account=self.account, email_verified=True,
        )
        CoreAccountMembership.objects.create(
            account=self.account, member=member2,
            role=CoreAccountMembership.Role.MEMBER,
        )
        token2 = Token.objects.create(user=other_user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token2.key}')
        response = self.client.patch('/api/v1/mobile/account/', {'name': 'Nope'},
                                     format='json')
        self.assertEqual(response.status_code, 403)
