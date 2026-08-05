"""Read-only inventory for AWS application and integration services.

This module owns the Priority 1 application surface only.  Every control
plane call is regional and read-only; the account's enabled regions are the
authoritative set of endpoints.  Provider identifiers are retained in
metadata even when a bounded local identifier needs a digest, and a family is
reconciled only after its complete collection (including bounded child reads)
has been validated.

The integration lane can register ``AWS_APPLICATION_ASSET_MODELS`` and the
check registry without changing the shared asset choices or ``CoreAWSAccount``.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import logging
import re
from urllib.parse import quote

from django.db import models

from apps.console.cloud.aws.discovery import (
    aws_client,
    aws_error_code,
    get_enabled_regions,
    iter_pages,
    require_collection,
    serialize_aws,
)
from apps.console.cloud.aws.models import CoreAWSAccount
from apps.console.cloud.models import CloudInventoryTransientError
from apps.console.utils.models import UtilAsset
from apps.monitoring.metadata import redact_sensitive_metadata


logger = logging.getLogger(__name__)


AWS_APIGATEWAY_REST_API = "aws_apigateway_rest_api"
AWS_APIGATEWAY_V2_API = "aws_apigateway_v2_api"
AWS_EVENTBRIDGE_BUS = "aws_eventbridge_bus"
AWS_EVENTBRIDGE_RULE = "aws_eventbridge_rule"
AWS_EVENTBRIDGE_SCHEDULE = "aws_eventbridge_schedule"
AWS_EVENTBRIDGE_PIPE = "aws_eventbridge_pipe"
AWS_SNS_TOPIC = "aws_sns_topic"
AWS_SQS_QUEUE = "aws_sqs_queue"
AWS_STEPFUNCTIONS_STATE_MACHINE = "aws_stepfunctions_state_machine"
AWS_ATHENA_WORKGROUP = "aws_athena_workgroup"
AWS_ATHENA_DATA_CATALOG = "aws_athena_data_catalog"
AWS_CLOUDFORMATION_STACK = "aws_cloudformation_stack"


AWS_APPLICATION_ASSET_TYPES = (
    AWS_APIGATEWAY_REST_API,
    AWS_APIGATEWAY_V2_API,
    AWS_EVENTBRIDGE_BUS,
    AWS_EVENTBRIDGE_RULE,
    AWS_EVENTBRIDGE_SCHEDULE,
    AWS_EVENTBRIDGE_PIPE,
    AWS_SNS_TOPIC,
    AWS_SQS_QUEUE,
    AWS_STEPFUNCTIONS_STATE_MACHINE,
    AWS_ATHENA_WORKGROUP,
    AWS_ATHENA_DATA_CATALOG,
    AWS_CLOUDFORMATION_STACK,
)
APPLICATION_ASSET_TYPES = AWS_APPLICATION_ASSET_TYPES
AWS_APPLICATION_SERVICE_ASSET_TYPES = AWS_APPLICATION_ASSET_TYPES


# All services in this lane expose a regional control plane.  Some list APIs
# enumerate an account's resources within that region, but none is a global
# endpoint.  Keeping this explicit makes endpoint selection auditable and
# prevents accidental use of the account's default region for every family.
AWS_APPLICATION_ENDPOINTS = {
    asset_type: {"scope": "regional", "service": service}
    for asset_type, service in (
        (AWS_APIGATEWAY_REST_API, "apigateway"),
        (AWS_APIGATEWAY_V2_API, "apigatewayv2"),
        (AWS_EVENTBRIDGE_BUS, "events"),
        (AWS_EVENTBRIDGE_RULE, "events"),
        (AWS_EVENTBRIDGE_SCHEDULE, "scheduler"),
        (AWS_EVENTBRIDGE_PIPE, "pipes"),
        (AWS_SNS_TOPIC, "sns"),
        (AWS_SQS_QUEUE, "sqs"),
        (AWS_STEPFUNCTIONS_STATE_MACHINE, "stepfunctions"),
        (AWS_ATHENA_WORKGROUP, "athena"),
        (AWS_ATHENA_DATA_CATALOG, "athena"),
        (AWS_CLOUDFORMATION_STACK, "cloudformation"),
    )
}
AWS_APPLICATION_GLOBAL_ASSET_TYPES = frozenset()
AWS_APPLICATION_GLOBAL_ENDPOINTS = {}


MAX_PAGES_PER_COLLECTION = 100
MAX_ITEMS_PER_COLLECTION = 10_000
MAX_CHILD_ITEMS = 2_000
MAX_API_STAGES = 100
MAX_STACK_RESOURCES = 2_000
MAX_METADATA_TEXT = 2_048
MAX_METADATA_ITEMS = 100
MAX_METADATA_LIST_ITEMS = 100
MAX_METADATA_DEPTH = 5
MAX_REGION_LENGTH = 64
MAX_PROVIDER_ID_LENGTH = 4_096

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


def _owner_identifier_constraint(name):
    return models.UniqueConstraint(
        fields=("owner", "region", "unique_id"),
        name=f"aws_application_{name}_owner_region_uid_uniq",
    )


class CoreAWSApplicationServiceAsset(UtilAsset):
    """Common regional identity and read-only monitoring context."""

    unique_id = models.CharField(max_length=255)
    region = models.CharField(max_length=MAX_REGION_LENGTH, db_index=True)
    owner = models.ForeignKey(
        CoreAWSAccount,
        on_delete=models.CASCADE,
        related_name="%(class)s_assets",
    )
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
        """Return ephemeral checker context; credentials are never metadata."""
        metadata = self.metadata if isinstance(self.metadata, dict) else {}
        provider_id = metadata.get("_cloudmoo_raw_id") or metadata.get("_cloudmoo_provider_id")
        context = {
            "access_key": self.owner.access_key,
            "secret_key": self.owner.secret_key,
            "region": self.region,
            "resource_region": self.region,
            "provider_id": provider_id or self.unique_id,
            "resource_name": metadata.get("_cloudmoo_resource_name") or self.name,
            "asset_type": self.type or self.asset_type,
            "provider_type": self.provider_type or self.asset_type,
            "metadata": metadata,
        }
        queue_url = metadata.get("_cloudmoo_queue_url")
        if queue_url:
            context["queue_url"] = queue_url
        return context

    @property
    def provider_url(self):
        service = (AWS_APPLICATION_ENDPOINTS.get(self.asset_type or self.type) or {}).get(
            "service", "aws"
        )
        region = quote(str(self.region), safe="-")
        return f"https://{region}.console.aws.amazon.com/{service}/home?region={region}"

    def check_status(self):
        from apps.monitoring.checks.aws_application_services import AWS_APPLICATION_CHECKS

        asset_type = self.type or self.asset_type
        checker = AWS_APPLICATION_CHECKS.get(asset_type)
        if checker is None:
            return "error", {"errorCode": "UnsupportedAssetType"}
        return checker(self.unique_id, self.monitoring_credentials)

    def __str__(self):
        return self.name


class CoreAWSAPIGatewayRestAPI(CoreAWSApplicationServiceAsset):
    asset_type = provider_type = AWS_APIGATEWAY_REST_API

    class Meta:
        db_table = "core_aws_apigateway_rest_api"
        constraints = [_owner_identifier_constraint("apigateway_rest_api")]


class CoreAWSAPIGatewayV2API(CoreAWSApplicationServiceAsset):
    asset_type = provider_type = AWS_APIGATEWAY_V2_API

    class Meta:
        db_table = "core_aws_apigateway_v2_api"
        constraints = [_owner_identifier_constraint("apigateway_v2_api")]


class CoreAWSEventBridgeBus(CoreAWSApplicationServiceAsset):
    asset_type = provider_type = AWS_EVENTBRIDGE_BUS

    class Meta:
        db_table = "core_aws_eventbridge_bus"
        constraints = [_owner_identifier_constraint("eventbridge_bus")]


class CoreAWSEventBridgeRule(CoreAWSApplicationServiceAsset):
    asset_type = provider_type = AWS_EVENTBRIDGE_RULE

    class Meta:
        db_table = "core_aws_eventbridge_rule"
        constraints = [_owner_identifier_constraint("eventbridge_rule")]


class CoreAWSEventBridgeSchedule(CoreAWSApplicationServiceAsset):
    asset_type = provider_type = AWS_EVENTBRIDGE_SCHEDULE

    class Meta:
        db_table = "core_aws_eventbridge_schedule"
        constraints = [_owner_identifier_constraint("eventbridge_schedule")]


class CoreAWSEventBridgePipe(CoreAWSApplicationServiceAsset):
    asset_type = provider_type = AWS_EVENTBRIDGE_PIPE

    class Meta:
        db_table = "core_aws_eventbridge_pipe"
        constraints = [_owner_identifier_constraint("eventbridge_pipe")]


class CoreAWSSNSTopic(CoreAWSApplicationServiceAsset):
    asset_type = provider_type = AWS_SNS_TOPIC

    class Meta:
        db_table = "core_aws_sns_topic"
        constraints = [_owner_identifier_constraint("sns_topic")]


class CoreAWSSQSQueue(CoreAWSApplicationServiceAsset):
    asset_type = provider_type = AWS_SQS_QUEUE

    class Meta:
        db_table = "core_aws_sqs_queue"
        constraints = [_owner_identifier_constraint("sqs_queue")]


class CoreAWSStepFunctionsStateMachine(CoreAWSApplicationServiceAsset):
    asset_type = provider_type = AWS_STEPFUNCTIONS_STATE_MACHINE

    class Meta:
        db_table = "core_aws_stepfunctions_state_machine"
        constraints = [_owner_identifier_constraint("stepfunctions_state_machine")]


class CoreAWSAthenaWorkgroup(CoreAWSApplicationServiceAsset):
    asset_type = provider_type = AWS_ATHENA_WORKGROUP

    class Meta:
        db_table = "core_aws_athena_workgroup"
        constraints = [_owner_identifier_constraint("athena_workgroup")]


class CoreAWSAthenaDataCatalog(CoreAWSApplicationServiceAsset):
    asset_type = provider_type = AWS_ATHENA_DATA_CATALOG

    class Meta:
        db_table = "core_aws_athena_data_catalog"
        constraints = [_owner_identifier_constraint("athena_data_catalog")]


class CoreAWSCloudFormationStack(CoreAWSApplicationServiceAsset):
    asset_type = provider_type = AWS_CLOUDFORMATION_STACK

    class Meta:
        db_table = "core_aws_cloudformation_stack"
        constraints = [_owner_identifier_constraint("cloudformation_stack")]


# Compatibility spellings used by integration code and tests.
CoreAWSEventbridgeBus = CoreAWSEventBridgeBus
CoreAWSEventbridgeRule = CoreAWSEventBridgeRule
CoreAWSEventbridgeSchedule = CoreAWSEventBridgeSchedule
CoreAWSEventbridgePipe = CoreAWSEventBridgePipe
CoreAWSStepfunctionsStateMachine = CoreAWSStepFunctionsStateMachine
CoreAWSAthenaWorkGroup = CoreAWSAthenaWorkgroup


AWS_APPLICATION_ASSET_MODELS = {
    AWS_APIGATEWAY_REST_API: CoreAWSAPIGatewayRestAPI,
    AWS_APIGATEWAY_V2_API: CoreAWSAPIGatewayV2API,
    AWS_EVENTBRIDGE_BUS: CoreAWSEventBridgeBus,
    AWS_EVENTBRIDGE_RULE: CoreAWSEventBridgeRule,
    AWS_EVENTBRIDGE_SCHEDULE: CoreAWSEventBridgeSchedule,
    AWS_EVENTBRIDGE_PIPE: CoreAWSEventBridgePipe,
    AWS_SNS_TOPIC: CoreAWSSNSTopic,
    AWS_SQS_QUEUE: CoreAWSSQSQueue,
    AWS_STEPFUNCTIONS_STATE_MACHINE: CoreAWSStepFunctionsStateMachine,
    AWS_ATHENA_WORKGROUP: CoreAWSAthenaWorkgroup,
    AWS_ATHENA_DATA_CATALOG: CoreAWSAthenaDataCatalog,
    AWS_CLOUDFORMATION_STACK: CoreAWSCloudFormationStack,
}


def _safe_error_code(error):
    try:
        code = aws_error_code(getattr(error, "__cause__", None) or error)
    except Exception:
        code = type(error).__name__
    return str(code or type(error).__name__)[:128]


def _error_kind(error):
    return "incomplete_inventory" if isinstance(error, CloudInventoryTransientError) else "provider_error"


def _safe_fields(value, fields):
    if not isinstance(value, Mapping):
        return {}
    return {
        field: value[field]
        for field in fields
        if field in value and value[field] is not None
    }


def _safe_text(value, limit=MAX_METADATA_TEXT):
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    return value[:limit]


def _required_string(value, field, context):
    if not isinstance(value, str) or not value.strip():
        raise CloudInventoryTransientError(f"AWS returned an invalid {context} identifier")
    return value.strip()[:MAX_PROVIDER_ID_LENGTH]


def _normalize_regions(regions):
    if not isinstance(regions, (list, tuple, set)):
        raise CloudInventoryTransientError("AWS enabled regions are invalid")
    normalized = set()
    for value in regions:
        if isinstance(value, Mapping):
            value = value.get("RegionName") or value.get("region")
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value.strip()) > MAX_REGION_LENGTH
            or _REGION_RE.fullmatch(value.strip()) is None
        ):
            raise CloudInventoryTransientError("AWS enabled regions are invalid")
        normalized.add(value.strip())
    return sorted(normalized)


def _region_scoped_id(region, provider_id):
    raw = f"{region}|{provider_id}"
    if len(raw) <= 255:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"{region}|sha256:{digest}"


def _serialized_metadata(metadata):
    value = dict(metadata)
    raw_id = value.get("_cloudmoo_raw_id") or value.get("_cloudmoo_provider_id")
    if raw_id:
        value.setdefault("provider_id", raw_id)
        value.setdefault("raw_id", raw_id)
    value = serialize_aws(value)
    if not isinstance(value, dict):
        raise CloudInventoryTransientError("AWS returned invalid application metadata")
    return _bounded_metadata(redact_sensitive_metadata(value))


def _bounded_metadata(value, depth=0):
    if depth > MAX_METADATA_DEPTH:
        return "<truncated>"
    if isinstance(value, Mapping):
        result = {}
        for index, (key, child) in enumerate(value.items()):
            if index >= MAX_METADATA_ITEMS:
                result["_cloudmoo_truncated_items"] = True
                break
            result[str(key)[:128]] = _bounded_metadata(child, depth + 1)
        return result
    if isinstance(value, list):
        result = [
            _bounded_metadata(item, depth + 1)
            for item in value[:MAX_METADATA_LIST_ITEMS]
        ]
        if len(value) > MAX_METADATA_LIST_ITEMS:
            result.append("<truncated>")
        return result
    if isinstance(value, str):
        return value[:MAX_METADATA_TEXT]
    return value


def _collection(
    client,
    operation,
    collection_path,
    context,
    *,
    limit=MAX_ITEMS_PER_COLLECTION,
    require_mappings=True,
    **kwargs,
):
    values = []
    pages = 0
    for page in iter_pages(client, operation, **kwargs):
        pages += 1
        if pages > MAX_PAGES_PER_COLLECTION:
            raise CloudInventoryTransientError(
                f"AWS {context} pagination exceeded the safety bound"
            )
        items = require_collection(page, collection_path, context)
        if len(values) + len(items) > limit:
            raise CloudInventoryTransientError(
                f"AWS {context} inventory exceeded the safety bound"
            )
        for item in items:
            if require_mappings and not isinstance(item, Mapping):
                raise CloudInventoryTransientError(f"AWS returned an invalid {context} item")
        values.extend(items)
    return values


def _detail(client, operation, context, **kwargs):
    method = getattr(client, operation)
    response = method(**kwargs)
    if not isinstance(response, Mapping) or not response:
        raise CloudInventoryTransientError(f"AWS returned an invalid {context} response")
    return response


def _safe_endpoint_configuration(value):
    if not isinstance(value, Mapping):
        return None
    safe = _safe_fields(value, ("types", "vpcEndpointIds", "VpcEndpointIds", "ipAddressType"))
    return safe or None


def _safe_rest_stage(value):
    safe = _safe_fields(
        value,
        (
            "stageName",
            "StageName",
            "stageArn",
            "StageArn",
            "deploymentId",
            "DeploymentId",
            "description",
            "Description",
            "cacheClusterEnabled",
            "cacheClusterSize",
            "cacheClusterStatus",
            "tracingEnabled",
            "createdDate",
            "lastUpdatedDate",
            "webAclArn",
            "AutoDeploy",
            "DefaultRouteSettings",
            "RouteSettings",
        ),
    )
    access = value.get("accessLogSettings") or value.get("AccessLogSettings")
    if isinstance(access, Mapping):
        safe["accessLogSettings"] = _safe_fields(access, ("destinationArn", "DestinationArn", "format", "Format"))
    return safe


def _safe_api_metadata(item, detail, stages, region, provider_id, *, v2=False):
    fields = (
        ("ApiId", "Name", "Description", "ProtocolType", "ApiEndpoint", "ApiStatus",
         "ApiStatusReason", "CreatedDate", "LastUpdatedDate", "Version",
         "RouteSelectionExpression", "ApiGatewayManaged", "DisableExecuteApiEndpoint")
        if v2
        else
        ("id", "name", "description", "version", "createdDate", "warnings",
         "binaryMediaTypes", "minimumCompressionSize", "apiKeySource",
         "disableExecuteApiEndpoint")
    )
    safe = _safe_fields(item, fields)
    safe.update({key: value for key, value in _safe_fields(detail, fields).items()})
    endpoint_configuration = detail.get("EndpointConfiguration") or detail.get("endpointConfiguration")
    endpoint_configuration = _safe_endpoint_configuration(endpoint_configuration)
    if endpoint_configuration:
        safe["endpointConfiguration"] = endpoint_configuration
    endpoint = detail.get("ApiEndpoint") or detail.get("apiEndpoint") or safe.get("ApiEndpoint")
    if not isinstance(endpoint, str) or not endpoint.strip():
        endpoint = f"https://{provider_id}.execute-api.{region}.amazonaws.com"
    safe["endpoint"] = _safe_text(endpoint, 512)
    if stages:
        safe["stages"] = [_safe_rest_stage(stage) for stage in stages]
    else:
        safe["stages"] = []
    safe.update({
        "_cloudmoo_region": region,
        "_cloudmoo_raw_id": provider_id,
        "_cloudmoo_provider_id": provider_id,
        "_cloudmoo_resource_name": _safe_text(
            detail.get("Name") or detail.get("name") or item.get("Name") or item.get("name") or provider_id,
            256,
        ),
    })
    return _serialized_metadata(safe)


def _collect_api_gateway(account, region, *, v2=False):
    asset_type = AWS_APIGATEWAY_V2_API if v2 else AWS_APIGATEWAY_REST_API
    client = aws_client(account, "apigatewayv2" if v2 else "apigateway", region=region)
    items = _collection(
        client,
        "get_apis" if v2 else "get_rest_apis",
        "Items" if v2 else "items",
        asset_type,
    )
    records = []
    warnings = []
    for item in items:
        provider_id = _required_string(item.get("ApiId" if v2 else "id"), "api_id", asset_type)
        detail = {}
        stages = []
        detail_error = None
        try:
            detail = _detail(
                client,
                "get_api" if v2 else "get_rest_api",
                asset_type,
                **({"ApiId": provider_id} if v2 else {"restApiId": provider_id}),
            )
        except Exception as error:
            detail_error = _safe_error_code(error)
            warnings.append(detail_error)
        try:
            stages = _collection(
                client,
                "get_stages",
                "Items" if v2 else "item",
                f"{asset_type} stages",
                limit=MAX_API_STAGES,
                **({"ApiId": provider_id} if v2 else {"restApiId": provider_id}),
            )
        except Exception as error:
            detail_error = detail_error or _safe_error_code(error)
            warnings.append(_safe_error_code(error))
        metadata = _safe_api_metadata(item, detail, stages, region, provider_id, v2=v2)
        if detail_error:
            metadata["_cloudmoo_detail_error"] = detail_error
        name = detail.get("Name") or detail.get("name") or item.get("Name") or item.get("name") or provider_id
        records.append({
            "unique_id": _region_scoped_id(region, provider_id),
            "name": _safe_text(name, 100) or provider_id[:100],
            "metadata": metadata,
        })
    return records, warnings


def _safe_event_bus(value):
    return _safe_fields(value, ("Name", "Arn", "Description", "CreationTime", "LastModifiedTime", "KmsKeyIdentifier"))


def _collect_eventbridge_buses(account, region):
    client = aws_client(account, "events", region=region)
    items = _collection(client, "list_event_buses", "EventBuses", AWS_EVENTBRIDGE_BUS)
    records = []
    warnings = []
    for item in items:
        name = _required_string(item.get("Name"), "Name", AWS_EVENTBRIDGE_BUS)
        provider_id = _required_string(item.get("Arn") or name, "Arn", AWS_EVENTBRIDGE_BUS)
        detail = {}
        detail_error = None
        try:
            detail = _detail(client, "describe_event_bus", AWS_EVENTBRIDGE_BUS, Name=name)
        except Exception as error:
            detail_error = _safe_error_code(error)
            warnings.append(detail_error)
        metadata = _safe_event_bus(item)
        metadata.update(_safe_event_bus(detail))
        metadata.update({
            "_cloudmoo_region": region,
            "_cloudmoo_raw_id": provider_id,
            "_cloudmoo_provider_id": provider_id,
            "_cloudmoo_resource_name": name,
        })
        if detail_error:
            metadata["_cloudmoo_detail_error"] = detail_error
        records.append({
            "unique_id": _region_scoped_id(region, provider_id),
            "name": name[:100],
            "metadata": _serialized_metadata(metadata),
        })
    return records, warnings


def _event_bus_names(client, region):
    items = _collection(client, "list_event_buses", "EventBuses", f"{AWS_EVENTBRIDGE_RULE} buses")
    names = []
    for item in items:
        names.append(_required_string(item.get("Name"), "Name", AWS_EVENTBRIDGE_RULE))
    return names


def _collect_eventbridge_rules(account, region):
    client = aws_client(account, "events", region=region)
    bus_names = _event_bus_names(client, region)
    records = []
    warnings = []
    for bus_name in bus_names:
        try:
            items = _collection(
                client,
                "list_rules",
                "Rules",
                AWS_EVENTBRIDGE_RULE,
                EventBusName=bus_name,
            )
        except Exception as error:
            warnings.append(_safe_error_code(error))
            continue
        for item in items:
            name = _required_string(item.get("Name"), "Name", AWS_EVENTBRIDGE_RULE)
            provider_id = _required_string(item.get("Arn") or f"{bus_name}:{name}", "Arn", AWS_EVENTBRIDGE_RULE)
            metadata = _safe_fields(
                item,
                (
                    "Name", "Arn", "EventBusName", "State", "Description", "ScheduleExpression",
                    "RoleArn", "ManagedBy", "CreatedBy", "EventSource", "LastModifiedBy",
                ),
            )
            metadata["EventBusName"] = bus_name
            metadata.update({
                "_cloudmoo_region": region,
                "_cloudmoo_raw_id": provider_id,
                "_cloudmoo_provider_id": provider_id,
                "_cloudmoo_resource_name": name,
            })
            records.append({
                "unique_id": _region_scoped_id(region, provider_id),
                "name": name[:100],
                "metadata": _serialized_metadata(metadata),
            })
    return records, warnings


def _safe_schedule(value):
    safe = _safe_fields(
        value,
        (
            "Name", "GroupName", "Arn", "State", "Description", "ScheduleExpression",
            "ScheduleExpressionTimezone", "StartDate", "EndDate", "ActionAfterCompletion",
            "CreationDate", "LastModificationDate", "FlexibleTimeWindow",
        ),
    )
    return safe


def _collect_eventbridge_schedules(account, region):
    client = aws_client(account, "scheduler", region=region)
    items = _collection(client, "list_schedules", "Schedules", AWS_EVENTBRIDGE_SCHEDULE)
    records = []
    warnings = []
    for item in items:
        name = _required_string(item.get("Name"), "Name", AWS_EVENTBRIDGE_SCHEDULE)
        group = _required_string(item.get("GroupName") or "default", "GroupName", AWS_EVENTBRIDGE_SCHEDULE)
        provider_id = _required_string(item.get("Arn") or f"{group}:{name}", "Arn", AWS_EVENTBRIDGE_SCHEDULE)
        detail = {}
        detail_error = None
        try:
            detail = _detail(client, "get_schedule", AWS_EVENTBRIDGE_SCHEDULE, Name=name, GroupName=group)
        except Exception as error:
            detail_error = _safe_error_code(error)
            warnings.append(detail_error)
        metadata = _safe_schedule(item)
        metadata.update(_safe_schedule(detail))
        metadata.update({
            "_cloudmoo_region": region,
            "_cloudmoo_raw_id": provider_id,
            "_cloudmoo_provider_id": provider_id,
            "_cloudmoo_resource_name": name,
            "_cloudmoo_group_name": group,
        })
        if detail_error:
            metadata["_cloudmoo_detail_error"] = detail_error
        records.append({
            "unique_id": _region_scoped_id(region, provider_id),
            "name": name[:100],
            "metadata": _serialized_metadata(metadata),
        })
    return records, warnings


def _safe_pipe(value):
    return _safe_fields(
        value,
        (
            "Name", "Arn", "DesiredState", "CurrentState", "StateReason", "CreationTime",
            "LastModifiedTime", "Source", "Target", "Enrichment", "KmsKeyIdentifier",
        ),
    )


def _collect_eventbridge_pipes(account, region):
    client = aws_client(account, "pipes", region=region)
    items = _collection(client, "list_pipes", "Pipes", AWS_EVENTBRIDGE_PIPE)
    records = []
    warnings = []
    for item in items:
        name = _required_string(item.get("Name"), "Name", AWS_EVENTBRIDGE_PIPE)
        provider_id = _required_string(item.get("Arn") or name, "Arn", AWS_EVENTBRIDGE_PIPE)
        detail = {}
        detail_error = None
        try:
            detail = _detail(client, "describe_pipe", AWS_EVENTBRIDGE_PIPE, Name=name)
        except Exception as error:
            detail_error = _safe_error_code(error)
            warnings.append(detail_error)
        metadata = _safe_pipe(item)
        metadata.update(_safe_pipe(detail))
        metadata.update({
            "_cloudmoo_region": region,
            "_cloudmoo_raw_id": provider_id,
            "_cloudmoo_provider_id": provider_id,
            "_cloudmoo_resource_name": name,
        })
        if detail_error:
            metadata["_cloudmoo_detail_error"] = detail_error
        records.append({
            "unique_id": _region_scoped_id(region, provider_id),
            "name": name[:100],
            "metadata": _serialized_metadata(metadata),
        })
    return records, warnings


_SNS_ATTRIBUTE_FIELDS = frozenset({
    "TopicArn", "DisplayName", "Owner", "SubscriptionsConfirmed", "SubscriptionsPending",
    "SubscriptionsDeleted", "FifoTopic", "ContentBasedDeduplication", "KmsMasterKeyId",
    "BeginningArchiveTime",
})
_SQS_ATTRIBUTE_FIELDS = frozenset({
    "QueueArn", "ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible",
    "ApproximateNumberOfMessagesDelayed", "CreatedTimestamp", "LastModifiedTimestamp",
    "VisibilityTimeout", "MaximumMessageSize", "MessageRetentionPeriod", "DelaySeconds",
    "ReceiveMessageWaitTimeSeconds", "FifoQueue", "ContentBasedDeduplication",
    "DeduplicationScope", "FifoThroughputLimit", "KmsMasterKeyId",
    "KmsDataKeyReusePeriodSeconds", "SqsManagedSseEnabled",
})
_SQS_ATTRIBUTE_REQUEST = sorted(_SQS_ATTRIBUTE_FIELDS - {"QueueArn"}) + ["QueueArn"]


def _safe_attributes(value, fields):
    if not isinstance(value, Mapping):
        raise CloudInventoryTransientError("AWS returned an invalid attributes response")
    return {key: value[key] for key in sorted(fields) if key in value}


def _queue_name(queue_url):
    return str(queue_url).rstrip("/").rsplit("/", 1)[-1][:100]


def _collect_sns_topics(account, region):
    client = aws_client(account, "sns", region=region)
    items = _collection(client, "list_topics", "Topics", AWS_SNS_TOPIC)
    records = []
    warnings = []
    for item in items:
        provider_id = _required_string(item.get("TopicArn"), "TopicArn", AWS_SNS_TOPIC)
        attributes = {}
        detail_error = None
        try:
            response = _detail(client, "get_topic_attributes", AWS_SNS_TOPIC, TopicArn=provider_id)
            attributes = _safe_attributes(response.get("Attributes"), _SNS_ATTRIBUTE_FIELDS)
        except Exception as error:
            detail_error = _safe_error_code(error)
            warnings.append(detail_error)
        name = provider_id.rsplit(":", 1)[-1]
        metadata = {
            "TopicArn": provider_id,
            "Attributes": attributes,
            "_cloudmoo_region": region,
            "_cloudmoo_raw_id": provider_id,
            "_cloudmoo_provider_id": provider_id,
            "_cloudmoo_resource_name": name,
        }
        if detail_error:
            metadata["_cloudmoo_detail_error"] = detail_error
        records.append({
            "unique_id": _region_scoped_id(region, provider_id),
            "name": name[:100],
            "metadata": _serialized_metadata(metadata),
        })
    return records, warnings


def _collect_sqs_queues(account, region):
    client = aws_client(account, "sqs", region=region)
    items = _collection(
        client,
        "list_queues",
        "QueueUrls",
        AWS_SQS_QUEUE,
        require_mappings=False,
    )
    records = []
    warnings = []
    for item in items:
        if isinstance(item, str):
            queue_url = _required_string(item, "QueueUrl", AWS_SQS_QUEUE)
        elif isinstance(item, Mapping):
            queue_url = _required_string(item.get("QueueUrl") or item.get("queueUrl"), "QueueUrl", AWS_SQS_QUEUE)
        else:
            raise CloudInventoryTransientError(f"AWS returned an invalid {AWS_SQS_QUEUE} item")
        attributes = {}
        detail_error = None
        provider_id = queue_url
        try:
            response = _detail(
                client,
                "get_queue_attributes",
                AWS_SQS_QUEUE,
                QueueUrl=queue_url,
                AttributeNames=_SQS_ATTRIBUTE_REQUEST,
            )
            attributes = _safe_attributes(response.get("Attributes"), _SQS_ATTRIBUTE_FIELDS)
            provider_id = _required_string(attributes.get("QueueArn") or queue_url, "QueueArn", AWS_SQS_QUEUE)
        except Exception as error:
            detail_error = _safe_error_code(error)
            warnings.append(detail_error)
        name = _queue_name(queue_url)
        metadata = {
            "QueueUrl": queue_url,
            "QueueArn": attributes.get("QueueArn"),
            "Attributes": attributes,
            "_cloudmoo_queue_url": queue_url,
            "_cloudmoo_region": region,
            "_cloudmoo_raw_id": provider_id,
            "_cloudmoo_provider_id": provider_id,
            "_cloudmoo_resource_name": name,
        }
        if detail_error:
            metadata["_cloudmoo_detail_error"] = detail_error
        records.append({
            "unique_id": _region_scoped_id(region, provider_id),
            "name": name,
            "metadata": _serialized_metadata(metadata),
        })
    return records, warnings


def _safe_state_machine(value):
    return _safe_fields(
        value,
        (
            "stateMachineArn", "name", "status", "type", "creationDate", "revisionId",
            "loggingConfiguration", "tracingConfiguration", "encryptionConfiguration",
        ),
    )


def _collect_state_machines(account, region):
    client = aws_client(account, "stepfunctions", region=region)
    items = _collection(client, "list_state_machines", "stateMachines", AWS_STEPFUNCTIONS_STATE_MACHINE)
    records = []
    warnings = []
    for item in items:
        provider_id = _required_string(item.get("stateMachineArn"), "stateMachineArn", AWS_STEPFUNCTIONS_STATE_MACHINE)
        detail = {}
        detail_error = None
        try:
            response = _detail(
                client,
                "describe_state_machine",
                AWS_STEPFUNCTIONS_STATE_MACHINE,
                stateMachineArn=provider_id,
            )
            detail = response
        except Exception as error:
            detail_error = _safe_error_code(error)
            warnings.append(detail_error)
        metadata = _safe_state_machine(item)
        metadata.update(_safe_state_machine(detail))
        name = detail.get("name") or item.get("name") or provider_id.rsplit(":", 1)[-1]
        metadata.update({
            "_cloudmoo_region": region,
            "_cloudmoo_raw_id": provider_id,
            "_cloudmoo_provider_id": provider_id,
            "_cloudmoo_resource_name": name,
        })
        if detail_error:
            metadata["_cloudmoo_detail_error"] = detail_error
        records.append({
            "unique_id": _region_scoped_id(region, provider_id),
            "name": _safe_text(name, 100) or provider_id[:100],
            "metadata": _serialized_metadata(metadata),
        })
    return records, warnings


def _safe_workgroup(value):
    safe = _safe_fields(value, ("Name", "State", "Description", "CreationTime", "WorkGroupConfiguration", "EngineVersion", "IdentityCenterApplicationArn"))
    configuration = value.get("WorkGroupConfiguration")
    if isinstance(configuration, Mapping):
        safe["WorkGroupConfiguration"] = _safe_fields(
            configuration,
            (
                "EnforceWorkGroupConfiguration", "PublishCloudWatchMetricsEnabled",
                "BytesScannedCutoffPerQuery", "RequesterPaysEnabled", "EngineVersion",
                "ResultConfiguration", "AdditionalConfiguration", "ExecutionRole",
            ),
        )
        result = configuration.get("ResultConfiguration")
        if isinstance(result, Mapping):
            safe["WorkGroupConfiguration"]["ResultConfiguration"] = _safe_fields(
                result,
                ("OutputLocation", "ExpectedBucketOwner", "EncryptionConfiguration", "AclConfiguration"),
            )
    return safe


def _collect_athena_workgroups(account, region):
    client = aws_client(account, "athena", region=region)
    items = _collection(client, "list_work_groups", "WorkGroups", AWS_ATHENA_WORKGROUP)
    records = []
    warnings = []
    for item in items:
        name = _required_string(item.get("Name"), "Name", AWS_ATHENA_WORKGROUP)
        detail = {}
        detail_error = None
        try:
            response = _detail(client, "get_work_group", AWS_ATHENA_WORKGROUP, WorkGroup=name)
            detail = response.get("WorkGroup") if isinstance(response.get("WorkGroup"), Mapping) else response
        except Exception as error:
            detail_error = _safe_error_code(error)
            warnings.append(detail_error)
        metadata = _safe_workgroup(item)
        metadata.update(_safe_workgroup(detail))
        metadata.update({
            "_cloudmoo_region": region,
            "_cloudmoo_raw_id": name,
            "_cloudmoo_provider_id": name,
            "_cloudmoo_resource_name": name,
        })
        if detail_error:
            metadata["_cloudmoo_detail_error"] = detail_error
        records.append({
            "unique_id": _region_scoped_id(region, name),
            "name": name[:100],
            "metadata": _serialized_metadata(metadata),
        })
    return records, warnings


def _safe_catalog(value):
    return _safe_fields(
        value,
        (
            "CatalogName", "CatalogArn", "Description", "Type", "Status", "CreationTime",
            "LastModifiedTime", "ConnectionType",
        ),
    )


def _collect_athena_catalogs(account, region):
    client = aws_client(account, "athena", region=region)
    items = _collection(client, "list_data_catalogs", "DataCatalogsSummary", AWS_ATHENA_DATA_CATALOG)
    records = []
    warnings = []
    for item in items:
        name = _required_string(item.get("CatalogName"), "CatalogName", AWS_ATHENA_DATA_CATALOG)
        detail = {}
        detail_error = None
        try:
            response = _detail(client, "get_data_catalog", AWS_ATHENA_DATA_CATALOG, CatalogName=name)
            detail = response.get("DataCatalog") if isinstance(response.get("DataCatalog"), Mapping) else response
        except Exception as error:
            detail_error = _safe_error_code(error)
            warnings.append(detail_error)
        metadata = _safe_catalog(item)
        metadata.update(_safe_catalog(detail))
        metadata.update({
            "_cloudmoo_region": region,
            "_cloudmoo_raw_id": name,
            "_cloudmoo_provider_id": name,
            "_cloudmoo_resource_name": name,
        })
        if detail_error:
            metadata["_cloudmoo_detail_error"] = detail_error
        records.append({
            "unique_id": _region_scoped_id(region, name),
            "name": name[:100],
            "metadata": _serialized_metadata(metadata),
        })
    return records, warnings


def _safe_stack(value):
    return _safe_fields(
        value,
        (
            "StackId", "StackName", "Description", "CreationTime", "LastUpdatedTime", "DeletionTime",
            "StackStatus", "StackStatusReason", "DisableRollback", "EnableTerminationProtection",
            "EnableTerminationProtection", "RetainExceptOnCreate", "DriftInformation", "RoleARN",
        ),
    )


def _safe_stack_resource(value):
    return _safe_fields(
        value,
        (
            "LogicalResourceId", "PhysicalResourceId", "ResourceType", "Timestamp", "LastUpdatedTimestamp",
            "ResourceStatus", "ResourceStatusReason", "DriftInformation", "ModuleInfo",
        ),
    )


def _collect_cloudformation_stacks(account, region):
    client = aws_client(account, "cloudformation", region=region)
    summaries = _collection(client, "list_stacks", "StackSummaries", AWS_CLOUDFORMATION_STACK)
    records = []
    warnings = []
    for summary in summaries:
        provider_id = _required_string(summary.get("StackId") or summary.get("StackName"), "StackId", AWS_CLOUDFORMATION_STACK)
        stack = {}
        resources = []
        detail_error = None
        try:
            response = _detail(client, "describe_stacks", AWS_CLOUDFORMATION_STACK, StackName=provider_id)
            stacks = require_collection(response, "Stacks", AWS_CLOUDFORMATION_STACK)
            if not stacks or not isinstance(stacks[0], Mapping):
                raise CloudInventoryTransientError("AWS returned an invalid CloudFormation stack")
            stack = stacks[0]
        except Exception as error:
            detail_error = _safe_error_code(error)
            warnings.append(detail_error)
        try:
            resources = _collection(
                client,
                "list_stack_resources",
                "StackResourceSummaries",
                f"{AWS_CLOUDFORMATION_STACK} resources",
                limit=MAX_STACK_RESOURCES,
                StackName=provider_id,
            )
        except Exception as error:
            detail_error = detail_error or _safe_error_code(error)
            warnings.append(_safe_error_code(error))
        safe_resources = [_safe_stack_resource(resource) for resource in resources]
        metadata = _safe_stack(summary)
        metadata.update(_safe_stack(stack))
        metadata["resources"] = safe_resources
        name = stack.get("StackName") or summary.get("StackName") or provider_id.rsplit("/", 1)[-1]
        metadata.update({
            "_cloudmoo_region": region,
            "_cloudmoo_raw_id": provider_id,
            "_cloudmoo_provider_id": provider_id,
            "_cloudmoo_resource_name": name,
        })
        if detail_error:
            metadata["_cloudmoo_detail_error"] = detail_error
        records.append({
            "unique_id": _region_scoped_id(region, provider_id),
            "name": _safe_text(name, 100) or provider_id[:100],
            "metadata": _serialized_metadata(metadata),
        })
    return records, warnings


AWS_APPLICATION_COLLECTION_SPECS = (
    (AWS_APIGATEWAY_REST_API, CoreAWSAPIGatewayRestAPI, _collect_api_gateway, {"v2": False}),
    (AWS_APIGATEWAY_V2_API, CoreAWSAPIGatewayV2API, _collect_api_gateway, {"v2": True}),
    (AWS_EVENTBRIDGE_BUS, CoreAWSEventBridgeBus, _collect_eventbridge_buses, {}),
    (AWS_EVENTBRIDGE_RULE, CoreAWSEventBridgeRule, _collect_eventbridge_rules, {}),
    (AWS_EVENTBRIDGE_SCHEDULE, CoreAWSEventBridgeSchedule, _collect_eventbridge_schedules, {}),
    (AWS_EVENTBRIDGE_PIPE, CoreAWSEventBridgePipe, _collect_eventbridge_pipes, {}),
    (AWS_SNS_TOPIC, CoreAWSSNSTopic, _collect_sns_topics, {}),
    (AWS_SQS_QUEUE, CoreAWSSQSQueue, _collect_sqs_queues, {}),
    (AWS_STEPFUNCTIONS_STATE_MACHINE, CoreAWSStepFunctionsStateMachine, _collect_state_machines, {}),
    (AWS_ATHENA_WORKGROUP, CoreAWSAthenaWorkgroup, _collect_athena_workgroups, {}),
    (AWS_ATHENA_DATA_CATALOG, CoreAWSAthenaDataCatalog, _collect_athena_catalogs, {}),
    (AWS_CLOUDFORMATION_STACK, CoreAWSCloudFormationStack, _collect_cloudformation_stacks, {}),
)
AWS_APPLICATION_SERVICE_COLLECTION_SPECS = AWS_APPLICATION_COLLECTION_SPECS


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
    asset.region = region
    asset.name = record["name"][:100]
    asset.type = asset_type
    asset.metadata = record["metadata"]
    if not created and getattr(asset, "monitoring", None) == UtilAsset.Monitoring.NO_LONGER_EXISTS:
        asset.monitoring = UtilAsset.Monitoring.ACTIVE
    asset.save()
    return asset


def _reconcile(model, account, region, records, asset_type):
    if not isinstance(records, list):
        raise CloudInventoryTransientError(f"AWS returned an invalid {asset_type} collection")
    current_ids = []
    seen_ids = set()
    for record in records:
        if not isinstance(record, Mapping) or not record.get("unique_id"):
            raise CloudInventoryTransientError(f"AWS returned an invalid {asset_type} record")
        unique_id = str(record["unique_id"])
        if unique_id in seen_ids:
            raise CloudInventoryTransientError(f"AWS returned a duplicate {asset_type} identifier")
        seen_ids.add(unique_id)
        _upsert_asset(model, account, region, record, asset_type)
        current_ids.append(unique_id)
    model.objects.filter(owner=account, region=region).exclude(unique_id__in=current_ids).update(
        monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
    )
    return len(current_ids)


def _persist_without_reconcile(model, account, region, records, asset_type):
    if not isinstance(records, list):
        raise CloudInventoryTransientError(f"AWS returned an invalid {asset_type} collection")
    seen_ids = set()
    for record in records:
        if not isinstance(record, Mapping) or not record.get("unique_id"):
            raise CloudInventoryTransientError(f"AWS returned an invalid {asset_type} record")
        unique_id = str(record["unique_id"])
        if unique_id in seen_ids:
            raise CloudInventoryTransientError(f"AWS returned a duplicate {asset_type} identifier")
        seen_ids.add(unique_id)
        _upsert_asset(model, account, region, record, asset_type)
    return len(records)


def _summary_error(asset_type, region, error):
    return {
        "assetType": asset_type,
        "region": region,
        "errorCode": _safe_error_code(error),
        "kind": _error_kind(error),
    }


def sync_aws_application_service_assets(account):
    """Synchronize all application-service families with fail-closed scopes."""
    summary = {
        "regions": [],
        "counts": {asset_type: 0 for asset_type in AWS_APPLICATION_ASSET_TYPES},
        "synced": {asset_type: 0 for asset_type in AWS_APPLICATION_ASSET_TYPES},
        "families": {asset_type: {} for asset_type in AWS_APPLICATION_ASSET_TYPES},
        "errors": [],
    }
    try:
        regions = _normalize_regions(get_enabled_regions(account))
    except Exception as error:
        summary["errors"].append(_summary_error("region_discovery", None, error))
        return summary
    summary["regions"] = regions

    for asset_type, model, collector, options in AWS_APPLICATION_COLLECTION_SPECS:
        for region in regions:
            try:
                records, warnings = collector(account, region, **options)
                if warnings:
                    count = _persist_without_reconcile(model, account, region, records, asset_type)
                    result = {
                        "status": "partial",
                        "complete": False,
                        "reconciled": False,
                        "count": count,
                    }
                    for warning in warnings:
                        summary["errors"].append({
                            "assetType": asset_type,
                            "region": region,
                            "errorCode": str(warning)[:128],
                            "kind": "incomplete_inventory",
                        })
                else:
                    count = _reconcile(model, account, region, records, asset_type)
                    result = {
                        "status": "ok",
                        "complete": True,
                        "reconciled": True,
                        "count": count,
                    }
                summary["counts"][asset_type] += count
                summary["synced"][asset_type] += count
            except Exception as error:
                result = {
                    "status": "error",
                    "complete": False,
                    "reconciled": False,
                    "count": None,
                    "error": _summary_error(asset_type, region, error),
                }
                summary["errors"].append(result["error"])
                logger.warning(
                    "AWS application inventory failed for %s/%s (%s)",
                    asset_type,
                    region,
                    result["error"]["errorCode"],
                )
            summary["families"][asset_type][region] = result
        # Small callers can address each family without unpacking the envelope.
        summary[asset_type] = summary["families"][asset_type]

    return summary


__all__ = [
    "AWS_APIGATEWAY_REST_API",
    "AWS_APIGATEWAY_V2_API",
    "AWS_EVENTBRIDGE_BUS",
    "AWS_EVENTBRIDGE_RULE",
    "AWS_EVENTBRIDGE_SCHEDULE",
    "AWS_EVENTBRIDGE_PIPE",
    "AWS_SNS_TOPIC",
    "AWS_SQS_QUEUE",
    "AWS_STEPFUNCTIONS_STATE_MACHINE",
    "AWS_ATHENA_WORKGROUP",
    "AWS_ATHENA_DATA_CATALOG",
    "AWS_CLOUDFORMATION_STACK",
    "AWS_APPLICATION_ASSET_TYPES",
    "APPLICATION_ASSET_TYPES",
    "AWS_APPLICATION_SERVICE_ASSET_TYPES",
    "AWS_APPLICATION_ENDPOINTS",
    "AWS_APPLICATION_GLOBAL_ASSET_TYPES",
    "AWS_APPLICATION_GLOBAL_ENDPOINTS",
    "AWS_APPLICATION_COLLECTION_SPECS",
    "AWS_APPLICATION_SERVICE_COLLECTION_SPECS",
    "CoreAWSApplicationServiceAsset",
    "CoreAWSAPIGatewayRestAPI",
    "CoreAWSAPIGatewayV2API",
    "CoreAWSEventBridgeBus",
    "CoreAWSEventBridgeRule",
    "CoreAWSEventBridgeSchedule",
    "CoreAWSEventBridgePipe",
    "CoreAWSSNSTopic",
    "CoreAWSSQSQueue",
    "CoreAWSStepFunctionsStateMachine",
    "CoreAWSAthenaWorkgroup",
    "CoreAWSAthenaDataCatalog",
    "CoreAWSCloudFormationStack",
    "CoreAWSEventbridgeBus",
    "CoreAWSEventbridgeRule",
    "CoreAWSEventbridgeSchedule",
    "CoreAWSEventbridgePipe",
    "CoreAWSStepfunctionsStateMachine",
    "CoreAWSAthenaWorkGroup",
    "AWS_APPLICATION_ASSET_MODELS",
    "sync_aws_application_service_assets",
]
