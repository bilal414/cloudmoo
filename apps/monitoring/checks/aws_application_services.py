"""Read-only status checks for the AWS application-service inventory lane."""

from __future__ import annotations

from collections.abc import Mapping
import re
from types import SimpleNamespace

from botocore.exceptions import BotoCoreError, ClientError

from apps.console.cloud.aws.application_services import (
    AWS_APIGATEWAY_REST_API,
    AWS_APIGATEWAY_V2_API,
    AWS_ATHENA_DATA_CATALOG,
    AWS_ATHENA_WORKGROUP,
    AWS_CLOUDFORMATION_STACK,
    AWS_EVENTBRIDGE_BUS,
    AWS_EVENTBRIDGE_PIPE,
    AWS_EVENTBRIDGE_RULE,
    AWS_EVENTBRIDGE_SCHEDULE,
    AWS_SNS_TOPIC,
    AWS_SQS_QUEUE,
    AWS_STEPFUNCTIONS_STATE_MACHINE,
)
from apps.console.cloud.aws.discovery import (
    aws_client,
    aws_error_code,
    require_collection,
    serialize_aws,
)
from apps.console.cloud.models import CloudInventoryTransientError
from apps.monitoring.metadata import redact_sensitive_metadata


MAX_STATUS_METADATA_ITEMS = 200
MAX_STATUS_METADATA_LIST_ITEMS = 100
MAX_STATUS_METADATA_DEPTH = 5
MAX_STATUS_METADATA_TEXT = 2_048
_REGION_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)+$")

_NOT_FOUND_CODES = frozenset({
    "NotFoundException",
    "ResourceNotFoundException",
    "ResourceNotFound",
    "NoSuchEntity",
    "StateMachineDoesNotExist",
    "QueueDoesNotExist",
    "NotFound",
})
_AUTH_CODES = frozenset({
    "AccessDenied",
    "AccessDeniedException",
    "AuthFailure",
    "ExpiredToken",
    "InvalidClientTokenId",
    "UnrecognizedClientException",
    "UnauthorizedOperation",
})

_SQS_ATTRIBUTE_REQUEST = [
    "QueueArn",
    "ApproximateNumberOfMessages",
    "ApproximateNumberOfMessagesNotVisible",
    "ApproximateNumberOfMessagesDelayed",
    "CreatedTimestamp",
    "LastModifiedTimestamp",
    "VisibilityTimeout",
    "MaximumMessageSize",
    "MessageRetentionPeriod",
    "DelaySeconds",
    "ReceiveMessageWaitTimeSeconds",
    "FifoQueue",
    "ContentBasedDeduplication",
    "DeduplicationScope",
    "FifoThroughputLimit",
    "KmsMasterKeyId",
    "KmsDataKeyReusePeriodSeconds",
    "SqsManagedSseEnabled",
]
_SNS_ATTRIBUTE_FIELDS = frozenset({
    "TopicArn",
    "DisplayName",
    "Owner",
    "SubscriptionsConfirmed",
    "SubscriptionsPending",
    "SubscriptionsDeleted",
    "FifoTopic",
    "ContentBasedDeduplication",
    "KmsMasterKeyId",
    "BeginningArchiveTime",
})
_SQS_ATTRIBUTE_FIELDS = frozenset(_SQS_ATTRIBUTE_REQUEST)


def _error_code(error):
    if isinstance(error, (ValueError, TypeError, KeyError, CloudInventoryTransientError)):
        return "invalid_response"
    try:
        code = aws_error_code(getattr(error, "__cause__", None) or error)
    except Exception:
        code = type(error).__name__
    return str(code or type(error).__name__)[:128]


def _error_result(error):
    code = _error_code(error)
    lowered = code.lower()
    if code in _NOT_FOUND_CODES or "notfound" in lowered or "doesnotexist" in lowered:
        status = "not_found"
    elif code in _AUTH_CODES or "accessdenied" in lowered or "unauthorized" in lowered:
        status = "invalid_access_token"
    else:
        status = "error"
    return status, {"errorCode": code}


