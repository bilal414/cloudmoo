"""Read-only CloudWatch and CloudWatch Logs inventory for AWS.

This module deliberately owns only the CloudWatch/Logs inventory surface.  The
shared AWS discovery helpers own client construction, region discovery,
pagination, response validation, serialization, and provider error codes.
"""

from __future__ import annotations

import hashlib
import logging
import re

from django.db import models

from apps.console.cloud.aws.models import CoreAWSAccount
from apps.console.utils.models import UtilAsset
from apps.monitoring.metadata import redact_error_message, redact_sensitive_metadata

try:
    from apps.console.cloud.aws.discovery import (
        aws_client,
        aws_error_code,
        get_enabled_regions,
        iter_pages,
        require_collection,
        serialize_aws,
    )
except ModuleNotFoundError as error:
    # The discovery module is supplied by the AWS integration lane.  Keeping
    # the import lazy-compatible makes this module importable while that lane
    # is being assembled, without reimplementing any discovery behavior here.
    if error.name != "apps.console.cloud.aws.discovery":
        raise

    _DISCOVERY_IMPORT_ERROR = error

    def _missing_discovery(*_args, **_kwargs):
        raise RuntimeError(
            "AWS discovery helpers are unavailable; install "
            "apps.console.cloud.aws.discovery"
        ) from _DISCOVERY_IMPORT_ERROR

    aws_client = _missing_discovery
    aws_error_code = _missing_discovery
    get_enabled_regions = _missing_discovery
    iter_pages = _missing_discovery
    require_collection = _missing_discovery
    serialize_aws = _missing_discovery


logger = logging.getLogger(__name__)

ASSET_TYPE_CLOUDWATCH_ALARM = "aws_cloudwatch_alarm"
ASSET_TYPE_CLOUDWATCH_METRIC = "aws_cloudwatch_metric"
ASSET_TYPE_LOG_GROUP = "aws_log_group"

OBSERVABILITY_ASSET_TYPES = (
    ASSET_TYPE_CLOUDWATCH_ALARM,
    ASSET_TYPE_CLOUDWATCH_METRIC,
    ASSET_TYPE_LOG_GROUP,
)

# CloudWatch can contain very large metric inventories.  The paginator still
# walks every normal response page, while these hard bounds prevent a broken
# paginator or unexpectedly huge account from making a worker unbounded.
MAX_PAGES_PER_COLLECTION = 100
MAX_ITEMS_PER_COLLECTION = 10_000
MAX_DIMENSIONS = 30
MAX_TEXT_LENGTH = 1_024

_SENSITIVE_DIMENSION_PARTS = (
    "password",
    "secret",
    "token",
    "accesskey",
    "privatekey",
    "credential",
    "authorization",
    "apikey",
)
_SENSITIVE_VALUE_PATTERN = re.compile(
    r"(?i)(?:password|secret|token|access[_-]?key|private[_-]?key|"
    r"credential|authorization|api[_-]?key)\s*[:=]"
)


class CoreAWSObservabilityAsset(UtilAsset):
    """Common owner, region, identity, and credential behavior."""

    region = models.CharField(max_length=32)

    class Meta:
        abstract = True

    asset_type = None

    def save(self, *args, **kwargs):
        if not self.type and self.asset_type:
            self.type = self.asset_type
        super().save(*args, **kwargs)

    @property
    def monitoring_credentials(self):
        metadata = self.metadata if isinstance(self.metadata, dict) else {}
        return {
            "access_key": self.owner.access_key,
            "secret_key": self.owner.secret_key,
            "region": self.region,
            "resource_region": self.region,
            "resource_name": self.unique_id,
            "asset_type": self.type or self.asset_type,
            "metadata": metadata,
        }

    def check_status(self):
        from apps.monitoring.checks.aws_observability import AWS_OBSERVABILITY_CHECKS

        check = AWS_OBSERVABILITY_CHECKS[self.type or self.asset_type]
        return check(self.unique_id, self.monitoring_credentials)


class CoreAWSCloudWatchAlarm(CoreAWSObservabilityAsset):
    asset_type = ASSET_TYPE_CLOUDWATCH_ALARM
    owner = models.ForeignKey(
        CoreAWSAccount,
        on_delete=models.CASCADE,
        related_name="cloudwatch_alarms",
    )

    class Meta:
        db_table = "core_aws_cloudwatch_alarm"
        constraints = [
            models.UniqueConstraint(
                fields=("owner", "region", "unique_id"),
                name="aws_cw_alarm_owner_region_uid_uniq",
            ),
        ]

    def __str__(self):
        return self.name


