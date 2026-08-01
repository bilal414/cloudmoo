"""
Monitoring engine models.

These models live in the single ``apps`` Django app (they are re-exported
from ``apps.models``) and replace the DynamoDB tables previously used by the
AWS-based monitoring engine:

- ``AssetStatusLog`` replaces the ``cloudmoo-*-asset-logs`` table.
- ``AssetStatusEmail`` replaces the ``cloudmoo-*-asset-emails`` table.
"""
from django.db import models


class AssetStatusLog(models.Model):
    """A single asset status observation recorded by a status check."""

    asset_key = models.CharField(max_length=80, db_index=True)
    account_id = models.IntegerField(db_index=True)
    provider = models.CharField(max_length=32)
    asset_type = models.CharField(max_length=32)
    status = models.CharField(max_length=64)
    timestamp = models.DateTimeField()
    error_message = models.TextField(blank=True, default='')
    metadata = models.JSONField(null=True, blank=True)
    metadata_changes = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "asset_status_log"
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['asset_key', '-timestamp']),
            models.Index(fields=['account_id', '-timestamp']),
        ]

    def __str__(self):
        return f"{self.asset_key}: {self.status} at {self.timestamp}"


class AssetMonitoringState(models.Model):
    """Durable execution state for an asset's monitoring loop.

    Status logs intentionally contain only changes. This separate row records
    every check heartbeat so the UI and operators can distinguish "healthy"
    from "the worker has not checked this asset recently".
    """

    asset_key = models.CharField(max_length=80, unique=True)
    account_id = models.IntegerField(db_index=True)
    provider = models.CharField(max_length=32)
    asset_type = models.CharField(max_length=32)
    last_checked_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_status = models.CharField(max_length=64, blank=True, default='')
    last_error_status = models.CharField(max_length=64, blank=True, default='')
    last_error_message = models.TextField(blank=True, default='')
    consecutive_failures = models.PositiveIntegerField(default=0)
    # Incremented when a check starts. A result may only be committed if its
    # generation is still current; this prevents out-of-order provider
    # responses from regressing a newer health result.
    check_generation = models.PositiveBigIntegerField(default=0)
    check_started_at = models.DateTimeField(null=True, blank=True)
    last_change_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "asset_monitoring_state"
        indexes = [
            models.Index(fields=['account_id', 'last_checked_at']),
        ]

    def __str__(self):
        return f"{self.asset_key}: {self.last_status or 'unknown'}"


class AssetStatusEmail(models.Model):
    """Record of a status-change notification email sent to a recipient."""

    asset_key = models.CharField(max_length=80, db_index=True)
    account_id = models.IntegerField(db_index=True)
    asset_id = models.IntegerField()
    provider = models.CharField(max_length=32)
    asset_type = models.CharField(max_length=32)
    recipient = models.EmailField(db_index=True)
    subject = models.CharField(max_length=255)
    text_body = models.TextField()
    html_body = models.TextField()
    status_previous = models.CharField(max_length=64, blank=True, default='')
    status_current = models.CharField(max_length=64, blank=True, default='')
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    # A status-log id + recipient makes email delivery idempotent across
    # Celery retries. Nullable preserves compatibility with legacy rows.
    event_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    asset_content_type_id = models.IntegerField(null=True, blank=True, db_index=True)
    delivery_key = models.CharField(max_length=400, unique=True, null=True, blank=True)
    delivery_status = models.CharField(max_length=16, default='pending')
    attempt_count = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True, default='')
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "asset_status_email"
        ordering = ['-timestamp']
        indexes = [
            models.Index(
                fields=['delivery_status', 'last_attempt_at'],
                name='asset_email_delivery_idx',
            ),
        ]

    def __str__(self):
        return f"{self.asset_key} -> {self.recipient} at {self.timestamp}"
