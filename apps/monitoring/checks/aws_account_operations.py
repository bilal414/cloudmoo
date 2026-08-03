"""Read-only status checks for AWS account operations and FinOps signals."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta

from apps.console.cloud.aws.account_operations import (
    ANOMALY_LOOKBACK_DAYS,
    AWS_ACCOUNT_OPERATIONS_CONTROL_PLANE_REGION,
    AWS_ACCOUNT_SCOPE,
    AWS_COST_ANOMALY,
    AWS_COST_ANOMALY_MONITOR,
    AWS_COST_ANOMALY_SUBSCRIPTION,
    AWS_COST_EXPLORER_SIGNAL,
    AWS_HEALTH_EVENT,
    AWS_TRUSTED_ADVISOR_CHECK,
    COST_EXPLORER_FORECAST_DAYS,
    COST_EXPLORER_LOOKBACK_DAYS,
    MAX_ANOMALIES,
    MAX_ANOMALY_MONITORS,
    MAX_ANOMALY_SUBSCRIPTIONS,
    MAX_COST_DIMENSION_VALUES,
    MAX_COST_PERIODS,
    MAX_HEALTH_EVENTS,
    MAX_PAGES_PER_COLLECTION,
    _error_status,
    _safe_anomaly,
    _safe_anomaly_monitor,
    _safe_anomaly_subscription,
    _safe_cost_period,
    _safe_dimension_value,
    _safe_forecast,
    _safe_health_event,
    _safe_serialized,
    _safe_trusted_advisor_result,
    account_operation_date_window,
    assert_read_only_aws_operation,
    aws_client,
    aws_error_code,
)
from apps.console.cloud.aws.discovery import iter_pages, require_collection
from apps.console.cloud.models import CloudInventoryTransientError


class _CredentialAccount:
    """Account-shaped object for the shared discovery client helper."""

    def __init__(self, credentials, region):
        self.access_key = credentials.get("access_key") or credentials.get("aws_access_key_id")
        self.secret_key = credentials.get("secret_key") or credentials.get("aws_secret_access_key")
        self.region = region


def _context(unique_id, credentials):
    if not isinstance(credentials, Mapping):
        raise ValueError("AWS account-operation credentials are invalid")
    if not credentials.get("access_key") and not credentials.get("aws_access_key_id"):
        raise ValueError("AWS account-operation credentials are incomplete")
    if not credentials.get("secret_key") and not credentials.get("aws_secret_access_key"):
        raise ValueError("AWS account-operation credentials are incomplete")

    metadata = credentials.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    scope = credentials.get("scope") or metadata.get("_cloudmoo_scope") or AWS_ACCOUNT_SCOPE
    if scope not in {AWS_ACCOUNT_SCOPE, "global"}:
        raise ValueError("AWS account-operation scope is invalid")

    region = (
        credentials.get("control_plane_region")
        or credentials.get("resource_region")
        or credentials.get("region")
        or AWS_ACCOUNT_OPERATIONS_CONTROL_PLANE_REGION
    )
    if region != AWS_ACCOUNT_OPERATIONS_CONTROL_PLANE_REGION:
        # Account-operation services are not regional workload checks.  A
        # stored asset with a workload Region is invalid rather than silently
        # redirected to a different endpoint.
        raise ValueError("AWS account-operation control-plane Region is invalid")

    provider_id = (
        credentials.get("provider_id")
        or metadata.get("_cloudmoo_provider_id")
        or metadata.get("_cloudmoo_raw_id")
    )
    if not provider_id and isinstance(unique_id, str):
        parts = unique_id.split("|", 2)
        if len(parts) == 3 and parts[0] in {AWS_ACCOUNT_SCOPE, "global"}:
            provider_id = parts[2]
        else:
            provider_id = unique_id
    if not isinstance(provider_id, str) or not provider_id.strip():
        raise ValueError("AWS account-operation provider identifier is missing")
    provider_id = provider_id.strip()
    if len(provider_id) > 512:
        raise ValueError("AWS account-operation provider identifier is oversized")
    return str(scope), region, provider_id, metadata


def _client(credentials, service, region):
    account = _CredentialAccount(credentials, region)
    return aws_client(account, service, region=region)


def _safe_error_code(error):
    provider_error = getattr(error, "__cause__", None) or error
    try:
        code = aws_error_code(provider_error)
    except Exception:
        code = None
    return str(code or type(provider_error).__name__)[:128]


def _provider_error(error, asset_type):
    code = _safe_error_code(error)
    status = _error_status(error)
    if isinstance(error, CloudInventoryTransientError) and not getattr(error, "__cause__", None):
        status = "error"
    return status, {
        asset_type: {
            "scope": AWS_ACCOUNT_SCOPE,
            "controlPlaneRegion": AWS_ACCOUNT_OPERATIONS_CONTROL_PLANE_REGION,
            "errorCode": code,
        }
    }


def _read(client, operation, **kwargs):
    assert_read_only_aws_operation(operation)
    response = getattr(client, operation)(**kwargs)
    if not isinstance(response, Mapping):
        raise CloudInventoryTransientError(f"AWS returned an invalid {operation} response")
    return response


def _pages(client, operation, **kwargs):
    assert_read_only_aws_operation(operation)
    seen_tokens = set()
    for page_number, page in enumerate(iter_pages(client, operation, **kwargs), start=1):
        if page_number > MAX_PAGES_PER_COLLECTION:
            raise CloudInventoryTransientError(
                f"AWS {operation} pagination exceeded the safety bound"
            )
        if not isinstance(page, Mapping):
            raise CloudInventoryTransientError(f"AWS returned an invalid {operation} page")
        for key in ("NextToken", "nextToken", "NextPageToken", "nextPageToken"):
            if key not in page:
                continue
            token = page.get(key)
            if token in (None, ""):
                continue
            if not isinstance(token, str) or not token.strip() or token in seen_tokens:
                raise CloudInventoryTransientError(
                    f"AWS returned an invalid {operation} pagination token"
                )
            seen_tokens.add(token)
        yield page


def _collection(client, operation, key, context, *, limit, **kwargs):
    values = []
    for page in _pages(client, operation, **kwargs):
        page_values = require_collection(page, (key,), context)
        if len(values) + len(page_values) > limit:
            raise CloudInventoryTransientError(f"AWS {context} exceeded the safety bound")
        if any(not isinstance(item, Mapping) for item in page_values):
            raise CloudInventoryTransientError(f"AWS returned an invalid {context} item")
        values.extend(page_values)
    return values


def _result(asset_type, status, metadata):
    value = dict(metadata) if isinstance(metadata, Mapping) else {}
    value.setdefault("scope", AWS_ACCOUNT_SCOPE)
    value.setdefault("controlPlaneRegion", AWS_ACCOUNT_OPERATIONS_CONTROL_PLANE_REGION)
    value["normalized_status"] = status
    return status, {asset_type: _safe_serialized(value)}


def _check_health_event(unique_id, credentials):
    scope, region, provider_id, metadata = _context(unique_id, credentials)
    client = _client(credentials, "health", region)
    events = _collection(
        client,
        "describe_events",
        "events",
        "AWS Health events",
        limit=MAX_HEALTH_EVENTS,
        filter={"eventArns": [provider_id]},
        maxResults=1,
        locale="en",
    )
    event = next(
        (
            item
            for item in events
            if isinstance(item, Mapping)
            and (item.get("arn") or item.get("eventArn")) == provider_id
        ),
        None,
    )
    if event is None:
        return _result(AWS_HEALTH_EVENT, "missing", {"errorCode": "EventNotFound"})
    detail_response = _read(
        client,
        "describe_event_details",
        eventArns=[provider_id],
        locale="en",
    )
    successful = require_collection(
        detail_response,
        ("successfulSet",),
        "AWS Health event details",
    )
    detail = {}
    for item in successful:
        if not isinstance(item, Mapping):
            raise CloudInventoryTransientError("AWS returned an invalid AWS Health event detail")
        event_id = item.get("eventArn") or item.get("arn")
        if event_id == provider_id:
            detail = item.get("eventDescription") or item
            break
    status = _normalize_check_status(event.get("statusCode") or event.get("status"), "health")
    return _result(
        AWS_HEALTH_EVENT,
        status,
        _safe_health_event(event, detail),
    )


def _normalize_check_status(value, family):
    # Keep the checker module independent of the adapter's private status
    # implementation while retaining the same normalized vocabulary.
    raw = str(value or "unknown").strip().lower().replace("-", "_").replace(" ", "_")
    mappings = {
        "health": {"open": "degraded", "upcoming": "pending", "closed": "available", "resolved": "available"},
        "trusted_advisor": {"ok": "available", "warning": "degraded", "error": "failed", "not_applicable": "available"},
        "monitor": {"active": "available", "enabled": "available", "disabled": "stopped", "inactive": "stopped"},
        "subscription": {"active": "available", "enabled": "available", "disabled": "stopped", "inactive": "stopped"},
        "anomaly": {"open": "degraded", "active": "degraded", "closed": "available", "resolved": "available"},
    }
    return mappings.get(family, {}).get(raw, raw[:64] or "unknown")


def check_aws_health_event_status(unique_id, credentials):
    try:
        return _check_health_event(unique_id, credentials)
    except Exception as error:
        return _provider_error(error, AWS_HEALTH_EVENT)


def _check_trusted_advisor(unique_id, credentials):
    _scope, region, provider_id, _metadata = _context(unique_id, credentials)
    client = _client(credentials, "support", region)
    try:
        result_method = getattr(client, "describe_trusted_advisor_check_result")
    except AttributeError:
        result_method = None
    if callable(result_method):
        response = _read(
            client,
            "describe_trusted_advisor_check_result",
            checkId=provider_id,
            language="en",
        )
        result = response.get("result")
    else:
        response = _read(
            client,
            "describe_trusted_advisor_check_summaries",
            checkIds=[provider_id],
        )
        summaries = require_collection(
            response,
            ("summaries",),
            "Trusted Advisor check summaries",
        )
        result = next(
            (
                item
                for item in summaries
                if isinstance(item, Mapping)
                and (item.get("checkId") or item.get("id")) == provider_id
            ),
            None,
        )
    if not isinstance(result, Mapping):
        raise CloudInventoryTransientError("AWS returned an invalid Trusted Advisor check result")
    metadata = _safe_trusted_advisor_result(result)
    status = _normalize_check_status(result.get("status"), "trusted_advisor")
    return _result(AWS_TRUSTED_ADVISOR_CHECK, status, metadata)


def check_aws_trusted_advisor_check_status(unique_id, credentials):
    try:
        return _check_trusted_advisor(unique_id, credentials)
    except Exception as error:
        return _provider_error(error, AWS_TRUSTED_ADVISOR_CHECK)


def _cost_window(metadata):
    period = metadata.get("time_period")
    if isinstance(period, Mapping):
        start = period.get("Start") or period.get("start")
        end = period.get("End") or period.get("end")
        if isinstance(start, str) and isinstance(end, str):
            return start, end
    return account_operation_date_window(days=COST_EXPLORER_LOOKBACK_DAYS)


def _check_cost_explorer(unique_id, credentials):
    _scope, region, _provider_id, metadata = _context(unique_id, credentials)
    start, end = _cost_window(metadata)
    client = _client(credentials, "ce", region)
    time_period = {"Start": start, "End": end}
    periods = _collection(
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
    forecast_end = (
        date.fromisoformat(end)
        + timedelta(days=COST_EXPLORER_FORECAST_DAYS)
    ).isoformat()
    forecast_response = _read(
        client,
        "get_cost_forecast",
        TimePeriod={"Start": end, "End": forecast_end},
        Metric="UNBLENDED_COST",
        Granularity="DAILY",
        PredictionIntervalLevel=80,
    )
    dimension_values = _collection(
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
    payload = {
        "time_period": time_period,
        "granularity": "DAILY",
        "results_by_time": [_safe_cost_period(item) for item in periods],
        "forecast": _safe_forecast(forecast_response),
        "dimension": "SERVICE",
        "dimension_values": [_safe_dimension_value(item) for item in dimension_values],
    }
    return _result(AWS_COST_EXPLORER_SIGNAL, "available", payload)


def check_aws_cost_explorer_signal_status(unique_id, credentials):
    try:
        return _check_cost_explorer(unique_id, credentials)
    except Exception as error:
        return _provider_error(error, AWS_COST_EXPLORER_SIGNAL)


def _anomaly_window(metadata):
    period = metadata.get("date_interval")
    if isinstance(period, Mapping):
        start = period.get("StartDate") or period.get("start")
        end = period.get("EndDate") or period.get("end")
        if isinstance(start, str) and isinstance(end, str):
            return start, end
    return account_operation_date_window(days=ANOMALY_LOOKBACK_DAYS)


def _check_anomaly_monitor(unique_id, credentials):
    _scope, region, provider_id, _metadata = _context(unique_id, credentials)
    client = _client(credentials, "ce", region)
    monitors = _collection(
        client,
        "get_anomaly_monitors",
        "AnomalyMonitors",
        "Cost Anomaly Detection monitors",
        limit=MAX_ANOMALY_MONITORS,
        MaxResults=MAX_ANOMALY_MONITORS,
    )
    item = next(
        (
            value
            for value in monitors
            if (value.get("MonitorArn") or value.get("MonitorId")) == provider_id
        ),
        None,
    )
    if item is None:
        return _result(AWS_COST_ANOMALY_MONITOR, "missing", {"errorCode": "MonitorNotFound"})
    status = _normalize_check_status(item.get("Status") or item.get("MonitorStatus") or "active", "monitor")
    return _result(AWS_COST_ANOMALY_MONITOR, status, _safe_anomaly_monitor(item))


def check_aws_cost_anomaly_monitor_status(unique_id, credentials):
    try:
        return _check_anomaly_monitor(unique_id, credentials)
    except Exception as error:
        return _provider_error(error, AWS_COST_ANOMALY_MONITOR)


def _check_anomaly_subscription(unique_id, credentials):
    _scope, region, provider_id, _metadata = _context(unique_id, credentials)
    client = _client(credentials, "ce", region)
    subscriptions = _collection(
        client,
        "get_anomaly_subscriptions",
        "AnomalySubscriptions",
        "Cost Anomaly Detection subscriptions",
        limit=MAX_ANOMALY_SUBSCRIPTIONS,
        MaxResults=MAX_ANOMALY_SUBSCRIPTIONS,
    )
    item = next(
        (
            value
            for value in subscriptions
            if (value.get("SubscriptionArn") or value.get("SubscriptionId")) == provider_id
        ),
        None,
    )
    if item is None:
        return _result(
            AWS_COST_ANOMALY_SUBSCRIPTION,
            "missing",
            {"errorCode": "SubscriptionNotFound"},
        )
    status = _normalize_check_status(item.get("Status") or "active", "subscription")
    return _result(AWS_COST_ANOMALY_SUBSCRIPTION, status, _safe_anomaly_subscription(item))


def check_aws_cost_anomaly_subscription_status(unique_id, credentials):
    try:
        return _check_anomaly_subscription(unique_id, credentials)
    except Exception as error:
        return _provider_error(error, AWS_COST_ANOMALY_SUBSCRIPTION)


def _check_anomaly(unique_id, credentials):
    _scope, region, provider_id, metadata = _context(unique_id, credentials)
    start, end = _anomaly_window(metadata)
    client = _client(credentials, "ce", region)
    anomalies = _collection(
        client,
        "get_anomalies",
        "Anomalies",
        "Cost Anomaly Detection anomalies",
        limit=MAX_ANOMALIES,
        DateInterval={"StartDate": start, "EndDate": end},
        MaxResults=MAX_ANOMALIES,
    )
    item = next(
        (
            value
            for value in anomalies
            if (value.get("AnomalyId") or value.get("Id")) == provider_id
        ),
        None,
    )
    if item is None:
        return _result(AWS_COST_ANOMALY, "missing", {"errorCode": "AnomalyNotFound"})
    return _result(AWS_COST_ANOMALY, "degraded", _safe_anomaly(item))


def check_aws_cost_anomaly_status(unique_id, credentials):
    try:
        return _check_anomaly(unique_id, credentials)
    except Exception as error:
        return _provider_error(error, AWS_COST_ANOMALY)


def check_aws_account_operations_asset_status(asset_type, unique_id, credentials):
    checker = AWS_ACCOUNT_OPERATIONS_CHECKS.get(asset_type)
    if checker is None:
        return "error", {"errorCode": "UnsupportedAssetType"}
    return checker(unique_id, credentials)


AWS_ACCOUNT_OPERATIONS_CHECKS = {
    AWS_HEALTH_EVENT: check_aws_health_event_status,
    AWS_TRUSTED_ADVISOR_CHECK: check_aws_trusted_advisor_check_status,
    AWS_COST_EXPLORER_SIGNAL: check_aws_cost_explorer_signal_status,
    AWS_COST_ANOMALY_MONITOR: check_aws_cost_anomaly_monitor_status,
    AWS_COST_ANOMALY_SUBSCRIPTION: check_aws_cost_anomaly_subscription_status,
    AWS_COST_ANOMALY: check_aws_cost_anomaly_status,
}
AWS_ACCOUNT_OPERATIONS_STATUS_CHECKS = AWS_ACCOUNT_OPERATIONS_CHECKS
AWS_ACCOUNT_OPERATIONS_CHECK_FUNCTIONS = AWS_ACCOUNT_OPERATIONS_CHECKS
AWS_ACCOUNT_OPERATIONS_CHECK_REGISTRY = AWS_ACCOUNT_OPERATIONS_CHECKS
CHECK_REGISTRATION = AWS_ACCOUNT_OPERATIONS_CHECKS


# Generated names expected by the AWS dispatcher once the integration lane
# registers the account-operation prefix.  Both provider-qualified spellings
# are exported for compatibility with existing AWS service modules.
for _asset_type, _check in AWS_ACCOUNT_OPERATIONS_CHECKS.items():
    globals()[f"check_aws_{_asset_type}_status"] = _check
    if _asset_type.startswith("aws_"):
        globals()[f"check_aws_{_asset_type[4:]}_status"] = _check


__all__ = [
    "AWS_ACCOUNT_OPERATIONS_CHECKS",
    "AWS_ACCOUNT_OPERATIONS_CHECK_FUNCTIONS",
    "AWS_ACCOUNT_OPERATIONS_CHECK_REGISTRY",
    "AWS_ACCOUNT_OPERATIONS_STATUS_CHECKS",
    "CHECK_REGISTRATION",
    "check_aws_account_operations_asset_status",
    "check_aws_cost_anomaly_monitor_status",
    "check_aws_cost_anomaly_status",
    "check_aws_cost_anomaly_subscription_status",
    "check_aws_cost_explorer_signal_status",
    "check_aws_health_event_status",
    "check_aws_trusted_advisor_check_status",
]
