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
import random
from datetime import timedelta

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.console.account.models import CoreAccount
from apps.console.cloud.models import (
    CloudInventoryTransientError,
    CloudValidationTransientError,
    CoreCloud,
)
from apps.console.utils.models import UtilAsset
from apps.monitoring.checks import get_check_function
from apps.monitoring.checks.base import NON_ALERTING_STATUSES
from apps.monitoring.email import (
    EmailDeliveryError,
    deliver_status_email,
    ensure_status_change_email_outbox,
    send_status_change_emails,
)
from apps.monitoring.locks import cloud_sync_lock
from apps.monitoring.metadata import (
    compare_metadata,
    filter_metadata,
    redact_error_message,
    redact_sensitive_metadata,
)
from apps.monitoring.models import (
    AssetMonitoringState,
    AssetStatusEmail,
    AssetStatusLog,
    CLOUD_SYNC_RUN_TIMEOUT_SECONDS,
    CloudSyncRun,
)
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


# ---------------------------------------------------------------------------
# Distributed cloud inventory sync
#
# A full inventory pass (notably AWS, with 24+ families across all enabled
# regions) can far exceed any single task's time limit.  The periodic
# per-cloud task is therefore a short orchestrator that validates credentials
# and fans the inventory out into one ``sync_cloud_asset_family`` task per
# provider family.  A ``CloudSyncRun`` row is the cross-process lock and
# completion tracker: the family task that completes the last family
# finalizes the run (stamps ``last_synced``, recovers the cloud status, and
# reconciles every asset schedule).  Every family task always records its
# completion — even on failure — so the finalizer cannot be skipped the way
# an end-of-pass step was when the monolithic task hit its hard time limit.
# ---------------------------------------------------------------------------


def get_active_sync_run(cloud_uuid):
    """Return the cloud's in-flight sync run, expiring stale ones.

    A run stuck in ``running`` past the timeout died with its worker and is
    marked failed so the next orchestrator pass can proceed.  A run stuck in
    ``finalizing`` is finalized inline: its inventory work already completed,
    only the bookkeeping was interrupted.
    """
    cutoff = timezone.now() - timedelta(seconds=CLOUD_SYNC_RUN_TIMEOUT_SECONDS)
    run = (
        CloudSyncRun.objects
        .filter(
            cloud_uuid=cloud_uuid,
            status__in=(CloudSyncRun.Status.RUNNING, CloudSyncRun.Status.FINALIZING),
        )
        .order_by('-started_at')
        .first()
    )
    if run is None:
        return None
    if run.started_at >= cutoff:
        return run
    if run.status == CloudSyncRun.Status.FINALIZING:
        logger.warning("Finalizing expired cloud sync run %s", run.uuid)
        finalize_cloud_sync_run(str(run.uuid))
        return None
    logger.warning("Expiring stale cloud sync run %s", run.uuid)
    CloudSyncRun.objects.filter(
        pk=run.pk,
        status__in=(CloudSyncRun.Status.RUNNING, CloudSyncRun.Status.FINALIZING),
    ).update(
        status=CloudSyncRun.Status.FAILED,
        error='Sync run expired before completion',
        finished_at=timezone.now(),
    )
    return None


def _create_sync_run(cloud, families):
    """Atomically claim the cloud's sync slot and record the fan-out plan.

    Serializes competing orchestrators on the cloud row so at most one run
    per cloud is active.  Returns None when a run is already in progress.
    """
    with transaction.atomic():
        locked = CoreCloud.objects.select_for_update().get(pk=cloud.pk)
        if get_active_sync_run(locked.uuid) is not None:
            return None
        return CloudSyncRun.objects.create(
            cloud_uuid=locked.uuid,
            cloud_id=locked.pk,
            account_id=locked.account_id,
            provider=locked.provider.code.lower(),
            families_total=len(families),
        )


def _record_family_error(run_uuid, family_key, region, error):
    """Append a bounded error entry for a failed family without aborting the run."""
    with transaction.atomic():
        run = CloudSyncRun.objects.select_for_update().filter(uuid=run_uuid).first()
        if run is None or run.status != CloudSyncRun.Status.RUNNING:
            return
        errors = list(run.family_errors or [])
        errors.append({
            'family': family_key,
            'region': region or '',
            'error': str(error)[:500],
        })
        run.family_errors = errors[-50:]
        run.save(update_fields=['family_errors'])