def _context(unique_id, credentials):
    if not isinstance(credentials, dict):
        raise ValueError("AWS application credentials are not configured")
    region = credentials.get("resource_region") or credentials.get("region")
    if (
        not isinstance(region, str)
        or not region.strip()
        or len(region.strip()) > 64
        or _REGION_RE.fullmatch(region.strip()) is None
    ):
        raise ValueError("AWS application credentials are missing a region")
    if not credentials.get("access_key") or not credentials.get("secret_key"):
        raise ValueError("AWS application credentials are incomplete")
    metadata = credentials.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    provider_id = (
        credentials.get("provider_id")
        or metadata.get("_cloudmoo_raw_id")
        or metadata.get("_cloudmoo_provider_id")
    )
    if not provider_id and isinstance(unique_id, str) and "|" in unique_id:
        prefix, remainder = unique_id.split("|", 1)
        if prefix == region and remainder:
            provider_id = remainder
    if not provider_id:
        provider_id = unique_id
    if not isinstance(provider_id, str) or not provider_id:
        raise ValueError("AWS application resource identity is missing")
    return region.strip(), provider_id, metadata


def _client(credentials, service, region):
    account = SimpleNamespace(
        access_key=credentials["access_key"],
        secret_key=credentials["secret_key"],
        region=region,
    )
    return aws_client(account, service, region=region)


def _detail(client, operation, **kwargs):
    response = getattr(client, operation)(**kwargs)
    if not isinstance(response, Mapping) or not response:
        raise ValueError("AWS returned an invalid application status response")
    return response


def _safe_fields(value, fields):
    if not isinstance(value, Mapping):
        return {}
    return {
        field: value[field]
        for field in fields
        if field in value and value[field] is not None
    }


def _safe_attributes(value, fields):
    if not isinstance(value, Mapping):
        raise ValueError("AWS returned an invalid application attributes response")
    return {key: value[key] for key in sorted(fields) if key in value}


def _resource_name(metadata, provider_id, *keys):
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return value
    return provider_id.rsplit("/", 1)[-1].rsplit(":", 1)[-1]


def _normalize_status(value, default="active"):
    raw = str(value or default).strip().lower().replace("-", "_").replace(" ", "_")
    if raw in {"available", "operational", "healthy", "ready"}:
        return "available"
    if raw in {"active", "enabled", "running", "deployed", "complete", "completed", "create_complete", "update_complete", "rollback_complete", "succeeded", "success"}:
        return "active"
    if raw in {"pending", "creating", "provisioning", "updating", "in_progress", "processing", "deploying"}:
        return "pending"
    if raw in {"disabled", "inactive", "stopped", "paused", "deactivated"}:
        return "disabled"
    if raw in {"deleting", "delete_in_progress"}:
        return "deleting"
    if raw in {"deleted", "removed"}:
        return "deleted"
    if raw in {"failed", "failure", "error", "errored", "cancelled", "canceled"}:
        return "failed"
    return raw[:128]


def _cloudformation_status(value):
    raw = str(value or "").strip().upper()
    if raw == "DELETE_COMPLETE":
        return "deleted"
    if raw.endswith("_IN_PROGRESS"):
        return "pending" if not raw.startswith("DELETE_") else "deleting"
    if raw.endswith("_FAILED") or "_ROLLBACK_" in raw:
        return "failed"
    if raw.endswith("_COMPLETE"):
        return "active"
    return _normalize_status(raw, default="unknown")


def _payload(asset_type, resource, region, status, fields, **extra):
    safe = _safe_fields(resource, fields)
    safe["region"] = region
    safe["providerStatus"] = status
    safe.update(extra)
    safe = serialize_aws(redact_sensitive_metadata(safe))
    if not isinstance(safe, dict):
        raise ValueError("AWS returned invalid serialized status metadata")
    safe = _bound_payload(safe)
    return status, {asset_type: safe}


