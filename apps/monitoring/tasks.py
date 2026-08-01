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
from django.utils import timezone

from apps.console.account.models import CoreAccount
from apps.console.cloud.models import CoreCloud
from apps.console.utils.models import UtilAsset
from apps.monitoring.checks import get_check_function
from apps.monitoring.checks.base import NON_ALERTING_STATUSES
from apps.monitoring.email import send_status_change_emails
from apps.monitoring.metadata import compare_metadata, filter_metadata
from apps.monitoring.models import AssetStatusEmail, AssetStatusLog
from apps.monitoring.timeline import calculate_status_timeline

logger = logging.getLogger(__name__)


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
    check_status_function = get_check_function(provider, asset_type)

    current_time = timezone.now()
    metadata_changes = None
    status_timeline = None

    # Get current status from provider
    current_status, current_metadata = check_status_function(asset.unique_id, asset.owner.access_token)

    # Handle non-alerting status first
    if current_status in NON_ALERTING_STATUSES:
        AssetStatusLog.objects.create(
            asset_key=asset.key,
            account_id=asset.owner.cloud.account_id,
            provider=provider,
            asset_type=asset_type,
            status=current_status,
            timestamp=current_time,
            error_message=str(current_metadata),
            metadata=None,
        )
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
            'error': f'Error checking {asset_type} status: {current_metadata}',
        }

    # Filter the metadata to only include important fields
    filtered_metadata = filter_metadata(current_metadata, provider, asset_type)

    # Get previous status data
    previous_log = (
        AssetStatusLog.objects
        .filter(asset_key=asset.key)
        .order_by('-timestamp')
        .first()
    )

    if previous_log and previous_log.metadata:
        # Compare metadata to find changes
        metadata_changes = compare_metadata(previous_log.metadata, filtered_metadata, provider, asset_type)

        # Only notify if there are metadata changes AND this is not the first entry
        if metadata_changes:
            # Store the new status with filtered metadata
            AssetStatusLog.objects.create(
                asset_key=asset.key,
                account_id=asset.owner.cloud.account_id,
                provider=provider,
                asset_type=asset_type,
                status=current_status,
                timestamp=current_time,
                metadata=filtered_metadata,
                metadata_changes=metadata_changes,
            )

            status_timeline = calculate_status_timeline(asset.key)
            email_args = (
                ContentType.objects.get_for_model(asset).id,
                asset.pk,
                previous_log.status,
                current_status,
                _serialize_timeline(status_timeline),
                metadata_changes,
            )
            try:
                send_status_change_email.delay(*email_args)
            except Exception as e:
                # No broker available (dev/test without RabbitMQ): send inline
                logger.warning(
                    f"Could not queue status change email for asset {asset.key} ({e}); sending synchronously"
                )
                send_status_change_email.run(*email_args)
    else:
        # Store the new status with filtered metadata
        AssetStatusLog.objects.create(
            asset_key=asset.key,
            account_id=asset.owner.cloud.account_id,
            provider=provider,
            asset_type=asset_type,
            status=current_status,
            timestamp=current_time,
            metadata=filtered_metadata,
        )

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

                if current_status == CoreCloud.Status.ACTIVE:
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

    except Exception as e:
        logger.exception(f"Error processing cloud {getattr(cloud, 'pk', None)}: {str(e)}")
        return {
            'success': False,
            'message': f'Error processing cloud: {str(e)}',
            'status_changed': False,
            'current_status': cloud.status,
            'last_synced': cloud.last_synced,
        }


@shared_task(name='cloudmoo.check_asset_status', ignore_result=True)
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


@shared_task(name='cloudmoo.sync_cloud_assets', ignore_result=True)
def sync_cloud_assets(cloud_uuid):
    """Periodic per-cloud asset sync."""
    try:
        cloud = CoreCloud.objects.get(uuid=cloud_uuid)
    except CoreCloud.DoesNotExist:
        logger.info(f"Cloud {cloud_uuid} no longer exists, skipping asset sync")
        return

    return run_cloud_sync(cloud)


@shared_task(name='cloudmoo.send_status_change_email', ignore_result=True)
def send_status_change_email(content_type_id, object_id, previous_status, current_status, status_timeline,
                             metadata_changes):
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
    )


@shared_task(name='cloudmoo.prune_status_logs', ignore_result=True)
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
