"""
Celery tasks and helpers for the monitoring engine.

Replaces the AWS-based engine (EventBridge Scheduler -> Lambda -> DynamoDB):
- ``cloudmoo.check_asset_status`` replaces the cloudMooCheckAssetStatus Lambda
  invocation on a per-asset schedule.
- ``cloudmoo.sync_cloud_assets`` replaces the cloudMooCloudSyncAssets Lambda +
  webhook round-trip on a per-cloud schedule.
- ``cloudmoo.send_status_change_email`` replaces the
  cloudMooEmailAssetStatusChange Lambda.
- ``cloudmoo.prune_status_logs`` enforces per-account log retention.
"""
import logging
from datetime import timedelta

from celery import shared_task
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.console.account.models import CoreAccount
from apps.console.cloud.models import CloudValidationTransientError, CoreCloud
from apps.console.utils.models import UtilAsset
from apps.monitoring.checks import get_check_function
from apps.monitoring.checks.base import NON_ALERTING_STATUSES
from apps.monitoring.email import (
    EmailDeliveryError,
    deliver_status_email,
    ensure_status_change_email_outbox,
    send_status_change_emails,
)
from apps.monitoring.metadata import (
    compare_metadata,
    filter_metadata,
    redact_error_message,
    redact_sensitive_metadata,
)
from apps.monitoring.models import AssetMonitoringState, AssetStatusEmail, AssetStatusLog
from apps.monitoring.timeline import calculate_status_timeline

logger = logging.getLogger(__name__)


class CloudSyncFailed(Exception):
    """A transient cloud inventory sync failure that should be retried."""


def _serialize_timeline(status_timeline):
    """Serialize timeline entries (datetime -> ISO string) for JSON payloads."""
    return [
        {
            'timestamp': entry['timestamp'].isoformat(),
            'status': entry['status'],
            'duration': entry.get('duration'),
        }
        for entry in status_timeline
    ]


def _start_monitoring_observation(asset, provider, asset_type):
    """Reserve a monotonically increasing generation for one asset check."""
    with transaction.atomic():
        state, _created = AssetMonitoringState.objects.select_for_update().get_or_create(
            asset_key=asset.key,
            defaults={
                'account_id': asset.owner.cloud.account_id,
                'provider': provider,
                'asset_type': asset_type,
            },
        )
        state.account_id = asset.owner.cloud.account_id
        state.provider = provider
        state.asset_type = asset_type
        state.check_generation += 1
        state.check_started_at = timezone.now()
        state.save(update_fields=[
            'account_id',
            'provider',
            'asset_type',
            'check_generation',
            'check_started_at',
        ])
        return state.check_generation