def _complete_sync_family(run_uuid, family_key, region):
    """Idempotently mark a family done; the last completion finalizes the run."""
    marker = f'{family_key}|{region or ""}'
    should_finalize = False
    with transaction.atomic():
        run = CloudSyncRun.objects.select_for_update().filter(uuid=run_uuid).first()
        if run is None or run.status != CloudSyncRun.Status.RUNNING:
            return
        done = list(run.families_done or [])
        if marker in done:
            # Redelivery after a worker died between commit and ack.
            return
        done.append(marker)
        run.families_done = done
        update_fields = ['families_done']
        if len(done) >= run.families_total:
            run.status = CloudSyncRun.Status.FINALIZING
            update_fields.append('status')
            should_finalize = True
        run.save(update_fields=update_fields)
    if should_finalize:
        finalize_cloud_sync_run(run_uuid)


def finalize_cloud_sync_run(run_uuid):
    """Stamp results and reconcile schedules once every family completed.

    This is the step the monolithic sync task could never reach after a hard
    time-limit kill: tombstoned assets lose their check schedules and new
    assets gain theirs here, so reconciliation now runs on every completed
    pass regardless of how long the provider calls took.
    """
    run = CloudSyncRun.objects.filter(uuid=run_uuid).first()
    if run is None:
        return

    try:
        cloud = CoreCloud.objects.select_related('provider').get(uuid=run.cloud_uuid)
    except CoreCloud.DoesNotExist:
        CloudSyncRun.objects.filter(pk=run.pk).update(
            status=CloudSyncRun.Status.FAILED,
            error='Cloud was removed while the sync was running',
            finished_at=timezone.now(),
        )
        return

    completed_at = timezone.now()
    CoreCloud.objects.filter(pk=cloud.pk).update(last_synced=completed_at)
    try:
        provider_account = cloud.provider_account
    except (AttributeError, NotImplementedError):
        provider_account = None
    if provider_account is not None:
        type(provider_account).objects.filter(pk=provider_account.pk).update(
            last_synced=completed_at
        )

    # A pause/suspension made while the families ran wins over this worker's
    # in-memory snapshot; only INVALID_AUTH recovers to ACTIVE here.
    with transaction.atomic():
        current = CoreCloud.objects.select_for_update().get(pk=cloud.pk)
        if current.status == CoreCloud.Status.INVALID_AUTH:
            current.status = CoreCloud.Status.ACTIVE
            current.save(update_fields=['status'])
        final_status = current.status

    error = ''
    try:
        if final_status == CoreCloud.Status.ACTIVE:
            current.create_all_asset_schedules()
        else:
            current.delete_all_asset_schedules()
    except Exception as e:
        logger.exception("Could not reconcile asset schedules for cloud %s", cloud.pk)
        error = f'Schedule reconciliation failed: {redact_error_message(e)}'

    run.refresh_from_db(fields=['family_errors'])
    has_errors = bool(run.family_errors) or bool(error)
    CloudSyncRun.objects.filter(pk=run.pk).update(
        status=(
            CloudSyncRun.Status.PARTIAL if has_errors else CloudSyncRun.Status.SUCCESS
        ),
        error=error,
        finished_at=timezone.now(),
    )


def _mark_cloud_invalid(cloud_pk, original_status):
    """Shared INVALID_AUTH transition for both sync entry points."""
    syncable_statuses = (
        CoreCloud.Status.ACTIVE,
        CoreCloud.Status.INVALID_AUTH,
    )
    with transaction.atomic():
        current = CoreCloud.objects.select_for_update().get(pk=cloud_pk)
        if (
            current.status in syncable_statuses
            and current.status != CoreCloud.Status.INVALID_AUTH
        ):
            current.status = CoreCloud.Status.INVALID_AUTH
            current.save(update_fields=['status'])
        final_status = current.status
        last_synced = current.last_synced

    # Also run this for an already-invalid cloud to repair any status-check
    # schedules left behind by a prior interruption.
    current.delete_all_asset_schedules()
    return {
        'success': True,
        'message': f'Cloud credentials are invalid for {current.name}',
        'status_changed': original_status != final_status,
        'current_status': final_status,
        'last_synced': last_synced,
    }


