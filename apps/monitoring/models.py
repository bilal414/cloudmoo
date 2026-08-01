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

    class Meta:
        db_table = "asset_status_email"
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.asset_key} -> {self.recipient} at {self.timestamp}"