def _record_monitoring_observation(
    asset,
    provider,
    asset_type,
    current_status,
    current_metadata,
    current_time,
    check_generation,
):
    """Persist one check heartbeat and any resulting state transition atomically."""
    with transaction.atomic():
        state = (
            AssetMonitoringState.objects
            .select_for_update()
            .filter(asset_key=asset.key)
            .first()
        )
        if state is None or state.check_generation != check_generation:
            logger.info(
                "Discarding stale monitoring result for %s (generation %s)",
                asset.key,
                check_generation,
            )
            return {
                'stale': True,
                'is_error': current_status in NON_ALERTING_STATUSES,
                'previous_status': state.last_status if state else None,
                'metadata_changes': None,
                'status_log': None,
                'filtered_metadata': None,
                'state_changed': False,
            }

        state.account_id = asset.owner.cloud.account_id
        state.provider = provider
        state.asset_type = asset_type

        previous_status = state.last_status or None
        previous_metadata = state.metadata
        previous_log = None
        if previous_status is None or previous_metadata is None:
            previous_log = (
                AssetStatusLog.objects
                .filter(asset_key=asset.key)
                .exclude(status__in=NON_ALERTING_STATUSES)
                .order_by('-timestamp')
                .first()
            )
            if previous_log:
                previous_status = previous_status or previous_log.status
                if previous_metadata is None:
                    previous_metadata = previous_log.metadata

        if current_status in NON_ALERTING_STATUSES:
            error_changed = state.last_error_status != current_status
            error_message = redact_error_message(current_metadata)
            state.last_checked_at = current_time
            state.last_error_status = current_status
            state.last_error_message = error_message
            state.consecutive_failures += 1
            state.save()

            error_log = None
            # Keep the audit trail compact: one row per error-state transition,
            # while the state row retains every check heartbeat and error count.
            if error_changed:
                error_log = AssetStatusLog.objects.create(
                    asset_key=asset.key,
                    account_id=asset.owner.cloud.account_id,
                    provider=provider,
                    asset_type=asset_type,
                    status=current_status,
                    timestamp=current_time,
                    error_message=error_message,
                    metadata=None,
                )

            return {
                'is_error': True,
                'previous_status': previous_status,
                'metadata_changes': None,
                'status_log': error_log,
                'filtered_metadata': None,
            }

        filtered_metadata = redact_sensitive_metadata(
            filter_metadata(current_metadata, provider, asset_type)
        )
        metadata_changes = (
            compare_metadata(previous_metadata, filtered_metadata, provider, asset_type)
            if previous_metadata is not None else None
        )
        state_changed = (
            previous_status is None
            or previous_status != current_status
            or bool(metadata_changes)
        )

        status_log = None
        notification_timeline = None
        if state_changed:
            status_log = AssetStatusLog.objects.create(
                asset_key=asset.key,
                account_id=asset.owner.cloud.account_id,
                provider=provider,
                asset_type=asset_type,
                status=current_status,
                timestamp=current_time,
                metadata=filtered_metadata,
                metadata_changes=metadata_changes,
            )

            should_notify = (
                previous_status is not None
                and previous_status not in NON_ALERTING_STATUSES
            )
            if should_notify:
                notification_timeline = calculate_status_timeline(asset.key)
                ensure_status_change_email_outbox(
                    asset,
                    previous_status,
                    current_status,
                    notification_timeline,
                    metadata_changes,
                    event_id=status_log.id,
                )

        state.last_checked_at = current_time
        state.last_success_at = current_time
        state.last_status = current_status
        state.last_error_status = ''
        state.last_error_message = ''
        state.consecutive_failures = 0
        state.metadata = filtered_metadata
        if state_changed:
            state.last_change_at = current_time
        state.save()

        return {
            'is_error': False,
            'previous_status': previous_status,
            'metadata_changes': metadata_changes,
            'status_log': status_log,
            'notification_timeline': notification_timeline,
            'filtered_metadata': filtered_metadata,
            'state_changed': state_changed,
        }


def run_status_check(asset):
    """
    Run a status check for an asset. Port of the cloudMooCheckAssetStatus
    Lambda handler flow.

    Returns a snapshot dict shaped like the old Lambda response body:
    ``asset_key``, ``unique_id``, ``asset_id``, ``asset_type``, ``provider``,
    ``status``, ``timestamp`` (ISO string), ``metadata_changes``, ``metadata``
    and ``status_timeline`` (list of dicts with ISO ``timestamp`` strings, or
    None). For non-alerting statuses (error, invalid_access_token, not_found)
    ``metadata``/``metadata_changes``/``status_timeline`` are None and an
    additional ``error`` key carries the error message.
    """
    provider = asset.provider_code
    asset_type = asset.type
    check_generation = _start_monitoring_observation(asset, provider, asset_type)

    metadata_changes = None
    status_timeline = None

    # Get current status from provider
    try:
        check_status_function = get_check_function(provider, asset_type)
        # Most providers use the control-plane token directly. Assets such
        # as DigitalOcean Spaces can require a different, provider-specific
        # credential set, exposed by the asset without changing the common
        # check function contract.
        credentials = getattr(asset, 'monitoring_credentials', None)
        if credentials is None:
            credentials = asset.owner.access_token
        current_status, current_metadata = check_status_function(
            asset.unique_id,
            credentials,
        )
    except Exception as error:
        # A provider adapter must normalize failures, but this guard ensures
        # an unexpected adapter bug still records a heartbeat and cannot leave
        # the UI showing an indefinitely healthy asset.
        logger.exception("Unexpected error checking asset %s", asset.key)
        current_status, current_metadata = 'error', str(error)

    # Timestamp the completed provider observation, not the task start. This
    # makes the freshness signal represent the last completed check.
    current_time = timezone.now()

    observation = _record_monitoring_observation(
        asset,
        provider,
        asset_type,
        current_status,
        current_metadata,
        current_time,
        check_generation,
    )

    if observation.get('stale'):
        return {
            'asset_key': asset.key,
            'unique_id': asset.unique_id,
            'asset_id': asset.id,
            'asset_type': asset_type,
            'provider': provider,
            'status': current_status,
            'timestamp': current_time.isoformat(),
            'metadata_changes': None,
            'metadata': None,
            'status_timeline': None,
            'stale': True,
        }

    # Handle non-alerting status after recording its heartbeat.
    if observation['is_error']:
        return {
            'asset_key': asset.key,
            'unique_id': asset.unique_id,
            'asset_id': asset.id,
            'asset_type': asset_type,
            'provider': provider,
            'status': current_status,
            'timestamp': current_time.isoformat(),
            'metadata_changes': None,
            'metadata': None,
            'status_timeline': None,
            'error': f'Error checking {asset_type} status: {redact_error_message(current_metadata)}',
        }

    filtered_metadata = observation['filtered_metadata']
    metadata_changes = observation['metadata_changes']
    previous_status = observation['previous_status']
    state_changed = observation['state_changed']
    status_log = observation['status_log']

    if state_changed:
        # Failed checks are deliberately non-alerting. A later healthy check
        # records recovery, but does not turn a transient API outage into a
        # false asset incident notification.
        should_notify = (
            previous_status is not None
            and previous_status not in NON_ALERTING_STATUSES
            and state_changed
        )
        if should_notify:
            status_timeline = observation.get('notification_timeline')
            if status_timeline is None:
                status_timeline = calculate_status_timeline(asset.key)
            email_args = (
                ContentType.objects.get_for_model(asset).id,
                asset.pk,
                previous_status,
                current_status,
                _serialize_timeline(status_timeline),
                metadata_changes or [],
                status_log.id if status_log else None,
            )
            try:
                send_status_change_email.delay(*email_args)
            except Exception as e:
                # No broker available (dev/test without RabbitMQ): send inline
                logger.warning(
                    f"Could not queue status change email for asset {asset.key} ({e}); sending synchronously"
                )
                send_status_change_email.run(*email_args)

    return {
        'asset_key': asset.key,
        'unique_id': asset.unique_id,
        'asset_id': asset.id,
        'asset_type': asset_type,
        'provider': provider,
        'status': current_status,
        'timestamp': current_time.isoformat(),
        'metadata_changes': metadata_changes,
        'metadata': filtered_metadata,
        'status_timeline': _serialize_timeline(status_timeline) if status_timeline else None,
    }