def _bound_payload(value, depth=0):
    if depth > MAX_STATUS_METADATA_DEPTH:
        return "<truncated>"
    if isinstance(value, Mapping):
        result = {}
        for index, (key, child) in enumerate(value.items()):
            if index >= MAX_STATUS_METADATA_ITEMS:
                result["_cloudmoo_truncated_items"] = True
                break
            result[str(key)[:128]] = _bound_payload(child, depth + 1)
        return result
    if isinstance(value, list):
        result = [
            _bound_payload(item, depth + 1)
            for item in value[:MAX_STATUS_METADATA_LIST_ITEMS]
        ]
        if len(value) > MAX_STATUS_METADATA_LIST_ITEMS:
            result.append("<truncated>")
        return result
    if isinstance(value, str):
        return value[:MAX_STATUS_METADATA_TEXT]
    return value


def _check_apigateway_rest(unique_id, credentials):
    region, provider_id, _metadata = _context(unique_id, credentials)
    response = _detail(_client(credentials, "apigateway", region), "get_rest_api", restApiId=provider_id)
    return _payload(
        AWS_APIGATEWAY_REST_API,
        response,
        region,
        "available",
        ("id", "name", "description", "version", "createdDate", "warnings", "apiKeySource", "endpointConfiguration", "disableExecuteApiEndpoint"),
        endpoint=response.get("apiEndpoint") or f"https://{provider_id}.execute-api.{region}.amazonaws.com",
    )


def _check_apigateway_v2(unique_id, credentials):
    region, provider_id, _metadata = _context(unique_id, credentials)
    response = _detail(_client(credentials, "apigatewayv2", region), "get_api", ApiId=provider_id)
    raw_status = response.get("ApiStatus") or response.get("apiStatus") or "AVAILABLE"
    return _payload(
        AWS_APIGATEWAY_V2_API,
        response,
        region,
        _normalize_status(raw_status, default="available"),
        ("ApiId", "Name", "Description", "ProtocolType", "ApiEndpoint", "ApiStatus", "ApiStatusReason", "CreatedDate", "LastUpdatedDate", "Version", "RouteSelectionExpression", "ApiGatewayManaged", "DisableExecuteApiEndpoint"),
        endpoint=response.get("ApiEndpoint"),
    )


def _check_eventbridge_bus(unique_id, credentials):
    region, provider_id, metadata = _context(unique_id, credentials)
    name = _resource_name(metadata, provider_id, "Name", "name", "_cloudmoo_resource_name")
    response = _detail(_client(credentials, "events", region), "describe_event_bus", Name=name)
    return _payload(
        AWS_EVENTBRIDGE_BUS,
        response,
        region,
        "active",
        ("Name", "Arn", "Description", "CreationTime", "LastModifiedTime", "KmsKeyIdentifier"),
    )


def _check_eventbridge_rule(unique_id, credentials):
    region, provider_id, metadata = _context(unique_id, credentials)
    name = _resource_name(metadata, provider_id, "Name", "name", "_cloudmoo_resource_name")
    kwargs = {"Name": name}
    bus_name = metadata.get("EventBusName") or metadata.get("event_bus_name")
    if bus_name:
        kwargs["EventBusName"] = bus_name
    response = _detail(_client(credentials, "events", region), "describe_rule", **kwargs)
    status = _normalize_status(response.get("State"), default="active")
    return _payload(
        AWS_EVENTBRIDGE_RULE,
        response,
        region,
        status,
        ("Name", "Arn", "EventBusName", "State", "Description", "ScheduleExpression", "RoleArn", "ManagedBy", "CreatedBy"),
    )


def _check_eventbridge_schedule(unique_id, credentials):
    region, provider_id, metadata = _context(unique_id, credentials)
    name = _resource_name(metadata, provider_id, "Name", "name", "_cloudmoo_resource_name")
    group = metadata.get("_cloudmoo_group_name") or metadata.get("GroupName") or "default"
    response = _detail(_client(credentials, "scheduler", region), "get_schedule", Name=name, GroupName=group)
    status = _normalize_status(response.get("State"), default="active")
    return _payload(
        AWS_EVENTBRIDGE_SCHEDULE,
        response,
        region,
        status,
        ("Name", "GroupName", "Arn", "State", "Description", "ScheduleExpression", "ScheduleExpressionTimezone", "StartDate", "EndDate", "ActionAfterCompletion", "CreationDate", "LastModificationDate", "FlexibleTimeWindow"),
    )


