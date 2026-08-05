"""Read-only AWS account operations and FinOps inventory.

AWS Health, Support/Trusted Advisor, Cost Explorer, and Cost Anomaly
Detection are account-scoped control planes.  They are deliberately kept out
of the regional workload inventory: the endpoint region used by this module
is the AWS-supported ``us-east-1`` control-plane convention, while the
persisted ``scope`` field makes the account/global nature of the data
explicit.

The module is an integration boundary.  It owns the account-operation asset
models, bounded provider collectors, and a sync entry point, but it does not
wire itself into shared asset choices, account orchestration, UI, IAM policy,
or migrations.  The integration lane can register
``AWS_ACCOUNT_OPERATIONS_ASSET_MODELS`` and call
``sync_aws_account_operations_assets`` when those shared changes are ready.

Only the following AWS operations are used:

* Health: ``DescribeEvents`` and ``DescribeEventDetails``;
* Support: ``DescribeTrustedAdvisorChecks`` and
  ``DescribeTrustedAdvisorCheckResult`` or
  ``DescribeTrustedAdvisorCheckSummaries``; and
* Cost Explorer: ``GetCostAndUsage``, ``GetCostForecast``,
  ``GetDimensionValues``, ``GetAnomalyMonitors``,
  ``GetAnomalySubscriptions``, and ``GetAnomalies``.

No Cost/Support mutation operation is exposed or invoked.  Provider values
are selected into allowlisted metadata before serialization and are bounded a
second time before persistence.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import hashlib
import logging
import re

from botocore.exceptions import BotoCoreError, ClientError
from django.db import models

from apps.console.cloud.aws.discovery import (
    aws_client,
    aws_error_code,
    iter_pages,
    require_collection,
    serialize_aws,
)
from apps.console.cloud.aws.models import CoreAWSAccount
from apps.console.cloud.models import CloudInventoryTransientError
from apps.console.utils.models import UtilAsset
from apps.monitoring.metadata import redact_error_message, redact_sensitive_metadata


logger = logging.getLogger(__name__)


# Public asset types.  Anomaly findings have their own type because
# ``GetAnomalies`` returns provider identifiers that are useful for durable
# identity and status checks even when a monitor or subscription is absent.
AWS_HEALTH_EVENT = "aws_health_event"
AWS_TRUSTED_ADVISOR_CHECK = "aws_trusted_advisor_check"
AWS_COST_EXPLORER_SIGNAL = "aws_cost_explorer_signal"
AWS_COST_ANOMALY_MONITOR = "aws_cost_anomaly_monitor"
AWS_COST_ANOMALY_SUBSCRIPTION = "aws_cost_anomaly_subscription"
AWS_COST_ANOMALY = "aws_cost_anomaly"

# Descriptive aliases keep the integration surface friendly to callers that
# use service names rather than the persisted type names.
AWS_HEALTH = AWS_HEALTH_EVENT
AWS_TRUSTED_ADVISOR = AWS_TRUSTED_ADVISOR_CHECK
AWS_COST_EXPLORER = AWS_COST_EXPLORER_SIGNAL
AWS_COST_USAGE_SIGNAL = AWS_COST_EXPLORER_SIGNAL
AWS_ANOMALY_MONITOR = AWS_COST_ANOMALY_MONITOR
AWS_ANOMALY_SUBSCRIPTION = AWS_COST_ANOMALY_SUBSCRIPTION
AWS_ANOMALY = AWS_COST_ANOMALY

AWS_ACCOUNT_OPERATIONS_ASSET_TYPES = (
    AWS_HEALTH_EVENT,
    AWS_TRUSTED_ADVISOR_CHECK,
    AWS_COST_EXPLORER_SIGNAL,
    AWS_COST_ANOMALY_MONITOR,
    AWS_COST_ANOMALY_SUBSCRIPTION,
    AWS_COST_ANOMALY,
)
ACCOUNT_OPERATIONS_ASSET_TYPES = AWS_ACCOUNT_OPERATIONS_ASSET_TYPES
AWS_ACCOUNT_OPERATION_ASSET_TYPES = AWS_ACCOUNT_OPERATIONS_ASSET_TYPES


# Account operations are not ordinary regional uptime resources.  The
# control-plane region is retained for endpoint/audit context only.
AWS_ACCOUNT_SCOPE = "account"
AWS_GLOBAL_SCOPE = "global"
AWS_ACCOUNT_OPERATIONS_SCOPE = AWS_ACCOUNT_SCOPE
AWS_ACCOUNT_OPERATIONS_CONTROL_PLANE_REGION = "us-east-1"
AWS_ACCOUNT_OPERATIONS_REGION = AWS_ACCOUNT_OPERATIONS_CONTROL_PLANE_REGION

AWS_ACCOUNT_OPERATIONS_ENDPOINTS = {
    AWS_HEALTH_EVENT: {
        "service": "health",
        "region": AWS_ACCOUNT_OPERATIONS_CONTROL_PLANE_REGION,
        "scope": AWS_ACCOUNT_SCOPE,
    },
    AWS_TRUSTED_ADVISOR_CHECK: {
        "service": "support",
        "region": AWS_ACCOUNT_OPERATIONS_CONTROL_PLANE_REGION,
        "scope": AWS_ACCOUNT_SCOPE,
    },
    AWS_COST_EXPLORER_SIGNAL: {
        "service": "ce",
        "region": AWS_ACCOUNT_OPERATIONS_CONTROL_PLANE_REGION,
        "scope": AWS_ACCOUNT_SCOPE,
    },
    AWS_COST_ANOMALY_MONITOR: {
        "service": "ce",
        "region": AWS_ACCOUNT_OPERATIONS_CONTROL_PLANE_REGION,
        "scope": AWS_ACCOUNT_SCOPE,
    },
    AWS_COST_ANOMALY_SUBSCRIPTION: {
        "service": "ce",
        "region": AWS_ACCOUNT_OPERATIONS_CONTROL_PLANE_REGION,
        "scope": AWS_ACCOUNT_SCOPE,
    },
    AWS_COST_ANOMALY: {
        "service": "ce",
        "region": AWS_ACCOUNT_OPERATIONS_CONTROL_PLANE_REGION,
        "scope": AWS_ACCOUNT_SCOPE,
    },
}
AWS_ACCOUNT_OPERATIONS_GLOBAL_ASSET_TYPES = frozenset(AWS_ACCOUNT_OPERATIONS_ASSET_TYPES)
AWS_ACCOUNT_OPERATIONS_REGIONAL_ASSET_TYPES = frozenset()


# Bounds are intentionally conservative.  They protect both the provider
# call and local metadata/database writes from an unexpectedly large account.
MAX_PAGES_PER_COLLECTION = 32
MAX_ITEMS_PER_COLLECTION = 500
MAX_PROVIDER_ID_LENGTH = 512
MAX_NAME_LENGTH = 100
MAX_METADATA_ITEMS = 80
MAX_METADATA_LIST_ITEMS = 100
MAX_METADATA_DEPTH = 7
MAX_METADATA_TEXT = 2_048

HEALTH_LOOKBACK_DAYS = 30
MAX_HEALTH_EVENTS = 100
MAX_HEALTH_DETAIL_EVENTS = 100
HEALTH_DETAIL_BATCH_SIZE = 10

MAX_TRUSTED_ADVISOR_CHECKS = 100
MAX_TRUSTED_ADVISOR_METADATA_ITEMS = 50
MAX_TRUSTED_ADVISOR_RESULT_CALLS = 100
MAX_TRUSTED_ADVISOR_SUMMARY_BATCH = 100

COST_EXPLORER_LOOKBACK_DAYS = 7
COST_EXPLORER_FORECAST_DAYS = 7
MAX_COST_PERIODS = 31
MAX_COST_GROUPS_PER_PERIOD = 50
MAX_COST_METRICS = 8
MAX_COST_DIMENSION_VALUES = 100

ANOMALY_LOOKBACK_DAYS = 30
MAX_ANOMALY_MONITORS = 100
MAX_ANOMALY_SUBSCRIPTIONS = 100
MAX_ANOMALIES = 100
MAX_ANOMALY_ROOT_CAUSES = 20


_REGION_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)+$")
_OPERATION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_MUTATING_OPERATION_PREFIXES = (
    "abort",
    "accept",
    "allocate",
    "associate",
    "attach",
    "authorize",
    "cancel",
    "change",
    "complete",
    "copy",
    "create",
    "delete",
    "deregister",
    "detach",
    "disable",
    "disassociate",
    "enable",
    "execute",
    "import",
    "invite",
    "modify",
    "publish",
    "put",
    "reboot",
    "register",
    "reject",
    "release",
    "remove",
    "replace",
    "reset",
    "restore",
    "resume",
    "revoke",
    "run",
    "send",
    "set",
    "start",
    "stop",
    "suspend",
    "tag",
    "terminate",
    "untag",
    "update",
    "upload",
)
READ_ONLY_AWS_OPERATIONS = frozenset(
    {
        "describe_events",
        "describe_event_details",
        "describe_trusted_advisor_checks",
        "describe_trusted_advisor_check_result",
        "describe_trusted_advisor_check_summaries",
        "get_cost_and_usage",
        "get_cost_forecast",
        "get_dimension_values",
        "get_anomaly_monitors",
        "get_anomaly_subscriptions",
        "get_anomalies",
    }
)
AWS_ACCOUNT_OPERATIONS_READ_ONLY_OPERATIONS = READ_ONLY_AWS_OPERATIONS

_PERMISSION_ERROR_CODES = frozenset(
    {
        "AccessDenied",
        "AccessDeniedException",
        "AuthFailure",
        "ExpiredToken",
        "InvalidClientTokenId",
        "SubscriptionRequiredException",
        "UnsupportedOperationException",
        "UnrecognizedClientException",
        "UnauthorizedOperation",
    }
)
_NOT_FOUND_ERROR_CODES = frozenset(
    {
        "EventNotFoundException",
        "ResourceNotFoundException",
        "NoSuchEntity",
    }
)
_SENSITIVE_KEY_PARTS = (
    "accesskey",
    "secretkey",
    "password",
    "passphrase",
    "token",
    "privatekey",
    "credential",
    "authorization",
    "apikey",
    "email",
    "address",
)


def _owner_scope_constraint(name):
    return models.UniqueConstraint(
        fields=("owner", "scope", "provider_id"),
        # Keep the database identifier below common backend limits even for
        # the subscription model's long provider name.
        name=f"aws_aops_{name}_owner_scope_uid",
    )


class CoreAWSAccountOperationsAsset(UtilAsset):
    """Common account-scoped fields for Health, Support, and FinOps assets."""

    owner = models.ForeignKey(
        CoreAWSAccount,
        on_delete=models.CASCADE,
        related_name="%(class)s_assets",
    )
    scope = models.CharField(max_length=32, default=AWS_ACCOUNT_SCOPE)
    provider_id = models.CharField(max_length=MAX_PROVIDER_ID_LENGTH)
    control_plane_region = models.CharField(
        max_length=32,
        default=AWS_ACCOUNT_OPERATIONS_CONTROL_PLANE_REGION,
    )
    provider_status = models.CharField(max_length=64, blank=True, default="")
    asset_type = None
    provider_type = None

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self.type and self.asset_type:
            self.type = self.asset_type
        if self.metadata is not None:
            self.metadata = redact_sensitive_metadata(self.metadata)
        return super().save(*args, **kwargs)

    @property
    def monitoring_credentials(self):
        metadata = self.metadata if isinstance(self.metadata, dict) else {}
        return {
            "access_key": self.owner.access_key,
            "secret_key": self.owner.secret_key,
            "region": self.control_plane_region,
            "resource_region": self.control_plane_region,
            "control_plane_region": self.control_plane_region,
            "scope": self.scope,
            "provider_id": self.provider_id,
            "resource_name": self.name,
            "asset_type": self.type or self.asset_type,
            "provider_type": self.provider_type or self.asset_type,
            "metadata": metadata,
        }

    @property
    def provider_url(self):
        endpoint = AWS_ACCOUNT_OPERATIONS_ENDPOINTS.get(self.type or self.asset_type, {})
        service = endpoint.get("service", "ce")
        return f"https://{self.control_plane_region}.console.aws.amazon.com/{service}/home"

    def check_status(self):
        from apps.monitoring.checks.aws_account_operations import (
            AWS_ACCOUNT_OPERATIONS_CHECKS,
        )

        asset_type = self.type or self.asset_type
        checker = AWS_ACCOUNT_OPERATIONS_CHECKS.get(asset_type)
        if checker is None:
            return "error", {"errorCode": "UnsupportedAssetType"}
        return checker(self.unique_id, self.monitoring_credentials)

    def __str__(self):
        return self.name


class CoreAWSHealthEvent(CoreAWSAccountOperationsAsset):
    asset_type = provider_type = AWS_HEALTH_EVENT

    class Meta:
        db_table = "core_aws_health_event"
        constraints = [_owner_scope_constraint("health_event")]


class CoreAWSTrustedAdvisorCheck(CoreAWSAccountOperationsAsset):
    asset_type = provider_type = AWS_TRUSTED_ADVISOR_CHECK

    class Meta:
        db_table = "core_aws_trusted_advisor_check"
        constraints = [_owner_scope_constraint("trusted_advisor_check")]


class CoreAWSCostExplorerSignal(CoreAWSAccountOperationsAsset):
    asset_type = provider_type = AWS_COST_EXPLORER_SIGNAL

    class Meta:
        db_table = "core_aws_cost_explorer_signal"
        constraints = [_owner_scope_constraint("cost_explorer_signal")]


class CoreAWSCostAnomalyMonitor(CoreAWSAccountOperationsAsset):
    asset_type = provider_type = AWS_COST_ANOMALY_MONITOR

    class Meta:
        db_table = "core_aws_cost_anomaly_monitor"
        constraints = [_owner_scope_constraint("cost_anomaly_monitor")]


class CoreAWSCostAnomalySubscription(CoreAWSAccountOperationsAsset):
    asset_type = provider_type = AWS_COST_ANOMALY_SUBSCRIPTION

    class Meta:
        db_table = "core_aws_cost_anomaly_subscription"
        constraints = [_owner_scope_constraint("cost_anomaly_subscription")]


class CoreAWSCostAnomaly(CoreAWSAccountOperationsAsset):
    asset_type = provider_type = AWS_COST_ANOMALY

    class Meta:
        db_table = "core_aws_cost_anomaly"
        constraints = [_owner_scope_constraint("cost_anomaly")]


# Compatibility aliases for integration lanes that use shorter class names.
CoreAWSHealth = CoreAWSHealthEvent
CoreAWSTrustedAdvisor = CoreAWSTrustedAdvisorCheck
CoreAWSCostExplorer = CoreAWSCostExplorerSignal
CoreAWSAnomalyMonitor = CoreAWSCostAnomalyMonitor
CoreAWSAnomalySubscription = CoreAWSCostAnomalySubscription
CoreAWSAnomaly = CoreAWSCostAnomaly

AWS_ACCOUNT_OPERATIONS_ASSET_MODELS = {
    AWS_HEALTH_EVENT: CoreAWSHealthEvent,
    AWS_TRUSTED_ADVISOR_CHECK: CoreAWSTrustedAdvisorCheck,
    AWS_COST_EXPLORER_SIGNAL: CoreAWSCostExplorerSignal,
    AWS_COST_ANOMALY_MONITOR: CoreAWSCostAnomalyMonitor,
    AWS_COST_ANOMALY_SUBSCRIPTION: CoreAWSCostAnomalySubscription,
    AWS_COST_ANOMALY: CoreAWSCostAnomaly,
}


def assert_read_only_aws_operation(operation):
    """Reject every AWS operation outside this module's read allowlist.

    This is intentionally stricter than a verb-prefix check.  It protects
    direct calls as well as paginator calls and gives mocked tests a single
    guard they can exercise without constructing a boto3 client.
    """

    if not isinstance(operation, str) or _OPERATION_RE.fullmatch(operation) is None:
        raise ValueError("AWS operation is invalid")
    normalized = operation.lower()
    if normalized.startswith(_MUTATING_OPERATION_PREFIXES):
        raise ValueError("AWS mutation operations are not supported")
    if normalized not in READ_ONLY_AWS_OPERATIONS:
        raise ValueError("AWS operation is outside the account-operations read allowlist")
    return operation


# Private spelling is useful to tests and mirrors the shared discovery helper.
_assert_read_only_operation = assert_read_only_aws_operation


def _utc_now(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    if not isinstance(value, datetime):
        raise ValueError("account-operation time must be a datetime")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def account_operation_date_window(*, now=None, days):
    """Return a deterministic UTC date window as ``(start, end)`` strings.

    ``end`` is exclusive, which matches Cost Explorer and avoids querying a
    partially populated current day.  The helper is public so the shared
    integration and credential-free tests can assert exact request bounds.
    """

    if not isinstance(days, int) or days <= 0 or days > 366:
        raise ValueError("account-operation date-window bound is invalid")
    end = _utc_now(now).date()
    start = end - timedelta(days=days)
    return start.isoformat(), end.isoformat()


def _control_plane_region(regions):
    """Resolve one supported account endpoint without scanning workload Regions."""

    if regions is None:
        return AWS_ACCOUNT_OPERATIONS_CONTROL_PLANE_REGION
    if isinstance(regions, str):
        values = [regions]
    else:
        try:
            values = list(regions)
        except TypeError as error:
            raise CloudInventoryTransientError(
                "AWS account-operation endpoint selection is invalid"
            ) from error
    for value in values:
        if not isinstance(value, str) or _REGION_RE.fullmatch(value.strip()) is None:
            raise CloudInventoryTransientError(
                "AWS account-operation endpoint selection is invalid"
            )
    # The caller may pass the enabled-region list used by regional adapters.
    # It is deliberately ignored after validation: these APIs have one
    # account-level endpoint and must never be fan-out queried by this lane.
    return AWS_ACCOUNT_OPERATIONS_CONTROL_PLANE_REGION


def _safe_text(value, *, limit=MAX_METADATA_TEXT):
    if value is None:
        return None
    if isinstance(value, (dict, list, tuple, set)):
        raise CloudInventoryTransientError("AWS returned an invalid account-operation text value")
    return str(value)[:limit]


def _safe_redacted_text(value, *, limit=MAX_METADATA_TEXT):
    text = _safe_text(value, limit=limit)
    return redact_error_message(text) if text is not None else None


def _normalized_key(value):
    return "".join(character for character in str(value).lower() if character.isalnum())


def _is_sensitive_key(value):
    normalized = _normalized_key(value)
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _bounded_value(value, depth=0):
    """Bound a serialized provider value and drop secret-like keys."""

    if depth > MAX_METADATA_DEPTH:
        return "<truncated>"
    if isinstance(value, Mapping):
        result = {}
        for index, (key, child) in enumerate(value.items()):
            if index >= MAX_METADATA_ITEMS:
                result["_cloudmoo_truncated_items"] = True
                break
            key_text = str(key)[:128]
            if _is_sensitive_key(key_text):
                continue
            result[key_text] = _bounded_value(child, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        result = [_bounded_value(item, depth + 1) for item in value[:MAX_METADATA_LIST_ITEMS]]
        if len(value) > MAX_METADATA_LIST_ITEMS:
            result.append("<truncated>")
        return result
    if isinstance(value, str):
        return value[:MAX_METADATA_TEXT]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:MAX_METADATA_TEXT]


def _safe_serialized(value):
    serialized = serialize_aws(redact_sensitive_metadata(value))
    return _bounded_value(serialized)


def _required_provider_id(value, context):
    if not isinstance(value, str) or not value.strip():
        raise CloudInventoryTransientError(f"AWS returned an account-operation item without a {context} identifier")
    value = value.strip()
    if len(value) > MAX_PROVIDER_ID_LENGTH:
        raise CloudInventoryTransientError(f"AWS returned an oversized {context} identifier")
    return value


def _first_text(item, keys, context):
    if not isinstance(item, Mapping):
        raise CloudInventoryTransientError(f"AWS returned an invalid {context} item")
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return _required_provider_id(value, context)
    raise CloudInventoryTransientError(f"AWS returned an account-operation item without a {context} identifier")


def _safe_error_code(error):
    provider_error = getattr(error, "__cause__", None) or error
    try:
        code = aws_error_code(provider_error)
    except Exception:
        code = None
    return str(code or type(provider_error).__name__)[:128]


def _error_kind(error):
    """Classify without exposing provider messages or credentials."""

    provider_error = getattr(error, "__cause__", None)
    if isinstance(provider_error, (ClientError, BotoCoreError)):
        return "provider_error"
    if isinstance(error, (ClientError, BotoCoreError)):
        return "provider_error"
    if isinstance(error, CloudInventoryTransientError):
        return "incomplete_inventory"
    return "provider_error"


def _error_status(error):
    code = _safe_error_code(error)
    if code in _NOT_FOUND_ERROR_CODES:
        return "missing"
    if code in _PERMISSION_ERROR_CODES:
        return "invalid_access_token"
    return "error"


def _error_record(asset_type, region, error):
    return {
        "assetType": asset_type,
        "scope": AWS_ACCOUNT_SCOPE,
        "controlPlaneRegion": region,
        "errorCode": _safe_error_code(error),
        "errorKind": _error_kind(error),
    }


def _read_response(client, operation, **kwargs):
    assert_read_only_aws_operation(operation)
    method = getattr(client, operation)
    response = method(**kwargs)
    if not isinstance(response, Mapping):
        raise CloudInventoryTransientError(
            f"AWS returned an invalid {operation} response"
        )
    return response


def _bounded_pages(client, operation, **kwargs):
    """Yield bounded, mapping pages and reject malformed token streams."""

    assert_read_only_aws_operation(operation)
    seen_tokens = set()
    for page_number, page in enumerate(iter_pages(client, operation, **kwargs), start=1):
        if page_number > MAX_PAGES_PER_COLLECTION:
            raise CloudInventoryTransientError(
                f"AWS {operation} pagination exceeded the safety bound"
            )
        if not isinstance(page, Mapping):
            raise CloudInventoryTransientError(
                f"AWS returned an invalid {operation} page"
            )
        for token_key in ("NextToken", "nextToken", "NextPageToken", "nextPageToken"):
            if token_key not in page:
                continue
            token = page.get(token_key)
            if token in (None, ""):
                continue
            if not isinstance(token, str) or not token.strip() or token in seen_tokens:
                raise CloudInventoryTransientError(
                    f"AWS returned an invalid {operation} pagination token"
                )
            seen_tokens.add(token)
        yield page


def _paged_collection(client, operation, collection_key, context, *, limit, **kwargs):
    values = []
    for page in _bounded_pages(client, operation, **kwargs):
        items = require_collection(page, (collection_key,), context)
        if len(values) + len(items) > limit:
            raise CloudInventoryTransientError(
                f"AWS {context} inventory exceeded the safety bound"
            )
        for item in items:
            if not isinstance(item, Mapping):
                raise CloudInventoryTransientError(
                    f"AWS returned an invalid {context} item"
                )
        values.extend(items)
    return values


def _stable_unique_id(asset_type, scope, provider_id):
    raw = f"{scope}|{asset_type}|{provider_id}"
    if len(raw) <= 100:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"{scope}|{asset_type}|sha256:{digest}"[:100]


def _normalize_status(value, family):
    if value is None:
        return "unknown"
    raw = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    if not raw:
        return "unknown"
    mappings = {
        "health": {
            "open": "degraded",
            "upcoming": "pending",
            "closed": "available",
            "resolved": "available",
        },
        "trusted_advisor": {
            "ok": "available",
            "warning": "degraded",
            "error": "failed",
            "not_applicable": "available",
            "not_available": "error",
        },
        "monitor": {
            "active": "available",
            "enabled": "available",
            "inactive": "stopped",
            "disabled": "stopped",
        },
        "subscription": {
            "active": "available",
            "enabled": "available",
            "inactive": "stopped",
            "disabled": "stopped",
        },
        "anomaly": {
            "open": "degraded",
            "active": "degraded",
            "closed": "available",
            "resolved": "available",
        },
    }
    return mappings.get(family, {}).get(raw, raw[:64])


def _record(
    asset_type,
    provider_id,
    name,
    provider_status,
    metadata,
    region,
    *,
    scope=AWS_ACCOUNT_SCOPE,
):
    provider_id = _required_provider_id(provider_id, asset_type)
    name = _safe_text(name or provider_id, limit=MAX_NAME_LENGTH) or provider_id[:MAX_NAME_LENGTH]
    status = _safe_text(provider_status, limit=64) or "unknown"
    value = dict(metadata) if isinstance(metadata, Mapping) else {}
    value.update(
        {
            "_cloudmoo_scope": scope,
            "_cloudmoo_provider_id": provider_id,
            "_cloudmoo_raw_id": provider_id,
            "_cloudmoo_control_plane_region": region,
            "provider_id": provider_id,
            "scope": scope,
            "control_plane_region": region,
            "provider_status": status,
            "normalized_status": _normalize_status(status, _family_for_asset_type(asset_type)),
        }
    )
    return {
        "unique_id": _stable_unique_id(asset_type, scope, provider_id),
        "provider_id": provider_id,
        "scope": scope,
        "control_plane_region": region,
        "name": name,
        "provider_status": status,
        "metadata": _safe_serialized(value),
        "asset_type": asset_type,
    }


def _family_for_asset_type(asset_type):
    if asset_type == AWS_HEALTH_EVENT:
        return "health"
    if asset_type == AWS_TRUSTED_ADVISOR_CHECK:
        return "trusted_advisor"
    if asset_type == AWS_COST_ANOMALY_MONITOR:
        return "monitor"
    if asset_type == AWS_COST_ANOMALY_SUBSCRIPTION:
        return "subscription"
    if asset_type == AWS_COST_ANOMALY:
        return "anomaly"
    return "signal"


def _safe_selected(item, keys):
    if not isinstance(item, Mapping):
        raise CloudInventoryTransientError("AWS returned an invalid account-operation object")
    return {key: item[key] for key in keys if key in item and item[key] is not None}


def _safe_datetime_value(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return _safe_text(value, limit=128)


def _safe_health_event(item, detail):
    metadata = _safe_selected(
        item,
        (
            "arn",
            "eventArn",
            "service",
            "eventTypeCode",
            "eventTypeCategory",
            "region",
            "availabilityZone",
            "startTime",
            "endTime",
            "lastUpdatedTime",
            "statusCode",
            "eventScopeCode",
            "communicationId",
        ),
    )
    for key in ("startTime", "endTime", "lastUpdatedTime"):
        if key in metadata:
            metadata[key] = _safe_datetime_value(metadata[key])
    if isinstance(detail, Mapping):
        description = detail.get("latestDescription")
        if description is None:
            description = detail.get("eventDescription")
        if description is not None:
            metadata["latestDescription"] = _safe_redacted_text(
                description,
                limit=MAX_METADATA_TEXT,
            )
        if detail.get("language") is not None:
            metadata["language"] = _safe_text(detail.get("language"), limit=32)
        if detail.get("latestDescriptionUpdateTime") is not None:
            metadata["latestDescriptionUpdateTime"] = _safe_datetime_value(
                detail.get("latestDescriptionUpdateTime")
            )
    return metadata


def _health_details(client, event_ids):
    details = {}
    for offset in range(0, len(event_ids), HEALTH_DETAIL_BATCH_SIZE):
        batch = event_ids[offset : offset + HEALTH_DETAIL_BATCH_SIZE]
        response = _read_response(
            client,
            "describe_event_details",
            eventArns=batch,
            locale="en",
        )
        successful = require_collection(response, ("successfulSet",), "AWS Health event details")
        if len(successful) > HEALTH_DETAIL_BATCH_SIZE:
            raise CloudInventoryTransientError(
                "AWS Health event detail response exceeded the safety bound"
            )
        failed = response.get("failedSet", [])
        if not isinstance(failed, list):
            raise CloudInventoryTransientError(
                "AWS returned an invalid AWS Health event detail failure collection"
            )
        if failed:
            # A detail failure is not an empty event inventory.  Keep the
            # whole family unreconciled so a permission/unsupported response
            # cannot mark prior events missing.
            raise CloudInventoryTransientError(
                "AWS Health event details were incomplete"
            )
        for item in successful:
            if not isinstance(item, Mapping):
                raise CloudInventoryTransientError(
                    "AWS returned an invalid AWS Health event detail"
                )
            event_id = item.get("eventArn") or item.get("arn")
            event_id = _required_provider_id(event_id, "AWS Health event")
            detail = item.get("eventDescription")
            if detail is None:
                detail = item
            if not isinstance(detail, Mapping):
                raise CloudInventoryTransientError(
                    "AWS returned an invalid AWS Health event description"
                )
            details[event_id] = detail
    return details


def collect_aws_health_events(account, region=AWS_ACCOUNT_OPERATIONS_CONTROL_PLANE_REGION, *, now=None):
    """Collect bounded recent AWS Health events from the account endpoint."""

    current = _utc_now(now)
    start = current - timedelta(days=HEALTH_LOOKBACK_DAYS)
    client = aws_client(account, "health", region=region)
    events = _paged_collection(
        client,
        "describe_events",
        "events",
        "AWS Health events",
        limit=MAX_HEALTH_EVENTS,
        filter={
            "startTimes": [{"from": start, "to": current}],
            "eventStatusCodes": ["open", "upcoming", "closed"],
        },
        maxResults=MAX_HEALTH_EVENTS,
        locale="en",
    )
    event_ids = []
    unique_events = []
    seen = set()
    for item in events:
        event_id = _first_text(item, ("arn", "eventArn"), "AWS Health event")
        if event_id in seen:
            continue
        seen.add(event_id)
        event_ids.append(event_id)
        unique_events.append(item)
    if len(event_ids) > MAX_HEALTH_DETAIL_EVENTS:
        raise CloudInventoryTransientError("AWS Health event detail bound was exceeded")
    details = _health_details(client, event_ids) if event_ids else {}

    records = []
    for item, event_id in zip(unique_events, event_ids):
        status = item.get("statusCode") or item.get("status") or "unknown"
        name = item.get("eventTypeCode") or item.get("service") or event_id
        records.append(
            _record(
                AWS_HEALTH_EVENT,
                event_id,
                name,
                status,
                _safe_health_event(item, details.get(event_id)),
                region,
            )
        )
    return records


def _safe_trusted_advisor_definition(item):
    metadata = _safe_selected(
        item,
        ("id", "checkId", "name", "category", "description", "status"),
    )
    if "description" in metadata:
        metadata["description"] = _safe_redacted_text(metadata["description"], limit=512)
    raw_metadata = item.get("metadata")
    if raw_metadata is not None:
        if not isinstance(raw_metadata, list):
            raise CloudInventoryTransientError(
                "AWS returned an invalid Trusted Advisor metadata collection"
            )
        metadata["metadata"] = [
            _safe_text(value, limit=256)
            for value in raw_metadata[:MAX_TRUSTED_ADVISOR_METADATA_ITEMS]
        ]
        if len(raw_metadata) > MAX_TRUSTED_ADVISOR_METADATA_ITEMS:
            metadata["metadata_truncated"] = True
    return metadata


def _safe_trusted_advisor_result(result):
    if not isinstance(result, Mapping):
        raise CloudInventoryTransientError(
            "AWS returned an invalid Trusted Advisor check result"
        )
    metadata = _safe_selected(
        result,
        ("checkId", "status", "category", "hasFlaggedResources"),
    )
    summary = result.get("resourcesSummary")
    if summary is not None:
        if not isinstance(summary, Mapping):
            raise CloudInventoryTransientError(
                "AWS returned an invalid Trusted Advisor resources summary"
            )
        metadata["resourcesSummary"] = _safe_selected(
            summary,
            (
                "resourcesProcessed",
                "resourcesFlagged",
                "resourcesIgnored",
                "resourcesSuppressed",
            ),
        )
    # ``flaggedResources`` contains resource identifiers and arbitrary
    # provider metadata.  Store only a bounded count, never the raw list.
    if "flaggedResources" in result:
        flagged = result["flaggedResources"]
        if not isinstance(flagged, list):
            raise CloudInventoryTransientError(
                "AWS returned an invalid Trusted Advisor flagged-resource collection"
            )
        metadata["flaggedResourceCount"] = len(flagged)
    return metadata


def _trusted_advisor_details(client, check_ids):
    details = {}
    # The result endpoint is supported by the long-standing Support API and
    # avoids treating a supported check as empty merely because summaries are
    # unavailable.  A client without that operation may use summaries.
    try:
        result_method = getattr(client, "describe_trusted_advisor_check_result")
    except AttributeError:
        result_method = None

    if callable(result_method):
        for index, check_id in enumerate(check_ids):
            if index >= MAX_TRUSTED_ADVISOR_RESULT_CALLS:
                raise CloudInventoryTransientError(
                    "Trusted Advisor result bound was exceeded"
                )
            response = _read_response(
                client,
                "describe_trusted_advisor_check_result",
                checkId=check_id,
                language="en",
            )
            result = response.get("result")
            if not isinstance(result, Mapping):
                raise CloudInventoryTransientError(
                    "AWS returned an invalid Trusted Advisor check result"
                )
            details[check_id] = _safe_trusted_advisor_result(result)
        return details

    try:
        summary_method = getattr(client, "describe_trusted_advisor_check_summaries")
    except AttributeError as error:
        raise CloudInventoryTransientError(
            "AWS Trusted Advisor result and summary APIs are unavailable"
        ) from error
    if not callable(summary_method):
        raise CloudInventoryTransientError(
            "AWS Trusted Advisor summary API is unavailable"
        )
    for offset in range(0, len(check_ids), MAX_TRUSTED_ADVISOR_SUMMARY_BATCH):
        batch = check_ids[offset : offset + MAX_TRUSTED_ADVISOR_SUMMARY_BATCH]
        response = _read_response(
            client,
            "describe_trusted_advisor_check_summaries",
            checkIds=batch,
        )
        summaries = require_collection(
            response,
            ("summaries",),
            "Trusted Advisor check summaries",
        )
        for summary in summaries:
            if not isinstance(summary, Mapping):
                raise CloudInventoryTransientError(
                    "AWS returned an invalid Trusted Advisor check summary"
                )
            check_id = _first_text(summary, ("checkId", "id"), "Trusted Advisor check")
            details[check_id] = _safe_trusted_advisor_result(summary)
    return details


def collect_aws_trusted_advisor_checks(account, region=AWS_ACCOUNT_OPERATIONS_CONTROL_PLANE_REGION):
    """Collect bounded Trusted Advisor definitions and result summaries."""

    client = aws_client(account, "support", region=region)
    response = _read_response(
        client,
        "describe_trusted_advisor_checks",
        language="en",
    )
    checks = require_collection(response, ("checks",), "Trusted Advisor checks")
    if len(checks) > MAX_TRUSTED_ADVISOR_CHECKS:
        raise CloudInventoryTransientError("Trusted Advisor check bound was exceeded")
    definitions = {}
    check_ids = []
    for item in checks:
        check_id = _first_text(item, ("id", "checkId"), "Trusted Advisor check")
        if check_id in definitions:
            continue
        definitions[check_id] = item
        check_ids.append(check_id)
    details = _trusted_advisor_details(client, check_ids) if check_ids else {}

    records = []
    for check_id in check_ids:
        definition = definitions[check_id]
        detail = details.get(check_id, {})
        metadata = _safe_trusted_advisor_definition(definition)
        metadata.update(detail)
        status = detail.get("status") or definition.get("status") or "unknown"
        name = definition.get("name") or definition.get("category") or check_id
        records.append(
            _record(
                AWS_TRUSTED_ADVISOR_CHECK,
                check_id,
                name,
                status,
                metadata,
                region,
            )
        )
    return records


def _safe_cost_metric(value):
    if not isinstance(value, Mapping):
        raise CloudInventoryTransientError("AWS returned an invalid Cost Explorer metric")
    return _safe_selected(value, ("Amount", "Unit"))


def _safe_cost_totals(value):
    if not isinstance(value, Mapping):
        raise CloudInventoryTransientError("AWS returned an invalid Cost Explorer totals object")
    result = {}
    for index, (metric, metric_value) in enumerate(value.items()):
        if index >= MAX_COST_METRICS:
            break
        if metric not in {"UnblendedCost", "UsageQuantity", "AmortizedCost", "BlendedCost"}:
            continue
        result[str(metric)[:64]] = _safe_cost_metric(metric_value)
    return result


def _safe_cost_period(item):
    if not isinstance(item, Mapping):
        raise CloudInventoryTransientError("AWS returned an invalid Cost Explorer time period")
    metadata = {}
    period = item.get("TimePeriod")
    if not isinstance(period, Mapping):
        raise CloudInventoryTransientError("AWS returned an invalid Cost Explorer time period")
    metadata["TimePeriod"] = _safe_selected(period, ("Start", "End"))
    if "Estimated" in item:
        metadata["Estimated"] = bool(item["Estimated"])
    if "Total" in item:
        metadata["Total"] = _safe_cost_totals(item["Total"])
    groups = item.get("Groups", [])
    if not isinstance(groups, list):
        raise CloudInventoryTransientError("AWS returned an invalid Cost Explorer groups collection")
    safe_groups = []
    for group in groups[:MAX_COST_GROUPS_PER_PERIOD]:
        if not isinstance(group, Mapping):
            raise CloudInventoryTransientError("AWS returned an invalid Cost Explorer group")
        safe_group = {}
        keys = group.get("Keys", [])
        if not isinstance(keys, list):
            raise CloudInventoryTransientError("AWS returned an invalid Cost Explorer group keys collection")
        safe_group["Keys"] = [_safe_text(key, limit=256) for key in keys[:10]]
        if "Metrics" in group:
            safe_group["Metrics"] = _safe_cost_totals(group["Metrics"])
        safe_groups.append(safe_group)
    if len(groups) > MAX_COST_GROUPS_PER_PERIOD:
        safe_groups.append({"_cloudmoo_truncated": True})
    metadata["Groups"] = safe_groups
    return metadata


def _safe_forecast(response):
    if not isinstance(response, Mapping):
        raise CloudInventoryTransientError("AWS returned an invalid Cost Explorer forecast response")
    value = _safe_selected(
        response,
        ("Total", "PredictionIntervalLowerBound", "PredictionIntervalUpperBound", "ForecastResultsByTime"),
    )
    if "Total" in value:
        value["Total"] = _safe_cost_metric(value["Total"])
    for key in ("PredictionIntervalLowerBound", "PredictionIntervalUpperBound"):
        if key in value:
            value[key] = _safe_text(value[key], limit=128)
    if "ForecastResultsByTime" in value:
        forecasts = value["ForecastResultsByTime"]
        if not isinstance(forecasts, list):
            raise CloudInventoryTransientError("AWS returned an invalid Cost Explorer forecast collection")
        safe_forecasts = []
        for item in forecasts[:MAX_COST_PERIODS]:
            if not isinstance(item, Mapping):
                raise CloudInventoryTransientError("AWS returned an invalid Cost Explorer forecast item")
            safe_forecasts.append(
                _safe_selected(item, ("TimePeriod", "MeanValue", "PredictionIntervalLowerBound", "PredictionIntervalUpperBound"))
            )
        value["ForecastResultsByTime"] = safe_forecasts
    return value


def _safe_dimension_value(item):
    if not isinstance(item, Mapping):
        raise CloudInventoryTransientError("AWS returned an invalid Cost Explorer dimension value")
    result = _safe_selected(item, ("Value", "Attributes"))
    if "Attributes" in result:
        attributes = result["Attributes"]
        if not isinstance(attributes, Mapping):
            raise CloudInventoryTransientError("AWS returned an invalid Cost Explorer dimension attributes object")
        result["Attributes"] = _safe_selected(attributes, ("unit", "description"))
        if "description" in result["Attributes"]:
            result["Attributes"]["description"] = _safe_redacted_text(
                result["Attributes"]["description"], limit=512
            )
    if "Value" in result:
        result["Value"] = _safe_text(result["Value"], limit=256)
    return result


def collect_aws_cost_explorer_signals(
    account,
    region=AWS_ACCOUNT_OPERATIONS_CONTROL_PLANE_REGION,
    *,
    now=None,
):
    """Collect one bounded deterministic Cost Explorer usage/cost signal."""

    start, end = account_operation_date_window(
        now=now,
        days=COST_EXPLORER_LOOKBACK_DAYS,
    )
    forecast_end = (
        datetime.fromisoformat(end).date() + timedelta(days=COST_EXPLORER_FORECAST_DAYS)
    ).isoformat()
    client = aws_client(account, "ce", region=region)
    time_period = {"Start": start, "End": end}
    usage_periods = _paged_collection(
        client,
        "get_cost_and_usage",
        "ResultsByTime",
        "Cost Explorer usage and cost results",
        limit=MAX_COST_PERIODS,
        TimePeriod=time_period,
        Granularity="DAILY",
        Metrics=["UnblendedCost", "UsageQuantity"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )
    safe_periods = [_safe_cost_period(item) for item in usage_periods]

    forecast_response = _read_response(
        client,
        "get_cost_forecast",
        TimePeriod={"Start": end, "End": forecast_end},
        Metric="UNBLENDED_COST",
        Granularity="DAILY",
        PredictionIntervalLevel=80,
    )
    forecast = _safe_forecast(forecast_response)

    dimension_values = _paged_collection(
        client,
        "get_dimension_values",
        "DimensionValues",
        "Cost Explorer service dimension values",
        limit=MAX_COST_DIMENSION_VALUES,
        TimePeriod=time_period,
        Dimension="SERVICE",
        Context="COST_AND_USAGE",
        MaxResults=MAX_COST_DIMENSION_VALUES,
    )
    safe_dimensions = [_safe_dimension_value(item) for item in dimension_values]
    provider_id = f"usage-cost:{start}:{end}:daily:service"
    metadata = {
        "time_period": time_period,
        "forecast_period": {"Start": end, "End": forecast_end},
        "granularity": "DAILY",
        "metrics": ["UnblendedCost", "UsageQuantity"],
        "group_by": [{"Type": "DIMENSION", "Key": "SERVICE"}],
        "results_by_time": safe_periods,
        "forecast": forecast,
        "dimension": "SERVICE",
        "dimension_values": safe_dimensions,
    }
    return [
        _record(
            AWS_COST_EXPLORER_SIGNAL,
            provider_id,
            f"Cost Explorer {start} to {end}",
            "available",
            metadata,
            region,
        )
    ]


def _safe_anomaly_monitor(item):
    metadata = _safe_selected(
        item,
        (
            "MonitorArn",
            "MonitorName",
            "MonitorType",
            "MonitorDimension",
            "DimensionalValueCount",
            "CreationDate",
            "LastUpdatedDate",
        ),
    )
    for key in ("CreationDate", "LastUpdatedDate"):
        if key in metadata:
            metadata[key] = _safe_datetime_value(metadata[key])
    return metadata


def _safe_anomaly_subscription(item):
    metadata = _safe_selected(
        item,
        (
            "SubscriptionArn",
            "SubscriptionName",
            "MonitorArn",
            "Threshold",
            "Frequency",
            "Status",
            "CreationDate",
            "LastUpdatedDate",
        ),
    )
    subscribers = item.get("Subscribers")
    if subscribers is not None:
        if not isinstance(subscribers, list):
            raise CloudInventoryTransientError("AWS returned an invalid anomaly subscription subscriber collection")
        metadata["subscriberCount"] = len(subscribers)
        metadata["subscriberTypes"] = [
            _safe_text(value.get("Type"), limit=32)
            for value in subscribers[:MAX_METADATA_LIST_ITEMS]
            if isinstance(value, Mapping) and value.get("Type") is not None
        ]
    for key in ("CreationDate", "LastUpdatedDate"):
        if key in metadata:
            metadata[key] = _safe_datetime_value(metadata[key])
    return metadata


def _safe_anomaly(item):
    metadata = _safe_selected(
        item,
        (
            "AnomalyId",
            "AnomalyStartDate",
            "AnomalyEndDate",
            "MonitorArn",
            "DimensionValue",
            "TotalImpact",
            "Impact",
            "Feedback",
        ),
    )
    for key in ("AnomalyStartDate", "AnomalyEndDate"):
        if key in metadata:
            metadata[key] = _safe_datetime_value(metadata[key])
    for key in ("TotalImpact", "Impact"):
        if key in metadata and isinstance(metadata[key], Mapping):
            metadata[key] = _safe_selected(
                metadata[key],
                ("TotalActualSpend", "TotalExpectedSpend", "TotalImpact", "MaxImpact"),
            )
    root_causes = item.get("RootCauses")
    if root_causes is not None:
        if not isinstance(root_causes, list):
            raise CloudInventoryTransientError("AWS returned an invalid anomaly root-cause collection")
        safe_causes = []
        for cause in root_causes[:MAX_ANOMALY_ROOT_CAUSES]:
            if not isinstance(cause, Mapping):
                raise CloudInventoryTransientError("AWS returned an invalid anomaly root cause")
            safe_causes.append(
                _safe_selected(
                    cause,
                    ("Service", "Region", "UsageType", "LinkedAccount", "ApiName"),
                )
            )
        metadata["rootCauseCount"] = len(root_causes)
        metadata["rootCauses"] = safe_causes
    return metadata


def collect_aws_cost_anomaly_assets(
    account,
    region=AWS_ACCOUNT_OPERATIONS_CONTROL_PLANE_REGION,
    *,
    now=None,
):
    """Collect monitors, subscriptions, and bounded anomaly metadata."""

    start, end = account_operation_date_window(now=now, days=ANOMALY_LOOKBACK_DAYS)
    client = aws_client(account, "ce", region=region)
    monitors = _paged_collection(
        client,
        "get_anomaly_monitors",
        "AnomalyMonitors",
        "Cost Anomaly Detection monitors",
        limit=MAX_ANOMALY_MONITORS,
        MaxResults=MAX_ANOMALY_MONITORS,
    )
    subscriptions = _paged_collection(
        client,
        "get_anomaly_subscriptions",
        "AnomalySubscriptions",
        "Cost Anomaly Detection subscriptions",
        limit=MAX_ANOMALY_SUBSCRIPTIONS,
        MaxResults=MAX_ANOMALY_SUBSCRIPTIONS,
    )
    anomalies = _paged_collection(
        client,
        "get_anomalies",
        "Anomalies",
        "Cost Anomaly Detection anomalies",
        limit=MAX_ANOMALIES,
        DateInterval={"StartDate": start, "EndDate": end},
        MaxResults=MAX_ANOMALIES,
    )

    anomaly_records = []
    anomaly_ids_by_monitor = {}
    for item in anomalies:
        anomaly_id = _first_text(item, ("AnomalyId", "Id"), "Cost Anomaly Detection anomaly")
        metadata = _safe_anomaly(item)
        metadata["date_interval"] = {"StartDate": start, "EndDate": end}
        monitor_arn = item.get("MonitorArn")
        if isinstance(monitor_arn, str) and monitor_arn:
            anomaly_ids_by_monitor.setdefault(monitor_arn, []).append(anomaly_id)
        status = "degraded"
        anomaly_records.append(
            _record(
                AWS_COST_ANOMALY,
                anomaly_id,
                f"Cost anomaly {anomaly_id}",
                status,
                metadata,
                region,
            )
        )

    records = []
    for item in monitors:
        provider_id = _first_text(item, ("MonitorArn", "MonitorId"), "Cost Anomaly Detection monitor")
        metadata = _safe_anomaly_monitor(item)
        anomaly_ids = anomaly_ids_by_monitor.get(provider_id, [])
        metadata["anomalyCount"] = len(anomaly_ids)
        metadata["anomalyIds"] = anomaly_ids[:MAX_ANOMALIES]
        status = item.get("Status") or item.get("MonitorStatus") or "active"
        records.append(
            _record(
                AWS_COST_ANOMALY_MONITOR,
                provider_id,
                item.get("MonitorName") or provider_id,
                status,
                metadata,
                region,
            )
        )
    for item in subscriptions:
        provider_id = _first_text(
            item,
            ("SubscriptionArn", "SubscriptionId"),
            "Cost Anomaly Detection subscription",
        )
        records.append(
            _record(
                AWS_COST_ANOMALY_SUBSCRIPTION,
                provider_id,
                item.get("SubscriptionName") or provider_id,
                item.get("Status") or "active",
                _safe_anomaly_subscription(item),
                region,
            )
        )
    return records + anomaly_records


def _upsert_asset(model, account, record):
    defaults = {
        "unique_id": record["unique_id"],
        "name": record["name"][:MAX_NAME_LENGTH],
        "type": record["asset_type"],
        "metadata": record["metadata"],
        "monitoring": UtilAsset.Monitoring.ACTIVE,
        "scope": record["scope"],
        "provider_id": record["provider_id"],
        "control_plane_region": record["control_plane_region"],
        "provider_status": record["provider_status"],
    }
    asset, _created = model.objects.get_or_create(
        owner=account,
        scope=record["scope"],
        provider_id=record["provider_id"],
        defaults=defaults,
    )
    asset.unique_id = record["unique_id"]
    asset.name = record["name"][:MAX_NAME_LENGTH]
    asset.type = record["asset_type"]
    asset.metadata = record["metadata"]
    asset.monitoring = UtilAsset.Monitoring.ACTIVE
    asset.scope = record["scope"]
    asset.provider_id = record["provider_id"]
    asset.control_plane_region = record["control_plane_region"]
    asset.provider_status = record["provider_status"]
    asset.save()
    return asset


def _reconcile(model, account, records):
    current_ids = set()
    for record in records:
        _upsert_asset(model, account, record)
        current_ids.add(record["provider_id"])
    model.objects.filter(owner=account, scope=AWS_ACCOUNT_SCOPE).exclude(
        provider_id__in=current_ids
    ).update(monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS)
    return len(records)


def _record_family_error(result, asset_types, region, error):
    for asset_type in asset_types:
        result["errors"].append(_error_record(asset_type, region, error))
        result["families"][asset_type] = {
            "scope": AWS_ACCOUNT_SCOPE,
            "controlPlaneRegion": region,
            "reconciled": False,
            "count": 0,
            "errorCode": _safe_error_code(error),
        }


def sync_aws_account_operations_assets(account, regions=None, *, now=None):
    """Synchronize account-scoped AWS operations without regional fan-out.

    ``regions`` is accepted so the shared integration can pass its normal
    sync context.  It is validated for shape but intentionally does not drive
    endpoint fan-out; Health/Support/Cost Explorer are queried only once at
    the account control-plane endpoint.
    """

    result = {
        "scope": AWS_ACCOUNT_SCOPE,
        "controlPlaneRegion": AWS_ACCOUNT_OPERATIONS_CONTROL_PLANE_REGION,
        "regions": [AWS_ACCOUNT_OPERATIONS_CONTROL_PLANE_REGION],
        "counts": {asset_type: 0 for asset_type in AWS_ACCOUNT_OPERATIONS_ASSET_TYPES},
        "synced": {asset_type: 0 for asset_type in AWS_ACCOUNT_OPERATIONS_ASSET_TYPES},
        "errors": [],
        "families": {},
    }
    try:
        region = _control_plane_region(regions)
    except Exception as error:
        _record_family_error(result, AWS_ACCOUNT_OPERATIONS_ASSET_TYPES, AWS_ACCOUNT_OPERATIONS_CONTROL_PLANE_REGION, error)
        return result
    result["controlPlaneRegion"] = region
    result["regions"] = [region]

    collection_specs = (
        (AWS_HEALTH_EVENT, CoreAWSHealthEvent, collect_aws_health_events),
        (AWS_TRUSTED_ADVISOR_CHECK, CoreAWSTrustedAdvisorCheck, collect_aws_trusted_advisor_checks),
        (AWS_COST_EXPLORER_SIGNAL, CoreAWSCostExplorerSignal, collect_aws_cost_explorer_signals),
    )
    for asset_type, model, collector in collection_specs:
        try:
            if asset_type in {AWS_HEALTH_EVENT, AWS_COST_EXPLORER_SIGNAL}:
                records = collector(account, region, now=now)
            else:
                records = collector(account, region)
            count = _reconcile(model, account, records)
            result["counts"][asset_type] = count
            result["synced"][asset_type] = count
            result["families"][asset_type] = {
                "scope": AWS_ACCOUNT_SCOPE,
                "controlPlaneRegion": region,
                "reconciled": True,
                "count": count,
            }
        except Exception as error:
            _record_family_error(result, (asset_type,), region, error)
            logger.warning(
                "AWS account-operation collection failed for %s (%s)",
                asset_type,
                _safe_error_code(error),
            )

    anomaly_types = (
        AWS_COST_ANOMALY_MONITOR,
        AWS_COST_ANOMALY_SUBSCRIPTION,
        AWS_COST_ANOMALY,
    )
    try:
        records = collect_aws_cost_anomaly_assets(account, region, now=now)
        records_by_type = {asset_type: [] for asset_type in anomaly_types}
        for record in records:
            asset_type = record["asset_type"]
            if asset_type in records_by_type:
                records_by_type[asset_type].append(record)
        for asset_type in anomaly_types:
            model = AWS_ACCOUNT_OPERATIONS_ASSET_MODELS[asset_type]
            count = _reconcile(model, account, records_by_type[asset_type])
            result["counts"][asset_type] = count
            result["synced"][asset_type] = count
            result["families"][asset_type] = {
                "scope": AWS_ACCOUNT_SCOPE,
                "controlPlaneRegion": region,
                "reconciled": True,
                "count": count,
            }
    except Exception as error:
        _record_family_error(result, anomaly_types, region, error)
        logger.warning(
            "AWS account-operation collection failed for cost anomaly detection (%s)",
            _safe_error_code(error),
        )
    return result


# Integration-friendly aliases used by adjacent provider lanes.
sync_aws_account_operations = sync_aws_account_operations_assets
sync_aws_account_operations_inventory = sync_aws_account_operations_assets
sync_aws_finops_assets = sync_aws_account_operations_assets


__all__ = [
    "ACCOUNT_OPERATIONS_ASSET_TYPES",
    "AWS_ACCOUNT_OPERATIONS_ASSET_MODELS",
    "AWS_ACCOUNT_OPERATIONS_ASSET_TYPES",
    "AWS_ACCOUNT_OPERATIONS_CONTROL_PLANE_REGION",
    "AWS_ACCOUNT_OPERATIONS_ENDPOINTS",
    "AWS_ACCOUNT_OPERATIONS_GLOBAL_ASSET_TYPES",
    "AWS_ACCOUNT_OPERATIONS_READ_ONLY_OPERATIONS",
    "AWS_ACCOUNT_OPERATIONS_REGION",
    "AWS_ACCOUNT_OPERATIONS_SCOPE",
    "AWS_ACCOUNT_OPERATIONS_REGIONAL_ASSET_TYPES",
    "AWS_ACCOUNT_SCOPE",
    "AWS_ANOMALY",
    "AWS_ANOMALY_MONITOR",
    "AWS_ANOMALY_SUBSCRIPTION",
    "AWS_COST_ANOMALY",
    "AWS_COST_ANOMALY_MONITOR",
    "AWS_COST_ANOMALY_SUBSCRIPTION",
    "AWS_COST_EXPLORER",
    "AWS_COST_EXPLORER_SIGNAL",
    "AWS_COST_USAGE_SIGNAL",
    "AWS_GLOBAL_SCOPE",
    "AWS_HEALTH",
    "AWS_HEALTH_EVENT",
    "AWS_TRUSTED_ADVISOR",
    "AWS_TRUSTED_ADVISOR_CHECK",
    "CoreAWSAccountOperationsAsset",
    "CoreAWSAnomaly",
    "CoreAWSAnomalyMonitor",
    "CoreAWSAnomalySubscription",
    "CoreAWSCostAnomaly",
    "CoreAWSCostAnomalyMonitor",
    "CoreAWSCostAnomalySubscription",
    "CoreAWSCostExplorer",
    "CoreAWSCostExplorerSignal",
    "CoreAWSHealth",
    "CoreAWSHealthEvent",
    "CoreAWSTrustedAdvisor",
    "CoreAWSTrustedAdvisorCheck",
    "READ_ONLY_AWS_OPERATIONS",
    "account_operation_date_window",
    "assert_read_only_aws_operation",
    "collect_aws_cost_anomaly_assets",
    "collect_aws_cost_explorer_signals",
    "collect_aws_health_events",
    "collect_aws_trusted_advisor_checks",
    "sync_aws_account_operations",
    "sync_aws_account_operations_assets",
    "sync_aws_account_operations_inventory",
    "sync_aws_finops_assets",
]