class CoreAWSCloudWatchMetric(CoreAWSObservabilityAsset):
    asset_type = ASSET_TYPE_CLOUDWATCH_METRIC
    owner = models.ForeignKey(
        CoreAWSAccount,
        on_delete=models.CASCADE,
        related_name="cloudwatch_metrics",
    )

    class Meta:
        db_table = "core_aws_cloudwatch_metric"
        constraints = [
            models.UniqueConstraint(
                fields=("owner", "region", "unique_id"),
                name="aws_cw_metric_owner_region_uid_uniq",
            ),
        ]

    def __str__(self):
        return self.name


class CoreAWSLogGroup(CoreAWSObservabilityAsset):
    asset_type = ASSET_TYPE_LOG_GROUP
    owner = models.ForeignKey(
        CoreAWSAccount,
        on_delete=models.CASCADE,
        related_name="log_groups",
    )

    class Meta:
        db_table = "core_aws_log_group"
        constraints = [
            models.UniqueConstraint(
                fields=("owner", "region", "unique_id"),
                name="aws_log_group_owner_region_uid_uniq",
            ),
        ]

    def __str__(self):
        return self.name


# A descriptive alias is useful to callers that name the service explicitly.
CoreAWSCloudWatchLogGroup = CoreAWSLogGroup


def _normalized_key(value):
    return "".join(character for character in str(value).lower() if character.isalnum())


def _safe_text(value, *, limit=MAX_TEXT_LENGTH):
    if value is None:
        return None
    value = str(value)
    return value[:limit]


def _sensitive_dimension(name, value):
    normalized_name = _normalized_key(name)
    if any(part in normalized_name for part in _SENSITIVE_DIMENSION_PARTS):
        return True
    return bool(_SENSITIVE_VALUE_PATTERN.search(str(value)))


def _safe_dimensions(value):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("AWS returned an invalid CloudWatch dimensions collection")

    dimensions = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("AWS returned an invalid CloudWatch dimension")
        name = _safe_text(item.get("Name"), limit=256)
        dimension_value = _safe_text(item.get("Value"), limit=MAX_TEXT_LENGTH)
        if not name or dimension_value is None or _sensitive_dimension(name, dimension_value):
            continue
        dimensions.append({"Name": name, "Value": dimension_value})

    dimensions.sort(key=lambda item: (item["Name"], item["Value"]))
    return dimensions[:MAX_DIMENSIONS]


def _safe_error_code(error):
    try:
        code = aws_error_code(error)
    except Exception:
        code = None
    return _safe_text(code or type(error).__name__, limit=128)


def _error_result(error):
    """Return a compact provider error without persisting provider text."""
    code = _safe_error_code(error)
    normalized = code.lower()
    if "notfound" in normalized or normalized in {
        "resourcenotfoundexception",
        "resourcearnnotfound",
    }:
        status = "not_found"
    elif normalized in {
        "accessdenied",
        "accessdeniedexception",
        "authfailure",
        "expiredtoken",
        "invalidclienttokenid",
        "unrecognizedclientexception",
        "unauthorizedoperation",
    }:
        status = "invalid_access_token"
    else:
        status = "error"
    return status, {"errorCode": code}


def _required_collection(payload, key, context):
    value = require_collection(payload, key, context)
    if not isinstance(value, list):
        raise ValueError(f"AWS returned an invalid {context} collection")
    return value


def _bounded_pages(client, operation, **kwargs):
    for page_number, page in enumerate(iter_pages(client, operation, **kwargs), start=1):
        if page_number > MAX_PAGES_PER_COLLECTION:
            raise ValueError(f"AWS {operation} pagination exceeded the safety bound")
        if not isinstance(page, dict):
            raise ValueError(f"AWS returned an invalid {operation} page")
        yield page


def _bounded_identifier(prefix, region, identity):
    raw = f"{prefix}:{region}:{identity}"
    if len(raw) <= 100:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:48]
    return f"{prefix}:{region}:{digest}"[:100]


def _serialized(value):
    result = serialize_aws(value)
    if not isinstance(result, dict):
        raise ValueError("AWS returned an invalid object")
    return result


