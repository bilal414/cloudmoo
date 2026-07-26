from django.conf import settings
from django.urls import resolve


def active_url_processor(request):
    try:
        # Get the current URL name
        url_name = resolve(request.path_info).url_name
        namespace = resolve(request.path_info).namespace

        # Map URL names to sidebar items
        url_mapping = {
            'index': 'dashboard',
            'list': {
                'console:asset': 'assets',
                'console:cloud': 'clouds'
            },
            'detail': {
                'console:asset': 'assets',
                'console:cloud': 'clouds'
            },
            'edit': {
                'console:asset': 'assets',
                'console:cloud': 'clouds'
            },
            'connect': 'connect',
            'console:security:profile': 'security_profile',
            'console:security:app_two_factor': 'security_2fa',
            'console:security:password_change': 'security_password',
            'console:security:disable_app_two_factor': 'security_2fa',
            'console:security:setup_app_two_factor': 'security_2fa',
        }

        # Handle list views specifically
        if url_name == 'list' and namespace:
            active_url = url_mapping['list'].get(namespace, '')
        elif url_name == 'detail' and namespace:
            active_url = url_mapping['list'].get(namespace, '')
        elif url_name == 'edit' and namespace:
            active_url = url_mapping['list'].get(namespace, '')
        else:
            # Get the mapped name or the original URL name
            active_url = url_mapping.get(f"{namespace}:{url_name}" if namespace else url_name, '')
            if not active_url:
                active_url = url_mapping.get(url_name, '')

        return {'active_url': active_url}
    except Exception:
        return {'active_url': ''}


def recaptcha_settings(request):
    """Expose reCAPTCHA configuration to templates (empty when not configured)."""
    return {
        'recaptcha_enabled': bool(settings.RECAPTCHA_PUBLIC_KEY and settings.RECAPTCHA_PRIVATE_KEY),
        'recaptcha_site_key': settings.RECAPTCHA_PUBLIC_KEY,
    }
