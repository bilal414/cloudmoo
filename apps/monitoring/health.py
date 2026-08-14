"""Normalized health buckets and uptime math for asset status data.

Provider checks return provider-native status words (``running``, ``active``,
``off`` ...).  The console shows those raw values; API consumers (mobile
apps) additionally get a normalized ``health`` bucket from this module so
every client renders the same coarse state.  The raw status is always
returned alongside — the bucket is a display aid, not a lossy replacement.
"""
from datetime import timedelta

from django.utils import timezone

from apps.monitoring.checks.base import NON_ALERTING_STATUSES
from apps.monitoring.models import AssetStatusLog

# Provider-native statuses that mean "the resource is serving".
UP_STATUSES = frozenset({
    'active', 'applied', 'assigned', 'attached', 'available', 'complete',
    'completed', 'created', 'deployed', 'enabled', 'healthy', 'in-use',
    'insync', 'normal', 'ok', 'online', 'present', 'provisioned', 'ready',
    'running', 'started', 'up',
})

# Still serving, but worth attention (rendered as "warning").
WARNING_STATUSES = frozenset({
    'creating', 'degraded', 'impaired', 'maintenance', 'migrating', 'pending',
    'provisioning', 'rebooting', 'scaling', 'starting', 'updating', 'warning',
})

# Not serving.
DOWN_STATUSES = frozenset({
    'deleted', 'down', 'failed', 'failure', 'off', 'shutting-down',
    'stopped', 'stopping', 'terminated', 'unhealthy', 'unreachable',
})

HEALTH_HEALTHY = 'healthy'
HEALTH_WARNING = 'warning'
HEALTH_DOWN = 'down'
HEALTH_PAUSED = 'paused'
HEALTH_GONE = 'gone'
HEALTH_UNKNOWN = 'unknown'


def classify_health(status, monitoring='active', stale=False):
    """Map a raw provider status onto a normalized health bucket.

    ``monitoring`` is the asset's ``UtilAsset.Monitoring`` value and wins over
    any status: paused and removed resources are not health states.  Unknown
    is the honest bucket for missing/stale/diagnostic statuses.
    """
    if monitoring == 'no_longer_exists':
        return HEALTH_GONE
    if monitoring != 'active':
        return HEALTH_PAUSED
    if stale or not status or status == HEALTH_UNKNOWN:
        return HEALTH_UNKNOWN
    normalized = str(status).lower()
    if normalized in NON_ALERTING_STATUSES:
        return HEALTH_UNKNOWN
    if normalized in UP_STATUSES:
        return HEALTH_HEALTHY
    if normalized in WARNING_STATUSES:
        return HEALTH_WARNING
    if normalized in DOWN_STATUSES:
        return HEALTH_DOWN
    return HEALTH_UNKNOWN


def _counts_as_up(status):
    return str(status).lower() in UP_STATUSES | WARNING_STATUSES


def calculate_uptime(asset_key, days=30, now=None):
    """Percentage of the window an asset spent in a serving status.

    Computed from the durable status-change log: each log row's status holds
    until the next row.  Time before the first in-window row inherits the
    last status observed before the window; with no data at all the result is
    None (not 0%, which would falsely claim an outage).
    """
    now = now or timezone.now()
    start = now - timedelta(days=days)

    logs = list(
        AssetStatusLog.objects
        .filter(asset_key=asset_key, timestamp__gte=start)
        .exclude(status__in=NON_ALERTING_STATUSES)
        .order_by('timestamp')
        .values_list('timestamp', 'status')[:10000]
    )

    seed_status = (
        AssetStatusLog.objects
        .filter(asset_key=asset_key, timestamp__lt=start)
        .exclude(status__in=NON_ALERTING_STATUSES)
        .order_by('-timestamp')
        .values_list('status', flat=True)
        .first()
    )

    if not logs and seed_status is None:
        return None

    up_seconds = 0.0
    previous_timestamp = start
    previous_status = seed_status  # None when the window opens with no history

    for timestamp, status in logs:
        if previous_status is not None and _counts_as_up(previous_status):
            up_seconds += (timestamp - previous_timestamp).total_seconds()
        previous_timestamp, previous_status = timestamp, status

    if previous_status is not None and _counts_as_up(previous_status):
        up_seconds += (now - previous_timestamp).total_seconds()

    total_seconds = (now - start).total_seconds()
    if total_seconds <= 0:
        return None
    return round(100 * up_seconds / total_seconds, 2)
