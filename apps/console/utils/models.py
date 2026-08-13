import logging
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import models
from django.utils import timezone
from django.utils.text import slugify
from model_utils.models import TimeStampedModel

from apps.console.account.models import CoreAccountMembership
from apps.monitoring import schedules
from apps.monitoring.checks.base import NON_ALERTING_STATUSES, format_duration
from apps.monitoring.metadata import redact_sensitive_metadata
from apps.monitoring.models import (
    AssetMonitoringState,
    AssetStatusEmail,
    AssetStatusLog,
)

logger = logging.getLogger(__name__)
MAX_NOTIFICATION_EMAILS = 50


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
        BACKUP = "backup", "Backup"
        ELASTIC_IP = "elastic_ip", "Elastic IP"
        RESERVED_IP = "reserved_ip", "Reserved IP"
        LOAD_BALANCER = "load_balancer", "Load Balancer"
        SECURITY_GROUP = "security_group", "Security Group"
        FIREWALL = "firewall", "Firewall"
        APP_PLATFORM = "app_platform", "App Platform App"
        OBJECT_STORAGE = "object_storage", "Object Storage"
        CONTAINER_REGISTRY = "container_registry", "Container Registry"
        ECS_SERVICE = "ecs_service", "ECS Service"
        ECS_TASK = "ecs_task", "ECS Task"
        KUBERNETES_CLUSTER = "kubernetes_cluster", "Kubernetes Cluster"
        KUBERNETES_NODE_POOL = "kubernetes_node_pool", "Kubernetes Node Pool"

        # Vultr read-only inventory families that do not have a precise
        # equivalent in the legacy provider-neutral set above.
        VULTR_BARE_METAL = "vultr_bare_metal", "Vultr Bare Metal"
        VULTR_BLOCK_SNAPSHOT = "vultr_block_snapshot", "Vultr Block Snapshot"
        VULTR_VFS = "vultr_vfs", "Vultr Virtual File System"
        VULTR_STORAGE_GATEWAY = "vultr_storage_gateway", "Vultr Storage Gateway"
        VULTR_BANDWIDTH_METRIC = "vultr_bandwidth_metric", "Vultr Bandwidth Metric"
        VULTR_REGISTRY_REPOSITORY = "vultr_registry_repository", "Vultr Registry Repository"
        VULTR_REGISTRY_ARTIFACT = "vultr_registry_artifact", "Vultr Registry Artifact"
        VULTR_INFERENCE = "vultr_inference", "Vultr Inference Endpoint"
        VULTR_FIREWALL_RULE = "vultr_firewall_rule", "Vultr Firewall Rule"
        VULTR_STORAGE_CLUSTER = "vultr_storage_cluster", "Vultr Storage Cluster"
        VULTR_STORAGE_TIER = "vultr_storage_tier", "Vultr Storage Tier"
        VULTR_PLAN = "vultr_plan", "Vultr Plan"
        VULTR_REGION = "vultr_region", "Vultr Region"
        VULTR_COMPUTE_PLAN = "vultr_compute_plan", "Vultr Compute Plan"
        VULTR_ACCOUNT_LOG = "vultr_account_log", "Vultr Account Log"
        VULTR_STATUS_INCIDENT = "vultr_status_incident", "Vultr Status Incident"

        # Hetzner Cloud inventory families.  These names are provider-neutral
        # where the same concept is useful across providers, while the
        # provider-specific adapter remains responsible for semantics.
        PRIMARY_IP = "primary_ip", "Primary IP"
        FLOATING_IP = "floating_ip", "Floating IP"
        NETWORK = "network", "Network"
        PLACEMENT_GROUP = "placement_group", "Placement Group"
        IMAGE = "image", "Image"
        LOCATION = "location", "Location"
        DATACENTER = "datacenter", "Datacenter"
        SERVER_TYPE = "server_type", "Server Type"
        ISO = "iso", "ISO"
        SSH_KEY = "ssh_key", "SSH Key"
        LOAD_BALANCER_TYPE = "load_balancer_type", "Load Balancer Type"
        ZONE = "zone", "DNS Zone"
        RRSET = "rrset", "DNS RRSet"
        ACTION = "action", "Provider Action"

        AWS_CLOUDWATCH_ALARM = "aws_cloudwatch_alarm", "AWS CloudWatch Alarm"
        AWS_CLOUDWATCH_METRIC = "aws_cloudwatch_metric", "AWS CloudWatch Metric"
        AWS_LOG_GROUP = "aws_log_group", "AWS Log Group"

        AWS_ECR_REPOSITORY = "aws_ecr_repository", "AWS ECR Repository"
        AWS_ECR_IMAGE = "aws_ecr_image", "AWS ECR Image"
        AWS_ECS_TASK_DEFINITION = "aws_ecs_task_definition", "AWS ECS Task Definition"
        AWS_ECS_DEPLOYMENT = "aws_ecs_deployment", "AWS ECS Deployment"
        AWS_EKS_CLUSTER = "aws_eks_cluster", "AWS EKS Cluster"
        AWS_EKS_NODE_GROUP = "aws_eks_node_group", "AWS EKS Node Group"
        AWS_EKS_ADDON = "aws_eks_addon", "AWS EKS Add-on"
        AWS_EKS_FARGATE_PROFILE = "aws_eks_fargate_profile", "AWS EKS Fargate Profile"
        AWS_APPRUNNER_SERVICE = "aws_apprunner_service", "AWS App Runner Service"
        AWS_APPRUNNER_DEPLOYMENT = "aws_apprunner_deployment", "AWS App Runner Deployment"

        AWS_ROUTE53_ZONE = "aws_route53_zone", "AWS Route 53 Zone"
        AWS_ROUTE53_RECORD = "aws_route53_record", "AWS Route 53 Record"
        AWS_CLOUDFRONT_DISTRIBUTION = "aws_cloudfront_distribution", "AWS CloudFront Distribution"
        AWS_CLOUDFRONT_ORIGIN_ACCESS_CONTROL = (
            "aws_cloudfront_origin_access_control",
            "AWS CloudFront Origin Access Control",
        )
        AWS_WAF_WEB_ACL = "aws_waf_web_acl", "AWS WAF Web ACL"
        AWS_GLOBAL_ACCELERATOR = "aws_global_accelerator", "AWS Global Accelerator"

        AWS_BACKUP_VAULT = "aws_backup_vault", "AWS Backup Vault"
        AWS_BACKUP_PLAN = "aws_backup_plan", "AWS Backup Plan"
        AWS_BACKUP_RECOVERY_POINT = "aws_backup_recovery_point", "AWS Backup Recovery Point"
        AWS_BACKUP_JOB = "aws_backup_job", "AWS Backup Job"
        AWS_BACKUP_COPY_JOB = "aws_backup_copy_job", "AWS Backup Copy Job"

        AWS_RDS_CLUSTER = "aws_rds_cluster", "AWS RDS Cluster"
        AWS_ELASTICACHE_CLUSTER = "aws_elasticache_cluster", "AWS ElastiCache Cluster"
        AWS_ELASTICACHE_REPLICATION_GROUP = (
            "aws_elasticache_replication_group",
            "AWS ElastiCache Replication Group",
        )
        AWS_ELASTICACHE_SERVERLESS_CACHE = (
            "aws_elasticache_serverless_cache",
            "AWS ElastiCache Serverless Cache",
        )
        AWS_MEMORYDB_CLUSTER = "aws_memorydb_cluster", "AWS MemoryDB Cluster"
        AWS_OPENSEARCH_DOMAIN = "aws_opensearch_domain", "AWS OpenSearch Domain"
        AWS_EFS_FILE_SYSTEM = "aws_efs_file_system", "AWS EFS File System"
        AWS_FSX_FILE_SYSTEM = "aws_fsx_file_system", "AWS FSx File System"

        AWS_APIGATEWAY_REST_API = "aws_apigateway_rest_api", "AWS API Gateway REST API"
        AWS_APIGATEWAY_V2_API = "aws_apigateway_v2_api", "AWS API Gateway v2 API"
        AWS_EVENTBRIDGE_BUS = "aws_eventbridge_bus", "AWS EventBridge Bus"
        AWS_EVENTBRIDGE_RULE = "aws_eventbridge_rule", "AWS EventBridge Rule"
        AWS_EVENTBRIDGE_SCHEDULE = "aws_eventbridge_schedule", "AWS EventBridge Schedule"
        AWS_EVENTBRIDGE_PIPE = "aws_eventbridge_pipe", "AWS EventBridge Pipe"
        AWS_SNS_TOPIC = "aws_sns_topic", "AWS SNS Topic"
        AWS_SQS_QUEUE = "aws_sqs_queue", "AWS SQS Queue"
        AWS_STEPFUNCTIONS_STATE_MACHINE = (
            "aws_stepfunctions_state_machine",
            "AWS Step Functions State Machine",
        )
        AWS_ATHENA_WORKGROUP = "aws_athena_workgroup", "AWS Athena Workgroup"
        AWS_ATHENA_DATA_CATALOG = "aws_athena_data_catalog", "AWS Athena Data Catalog"
        AWS_CLOUDFORMATION_STACK = "aws_cloudformation_stack", "AWS CloudFormation Stack"

        AWS_ELASTIC_BEANSTALK_APPLICATION = (
            "aws_elastic_beanstalk_application",
            "AWS Elastic Beanstalk Application",
        )
        AWS_ELASTIC_BEANSTALK_ENVIRONMENT = (
            "aws_elastic_beanstalk_environment",
            "AWS Elastic Beanstalk Environment",
        )
        AWS_CODEBUILD_PROJECT = "aws_codebuild_project", "AWS CodeBuild Project"
        AWS_CODEBUILD_BUILD = "aws_codebuild_build", "AWS CodeBuild Build"
        AWS_CODEPIPELINE_PIPELINE = "aws_codepipeline_pipeline", "AWS CodePipeline Pipeline"
        AWS_CODEPIPELINE_EXECUTION = "aws_codepipeline_execution", "AWS CodePipeline Execution"

        AWS_IAM_USER = "aws_iam_user", "AWS IAM User"
        AWS_IAM_ROLE = "aws_iam_role", "AWS IAM Role"
        AWS_IAM_POLICY = "aws_iam_policy", "AWS IAM Policy"
        AWS_KMS_KEY = "aws_kms_key", "AWS KMS Key"
        AWS_KMS_ALIAS = "aws_kms_alias", "AWS KMS Alias"
        AWS_CLOUDTRAIL_TRAIL = "aws_cloudtrail_trail", "AWS CloudTrail Trail"
        AWS_CONFIG_RULE = "aws_config_rule", "AWS Config Rule"
        AWS_CONFIG_RECORDER = "aws_config_recorder", "AWS Config Recorder"
        AWS_GUARDDUTY_DETECTOR = "aws_guardduty_detector", "AWS GuardDuty Detector"
        AWS_SECURITY_HUB = "aws_security_hub", "AWS Security Hub"
        AWS_INSPECTOR = "aws_inspector", "AWS Inspector"
        AWS_MACIE = "aws_macie", "AWS Macie"
        AWS_FIREWALL_MANAGER_POLICY = (
            "aws_firewall_manager_policy",
            "AWS Firewall Manager Policy",
        )

        AWS_SECRETS_MANAGER_SECRET = (
            "aws_secrets_manager_secret",
            "AWS Secrets Manager Secret",
        )
        AWS_SSM_PARAMETER = "aws_ssm_parameter", "AWS SSM Parameter"

        AWS_HEALTH_EVENT = "aws_health_event", "AWS Health Event"
        AWS_TRUSTED_ADVISOR_CHECK = (
            "aws_trusted_advisor_check",
            "AWS Trusted Advisor Check",
        )
        AWS_COST_EXPLORER_SIGNAL = "aws_cost_explorer_signal", "AWS Cost Explorer Signal"
        AWS_COST_ANOMALY_MONITOR = "aws_cost_anomaly_monitor", "AWS Cost Anomaly Monitor"
        AWS_COST_ANOMALY_SUBSCRIPTION = (
            "aws_cost_anomaly_subscription",
            "AWS Cost Anomaly Subscription",
        )
        AWS_COST_ANOMALY = "aws_cost_anomaly", "AWS Cost Anomaly"

        VPC = "vpc", "VPC"
        SUBNET = "subnet", "Subnet"
        ROUTE_TABLE = "route_table", "Route Table"
        INTERNET_GATEWAY = "internet_gateway", "Internet Gateway"
        VPC_PEERING = "vpc_peering", "VPC Peering"
        NAT_GATEWAY = "nat_gateway", "NAT Gateway"
        NETWORK_ACL = "network_acl", "Network ACL"
        NETWORK_INTERFACE = "network_interface", "Network Interface"
        TRANSIT_GATEWAY_ATTACHMENT = "transit_gateway_attachment", "Transit Gateway Attachment"
        VPN_CONNECTION = "vpn_connection", "VPN Connection"
        FLOW_LOG = "flow_log", "Flow Log"
        AUTO_SCALING_GROUP = "auto_scaling_group", "Auto Scaling Group"
        LAUNCH_TEMPLATE = "launch_template", "Launch Template"
        AMI = "ami", "AMI"
        EBS_ATTACHMENT = "ebs_attachment", "EBS Attachment"

        DOMAIN = "domain", "Domain"
        DNS_RECORD = "dns_record", "DNS Record"
        CDN_ENDPOINT = "cdn_endpoint", "CDN Endpoint"
        CERTIFICATE = "certificate", "Certificate"
        LIGHTSAIL_INSTANCE = "lightsail_instance", "Lightsail Instance"
        LIGHTSAIL_DISK = "lightsail_disk", "Lightsail Disk"
        LIGHTSAIL_INSTANCE_SNAPSHOT = "lightsail_instance_snapshot", "Lightsail Instance Snapshot"
        LIGHTSAIL_DISK_SNAPSHOT = "lightsail_disk_snapshot", "Lightsail Disk Snapshot"
        LIGHTSAIL_STATIC_IP = "lightsail_static_ip", "Lightsail Static IP"
        LIGHTSAIL_DATABASE = "lightsail_database", "Lightsail Database"
        LIGHTSAIL_DATABASE_SNAPSHOT = "lightsail_database_snapshot", "Lightsail Database Snapshot"
        LIGHTSAIL_LOAD_BALANCER = "lightsail_load_balancer", "Lightsail Load Balancer"
        LIGHTSAIL_CERTIFICATE = "lightsail_certificate", "Lightsail Certificate"
        LIGHTSAIL_BUCKET = "lightsail_bucket", "Lightsail Bucket"
        LIGHTSAIL_DISTRIBUTION = "lightsail_distribution", "Lightsail Distribution"
        LIGHTSAIL_DOMAIN = "lightsail_domain", "Lightsail DNS Zone"
        LIGHTSAIL_DNS_RECORD = "lightsail_dns_record", "Lightsail DNS Record"
        LIGHTSAIL_CONTAINER_SERVICE = "lightsail_container_service", "Lightsail Container Service"
        LIGHTSAIL_CONTAINER_DEPLOYMENT = "lightsail_container_deployment", "Lightsail Container Deployment"
        LIGHTSAIL_CONTAINER_IMAGE = "lightsail_container_image", "Lightsail Container Image"
        LIGHTSAIL_ALARM = "lightsail_alarm", "Lightsail Alarm"
        LIGHTSAIL_OPERATION = "lightsail_operation", "Lightsail Operation"
        LIGHTSAIL_AUTO_SNAPSHOT = "lightsail_auto_snapshot", "Lightsail Auto Snapshot"

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
            unique_id_hash = hashlib.md5(
                self.unique_id.encode(), usedforsecurity=False
            ).hexdigest()[:16]
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
    def monitoring_state(self):
        """Return the durable monitoring heartbeat for this asset, if present."""
        return AssetMonitoringState.objects.filter(asset_key=self.key).first()

    @property
    def last_checked_at(self):
        state = self.monitoring_state
        return state.last_checked_at if state else None

    @staticmethod
    def _monitoring_state_stale(asset, state, current_time=None):
        """Evaluate freshness without issuing another state query."""
        if asset.monitoring != asset.Monitoring.ACTIVE:
            return False
        if state is None or state.last_checked_at is None:
            return True
        interval_minutes = max(1, asset.owner.cloud.account.monitoring_interval)
        stale_after = timedelta(minutes=max(5, interval_minutes * 3))
        current_time = current_time or timezone.now()
        return state.last_checked_at < current_time - stale_after

    @property
    def monitoring_stale(self):
        """Whether the worker has missed several expected check intervals."""
        return self._monitoring_state_stale(self, self.monitoring_state)

    @property
    def monitoring_supported(self):
        """Whether this provider/type pair has a registered status checker."""
        from apps.monitoring.checks import get_check_function

        try:
            get_check_function(self.provider_code, self.type)
        except ValueError:
            return False
        return True

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
                monitoring_state = self.monitoring_state
                if monitoring_state:
                    if (
                        monitoring_state.last_error_status
                        or self.monitoring_stale
                        or not monitoring_state.last_status
                    ):
                        self._cached_status = "unknown"
                        return "unknown"
                    self._cached_status = monitoring_state.last_status
                    return monitoring_state.last_status

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
                    if latest_status not in NON_ALERTING_STATUSES:
                        self._cached_status = latest_status
                        return latest_status

                self._cached_status = "unknown"
                return "unknown"
            else:
                self._cached_status = "unknown"
                return "unknown"

        except Exception:
            logger.exception("Error fetching status for asset %s", self.pk)
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
            current_time = timezone.now()
            states = {
                state.asset_key: state
                for state in AssetMonitoringState.objects.filter(
                    asset_key__in=[asset.key for asset in active_assets]
                )
            }
            for log in latest_logs:
                if log.status not in NON_ALERTING_STATUSES:
                    status_map[log.asset_key] = log.status
                else:
                    status_map[log.asset_key] = "unknown"

            # Assets without any log entry get "unknown"
            for asset in active_assets:
                if asset.key not in status_map:
                    status_map[asset.key] = "unknown"

                state = states.get(asset.key)
                if state:
                    if (
                        state.last_error_status
                        or cls._monitoring_state_stale(asset, state, current_time)
                        or not state.last_status
                    ):
                        status_map[asset.key] = "unknown"
                    else:
                        status_map[asset.key] = state.last_status

            # Cache statuses on asset instances
            for asset in active_assets:
                asset._cached_status = status_map[asset.key]

            return status_map

        except Exception:
            logger.exception("Error fetching bulk asset statuses")
            return {}

    def save(self, *args, **kwargs):
        """
        Override save method to keep the status-check schedule in sync with
        the asset.
        """
        # Provider inventory payloads are persisted in this model. Sanitize at
        # the boundary so a provider-specific sync path cannot accidentally
        # retain credentials or secret-like configuration values.
        if self.metadata is not None:
            self.metadata = redact_sensitive_metadata(self.metadata)

        is_new = self._state.adding
        monitoring_changed = False

        if not is_new:
            # Get the original object from the database
            original = self.__class__.objects.get(pk=self.pk)
            monitoring_changed = original.monitoring != self.monitoring

        if is_new:
            emails = self.notification_emails if isinstance(self.notification_emails, list) else []
            owner_email = self.owner_email
            if owner_email and owner_email not in emails:
                self.notification_emails = [*emails, owner_email]

        super().save(*args, **kwargs)

        if is_new or monitoring_changed:
            try:
                if (
                    self.monitoring == self.Monitoring.NO_LONGER_EXISTS
                    or not self.monitoring_supported
                ):
                    schedules.asset_schedule_delete(self)
                else:
                    schedules.asset_schedule_update(self)
            except Exception as error:
                logger.warning(
                    "Could not reconcile the status-check schedule for asset %s (%s). "
                    "Schedules can be recreated with "
                    "'python manage.py create_all_cloud_schedules --confirm'.",
                    self.key,
                    type(error).__name__,
                )

    def delete(self, *args, **kwargs):
        """
        Override delete method to remove the status-check schedule and the
        asset's monitoring data before deleting.
        """
        schedules.asset_schedule_delete(self)
        AssetMonitoringState.objects.filter(asset_key=self.key).delete()
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
        if not isinstance(email_list, list):
            raise ValidationError("Email recipients must be provided as a list")
        if len(email_list) > MAX_NOTIFICATION_EMAILS:
            raise ValidationError(
                f"No more than {MAX_NOTIFICATION_EMAILS} notification recipients are allowed"
            )

        normalized = []
        seen = set()
        for value in email_list:
            if not isinstance(value, str):
                raise ValidationError("Every notification recipient must be an email address")
            email = value.strip()
            validate_email(email)
            identity = email.casefold()
            if identity not in seen:
                seen.add(identity)
                normalized.append(email)

        self.notification_emails = normalized
        self.save(update_fields=['notification_emails'])
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
            .exclude(status__in=NON_ALERTING_STATUSES)
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
        except Exception:
            logger.exception("Error fetching status timeline for asset %s", self.pk)
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

        except Exception:
            logger.exception("Error fetching paginated status timeline for asset %s", self.pk)
            return {
                'items': [],
                'has_next': False,
                'has_previous': False,
                'total_pages': 1,
                'current_page': page,
                'page_size': page_size
            }