def _check_eventbridge_pipe(unique_id, credentials):
    region, provider_id, metadata = _context(unique_id, credentials)
    name = _resource_name(metadata, provider_id, "Name", "name", "_cloudmoo_resource_name")
    response = _detail(_client(credentials, "pipes", region), "describe_pipe", Name=name)
    raw_status = response.get("CurrentState") or response.get("DesiredState") or "RUNNING"
    return _payload(
        AWS_EVENTBRIDGE_PIPE,
        response,
        region,
        _normalize_status(raw_status),
        ("Name", "Arn", "DesiredState", "CurrentState", "StateReason", "CreationTime", "LastModifiedTime", "Source", "Target", "Enrichment"),
    )


def _check_sns_topic(unique_id, credentials):
    region, provider_id, _metadata = _context(unique_id, credentials)
    response = _detail(_client(credentials, "sns", region), "get_topic_attributes", TopicArn=provider_id)
    attributes = _safe_attributes(response.get("Attributes"), _SNS_ATTRIBUTE_FIELDS)
    return _payload(AWS_SNS_TOPIC, {"Attributes": attributes, "TopicArn": provider_id}, region, "available", ("TopicArn", "Attributes"))


def _check_sqs_queue(unique_id, credentials):
    region, provider_id, metadata = _context(unique_id, credentials)
    queue_url = credentials.get("queue_url") or metadata.get("_cloudmoo_queue_url")
    if not queue_url and provider_id.startswith("http"):
        queue_url = provider_id
    if not queue_url:
        raise ValueError("SQS queue URL is missing")
    response = _detail(
        _client(credentials, "sqs", region),
        "get_queue_attributes",
        QueueUrl=queue_url,
        AttributeNames=_SQS_ATTRIBUTE_REQUEST,
    )
    attributes = _safe_attributes(response.get("Attributes"), _SQS_ATTRIBUTE_FIELDS)
    return _payload(AWS_SQS_QUEUE, {"QueueUrl": queue_url, "Attributes": attributes}, region, "available", ("QueueUrl", "Attributes"))


def _check_state_machine(unique_id, credentials):
    region, provider_id, _metadata = _context(unique_id, credentials)
    response = _detail(
        _client(credentials, "stepfunctions", region),
        "describe_state_machine",
        stateMachineArn=provider_id,
    )
    status = _normalize_status(response.get("status") or response.get("Status"), default="active")
    return _payload(
        AWS_STEPFUNCTIONS_STATE_MACHINE,
        response,
        region,
        status,
        ("stateMachineArn", "name", "status", "type", "creationDate", "revisionId", "loggingConfiguration", "tracingConfiguration", "encryptionConfiguration"),
    )


def _check_athena_workgroup(unique_id, credentials):
    region, provider_id, metadata = _context(unique_id, credentials)
    name = _resource_name(metadata, provider_id, "Name", "name", "_cloudmoo_resource_name")
    response = _detail(_client(credentials, "athena", region), "get_work_group", WorkGroup=name)
    resource = response.get("WorkGroup") if isinstance(response.get("WorkGroup"), Mapping) else response
    status = _normalize_status(resource.get("State"), default="active")
    return _payload(
        AWS_ATHENA_WORKGROUP,
        resource,
        region,
        status,
        ("Name", "State", "Description", "CreationTime", "WorkGroupConfiguration", "EngineVersion", "IdentityCenterApplicationArn"),
    )


def _check_athena_catalog(unique_id, credentials):
    region, provider_id, metadata = _context(unique_id, credentials)
    name = _resource_name(metadata, provider_id, "CatalogName", "name", "_cloudmoo_resource_name")
    response = _detail(_client(credentials, "athena", region), "get_data_catalog", CatalogName=name)
    resource = response.get("DataCatalog") if isinstance(response.get("DataCatalog"), Mapping) else response
    status = _normalize_status(resource.get("Status"), default="active")
    return _payload(
        AWS_ATHENA_DATA_CATALOG,
        resource,
        region,
        status,
        ("CatalogName", "CatalogArn", "Description", "Type", "Status", "CreationTime", "LastModifiedTime", "ConnectionType"),
    )


