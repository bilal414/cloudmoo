import logging
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.text import slugify
from model_utils.models import TimeStampedModel

from apps.console.account.models import CoreAccountMembership
from apps.monitoring import schedules
from apps.monitoring.checks.base import format_duration
from apps.monitoring.models import AssetStatusEmail, AssetStatusLog

logger = logging.getLogger(__name__)


class UtilCloud(TimeStampedModel):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        DISABLED = "disabled", "Disabled"

    name = models.CharField(max_length=255)
    status = models.CharField(max_length=64, choices=Status.choices, default=Status.ACTIVE)
    last_synced = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(null=True)
    notes = models.TextField(null=True, blank=True)

    class Meta:
        abstract = True


class AssetQuerySet(models.QuerySet):
    def for_user(self, user):
        if user.is_superuser:
            return self
        return self.filter(owner__cloud__account=user.member.active_account)


class AssetManager(models.Manager):
    def get_queryset(self):
        return AssetQuerySet(self.model, using=self._db)

    def for_user(self, user):
        return self.get_queryset().for_user(user)


class UtilAsset(TimeStampedModel):
    objects = AssetManager()

    class Monitoring(models.TextChoices):
        ACTIVE = "active", "Active"
        DISABLED = "disabled", "Disabled"
        NO_LONGER_EXISTS = "no_longer_exists", "No Longer Exists"

    class Type(models.TextChoices):
        SERVER = "server", "Server"
        VOLUME = "volume", "Volume"
        DATABASE = "database", "Database"
        RDS_DATABASE = "rds_database", "RDS Database"
        LAMBDA = "lambda", "Lambda Function"
        DYNAMODB = "dynamodb", "DynamoDB Table"
        S3_BUCKET = "s3_bucket", "S3 Bucket"
        ACM_CERTIFICATE = "acm_certificate", "ACM Certificate"
        SNAPSHOT = "snapshot", "Snapshot"
        ELASTIC_IP = "elastic_ip", "Elastic IP"
        LOAD_BALANCER = "load_balancer", "Load Balancer"
        SECURITY_GROUP = "security_group", "Security Group"
        ECS_SERVICE = "ecs_service", "ECS Service"
        ECS_TASK = "ecs_task", "ECS Task"

    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    unique_id = models.CharField(max_length=100)
    name = models.CharField(max_length=100)
    metadata = models.JSONField(null=True)
    notes = models.TextField(null=True, blank=True)
    monitoring = models.CharField(max_length=64, choices=Monitoring.choices, default=Monitoring.ACTIVE)
    type = models.CharField(max_length=64, choices=Type.choices, null=True)
    notification_emails = models.JSONField(default=list,
                                           help_text="List of email addresses to notify for asset status changes")

    class Meta:
        abstract = True

    @property
    def key(self):
        import hashlib

        # Create a base key without unique_id first
        base_key = f"cm__{self.owner.cloud.account.id}__{self.provider_code}__"

        # If the full key is too long (over 64 chars), hash the unique_id
        full_key = f"{base_key}{self.unique_id}"
        if len(slugify(full_key)) > 64:
            # Hash the unique_id to keep it short but unique
            unique_id_hash = hashlib.md5(self.unique_id.encode()).hexdigest()[:16]
            final_key = f"{base_key}{unique_id_hash}"
        else:
            final_key = full_key

        return slugify(final_key)

    @property
    def owner_email(self):
        return self.owner.cloud.account.memberships.get(role=CoreAccountMembership.Role.OWNER).member.user.email

    @property
    def provider_code(self):
        return self.owner.cloud.provider.code.lower()

    @property
    def provider_name(self):
        return self.owner.cloud.provider.name

    @property
    def provider_url(self):
        return None

    @property
    def cloudmoo_url(self):
        """
        Returns the CloudMoo web URL for the asset detail page.
        Example: https://cloudmoo.com/console/assets/digitalocean/server/35/
        """
        return f"{settings.APP_URL}/console/assets/{self.provider_code}/{self.type.lower()}/{self.id}/"

    @property
    def status(self):
        """
        Gets the latest status from the asset status logs.
        Returns "unknown" if no status is found or if there's an error.

        For better performance when loading multiple assets, use get_bulk_statuses() class method.
        """
        # Check if status is already cached on this instance
        if hasattr(self, '_cached_status'):
            return self._cached_status

        try:
            if self.monitoring == self.Monitoring.ACTIVE:
                # Query the most recent log entry for this asset
                latest_log = (
                    AssetStatusLog.objects
                    .filter(asset_key=self.key)
                    .order_by('-timestamp')
                    .first()
                )

                if latest_log:
                    latest_status = latest_log.status
                    # Filter out error statuses
                    if latest_status not in ['error', 'invalid_access_token']:
                        self._cached_status = latest_status
                        return latest_status

                self._cached_status = "unknown"
                return "unknown"
            else:
                self._cached_status = "unknown"
                return "unknown"

        except Exception as e:
            print(f"Error fetching status for asset {self.name}: {str(e)}")
            self._cached_status = "unknown"
            return "unknown"

    @classmethod
    def get_bulk_statuses(cls, assets):
        """
        Efficiently fetches statuses for multiple assets in a single query.

        Args:
            assets: QuerySet or list of UtilAsset instances

        Returns:
            dict: {asset.key: status} mapping
        """
        try:
            # Prepare asset keys for active monitoring assets only
            active_assets = [asset for asset in assets if asset.monitoring == cls.Monitoring.ACTIVE]
            if not active_assets:
                return {}

            # Latest log entry per asset key (PostgreSQL DISTINCT ON)
            latest_logs = (
                AssetStatusLog.objects
                .filter(asset_key__in=[asset.key for asset in active_assets])
                .order_by('asset_key', '-timestamp')
                .distinct('asset_key')
            )

            status_map = {}
            for log in latest_logs:
                if log.status not in ['error', 'invalid_access_token']:
                    status_map[log.asset_key] = log.status
                else:
                    status_map[log.asset_key] = "unknown"

            # Assets without any log entry get "unknown"
            for asset in active_assets:
                if asset.key not in status_map:
                    status_map[asset.key] = "unknown"

            # Cache statuses on asset instances
            for asset in active_assets:
                asset._cached_status = status_map[asset.key]

            return status_map

        except Exception as e:
            print(f"Error fetching bulk statuses: {str(e)}")
            return {}

    def save(self, *args, **kwargs):
        """
        Override save method to keep the status-check schedule in sync with
        the asset.
        """
        is_new = self._state.adding
        monitoring_changed = False

        if not is_new:
            # Get the original object from the database
            original = self.__class__.objects.get(pk=self.pk)
            monitoring_changed = original.monitoring != self.monitoring

        super().save(*args, **kwargs)

        if is_new and self.type in [self.Type.SERVER, self.Type.VOLUME, self.Type.DATABASE, self.Type.RDS_DATABASE, self.Type.LAMBDA, self.Type.DYNAMODB, self.Type.S3_BUCKET, self.Type.ACM_CERTIFICATE, self.Type.SNAPSHOT, self.Type.ELASTIC_IP, self.Type.LOAD_BALANCER, self.Type.SECURITY_GROUP, self.Type.ECS_SERVICE, self.Type.ECS_TASK]:
            if self.monitoring == self.Monitoring.ACTIVE:
                try:
                    schedules.asset_schedule_create(self)
                except Exception as e:
                    logger.warning(
                        f"Could not create status check schedule for asset {self.key}: {e}. "
                        f"Schedules can be recreated with 'python manage.py create_all_cloud_schedules --confirm'."
                    )
            # When it's new asset then add owner email to notification_emails
            if self.owner_email not in self.notification_emails:
                self.notification_emails.append(self.owner_email)
                self.save()
                return
        elif monitoring_changed:
            # Check if monitoring status has changed to NO_LONGER_EXISTS
            if self.monitoring == self.Monitoring.NO_LONGER_EXISTS:
                schedules.asset_schedule_delete(self)
            else:
                # ACTIVE/DISABLED flip: refresh the task's enabled state
                try:
                    schedules.asset_schedule_update(self)
                except Exception as e:
                    logger.warning(
                        f"Could not update status check schedule for asset {self.key}: {e}. "
                        f"Schedules can be recreated with 'python manage.py create_all_cloud_schedules --confirm'."
                    )

    def delete(self, *args, **kwargs):
        """
        Override delete method to remove the status-check schedule and the
        asset's monitoring data before deleting.
        """
        schedules.asset_schedule_delete(self)
        AssetStatusLog.objects.filter(asset_key=self.key).delete()
        AssetStatusEmail.objects.filter(asset_key=self.key).delete()
        super().delete(*args, **kwargs)

    def get_email_config(self):
        """
        Get the list of notification email addresses for this asset
        """
        return self.notification_emails

    def update_email_config(self, email_list):
        """
        Update the notification email list for this asset
        """
        # Remove duplicates
        self.notification_emails = list(set(email_list))
        self.save()
        return True

    def _get_status_timeline_entries(self, days=30):
        """
        Collapse the asset's status logs into status-change entries (newest
        first) with durations and metadata changes.
        """
        end_date = timezone.now()
        start_date = end_date - timedelta(days=days)
        max_items = 10000  # Reasonable limit to prevent runaway queries

        logs = (
            AssetStatusLog.objects
            .filter(asset_key=self.key, timestamp__gte=start_date)
            .exclude(status__in=['error', 'invalid_access_token'])
            .order_by('-timestamp')[:max_items]
        )

        # Collapse consecutive equal statuses, but always keep entries that
        # carry metadata changes
        status_changes = []
        previous_status = None
        for log in logs:
            has_metadata_changes = bool(log.metadata_changes)
            if previous_status is None or log.status != previous_status or has_metadata_changes:
                status_changes.append({
                    'timestamp': log.timestamp,
                    'timezone': log.timestamp.tzinfo.tzname(log.timestamp) if log.timestamp.tzinfo else 'UTC',
                    'status': log.status,
                    'metadata_changes': log.metadata_changes or [],
                })
            previous_status = log.status

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

    def get_status_timeline(self, days=30):
        """
        Gets the status timeline for the asset from the status logs.
        Returns status changes with durations and metadata changes over the specified number of days.
        """
        try:
            return self._get_status_timeline_entries(days=days)
        except Exception as e:
            print(f"Error fetching status timeline for asset {self.name}: {str(e)}")
            return []

    def get_status_timeline_paginated(self, page=1, page_size=10, days=30):
        """
        Gets a paginated status timeline for the asset from the status logs.
        Returns status changes with pagination metadata over the specified number of days.
        """
        try:
            status_changes = self._get_status_timeline_entries(days=days)

            total_items = len(status_changes)
            total_pages = max(1, (total_items + page_size - 1) // page_size)
            page = max(1, page)

            offset = (page - 1) * page_size
            items = status_changes[offset:offset + page_size]

            return {
                'items': items,
                'has_next': page < total_pages,
                'has_previous': page > 1,
                'total_pages': total_pages,
                'current_page': page,
                'page_size': page_size
            }

        except Exception as e:
            print(f"Error fetching paginated status timeline for asset {self.name}: {str(e)}")
            return {
                'items': [],
                'has_next': False,
                'has_previous': False,
                'total_pages': 1,
                'current_page': page,
                'page_size': page_size
            }
