"""Shared helpers for the provider status-check functions."""
from datetime import datetime

# Statuses that should not trigger email notifications
NON_ALERTING_STATUSES = ['invalid_access_token', 'error', 'not_found']


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