def _check_cloudformation_stack(unique_id, credentials):
    region, provider_id, _metadata = _context(unique_id, credentials)
    response = _detail(_client(credentials, "cloudformation", region), "describe_stacks", StackName=provider_id)
    stacks = require_collection(response, "Stacks", AWS_CLOUDFORMATION_STACK)
    if not stacks:
        return "not_found", {"errorCode": "ResourceNotFoundException"}
    stack = stacks[0]
    if not isinstance(stack, Mapping):
        raise ValueError("AWS returned an invalid CloudFormation stack")
    status = _cloudformation_status(stack.get("StackStatus"))
    return _payload(
        AWS_CLOUDFORMATION_STACK,
        stack,
        region,
        status,
        ("StackId", "StackName", "Description", "CreationTime", "LastUpdatedTime", "DeletionTime", "StackStatus", "StackStatusReason", "DisableRollback", "EnableTerminationProtection", "RetainExceptOnCreate", "DriftInformation", "RoleARN"),
    )


def _checked(checker, unique_id, credentials):
    try:
        return checker(unique_id, credentials)
    except (ClientError, BotoCoreError) as error:
        return _error_result(error)
    except Exception as error:
        return _error_result(error)


def check_aws_apigateway_rest_api_status(unique_id, credentials):
    return _checked(_check_apigateway_rest, unique_id, credentials)


def check_aws_apigateway_v2_api_status(unique_id, credentials):
    return _checked(_check_apigateway_v2, unique_id, credentials)


def check_aws_eventbridge_bus_status(unique_id, credentials):
    return _checked(_check_eventbridge_bus, unique_id, credentials)


def check_aws_eventbridge_rule_status(unique_id, credentials):
    return _checked(_check_eventbridge_rule, unique_id, credentials)


def check_aws_eventbridge_schedule_status(unique_id, credentials):
    return _checked(_check_eventbridge_schedule, unique_id, credentials)


def check_aws_eventbridge_pipe_status(unique_id, credentials):
    return _checked(_check_eventbridge_pipe, unique_id, credentials)


def check_aws_sns_topic_status(unique_id, credentials):
    return _checked(_check_sns_topic, unique_id, credentials)


def check_aws_sqs_queue_status(unique_id, credentials):
    return _checked(_check_sqs_queue, unique_id, credentials)


def check_aws_stepfunctions_state_machine_status(unique_id, credentials):
    return _checked(_check_state_machine, unique_id, credentials)


def check_aws_athena_workgroup_status(unique_id, credentials):
    return _checked(_check_athena_workgroup, unique_id, credentials)


def check_aws_athena_data_catalog_status(unique_id, credentials):
    return _checked(_check_athena_catalog, unique_id, credentials)


def check_aws_cloudformation_stack_status(unique_id, credentials):
    return _checked(_check_cloudformation_stack, unique_id, credentials)


AWS_APPLICATION_CHECKS = {
    AWS_APIGATEWAY_REST_API: check_aws_apigateway_rest_api_status,
    AWS_APIGATEWAY_V2_API: check_aws_apigateway_v2_api_status,
    AWS_EVENTBRIDGE_BUS: check_aws_eventbridge_bus_status,
    AWS_EVENTBRIDGE_RULE: check_aws_eventbridge_rule_status,
    AWS_EVENTBRIDGE_SCHEDULE: check_aws_eventbridge_schedule_status,
    AWS_EVENTBRIDGE_PIPE: check_aws_eventbridge_pipe_status,
    AWS_SNS_TOPIC: check_aws_sns_topic_status,
    AWS_SQS_QUEUE: check_aws_sqs_queue_status,
    AWS_STEPFUNCTIONS_STATE_MACHINE: check_aws_stepfunctions_state_machine_status,
    AWS_ATHENA_WORKGROUP: check_aws_athena_workgroup_status,
    AWS_ATHENA_DATA_CATALOG: check_aws_athena_data_catalog_status,
    AWS_CLOUDFORMATION_STACK: check_aws_cloudformation_stack_status,
}
AWS_APPLICATION_STATUS_CHECKS = AWS_APPLICATION_CHECKS
AWS_APPLICATION_CHECK_REGISTRY = AWS_APPLICATION_CHECKS
AWS_APPLICATION_CHECK_FUNCTIONS = AWS_APPLICATION_CHECKS
AWS_APPLICATION_ASSET_TYPE_CHECKS = AWS_APPLICATION_CHECKS
AWS_APPLICATION_CHECK_MAP = AWS_APPLICATION_CHECKS
CHECK_REGISTRATION = AWS_APPLICATION_CHECKS
APPLICATION_CHECKS = AWS_APPLICATION_CHECKS


