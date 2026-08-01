"""
Status timeline calculation.

ORM port of ``calculate_status_timeline`` from the cloudMooCheckAssetStatus
Lambda (DynamoDB -> PostgreSQL).
"""
from datetime import timedelta

from django.utils import timezone

from apps.monitoring.checks.base import NON_ALERTING_STATUSES, format_duration
from apps.monitoring.models import AssetStatusLog

# Statuses excluded from the timeline (transient check failures)
TIMELINE_EXCLUDED_STATUSES = NON_ALERTING_STATUSES

# Reasonable limit to prevent runaway queries
MAX_TIMELINE_ITEMS = 10000


def calculate_status_timeline(asset_key, days=30):
    """
    Calculate the status timeline for an asset, only including status changes.

    Returns a list of dicts (newest first) with keys:
      - timestamp: timezone-aware datetime of the status change
      - status: the status value
      - duration: human-readable duration the asset stayed in that status
    """
    end_date = timezone.now()
    start_date = end_date - timedelta(days=days)

    logs = (
        AssetStatusLog.objects
        .filter(asset_key=asset_key, timestamp__gte=start_date)
        .exclude(status__in=TIMELINE_EXCLUDED_STATUSES)
        .order_by('-timestamp')
        .values_list('timestamp', 'status')[:MAX_TIMELINE_ITEMS]
    )

    # Collapse consecutive equal statuses into change entries (newest first)
    status_changes = []
    previous_status = None
    for timestamp, status in logs:
        if previous_status is None or status != previous_status:
            status_changes.append({
                'timestamp': timestamp,
                'status': status,
            })
        previous_status = status

    # Calculate durations (data is already in correct order)
    current_time = end_date  # Use the same timestamp for consistency
    for i in range(len(status_changes)):
        if i == 0:
            # First entry: duration from its timestamp to now
            duration = current_time - status_changes[i]['timestamp']
        else:
            # Subsequent entries: duration from this timestamp to the previous entry's timestamp
            duration = status_changes[i - 1]['timestamp'] - status_changes[i]['timestamp']
        status_changes[i]['duration'] = format_duration(duration)

    return status_changes