def check_asset_status_now(asset):
    """
    Synchronous status check for the console "Check status now" button.
    Returns the same snapshot dict as ``run_status_check``.
    """
    return run_status_check(asset)


def run_cloud_sync(cloud):
    """
    Validate cloud credentials and sync its assets. Port of the
    CloudSyncAPIView.post logic (previously reached via the
    cloudMooCloudSyncAssets Lambda -> webhook round-trip).

    Returns a dict with ``success``, ``message``, ``status_changed``,
    ``current_status`` and ``last_synced``.
    """
    try:
        with transaction.atomic():
            cloud = CoreCloud.objects.select_for_update().get(pk=cloud.pk)

            # Store original status
            original_status = cloud.status
            current_status = cloud.status

            # Validate cloud credentials
            is_valid = cloud.validate()

            if not is_valid and current_status == CoreCloud.Status.ACTIVE:
                cloud.delete_all_asset_schedules()

                # Now update cloud status
                cloud.status = CoreCloud.Status.INVALID_AUTH
                cloud.save(update_fields=['status'])

            elif is_valid:
                if current_status == CoreCloud.Status.INVALID_AUTH:
                    # If previously invalid, now valid
                    cloud.status = CoreCloud.Status.ACTIVE
                    cloud.save(update_fields=['status'])

                    # Create schedules after successful sync
                    cloud.create_all_asset_schedules()

                # Sync both an already-active cloud and one that just
                # recovered from invalid credentials. The old flow restored
                # schedules but waited for the next 15-minute beat tick before
                # importing assets again.
                if cloud.status == CoreCloud.Status.ACTIVE:
                    cloud.sync_assets()

            return {
                'success': True,
                'message': f'Successfully processed cloud {cloud.name}',
                'status_changed': original_status != cloud.status,
                'current_status': cloud.status,
                'last_synced': cloud.last_synced,
            }

    except NotImplementedError:
        return {
            'success': False,
            'not_implemented': True,
            'message': f'Asset synchronization not implemented for {cloud.provider.name}',
            'status_changed': False,
            'current_status': cloud.status,
            'last_synced': cloud.last_synced,
        }

    except CloudValidationTransientError as e:
        # A timeout, rate limit, or provider outage is not an authentication
        # failure. Keep schedules and the current cloud state intact so the
        # retry can resume monitoring when the provider recovers.
        logger.warning("Transient validation failure for cloud %s: %s", cloud.pk, e)
        return {
            'success': False,
            'retryable': True,
            'message': redact_error_message(e),
            'status_changed': False,
            'current_status': cloud.status,
            'last_synced': cloud.last_synced,
        }

    except Exception as e:
        logger.exception(f"Error processing cloud {getattr(cloud, 'pk', None)}: {str(e)}")
        return {
            'success': False,
            'message': f'Error processing cloud: {redact_error_message(e)}',
            'status_changed': False,
            'current_status': cloud.status,
            'last_synced': cloud.last_synced,
        }


