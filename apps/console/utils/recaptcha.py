"""
Optional Google reCAPTCHA v3 support.

When RECAPTCHA_PUBLIC_KEY / RECAPTCHA_PRIVATE_KEY are not configured,
verification is skipped so self-hosted instances work out of the box.
"""
import requests
from django.conf import settings


def recaptcha_enabled():
    return bool(settings.RECAPTCHA_PUBLIC_KEY and settings.RECAPTCHA_PRIVATE_KEY)


def verify_recaptcha(token, min_score=0.5):
    """
    Verify a reCAPTCHA v3 token against Google's siteverify API.
    Returns (success, error_message).
    """
    if not recaptcha_enabled():
        return True, ""

    if not token:
        return False, "Security check failed. Please try again."

    try:
        response = requests.post(
            'https://www.google.com/recaptcha/api/siteverify',
            data={
                'secret': settings.RECAPTCHA_PRIVATE_KEY,
                'response': token,
            },
            timeout=10,
        )
        result = response.json()
    except requests.RequestException:
        return False, "Security check failed. Please try again."

    if not result.get('success', False):
        return False, "Security check failed. Please try again."

    # Score: 0.0 is most likely a bot, 1.0 is most likely a human
    if result.get('score', 0.0) < min_score:
        return False, "Security check failed. Please try again."

    return True, ""
