"""Throttles for the mobile API."""
from rest_framework.throttling import AnonRateThrottle


class MobileLoginThrottle(AnonRateThrottle):
    """10 login attempts per 15 minutes per IP, mirroring the console login
    rate limit (``rate_limit('login', limit=10, period=900)``).  DRF's rate
    string cannot express multi-digit durations, so the limit is explicit."""

    scope = 'mobile_login'

    def parse_rate(self, rate):
        return 10, 900
