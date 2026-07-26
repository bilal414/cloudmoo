from django.core.cache import cache
from django.http import HttpResponseForbidden
from functools import wraps
import time


def rate_limit(key_prefix, limit=5, period=900):
    """
    Rate limiting decorator
    :param key_prefix: Prefix for the cache key
    :param limit: Number of allowed attempts
    :param period: Time period in seconds
    """

    def decorator(func):
        @wraps(func)
        def wrapped(self, request, *args, **kwargs):
            if request.method == 'POST':
                ip = request.META.get('HTTP_X_FORWARDED_FOR', request.META.get('REMOTE_ADDR'))
                if ip:
                    ip = ip.split(',')[0].strip()

                attempts_key = f"{key_prefix}_{ip}_attempts"
                expires_key = f"{key_prefix}_{ip}_expires"

                attempts = cache.get(attempts_key, 0)
                expires = cache.get(expires_key, time.time() + period)

                if time.time() > expires:
                    attempts = 0
                    expires = time.time() + period

                if attempts >= limit:
                    remaining = int(expires - time.time())
                    return HttpResponseForbidden(
                        f"Too many attempts. Please try again in {remaining // 60} minutes."
                    )

                cache.set(attempts_key, attempts + 1, period)
                cache.set(expires_key, expires, period)

            return func(self, request, *args, **kwargs)

        return wrapped

    return decorator