@shared_task(
    name='cloudmoo.check_asset_status',
    ignore_result=True,
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=90,
    time_limit=120,
)
def check_asset_status(content_type_id, object_id):
    """Periodic per-asset status check."""
    content_type = ContentType.objects.get_for_id(content_type_id)
    try:
        asset = content_type.get_object_for_this_type(pk=object_id)
    except ObjectDoesNotExist:
        logger.info(f"Asset {content_type_id}:{object_id} no longer exists, skipping status check")
        return

    if asset.monitoring != UtilAsset.Monitoring.ACTIVE:
        return

    if asset.owner.cloud.status != CoreCloud.Status.ACTIVE:
        return

    run_status_check(asset)


@shared_task(
    name='cloudmoo.sync_cloud_assets',
    ignore_result=True,
    autoretry_for=(CloudSyncFailed,),
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={'max_retries': 5},
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=900,
    time_limit=1200,
)
def sync_cloud_assets(cloud_uuid):
    """Periodic per-cloud asset sync."""
    try:
        cloud = CoreCloud.objects.get(uuid=cloud_uuid)
    except CoreCloud.DoesNotExist:
        logger.info(f"Cloud {cloud_uuid} no longer exists, skipping asset sync")
        return

    result = run_cloud_sync(cloud)
    if not result.get('success') and not result.get('not_implemented'):
        raise CloudSyncFailed(result.get('message', f'Cloud sync failed for {cloud_uuid}'))
    return result


@shared_task(
    name='cloudmoo.send_status_change_email',
    ignore_result=True,
    autoretry_for=(EmailDeliveryError,),
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={'max_retries': 5},
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=180,
    time_limit=240,
)
def send_status_change_email(content_type_id, object_id, previous_status, current_status, status_timeline,
                             metadata_changes, event_id=None):
    """Send status-change notification emails for an asset."""
    content_type = ContentType.objects.get_for_id(content_type_id)
    try:
        asset = content_type.get_object_for_this_type(pk=object_id)
    except ObjectDoesNotExist:
        logger.info(f"Asset {content_type_id}:{object_id} no longer exists, skipping status change email")
        return

    if not asset.notification_emails:
        return

    send_status_change_emails(
        asset,
        previous_status,
        current_status,
        status_timeline,
        metadata_changes,
        event_id=event_id,
    )


@shared_task(
    name='cloudmoo.retry_pending_status_emails',
    ignore_result=True,
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=120,
    time_limit=180,
)
def retry_pending_status_emails():
    """Recover notification outbox rows after lost tasks or worker crashes."""
    now = timezone.now()
    retry_before = now - timedelta(minutes=5)
    stale_sending_before = now - timedelta(minutes=15)

    retryable = (
        Q(delivery_status__in=['pending', 'failed'], last_attempt_at__isnull=True)
        | Q(delivery_status__in=['pending', 'failed'], last_attempt_at__lt=retry_before)
        | (
            Q(delivery_status='sending')
            & (
                Q(last_attempt_at__isnull=True)
                | Q(last_attempt_at__lt=stale_sending_before)
            )
        )
    )
    deliveries = AssetStatusEmail.objects.filter(retryable).order_by('timestamp')[:100]

    recovered = 0
    for delivery in deliveries:
        try:
            if deliver_status_email(delivery):
                recovered += 1
        except Exception:
            logger.exception(
                "Error recovering status email %s for asset %s",
                delivery.pk,
                delivery.asset_key,
            )

    if recovered:
        logger.info("Recovered %s pending status-change email(s)", recovered)
    return recovered


@shared_task(
    name='cloudmoo.prune_status_logs',
    ignore_result=True,
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=300,
    time_limit=600,
)
def prune_status_logs():
    """Delete status logs and emails older than each account's retention period."""
    now = timezone.now()

    for account in CoreAccount.objects.all():
        cutoff = now - timedelta(days=account.log_retention_days)

        logs_deleted, _ = AssetStatusLog.objects.filter(
            account_id=account.id, timestamp__lt=cutoff
        ).delete()
        emails_deleted, _ = AssetStatusEmail.objects.filter(
            account_id=account.id, timestamp__lt=cutoff
        ).delete()

        if logs_deleted or emails_deleted:
            logger.info(
                f"Pruned {logs_deleted} status logs and {emails_deleted} status emails "
                f"for account {account.id} (cutoff {cutoff.isoformat()})"
            )