def check_aws_application_service_status(asset_type, unique_id, credentials):
    checker = AWS_APPLICATION_CHECKS.get(asset_type)
    if checker is None:
        return "error", {"errorCode": "UnsupportedAssetType"}
    return checker(unique_id, credentials)


# The generic AWS dispatcher uses both the provider-qualified and short
# generated names.  These aliases keep this module self-contained until the
# later integration lane adds the application family to its global routing map.
check_aws_aws_apigateway_rest_api_status = check_aws_apigateway_rest_api_status
check_aws_aws_apigateway_v2_api_status = check_aws_apigateway_v2_api_status
check_aws_aws_eventbridge_bus_status = check_aws_eventbridge_bus_status
check_aws_aws_eventbridge_rule_status = check_aws_eventbridge_rule_status
check_aws_aws_eventbridge_schedule_status = check_aws_eventbridge_schedule_status
check_aws_aws_eventbridge_pipe_status = check_aws_eventbridge_pipe_status
check_aws_aws_sns_topic_status = check_aws_sns_topic_status
check_aws_aws_sqs_queue_status = check_aws_sqs_queue_status
check_aws_aws_stepfunctions_state_machine_status = check_aws_stepfunctions_state_machine_status
check_aws_aws_athena_workgroup_status = check_aws_athena_workgroup_status
check_aws_aws_athena_data_catalog_status = check_aws_athena_data_catalog_status
check_aws_aws_cloudformation_stack_status = check_aws_cloudformation_stack_status


__all__ = [
    "AWS_APPLICATION_CHECKS",
    "AWS_APPLICATION_STATUS_CHECKS",
    "AWS_APPLICATION_CHECK_REGISTRY",
    "AWS_APPLICATION_CHECK_FUNCTIONS",
    "AWS_APPLICATION_ASSET_TYPE_CHECKS",
    "AWS_APPLICATION_CHECK_MAP",
    "CHECK_REGISTRATION",
    "APPLICATION_CHECKS",
    "check_aws_application_service_status",
    "check_aws_apigateway_rest_api_status",
    "check_aws_apigateway_v2_api_status",
    "check_aws_eventbridge_bus_status",
    "check_aws_eventbridge_rule_status",
    "check_aws_eventbridge_schedule_status",
    "check_aws_eventbridge_pipe_status",
    "check_aws_sns_topic_status",
    "check_aws_sqs_queue_status",
    "check_aws_stepfunctions_state_machine_status",
    "check_aws_athena_workgroup_status",
    "check_aws_athena_data_catalog_status",
    "check_aws_cloudformation_stack_status",
    "check_aws_aws_apigateway_rest_api_status",
    "check_aws_aws_apigateway_v2_api_status",
    "check_aws_aws_eventbridge_bus_status",
    "check_aws_aws_eventbridge_rule_status",
    "check_aws_aws_eventbridge_schedule_status",
    "check_aws_aws_eventbridge_pipe_status",
    "check_aws_aws_sns_topic_status",
    "check_aws_aws_sqs_queue_status",
    "check_aws_aws_stepfunctions_state_machine_status",
    "check_aws_aws_athena_workgroup_status",
    "check_aws_aws_athena_data_catalog_status",
    "check_aws_aws_cloudformation_stack_status",
]
