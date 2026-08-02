"""Shared helpers for the provider status-check functions."""
from datetime import datetime

# Statuses that represent a failed check rather than the asset's health.
# They are recorded for troubleshooting, but are not shown as uptime states
# and do not trigger status-change notifications.
NON_ALERTING_STATUSES = ['invalid_access_token', 'error', 'not_found']

# A provider API must never be able to hold a Celery worker indefinitely.
REQUEST_TIMEOUT_SECONDS = 15


def classify_http_error(error):
    """Map a provider HTTP exception to CloudMoo's normalized status."""
    response = getattr(error, 'response', None)
    status_code = getattr(response, 'status_code', None)

    if status_code == 404:
        return 'not_found'
    if status_code in (401, 403):
        return 'invalid_access_token'
    return 'error'


def classify_aws_error(error):
    """Map common AWS client errors to CloudMoo's normalized status."""
    response = getattr(error, 'response', {}) or {}
    error_details = response.get('Error', {}) if isinstance(response, dict) else {}
    code = error_details.get('Code', '')
    message = str(error)

    not_found_codes = {
        'InvalidInstanceID.NotFound',
        'InvalidVolume.NotFound',
        'DBInstanceNotFound',
        'ResourceNotFoundException',
        'NotFoundException',
        'InvalidSnapshot.NotFound',
        'InvalidAddress.NotFound',
        'InvalidAllocationID.NotFound',
        'LoadBalancerNotFound',
        'InvalidGroupId.NotFound',
        'ServiceNotFoundException',
        'ClusterNotFoundException',
        'TaskNotFound',
    }
    auth_codes = {
        'AccessDenied',
        'AccessDeniedException',
        'AuthFailure',
        'ExpiredToken',
        'InvalidClientTokenId',
        'UnrecognizedClientException',
    }

    if code in not_found_codes or any(value in message for value in not_found_codes):
        return 'not_found'
    if code in auth_codes or any(value in message for value in auth_codes):
        return 'invalid_access_token'
    return 'error'


def _serialize_datetime(obj):
    """Recursively convert datetime objects to ISO format strings."""
    if isinstance(obj, dict):
        return {key: _serialize_datetime(value) for key, value in obj.items()}
    elif isinstance(obj, list):
        return [_serialize_datetime(item) for item in obj]
    elif isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def format_duration(duration):
    """Format a timedelta duration into a human-readable string"""
    total_seconds = int(duration.total_seconds())
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60

    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if seconds > 0 or not parts:
        parts.append(f"{seconds}s")

    return " ".join(parts)