def _alarm_record(item, region, alarm_type=None):
    item = _serialized(item)
    alarm_name = _safe_text(item.get("AlarmName"), limit=MAX_TEXT_LENGTH)
    if not alarm_name:
        raise ValueError("AWS returned a CloudWatch alarm without a name")

    alarm_arn = _safe_text(item.get("AlarmArn"), limit=MAX_TEXT_LENGTH)
    alarm_type = alarm_type or ("composite" if "AlarmRule" in item else "metric")
    metadata = {
        "region": region,
        "alarmType": alarm_type,
    }
    for key in (
        "AlarmArn",
        "AlarmName",
        "AlarmDescription",
        "StateValue",
        "StateReason",
        "StateReasonData",
        "StateUpdatedTimestamp",
        "AlarmRule",
        "MetricName",
        "Namespace",
        "Statistic",
        "ExtendedStatistic",
        "Period",
        "EvaluationPeriods",
        "DatapointsToAlarm",
        "ComparisonOperator",
        "Threshold",
        "TreatMissingData",
        "EvaluateLowSampleCountPercentile",
    ):
        if key in item:
            metadata[key] = item[key]
    if "Dimensions" in item:
        metadata["Dimensions"] = _safe_dimensions(item.get("Dimensions"))

    metadata = redact_sensitive_metadata(metadata)
    identity = alarm_arn or alarm_name
    return {
        "unique_id": _bounded_identifier(ASSET_TYPE_CLOUDWATCH_ALARM, region, identity),
        "name": alarm_name,
        "state": _safe_text(item.get("StateValue"), limit=64),
        "metadata": metadata,
        "alarm_name": alarm_name,
    }


def _metric_record(item, region):
    item = _serialized(item)
    namespace = _safe_text(item.get("Namespace"), limit=256)
    metric_name = _safe_text(item.get("MetricName"), limit=256)
    if not namespace or not metric_name:
        raise ValueError("AWS returned a CloudWatch metric without an identity")
    dimensions = _safe_dimensions(item.get("Dimensions"))
    canonical_dimensions = ";".join(
        f"{dimension['Name']}={dimension['Value']}" for dimension in dimensions
    )
    identity = f"{namespace}:{metric_name}:{canonical_dimensions}"
    metadata = redact_sensitive_metadata({
        "region": region,
        "Namespace": namespace,
        "MetricName": metric_name,
        "Dimensions": dimensions,
    })
    return {
        "unique_id": _bounded_identifier(ASSET_TYPE_CLOUDWATCH_METRIC, region, identity),
        "name": f"{namespace}/{metric_name}"[:100],
        "metadata": metadata,
        "namespace": namespace,
        "metric_name": metric_name,
        "dimensions": dimensions,
    }


def _log_group_record(item, region):
    item = _serialized(item)
    log_group_name = _safe_text(item.get("logGroupName"), limit=MAX_TEXT_LENGTH)
    if not log_group_name:
        raise ValueError("AWS returned a CloudWatch log group without a name")

    metadata = {"region": region, "logGroupName": log_group_name}
    for key in (
        "arn",
        "creationTime",
        "retentionInDays",
        "metricFilterCount",
        "storedBytes",
        "kmsKeyId",
        "dataProtectionStatus",
        "inheritedProperties",
        "logGroupClass",
        "deletionProtectionEnabled",
    ):
        if key in item:
            metadata[key] = item[key]
    metadata = redact_sensitive_metadata(metadata)
    identity = _safe_text(item.get("arn"), limit=MAX_TEXT_LENGTH) or log_group_name
    return {
        "unique_id": _bounded_identifier(ASSET_TYPE_LOG_GROUP, region, identity),
        "name": log_group_name,
        "metadata": metadata,
        "log_group_name": log_group_name,
    }


def _collect_alarms(client, region):
    records = []
    for page in _bounded_pages(client, "describe_alarms"):
        metric_alarms = _required_collection(page, "MetricAlarms", "CloudWatch metric alarms")
        composite_alarms = _required_collection(
            page,
            "CompositeAlarms",
            "CloudWatch composite alarms",
        )
        for item, alarm_type in (
            *((item, "metric") for item in metric_alarms),
            *((item, "composite") for item in composite_alarms),
        ):
            if len(records) >= MAX_ITEMS_PER_COLLECTION:
                raise ValueError("AWS CloudWatch alarm inventory exceeded the safety bound")
            records.append(_alarm_record(item, region, alarm_type))
    return records


def _collect_metrics(client, region):
    records = []
    for page in _bounded_pages(client, "list_metrics"):
        for item in _required_collection(page, "Metrics", "CloudWatch metrics"):
            if len(records) >= MAX_ITEMS_PER_COLLECTION:
                raise ValueError("AWS CloudWatch metric inventory exceeded the safety bound")
            records.append(_metric_record(item, region))
    return records


def _collect_log_groups(client, region):
    records = []
    for page in _bounded_pages(client, "describe_log_groups"):
        for item in _required_collection(page, "logGroups", "CloudWatch log groups"):
            if len(records) >= MAX_ITEMS_PER_COLLECTION:
                raise ValueError("AWS log-group inventory exceeded the safety bound")
            records.append(_log_group_record(item, region))
    return records


