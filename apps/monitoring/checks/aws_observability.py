"""Read-only status checks for the CloudWatch/Logs observability adapter."""

from datetime import timedelta
from types import SimpleNamespace

from django.utils import timezone

from apps.console.cloud.aws.observability import (
    ASSET_TYPE_CLOUDWATCH_ALARM,
    ASSET_TYPE_CLOUDWATCH_METRIC,
    ASSET_TYPE_LOG_GROUP,
    MAX_PAGES_PER_COLLECTION,
    _alarm_record,
    _log_group_record,
    _metric_record,
    _safe_dimensions,
    _safe_error_code,
    aws_client,
    aws_error_code,
    iter_pages,
    require_collection,
    serialize_aws,
)
from apps.monitoring.metadata import redact_error_message


METRIC_WINDOW_MINUTES = 15
METRIC_PERIOD_SECONDS = 300
MAX_METRIC_DATAPOINTS = 100


def _required_collection(payload, key, context):
    value = require_collection(payload, key, context)
    if not isinstance(value, list):
        raise ValueError(f"AWS returned an invalid {context} collection")
    return value


def _pages(client, operation, **kwargs):
    for page_number, page in enumerate(iter_pages(client, operation, **kwargs), start=1):
        if page_number > MAX_PAGES_PER_COLLECTION:
            raise ValueError(f"AWS {operation} pagination exceeded the safety bound")
        if not isinstance(page, dict):
            raise ValueError(f"AWS returned an invalid {operation} page")
        yield page


def _provider_error(error):
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
    return status, {
        "errorCode": code,
        "error": redact_error_message(error),
    }


def _context(credentials):
    if not isinstance(credentials, dict):
        raise ValueError("AWS observability credentials are not configured")
    region = credentials.get("resource_region") or credentials.get("region")
    if not region:
        raise ValueError("AWS observability credentials are missing a region")
    metadata = credentials.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    return str(region), metadata


def _client(credentials, region):
    access_key = credentials.get("access_key")
    secret_key = credentials.get("secret_key")
    if not access_key or not secret_key:
        raise ValueError("AWS observability credentials are incomplete")
    # discovery.aws_client intentionally accepts CoreAWSAccount-shaped
    # objects.  Checks receive a JSON-safe credential mapping, so adapt it in
    # memory without persisting or logging the secret values.
    context = SimpleNamespace(
        access_key=access_key,
        secret_key=secret_key,
        region=region,
    )
    return aws_client(context, "cloudwatch", region=region)


def _resource_name(unique_id, metadata, keys):
    for key in keys:
        value = metadata.get(key)
        if value:
            return str(value)
    value = unique_id
    if isinstance(value, str) and value.startswith("arn:") and ":alarm/" in value:
        return value.rsplit(":alarm/", 1)[1]
    if isinstance(value, str) and value.count(":") >= 2:
        return value.split(":", 2)[2]
    return str(value)


def _find_alarm(client, alarm_name):
    for page in _pages(client, "describe_alarms", AlarmNames=[alarm_name]):
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
            if not isinstance(item, dict):
                raise ValueError("AWS returned an invalid CloudWatch alarm")
            if item.get("AlarmName") == alarm_name:
                return item, alarm_type
    return None, None


def check_aws_aws_cloudwatch_alarm_status(unique_id, credentials):
    """Return the exact CloudWatch alarm state or a normalized provider error."""

    try:
        region, metadata = _context(credentials)
        alarm_name = _resource_name(
            unique_id,
            metadata,
            ("AlarmName", "alarm_name", "name"),
        )
        item, alarm_type = _find_alarm(_client(credentials, region), alarm_name)
        if item is None:
            return "not_found", {"errorCode": "ResourceNotFoundException"}
        record = _alarm_record(item, region, alarm_type)
        state = (record.get("state") or "").upper()
        if state not in {"OK", "ALARM", "INSUFFICIENT_DATA"}:
            raise ValueError("AWS returned an invalid CloudWatch alarm state")
        return state, {ASSET_TYPE_CLOUDWATCH_ALARM: record["metadata"]}
    except Exception as error:
        return _provider_error(error)


def _metric_definition(unique_id, metadata):
    namespace = metadata.get("Namespace") or metadata.get("namespace")
    metric_name = metadata.get("MetricName") or metadata.get("metric_name")
    if not namespace or not metric_name:
        raise ValueError("CloudWatch metric metadata is incomplete")
    dimensions = metadata.get("Dimensions")
    if dimensions is None:
        dimensions = metadata.get("dimensions", [])
    return {
        "Namespace": str(namespace)[:256],
        "MetricName": str(metric_name)[:256],
        "Dimensions": _safe_dimensions(dimensions),
    }