def start_distributed_cloud_sync(cloud):
    """Validate the cloud and fan its inventory sync out into family tasks.

    Mirrors the validate/status semantics of ``run_cloud_sync`` but returns
    as soon as the sync run is queued; family tasks and the finalizer do the
    provider work.  Returns the same result dict shape as ``run_cloud_sync``
    plus ``queued``/``run_uuid`` keys.
    """
    cloud_pk = getattr(cloud, 'pk', None)
    cloud_uuid = getattr(cloud, 'uuid', None)
    syncable_statuses = (
        CoreCloud.Status.ACTIVE,
        CoreCloud.Status.INVALID_AUTH,
    )
    try:
        cloud = CoreCloud.objects.select_related('provider').get(pk=cloud_pk)
        original_status = cloud.status
        if cloud.status not in syncable_statuses:
            return {
                'success': True,
                'skipped': True,
                'message': f'Cloud sync skipped while status is {cloud.status}',
                'status_changed': False,
                'current_status': cloud.status,
                'last_synced': cloud.last_synced,
            }

        # Skip provider calls entirely while another run holds the slot; the
        # atomic re-check in ``_create_sync_run`` closes the race.
        if get_active_sync_run(cloud_uuid) is not None:
            return {
                'success': True,
                'skipped': True,
                'message': 'A cloud inventory sync is already in progress',
                'status_changed': False,
                'current_status': cloud.status,
                'last_synced': cloud.last_synced,
            }

        # Provider calls intentionally run outside a database transaction.
        is_valid = cloud.validate()

        if not is_valid:
            return _mark_cloud_invalid(cloud_pk, original_status)

        # Re-check state immediately before the fan-out: a pause made during
        # validation takes effect without waiting for the provider sync.
        cloud.refresh_from_db(fields=['status', 'last_synced'])
        if cloud.status not in syncable_statuses:
            cloud.delete_all_asset_schedules()
            return {
                'success': True,
                'skipped': True,
                'message': f'Cloud sync skipped while status is {cloud.status}',
                'status_changed': original_status != cloud.status,
                'current_status': cloud.status,
                'last_synced': cloud.last_synced,
            }

        families = cloud.provider_account.sync_asset_families()
        run = _create_sync_run(cloud, families)
        if run is None:
            current = CoreCloud.objects.only('status', 'last_synced').get(pk=cloud_pk)
            return {
                'success': True,
                'skipped': True,
                'message': 'A cloud inventory sync is already in progress',
                'status_changed': False,
                'current_status': current.status,
                'last_synced': current.last_synced,
            }

        for family_key, region in families:
            sync_cloud_asset_family.delay(str(cloud_uuid), str(run.uuid), family_key, region)

        return {
            'success': True,
            'queued': True,
            'run_uuid': str(run.uuid),
            'message': f'Successfully started cloud sync for {cloud.name}',
            'status_changed': False,
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
        logger.warning(
            "Transient validation failure for cloud %s: %s",
            cloud_pk,
            redact_error_message(e),
        )
        return {
            'success': False,
            'retryable': True,
            'message': redact_error_message(e),
            'status_changed': False,
            'current_status': cloud.status,
            'last_synced': cloud.last_synced,
        }

    except CoreCloud.DoesNotExist:
        logger.info("Cloud %s was removed while its inventory sync was starting", cloud_pk)
        return {
            'success': True,
            'skipped': True,
            'message': 'Cloud was removed before synchronization started',
            'status_changed': False,
            'current_status': None,
            'last_synced': None,
        }

    except Exception as e:
        logger.exception(
            "Error starting sync for cloud %s (%s)",
            cloud_pk,
            type(e).__name__,
        )
        return {
            'success': False,
            'message': f'Error processing cloud: {redact_error_message(e)}',
            'status_changed': False,
            'current_status': cloud.status,
            'last_synced': cloud.last_synced,
        }


def queue_cloud_sync(cloud):
    """Start a cloud inventory sync, distributed when a broker is available.

    Without a reachable broker (development, tests) the sync runs inline so
    those environments keep working without RabbitMQ.
    """
    try:
        sync_cloud_assets.delay(str(cloud.uuid))
        return {
            'success': True,
            'queued': True,
            'message': f'Started cloud sync for {cloud.name}',
            'status_changed': False,
            'current_status': cloud.status,
            'last_synced': cloud.last_synced,
        }
    except Exception as e:
        logger.warning(
            "Could not queue sync for cloud %s (%s); running inline",
            cloud.pk,
            e,
        )
        return run_cloud_sync(cloud)


def run_cloud_sync(cloud):
    """
    Validate cloud credentials and sync its assets. Port of the
    CloudSyncAPIView.post logic (previously reached via the
    cloudMooCloudSyncAssets Lambda -> webhook round-trip).

    Returns a dict with ``success``, ``message``, ``status_changed``,
    ``current_status`` and ``last_synced``.
    """
    cloud_pk = getattr(cloud, 'pk', None)
    cloud_uuid = getattr(cloud, 'uuid', None)
    try:
        with cloud_sync_lock(cloud_uuid) as acquired:
            if not acquired:
                current = CoreCloud.objects.filter(pk=cloud_pk).only(
                    'status', 'last_synced'
                ).first()
                return {
                    'success': True,
                    'skipped': True,
                    'message': 'A cloud inventory sync is already in progress',
                    'status_changed': False,
                    'current_status': current.status if current else None,
                    'last_synced': current.last_synced if current else None,
                }

            cloud = CoreCloud.objects.select_related('provider').get(pk=cloud_pk)
            original_status = cloud.status
            syncable_statuses = (
                CoreCloud.Status.ACTIVE,
                CoreCloud.Status.INVALID_AUTH,
            )
            if cloud.status not in syncable_statuses:
                return {
                    'success': True,
                    'skipped': True,
                    'message': f'Cloud sync skipped while status is {cloud.status}',
                    'status_changed': False,
                    'current_status': cloud.status,
                    'last_synced': cloud.last_synced,
                }

            # A distributed sync run holds no advisory lock; its run row is
            # the mutual-exclusion record this inline path must respect.
            if get_active_sync_run(cloud_uuid) is not None:
                return {
                    'success': True,
                    'skipped': True,
                    'message': 'A cloud inventory sync is already in progress',
                    'status_changed': False,
                    'current_status': cloud.status,
                    'last_synced': cloud.last_synced,
                }

            # Provider calls intentionally run outside a database transaction.
            # Some full inventory passes take many minutes and must not hold a
            # row lock or an open transaction for their entire duration.
            is_valid = cloud.validate()

            if not is_valid:
                return _mark_cloud_invalid(cloud_pk, original_status)

            # Re-check state immediately before the expensive inventory pass.
            # A pause made during validation should take effect without waiting
            # for the provider sync to finish.
            cloud.refresh_from_db(fields=['status', 'last_synced'])
            if cloud.status not in syncable_statuses:
                cloud.delete_all_asset_schedules()
                return {
                    'success': True,
                    'skipped': True,
                    'message': f'Cloud sync skipped while status is {cloud.status}',
                    'status_changed': original_status != cloud.status,
                    'current_status': cloud.status,
                    'last_synced': cloud.last_synced,
                }

            cloud.sync_assets()

            # Only recover INVALID_AUTH to ACTIVE. A user or administrator may
            # have paused/suspended the cloud while the provider call ran; that
            # newer state must win over this worker's stale in-memory object.
            with transaction.atomic():
                current = CoreCloud.objects.select_for_update().get(pk=cloud_pk)
                if current.status == CoreCloud.Status.INVALID_AUTH:
                    current.status = CoreCloud.Status.ACTIVE
                    current.save(update_fields=['status'])
                final_status = current.status
                last_synced = current.last_synced

            if final_status == CoreCloud.Status.ACTIVE:
                current.create_all_asset_schedules()
            else:
                current.delete_all_asset_schedules()

            return {
                'success': True,
                'message': f'Successfully processed cloud {current.name}',
                'status_changed': original_status != final_status,
                'current_status': final_status,
                'last_synced': last_synced,
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
        logger.warning(
            "Transient validation failure for cloud %s: %s",
            cloud.pk,
            redact_error_message(e),
        )
        return {
            'success': False,
            'retryable': True,
            'message': redact_error_message(e),
            'status_changed': False,
            'current_status': cloud.status,
            'last_synced': cloud.last_synced,
        }

    except CoreCloud.DoesNotExist:
        logger.info("Cloud %s was removed while its inventory sync was running", cloud_pk)
        return {
            'success': True,
            'skipped': True,
            'message': 'Cloud was removed before synchronization completed',
            'status_changed': False,
            'current_status': None,
            'last_synced': None,
        }

    except Exception as e:
        logger.exception(
            "Error processing cloud %s (%s)",
            cloud_pk,
            type(e).__name__,
        )
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

    # Beat enqueues one message per interval with no dedup, and a drained
    # backlog can hold many copies of the same check.  A heartbeat fresher
    # than half the interval means this delivery is a duplicate — skip the
    # provider call instead of hammering the provider API.  The half-interval
    # slack keeps normally scheduled deliveries (which land just under one
    # full interval after the previous check) running.
    interval_minutes = max(1, asset.owner.cloud.account.monitoring_interval)
    fresh_after = timezone.now() - timedelta(minutes=interval_minutes / 2)
    last_checked_at = (
        AssetMonitoringState.objects
        .filter(asset_key=asset.key)
        .values_list('last_checked_at', flat=True)
        .first()
    )
    if last_checked_at is not None and last_checked_at >= fresh_after:
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
    soft_time_limit=180,
    time_limit=300,
)
def sync_cloud_assets(cloud_uuid):
    """Periodic per-cloud sync orchestrator: validate, then fan out per family.

    The orchestrator itself is deliberately short — the provider inventory
    work happens in ``sync_cloud_asset_family`` tasks so no single task can
    outlive its time limit at AWS-scale inventories.
    """
    try:
        cloud = CoreCloud.objects.get(uuid=cloud_uuid)
    except CoreCloud.DoesNotExist:
        logger.info(f"Cloud {cloud_uuid} no longer exists, skipping asset sync")
        return

    result = start_distributed_cloud_sync(cloud)
    if not result.get('success') and not result.get('not_implemented'):
        raise CloudSyncFailed(result.get('message', f'Cloud sync failed for {cloud_uuid}'))
    return result


@shared_task(
    name='cloudmoo.sync_cloud_asset_family',
    ignore_result=True,
    bind=True,
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=600,
    time_limit=900,
    max_retries=2,
)
def sync_cloud_asset_family(self, cloud_uuid, run_uuid, family_key, region=None):
    """Run one task-sized inventory family of a distributed cloud sync.

    Never propagates provider failures past its bounded retries: the error is
    recorded on the run and the family marker is always completed, so the
    finalizer (status recovery + schedule reconciliation) always executes.
    """
    run = CloudSyncRun.objects.filter(uuid=run_uuid).first()
    if (
        run is None
        or str(run.cloud_uuid) != str(cloud_uuid)
        or run.status != CloudSyncRun.Status.RUNNING
    ):
        logger.info(
            "Sync run %s is no longer active, skipping family %s",
            run_uuid,
            family_key,
        )
        return

    try:
        cloud = CoreCloud.objects.get(uuid=cloud_uuid)
    except CoreCloud.DoesNotExist:
        logger.info(
            "Cloud %s was removed while family %s was queued",
            cloud_uuid,
            family_key,
        )
        _complete_sync_family(run_uuid, family_key, region)
        return

    try:
        cloud.provider_account.sync_asset_family(family_key, region)
    except SoftTimeLimitExceeded:
        logger.warning(
            "Family %s of sync run %s exceeded its time limit",
            family_key,
            run_uuid,
        )
        _record_family_error(run_uuid, family_key, region, 'Family sync exceeded its time limit')
    except (CloudValidationTransientError, CloudInventoryTransientError) as e:
        if self.request.retries < 2:
            countdown = 60 * (2 ** self.request.retries) + random.uniform(0, 30)
            raise self.retry(exc=e, countdown=countdown)
        logger.warning(
            "Family %s of sync run %s failed after retries: %s",
            family_key,
            run_uuid,
            redact_error_message(e),
        )
        _record_family_error(run_uuid, family_key, region, redact_error_message(e))
    except Exception as e:
        logger.exception(
            "Error syncing family %s of run %s",
            family_key,
            run_uuid,
        )
        _record_family_error(run_uuid, family_key, region, redact_error_message(e))

    _complete_sync_family(run_uuid, family_key, region)


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
