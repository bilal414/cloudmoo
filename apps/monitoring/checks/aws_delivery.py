"""Read-only status checks for AWS delivery and hosting assets.

The integration lane can route the exact asset types exported by
``AWS_DELIVERY_STATUS_CHECKS`` here.  All clients are built through the shared
AWS discovery helper, provider responses are allow-listed, and error output is
reduced to bounded codes.  No check invokes a build, pipeline execution, or
other AWS mutation.
"""

from collections.abc import Mapping
import re
from types import SimpleNamespace

from botocore.exceptions import BotoCoreError, ClientError

from apps.console.cloud.aws.delivery import (
    AWS_CODEBUILD_BUILD,
    AWS_CODEBUILD_PROJECT,
    AWS_CODEPIPELINE_EXECUTION,
    AWS_CODEPIPELINE_PIPELINE,
    AWS_ELASTIC_BEANSTALK_APPLICATION,
    AWS_ELASTIC_BEANSTALK_ENVIRONMENT,
    AWS_DELIVERY_ASSET_TYPES,
)
from apps.console.cloud.aws.discovery import (
    aws_client,
    aws_error_code,
    is_transient_aws_error,
    require_collection,
    serialize_aws,
)
from apps.console.cloud.models import CloudInventoryTransientError
from apps.monitoring.metadata import redact_sensitive_metadata


_NOT_FOUND_CODES = frozenset(
    {
        "ApplicationDoesNotExistException",
        "BuildNotFound",
        "PipelineNotFoundException",
        "ResourceNotFoundException",
    }
)
_AUTH_CODES = frozenset(
    {
        "AccessDenied",
        "AccessDeniedException",
        "ExpiredToken",
        "InvalidClientTokenId",
        "UnrecognizedClientException",
        "UnauthorizedOperation",
    }
)
_REGION_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)+$")
MAX_PAYLOAD_ITEMS = 100
MAX_PAYLOAD_LIST_ITEMS = 100
MAX_PAYLOAD_DEPTH = 5
MAX_PAYLOAD_TEXT = 2_048


class _ProviderNotFound(Exception):
    """The provider answered successfully but did not return the asset."""


def _context(unique_id, credentials):
    if not isinstance(credentials, Mapping):
        raise ValueError("AWS monitoring credentials are invalid")

    metadata = credentials.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    regions = {
        str(value).strip()
        for value in (
            credentials.get("resource_region"),
            credentials.get("region"),
            metadata.get("_cloudmoo_region"),
        )
        if value
    }
    if len(regions) > 1:
        raise ValueError("AWS resource region context is inconsistent")

    if not isinstance(unique_id, str) or not unique_id.strip():
        raise ValueError("AWS resource identifier is invalid")
    unique_id = unique_id.strip()
    identity_region = unique_id.split("|", 1)[0].strip() if "|" in unique_id else ""
    if identity_region and regions and identity_region in regions:
        region = identity_region
    else:
        region = next(iter(regions), "")
    if not region:
        raise ValueError("AWS resource region is missing")
    if len(region) > 64 or _REGION_RE.fullmatch(region) is None:
        raise ValueError("AWS resource region is invalid")
    if identity_region and regions and identity_region not in regions:
        raise ValueError("AWS resource region does not match stored context")
    return region, metadata