def _metric_data(client, metric, start_time, end_time):
    query = {
        "Id": "cloudmoo_metric",
        "MetricStat": {
            "Metric": metric,
            "Period": METRIC_PERIOD_SECONDS,
            "Stat": "Average",
        },
        "ReturnData": True,
    }
    results = []
    for page in _pages(
        client,
        "get_metric_data",
        MetricDataQueries=[query],
        StartTime=start_time,
        EndTime=end_time,
        ScanBy="TimestampDescending",
        MaxDatapoints=MAX_METRIC_DATAPOINTS,
    ):
        for result in _required_collection(page, "MetricDataResults", "CloudWatch metric data"):
            if not isinstance(result, dict):
                raise ValueError("AWS returned an invalid CloudWatch metric-data result")
            values = result.get("Values")
            if not isinstance(values, list):
                raise ValueError("AWS returned an invalid CloudWatch metric values collection")
            timestamps = result.get("Timestamps", [])
            if not isinstance(timestamps, list):
                raise ValueError("AWS returned an invalid CloudWatch metric timestamps collection")
            for index, value in enumerate(values):
                if len(results) >= MAX_METRIC_DATAPOINTS:
                    break
                datapoint = {"Value": value}
                if index < len(timestamps):
                    datapoint["Timestamp"] = timestamps[index]
                results.append(datapoint)
    return results


def _metric_statistics(client, metric, start_time, end_time):
    response = client.get_metric_statistics(
        Namespace=metric["Namespace"],
        MetricName=metric["MetricName"],
        Dimensions=metric["Dimensions"],
        StartTime=start_time,
        EndTime=end_time,
        Period=METRIC_PERIOD_SECONDS,
        Statistics=["Average"],
    )
    if not isinstance(response, dict):
        raise ValueError("AWS returned an invalid CloudWatch metric-statistics response")
    return [
        serialize_aws(item)
        for item in _required_collection(response, "Datapoints", "CloudWatch metric datapoints")
    ]


def check_aws_aws_cloudwatch_metric_status(unique_id, credentials):
    """Read a bounded recent metric window, returning ``no_data`` if empty."""

    try:
        region, metadata = _context(credentials)
        metric = _metric_definition(unique_id, metadata)
        end_time = timezone.now()
        start_time = end_time - timedelta(minutes=METRIC_WINDOW_MINUTES)
        client = _client(credentials, region)

        # GetMetricData is preferred because it has an explicit bounded
        # datapoint limit and native pagination.  Older/mocked clients may
        # expose only GetMetricStatistics, which is also read-only.
        if callable(getattr(client, "get_metric_data", None)):
            datapoints = _metric_data(client, metric, start_time, end_time)
        else:
            datapoints = _metric_statistics(client, metric, start_time, end_time)

        safe_datapoints = serialize_aws(datapoints[:MAX_METRIC_DATAPOINTS])
        payload = {
            "region": region,
            "Namespace": metric["Namespace"],
            "MetricName": metric["MetricName"],
            "Dimensions": metric["Dimensions"],
            "windowStart": start_time,
            "windowEnd": end_time,
            "datapoints": safe_datapoints,
        }
        if not datapoints:
            return "no_data", {ASSET_TYPE_CLOUDWATCH_METRIC: payload}
        return "ok", {ASSET_TYPE_CLOUDWATCH_METRIC: payload}
    except Exception as error:
        return _provider_error(error)


def _find_log_group(client, log_group_name):
    for page in _pages(
        client,
        "describe_log_groups",
        logGroupNamePrefix=log_group_name,
    ):
        groups = _required_collection(page, "logGroups", "CloudWatch log groups")
        for item in groups:
            if not isinstance(item, dict):
                raise ValueError("AWS returned an invalid CloudWatch log group")
            if item.get("logGroupName") == log_group_name:
                return item
    return None


def check_aws_aws_log_group_status(unique_id, credentials):
    """Check log-group configuration without fetching any log messages."""

    try:
        region, metadata = _context(credentials)
        log_group_name = _resource_name(
            unique_id,
            metadata,
            ("logGroupName", "log_group_name", "name"),
        )
        item = _find_log_group(_client(credentials, region), log_group_name)
        if item is None:
            return "not_found", {"errorCode": "ResourceNotFoundException"}
        record = _log_group_record(item, region)
        return "available", {ASSET_TYPE_LOG_GROUP: record["metadata"]}
    except Exception as error:
        return _provider_error(error)


AWS_OBSERVABILITY_CHECKS = {
    ASSET_TYPE_CLOUDWATCH_ALARM: check_aws_aws_cloudwatch_alarm_status,
    ASSET_TYPE_CLOUDWATCH_METRIC: check_aws_aws_cloudwatch_metric_status,
    ASSET_TYPE_LOG_GROUP: check_aws_aws_log_group_status,
}

# Names used by different integration lanes are intentionally aliases of the
# same mapping, so registration does not require global asset-type edits here.
AWS_OBSERVABILITY_CHECK_REGISTRY = AWS_OBSERVABILITY_CHECKS
CHECK_REGISTRATION = AWS_OBSERVABILITY_CHECKS
OBSERVABILITY_CHECKS = AWS_OBSERVABILITY_CHECKS


__all__ = [
    "AWS_OBSERVABILITY_CHECKS",
    "AWS_OBSERVABILITY_CHECK_REGISTRY",
    "CHECK_REGISTRATION",
    "OBSERVABILITY_CHECKS",
    "check_aws_aws_cloudwatch_alarm_status",
    "check_aws_aws_cloudwatch_metric_status",
    "check_aws_aws_log_group_status",
]
