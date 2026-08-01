"""
django-celery-beat schedule helpers for the monitoring engine.

Replaces the AWS EventBridge Scheduler: one PeriodicTask per monitored asset
(``cloudmoo.check_asset_status``) and one per cloud
(``cloudmoo.sync_cloud_assets``). All helpers are idempotent.

Console models are imported lazily inside the helpers so that this module can
be imported at module level from the console model modules without creating
an import cycle.
"""
import json
import logging

from django.contrib.contenttypes.models import ContentType
from django_celery_beat.models import IntervalSchedule, PeriodicTask

logger = logging.getLogger(__name__)

# How often each cloud is synced (validate credentials + sync assets)
CLOUD_SYNC_INTERVAL_MINUTES = 15

ASSET_CHECK_TASK = 'cloudmoo.check_asset_status'
CLOUD_SYNC_TASK = 'cloudmoo.sync_cloud_assets'


def _get_interval_schedule(minutes):
    interval, _ = IntervalSchedule.objects.get_or_create(
        every=minutes,
        period=IntervalSchedule.MINUTES,
    )
    return interval


def _asset_interval(asset):
    minutes = max(1, asset.owner.cloud.account.monitoring_interval)
    return _get_interval_schedule(minutes)


def _asset_kwargs(asset):
    return json.dumps({
        'content_type_id': ContentType.objects.get_for_model(asset).id,
        'object_id': asset.pk,
    })


def _cloud_kwargs(cloud):
    return json.dumps({'cloud_uuid': str(cloud.uuid)})


def _upsert_periodic_task(name, task_name, kwargs, interval, enabled):
    """Create a task or repair an existing task with the current definition."""
    task, created = PeriodicTask.objects.get_or_create(
        name=name,
        defaults={
            'task': task_name,
            'kwargs': kwargs,
            'interval': interval,
            'enabled': enabled,
        },
    )

    if not created:
        changed = False
        for field, value in (
            ('task', task_name),
            ('kwargs', kwargs),
            ('interval', interval),
            ('enabled', enabled),
        ):
            if getattr(task, field) != value:
                setattr(task, field, value)
                changed = True
        if changed:
            task.save()

    return task, created


def _cloud_schedule_enabled(cloud):
    """Keep invalid-auth clouds scheduled so credentials can recover."""
    from apps.console.cloud.models import CoreCloud

    return cloud.status in (CoreCloud.Status.ACTIVE, CoreCloud.Status.INVALID_AUTH)


def asset_schedule_create(asset):
    """Create (or fetch) the periodic status-check task for an asset."""
    task, created = _upsert_periodic_task(
        name=f'asset-{asset.uuid}',
        task_name=ASSET_CHECK_TASK,
        kwargs=_asset_kwargs(asset),
        interval=_asset_interval(asset),
        enabled=asset.monitoring == 'active',
    )
    if created:
        logger.info(f"Created status check schedule for asset {asset.key}")
    return task


def asset_schedule_update(asset):
    """
    Refresh the periodic status-check task for an asset: enabled state follows
    ``asset.monitoring``, interval/kwargs are refreshed. Creates the task when
    missing.
    """
    from apps.console.utils.models import UtilAsset

    enabled = asset.monitoring == UtilAsset.Monitoring.ACTIVE
    task, _created = _upsert_periodic_task(
        name=f'asset-{asset.uuid}',
        task_name=ASSET_CHECK_TASK,
        kwargs=_asset_kwargs(asset),
        interval=_asset_interval(asset),
        enabled=enabled,
    )
    return task


def asset_schedule_delete(asset):
    """Delete the periodic status-check task for an asset, if it exists."""
    try:
        PeriodicTask.objects.get(name=f'asset-{asset.uuid}').delete()
        logger.info(f"Deleted status check schedule for asset {asset.key}")
    except PeriodicTask.DoesNotExist:
        pass


def cloud_schedule_create(cloud):
    """Create (or fetch) the periodic asset-sync task for a cloud."""
    task, created = _upsert_periodic_task(
        name=f'cloud-{cloud.uuid}',
        task_name=CLOUD_SYNC_TASK,
        kwargs=_cloud_kwargs(cloud),
        interval=_get_interval_schedule(CLOUD_SYNC_INTERVAL_MINUTES),
        enabled=_cloud_schedule_enabled(cloud),
    )
    if created:
        logger.info(f"Created asset sync schedule for cloud {cloud.name}")
    return task


def cloud_schedule_update(cloud):
    """
    Refresh the periodic asset-sync task for a cloud: enabled state follows
    ``cloud.status``. Creates the task when missing.
    """
    enabled = _cloud_schedule_enabled(cloud)
    task, _created = _upsert_periodic_task(
        name=f'cloud-{cloud.uuid}',
        task_name=CLOUD_SYNC_TASK,
        kwargs=_cloud_kwargs(cloud),
        interval=_get_interval_schedule(CLOUD_SYNC_INTERVAL_MINUTES),
        enabled=enabled,
    )
    return task


def cloud_schedule_delete(cloud):
    """Delete the periodic asset-sync task for a cloud, if it exists."""
    try:
        PeriodicTask.objects.get(name=f'cloud-{cloud.uuid}').delete()
        logger.info(f"Deleted asset sync schedule for cloud {cloud.name}")
    except PeriodicTask.DoesNotExist:
        pass