def _raw_id(unique_id, metadata):
    value = metadata.get("_cloudmoo_raw_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return unique_id.split("|", 1)[1] if "|" in unique_id else unique_id


def _client(credentials, service, region):
    access_key = credentials.get("access_key") or credentials.get("aws_access_key_id")
    secret_key = credentials.get("secret_key") or credentials.get("aws_secret_access_key")
    if not access_key or not secret_key:
        raise ValueError("AWS monitoring credentials are incomplete")
    account = SimpleNamespace(
        access_key=access_key,
        secret_key=secret_key,
        region=region,
    )
    return aws_client(account, service, region=region)


def _first_resource(response, key, context, identifier_field=None, expected=None):
    resources = require_collection(response, key, context)
    for resource in resources:
        if not isinstance(resource, Mapping):
            raise CloudInventoryTransientError(f"AWS returned an invalid {context} resource")
        if identifier_field is None or expected is None or resource.get(identifier_field) == expected:
            return resource
    if resources and identifier_field is None:
        resource = resources[0]
        if isinstance(resource, Mapping):
            return resource
    if resources and len(resources) == 1:
        resource = resources[0]
        if isinstance(resource, Mapping):
            return resource
    raise _ProviderNotFound()


def _required_resource(response, key, context):
    if not isinstance(response, Mapping):
        raise CloudInventoryTransientError(f"AWS returned an invalid {context} response")
    resource = response.get(key)
    if not isinstance(resource, Mapping):
        raise CloudInventoryTransientError(f"AWS returned an invalid {context} resource")
    return resource


def _safe_fields(resource, fields, context):
    if not isinstance(resource, Mapping):
        raise CloudInventoryTransientError(f"AWS returned an invalid {context} resource")
    return {field: resource[field] for field in fields if field in resource}


def _normalize_status(value, family):
    if value in (None, ""):
        return "unknown"
    raw = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    if not raw:
        return "unknown"

    mappings = {
        "beanstalk": {
            "ready": "active",
            "green": "healthy",
            "yellow": "degraded",
            "red": "failed",
            "grey": "unknown",
            "launching": "provisioning",
            "updating": "provisioning",
            "update_in_progress": "provisioning",
            "pending": "provisioning",
            "terminating": "stopping",
            "terminated": "inactive",
        },
        "codebuild_project": {"available": "active", "ready": "active"},
        "codebuild_build": {
            "succeeded": "succeeded",
            "in_progress": "running",
            "queued": "queued",
            "failed": "failed",
            "fault": "failed",
            "timed_out": "failed",
            "stopped": "stopped",
            "phase_failure": "failed",
        },
        "codepipeline_pipeline": {"available": "active"},
        "codepipeline_execution": {
            "succeeded": "succeeded",
            "in_progress": "running",
            "failed": "failed",
            "stopped": "stopped",
            "superseded": "inactive",
        },
    }
    return mappings.get(family, {}).get(raw, raw)


def _payload(asset_type, resource, fields, region, status=None, context=None):
    safe = _safe_fields(resource, fields, asset_type)
    if asset_type == AWS_CODEPIPELINE_EXECUTION:
        if "sourceRevisions" in resource:
            revisions = resource["sourceRevisions"]
            if not isinstance(revisions, list):
                raise CloudInventoryTransientError(
                    "AWS returned an invalid CodePipeline source revisions collection"
                )
            safe["sourceRevisions"] = [
                _safe_fields(
                    revision,
                    ("actionName", "revisionId", "revisionType"),
                    "CodePipeline source revision",
                )
                for revision in revisions[:100]
            ]
        if "trigger" in resource:
            trigger = resource["trigger"]
            if trigger is not None:
                safe["trigger"] = _safe_fields(
                    trigger,
                    ("triggerType",),
                    "CodePipeline execution trigger",
                )
    value = {
        asset_type: serialize_aws(redact_sensitive_metadata(safe)),
        "_cloudmoo_region": region,
    }
    if status not in (None, ""):
        value["providerStatus"] = str(status)[:128]
    if isinstance(context, Mapping):
        value.update(context)
    return _bound_payload(serialize_aws(redact_sensitive_metadata(value)))


def _bound_payload(value, depth=0):
    if depth > MAX_PAYLOAD_DEPTH:
        return "<truncated>"
    if isinstance(value, Mapping):
        result = {}
        for index, (key, child) in enumerate(value.items()):
            if index >= MAX_PAYLOAD_ITEMS:
                result["_cloudmoo_truncated_items"] = True
                break
            result[str(key)[:128]] = _bound_payload(child, depth + 1)
        return result
    if isinstance(value, list):
        result = [_bound_payload(item, depth + 1) for item in value[:MAX_PAYLOAD_LIST_ITEMS]]
        if len(value) > MAX_PAYLOAD_LIST_ITEMS:
            result.append("<truncated>")
        return result
    if isinstance(value, str):
        return value[:MAX_PAYLOAD_TEXT]
    return value


def _check_beanstalk_application(unique_id, credentials, region, metadata):
    name = metadata.get("_cloudmoo_provider_name") or _raw_id(unique_id, metadata)
    client = _client(credentials, "elasticbeanstalk", region)
    response = client.describe_applications(ApplicationNames=[name])
    resource = _first_resource(
        response,
        "Applications",
        "Elastic Beanstalk application",
        "ApplicationName",
        name,
    )
    return "active", _payload(
        AWS_ELASTIC_BEANSTALK_APPLICATION,
        resource,
        (
            "ApplicationName",
            "ApplicationArn",
            "Description",
            "DateCreated",
            "DateUpdated",
            "OperationsRole",
        ),
        region,
        status="available",
    )


def _check_beanstalk_environment(unique_id, credentials, region, metadata):
    environment_id = metadata.get("_cloudmoo_environment_id")
    environment_name = metadata.get("_cloudmoo_environment_name") or metadata.get(
        "_cloudmoo_provider_name"
    )
    client = _client(credentials, "elasticbeanstalk", region)
    if environment_id:
        response = client.describe_environments(EnvironmentIds=[environment_id])
        expected_field = "EnvironmentId"
        expected_value = environment_id
    else:
        environment_name = environment_name or _raw_id(unique_id, metadata)
        response = client.describe_environments(EnvironmentNames=[environment_name])
        expected_field = "EnvironmentName"
        expected_value = environment_name
    resource = _first_resource(
        response,
        "Environments",
        "Elastic Beanstalk environment",
        expected_field,
        expected_value,
    )
    provider_status = resource.get("Status") or resource.get("Health")
    return _normalize_status(provider_status, "beanstalk"), _payload(
        AWS_ELASTIC_BEANSTALK_ENVIRONMENT,
        resource,
        (
            "EnvironmentId",
            "EnvironmentName",
            "EnvironmentArn",
            "ApplicationName",
            "ApplicationArn",
            "Status",
            "Health",
            "HealthStatus",
            "SolutionStackName",
            "PlatformArn",
            "VersionLabel",
            "CNAME",
            "EndpointURL",
            "DateCreated",
            "DateUpdated",
        ),
        region,
        status=provider_status,
    )


def _check_codebuild_project(unique_id, credentials, region, metadata):
    name = metadata.get("_cloudmoo_project_name") or _raw_id(unique_id, metadata)
    client = _client(credentials, "codebuild", region)
    response = client.batch_get_projects(names=[name])
    projects = require_collection(response, "projects", "CodeBuild project")
    if response.get("projectsNotFound"):
        raise _ProviderNotFound()
    resource = _first_resource(response, "projects", "CodeBuild project", "name", name)
    return "active", _payload(
        AWS_CODEBUILD_PROJECT,
        resource,
        (
            "name",
            "arn",
            "description",
            "created",
            "lastModified",
            "sourceVersion",
            "badgeEnabled",
            "timeoutInMinutes",
            "queuedTimeoutInMinutes",
            "serviceRole",
            "encryptionKey",
        ),
        region,
        status="available",
        context={"projectCount": len(projects)},
    )


def _check_codebuild_build(unique_id, credentials, region, metadata):
    build_id = metadata.get("_cloudmoo_build_id") or _raw_id(unique_id, metadata)
    client = _client(credentials, "codebuild", region)
    response = client.batch_get_builds(ids=[build_id])
    builds = require_collection(response, "builds", "CodeBuild build")
    if response.get("buildsNotFound"):
        raise _ProviderNotFound()
    resource = _first_resource(response, "builds", "CodeBuild build", "id", build_id)
    provider_status = resource.get("buildStatus")
    return _normalize_status(provider_status, "codebuild_build"), _payload(
        AWS_CODEBUILD_BUILD,
        resource,
        (
            "id",
            "arn",
            "buildNumber",
            "projectName",
            "buildStatus",
            "startTime",
            "endTime",
            "currentPhase",
            "sourceVersion",
            "resolvedSourceVersion",
            "initiator",
            "buildComplete",
        ),
        region,
        status=provider_status,
    )


def _check_codepipeline_pipeline(unique_id, credentials, region, metadata):
    name = metadata.get("_cloudmoo_pipeline_name") or _raw_id(unique_id, metadata)
    client = _client(credentials, "codepipeline", region)
    response = client.get_pipeline(name=name)
    resource = _required_resource(response, "pipeline", "CodePipeline pipeline")
    provider_status = resource.get("status") or "available"
    return _normalize_status(provider_status, "codepipeline_pipeline"), _payload(
        AWS_CODEPIPELINE_PIPELINE,
        resource,
        (
            "name",
            "version",
            "pipelineType",
            "executionMode",
            "roleArn",
            "created",
            "updated",
            "pipelineArn",
        ),
        region,
        status=provider_status,
    )


def _check_codepipeline_execution(unique_id, credentials, region, metadata):
    name = metadata.get("_cloudmoo_pipeline_name")
    execution_id = metadata.get("_cloudmoo_execution_id") or _raw_id(unique_id, metadata)
    if not name:
        raise ValueError("CodePipeline execution context is missing")
    client = _client(credentials, "codepipeline", region)
    response = client.get_pipeline_execution(
        pipelineName=name,
        pipelineExecutionId=execution_id,
    )
    resource = _required_resource(response, "pipelineExecution", "CodePipeline execution")
    provider_status = resource.get("status")
    return _normalize_status(provider_status, "codepipeline_execution"), _payload(
        AWS_CODEPIPELINE_EXECUTION,
        resource,
        (
            "pipelineExecutionId",
            "status",
            "startTime",
            "lastUpdateTime",
        ),
        region,
        status=provider_status,
        context={"pipelineName": name},
    )


_CHECK_HANDLERS = {
    AWS_ELASTIC_BEANSTALK_APPLICATION: _check_beanstalk_application,
    AWS_ELASTIC_BEANSTALK_ENVIRONMENT: _check_beanstalk_environment,
    AWS_CODEBUILD_PROJECT: _check_codebuild_project,
    AWS_CODEBUILD_BUILD: _check_codebuild_build,
    AWS_CODEPIPELINE_PIPELINE: _check_codepipeline_pipeline,
    AWS_CODEPIPELINE_EXECUTION: _check_codepipeline_execution,
}


def _provider_error(error):
    code = aws_error_code(error)
    normalized = str(code).lower()
    if code in _NOT_FOUND_CODES or "notfound" in normalized or "not_found" in normalized:
        status = "not_found"
        classification = "not_found"
    elif code in _AUTH_CODES or "accessdenied" in normalized or "unauthorized" in normalized:
        status = "invalid_access_token"
        classification = "provider"
    elif is_transient_aws_error(error):
        status = "error"
        classification = "transient"
    else:
        status = "error"
        classification = "provider"
    return status, {"error": {"code": str(code)[:128], "classification": classification}}


def _malformed_error(error):
    return "error", {"error": {"code": type(error).__name__[:128], "classification": "adapter"}}


def check_aws_delivery_asset_status(asset_type, unique_id, credentials):
    """Check one AWS delivery asset using read-only provider operations."""
    handler = _CHECK_HANDLERS.get(asset_type)
    if handler is None:
        return "error", {"error": {"code": "UnsupportedAssetType", "classification": "adapter"}}
    try:
        region, metadata = _context(unique_id, credentials)
        return handler(unique_id, credentials, region, metadata)
    except _ProviderNotFound:
        return "not_found", {"error": {"code": "ResourceNotFound", "classification": "not_found"}}
    except (ClientError, BotoCoreError) as error:
        return _provider_error(error)
    except (CloudInventoryTransientError, KeyError, TypeError, ValueError) as error:
        return _malformed_error(error)
    except Exception as error:
        return _malformed_error(error)


def _make_check(asset_type):
    def check(unique_id, credentials):
        return check_aws_delivery_asset_status(asset_type, unique_id, credentials)

    check.__name__ = f"check_aws_{asset_type}_status"
    return check


for _asset_type in AWS_DELIVERY_ASSET_TYPES:
    globals()[f"check_aws_{_asset_type}_status"] = _make_check(_asset_type)


AWS_DELIVERY_STATUS_CHECKS = {
    asset_type: globals()[f"check_aws_{asset_type}_status"]
    for asset_type in AWS_DELIVERY_ASSET_TYPES
}
AWS_DELIVERY_CHECKS = AWS_DELIVERY_STATUS_CHECKS
AWS_DELIVERY_CHECK_FUNCTIONS = AWS_DELIVERY_STATUS_CHECKS
AWS_DELIVERY_CHECK_REGISTRY = AWS_DELIVERY_STATUS_CHECKS
CHECK_REGISTRATION = AWS_DELIVERY_STATUS_CHECKS

# Short aliases mirror the other AWS check modules and make later registry
# wiring straightforward without changing the canonical asset type keys.
for _asset_type in AWS_DELIVERY_ASSET_TYPES:
    _short_name = _asset_type.removeprefix("aws_")
    globals()[f"check_aws_{_short_name}_status"] = AWS_DELIVERY_STATUS_CHECKS[_asset_type]


__all__ = [
    "AWS_DELIVERY_STATUS_CHECKS",
    "AWS_DELIVERY_CHECKS",
    "AWS_DELIVERY_CHECK_FUNCTIONS",
    "AWS_DELIVERY_CHECK_REGISTRY",
    "CHECK_REGISTRATION",
    "check_aws_delivery_asset_status",
    *[f"check_aws_{asset_type}_status" for asset_type in AWS_DELIVERY_ASSET_TYPES],
    *[
        f"check_aws_{asset_type.removeprefix('aws_')}_status"
        for asset_type in AWS_DELIVERY_ASSET_TYPES
    ],
]
