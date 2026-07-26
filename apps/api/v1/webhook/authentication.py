import hmac

from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from django.conf import settings


class APIKeyAuthentication(BaseAuthentication):
    def authenticate(self, request):
        if not settings.CLOUDMOO_API_KEY:
            raise AuthenticationFailed('API key authentication is not configured')

        api_key = request.META.get('HTTP_X_API_KEY')
        if not api_key:
            raise AuthenticationFailed('API key not provided')

        if not hmac.compare_digest(api_key, settings.CLOUDMOO_API_KEY):
            raise AuthenticationFailed('Invalid API key')

        # Return None for user since we don't have a user context
        return (None, None)
