import hashlib
import ipaddress
import time
from functools import wraps

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse


def _client_address(request):
    """Return a validated client address without trusting spoofable headers."""
    candidate = request.META.get("REMOTE_ADDR", "")
    if settings.RATE_LIMIT_TRUST_X_FORWARDED_FOR:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            candidate = forwarded.split(",", 1)[0].strip()

    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return "unknown"


def _rate_limit_key(key_prefix, request, window):
    address_digest = hashlib.sha256(_client_address(request).encode()).hexdigest()[:24]
    return f"rate-limit:{key_prefix}:{address_digest}:{window}"


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
                now = time.time()
                window = int(now // period)
                attempts_key = _rate_limit_key(key_prefix, request, window)
                timeout = max(1, int(period - (now % period)) + 1)

                if cache.add(attempts_key, 1, timeout=timeout):
                    attempts = 1
                else:
                    try:
                        attempts = cache.incr(attempts_key)
                    except ValueError:
                        # The key may expire between add() and incr(). Start the
                        # new window rather than turning authentication into a
                        # server error.
                        cache.set(attempts_key, 1, timeout=timeout)
                        attempts = 1

                if attempts > limit:
                    response = HttpResponse(
                        "Too many attempts. Please try again later.",
                        status=429,
                    )
                    response["Retry-After"] = str(timeout)
                    return response

            return func(self, request, *args, **kwargs)

        return wrapped

    return decorator
