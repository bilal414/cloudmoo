from django.core.cache import cache
from django.http import HttpResponse
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.console.login.views import PENDING_2FA_TIMEOUT_SECONDS
from apps.console.utils.decorators import rate_limit


class _RateLimitedEndpoint:
    @rate_limit('security-hardening-test', limit=1, period=900)
    def post(self, request):
        return HttpResponse('ok')


@override_settings(
    CACHES={
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'cloudmoo-security-tests',
        },
    },
)
class RateLimitHardeningTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()
        self.endpoint = _RateLimitedEndpoint()

    @override_settings(RATE_LIMIT_TRUST_X_FORWARDED_FOR=False)
    def test_spoofed_forwarded_address_cannot_bypass_default_limit(self):
        first = self.factory.post(
            '/',
            REMOTE_ADDR='192.0.2.10',
            HTTP_X_FORWARDED_FOR='198.51.100.1',
        )
        second = self.factory.post(
            '/',
            REMOTE_ADDR='192.0.2.10',
            HTTP_X_FORWARDED_FOR='198.51.100.2',
        )

        self.assertEqual(self.endpoint.post(first).status_code, 200)
        limited = self.endpoint.post(second)
        self.assertEqual(limited.status_code, 429)
        self.assertIn('Retry-After', limited)

    @override_settings(RATE_LIMIT_TRUST_X_FORWARDED_FOR=True)
    def test_trusted_proxy_mode_uses_valid_forwarded_client_address(self):
        first = self.factory.post(
            '/',
            REMOTE_ADDR='192.0.2.10',
            HTTP_X_FORWARDED_FOR='198.51.100.1',
        )
        second = self.factory.post(
            '/',
            REMOTE_ADDR='192.0.2.10',
            HTTP_X_FORWARDED_FOR='198.51.100.2',
        )

        self.assertEqual(self.endpoint.post(first).status_code, 200)
        self.assertEqual(self.endpoint.post(second).status_code, 200)


class TwoFactorSessionHardeningTestCase(TestCase):
    def test_expired_pending_login_is_cleared(self):
        session = self.client.session
        session['pending_user_id'] = 12345
        session['two_factor_type'] = 'app'
        session['pending_2fa_started_at'] = (
            timezone.now().timestamp() - PENDING_2FA_TIMEOUT_SECONDS - 1
        )
        session.save()

        response = self.client.get(reverse('console:verify-2fa'))

        self.assertRedirects(
            response,
            reverse('console:login'),
            fetch_redirect_response=False,
        )
        refreshed_session = self.client.session
        self.assertNotIn('pending_user_id', refreshed_session)
        self.assertNotIn('two_factor_type', refreshed_session)
        self.assertNotIn('pending_2fa_started_at', refreshed_session)
