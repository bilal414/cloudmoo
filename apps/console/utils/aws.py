"""
Shared boto3 helpers for the monitoring engine.

Credentials resolve through settings: when AWS_ACCESS_KEY /
AWS_SECRET_ACCESS_KEY are empty, boto3 falls back to the default credential
chain (IAM role, instance profile, environment variables, ~/.aws).
"""
import logging

import boto3
from django.conf import settings

logger = logging.getLogger(__name__)


def _aws_kwargs():
    kwargs = {"region_name": settings.AWS_REGION}
    if settings.AWS_ACCESS_KEY and settings.AWS_SECRET_ACCESS_KEY:
        kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY
        kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY
    return kwargs


def aws_client(service):
    """Return a boto3 client for the platform's own AWS account."""
    return boto3.client(service, **_aws_kwargs())


def aws_resource(service):
    """Return a boto3 resource for the platform's own AWS account."""
    return boto3.resource(service, **_aws_kwargs())


def monitoring_engine_configured():
    """
    True when the AWS monitoring engine (EventBridge + Lambda) is configured.
    When False, clouds and assets are saved without creating schedules, so
    the console works before AWS is set up; schedules can be created later
    with: python manage.py create_all_cloud_schedules --confirm
    """
    return bool(
        settings.AWS_SCHEDULER_ROLE
        and settings.AWS_LAMBDA_ASSET_STATUS
        and settings.AWS_LAMBDA_CLOUD_SYNC_ASSETS
    )