def _upsert_asset(model, account, region, record, asset_type):
    asset, created = model.objects.get_or_create(
        owner=account,
        region=region,
        unique_id=record["unique_id"],
        defaults={
            "name": record["name"][:100],
            "type": asset_type,
            "metadata": record["metadata"],
            "monitoring": UtilAsset.Monitoring.ACTIVE,
        },
    )
    asset.name = record["name"][:100]
    asset.region = region
    asset.type = asset_type
    asset.metadata = record["metadata"]
    if not created and asset.monitoring == UtilAsset.Monitoring.NO_LONGER_EXISTS:
        asset.monitoring = UtilAsset.Monitoring.ACTIVE
    asset.save()
    return asset


def _reconcile_collection(model, account, region, records, asset_type):
    current_ids = []
    for record in records:
        _upsert_asset(model, account, region, record, asset_type)
        current_ids.append(record["unique_id"])
    model.objects.filter(owner=account, region=region).exclude(
        unique_id__in=current_ids
    ).update(monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS)
    return len(current_ids)


def _region_error(region, asset_type, error):
    return {
        "region": region,
        "assetType": asset_type,
        "errorCode": _safe_error_code(error),
        "error": redact_error_message(error),
    }


def _normalize_regions(regions):
    if not isinstance(regions, (list, tuple, set)):
        raise ValueError("AWS returned an invalid enabled-region collection")
    normalized = []
    for item in regions:
        if isinstance(item, dict):
            item = item.get("RegionName") or item.get("region") or item.get("name")
        item = _safe_text(item, limit=32)
        if not item:
            raise ValueError("AWS returned an enabled region without a name")
        normalized.append(item)
    return sorted(set(normalized))


def _cloudwatch_client(context, region):
    return aws_client(context, "cloudwatch", region=region)


def sync_aws_observability_assets(account):
    """Synchronize CloudWatch alarms, metrics, and log groups.

    Each collection is reconciled independently.  Consequently a malformed
    response or provider failure for one collection/Region cannot make the
    previous assets in that collection/Region appear missing.  Errors are
    returned as bounded codes so the integration lane can decide whether to
    retry or surface them.
    """

    errors = []
    try:
        regions = _normalize_regions(get_enabled_regions(account))
    except Exception as error:
        return {
            "regions": [],
            "counts": {asset_type: 0 for asset_type in OBSERVABILITY_ASSET_TYPES},
            "errors": [{"assetType": "region_discovery", "errorCode": _safe_error_code(error)}],
        }

    counts = {asset_type: 0 for asset_type in OBSERVABILITY_ASSET_TYPES}
    collection_specs = (
        (ASSET_TYPE_CLOUDWATCH_ALARM, CoreAWSCloudWatchAlarm, _collect_alarms),
        (ASSET_TYPE_CLOUDWATCH_METRIC, CoreAWSCloudWatchMetric, _collect_metrics),
        (ASSET_TYPE_LOG_GROUP, CoreAWSLogGroup, _collect_log_groups),
    )

    for region in regions:
        try:
            client = _cloudwatch_client(account, region)
        except Exception as error:
            for asset_type, _model, _collector in collection_specs:
                errors.append(_region_error(region, asset_type, error))
            continue

        for asset_type, model, collector in collection_specs:
            try:
                records = collector(client, region)
                counts[asset_type] += _reconcile_collection(
                    model,
                    account,
                    region,
                    records,
                    asset_type,
                )
            except Exception as error:
                # Do not call the reconciliation update after a failed or
                # incomplete collection.  Existing rows remain untouched.
                errors.append(_region_error(region, asset_type, error))
                logger.warning(
                    "AWS observability collection failed for %s/%s: %s",
                    region,
                    asset_type,
                    _safe_error_code(error),
                )

    return {"regions": regions, "counts": counts, "errors": errors}


AWS_OBSERVABILITY_ASSET_MODELS = {
    ASSET_TYPE_CLOUDWATCH_ALARM: CoreAWSCloudWatchAlarm,
    ASSET_TYPE_CLOUDWATCH_METRIC: CoreAWSCloudWatchMetric,
    ASSET_TYPE_LOG_GROUP: CoreAWSLogGroup,
}


__all__ = [
    "ASSET_TYPE_CLOUDWATCH_ALARM",
    "ASSET_TYPE_CLOUDWATCH_METRIC",
    "ASSET_TYPE_LOG_GROUP",
    "OBSERVABILITY_ASSET_TYPES",
    "CoreAWSObservabilityAsset",
    "CoreAWSCloudWatchAlarm",
    "CoreAWSCloudWatchMetric",
    "CoreAWSLogGroup",
    "CoreAWSCloudWatchLogGroup",
    "AWS_OBSERVABILITY_ASSET_MODELS",
    "sync_aws_observability_assets",
    "_alarm_record",
    "_metric_record",
    "_log_group_record",
]
