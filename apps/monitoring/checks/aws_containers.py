"""Read-only AWS container/platform status checks.

The functions in this module use the same two-argument check contract as the
other providers: ``(unique_id, credentials) -> (status, metadata)``.  AWS
provider failures are reduced to a safe error code and provider payloads are
allow-listed before they are returned to the monitoring engine.
"""

from collections.abc import Mapping

from botocore.exceptions import BotoCoreError, ClientError

from apps.console.cloud.aws.containers import (
    AWS_CONTAINER_ASSET_TYPES,
    CoreAWSAppRunnerDeployment,
    CoreAWSAppRunnerService,
    CoreAWSECRImage,
    CoreAWSECRRepository,
    CoreAWSContainerAsset,
    CoreAWSECSDeployment,
    CoreAWSECSTaskDefinition,
    CoreAWSEKSAddon,
    CoreAWSEKSCluster,
    CoreAWSEKSFargateProfile,
    CoreAWSEKSNodeGroup,
    _safe_apprunner_service,
    aws_check_account,
    resource_context,
)
from apps.console.cloud.aws.discovery import (
    aws_client,
    aws_error_code,
    require_collection,
    serialize_aws,
)
from apps.console.cloud.models import CloudInventoryTransientError


_NOT_FOUND_CODES = {
    "ClusterNotFoundException",
    "ImageNotFoundException",
    "ResourceNotFoundException",
    "RepositoryNotFoundException",
    "ServiceNotFoundException",
    "TaskDefinitionNotFoundException",
    "FargateProfileNotFoundException",
    "AddonNotFoundException",
}
_AUTH_CODES = {
    "AccessDenied",
    "AccessDeniedException",
    "ExpiredToken",
    "InvalidClientTokenId",
    "UnrecognizedClientException",
}


class _ProviderNotFound(Exception):
    """The provider successfully answered but did not return the asset."""


def _context(credentials):
    if not isinstance(credentials, dict):
        raise ValueError("AWS credentials are not configured")
    region = credentials.get("resource_region") or credentials.get("region")
    if not isinstance(region, str) or not region.strip():
        raise ValueError("AWS resource region is missing")
    return region.strip(), resource_context(credentials)


def _client(credentials, service, region):
    return aws_client(aws_check_account(credentials, region), service, region=region)


def _required_resource(response, key, context):
    if not isinstance(response, Mapping):
        raise CloudInventoryTransientError(f"AWS returned an invalid {context} response")
    resource = response.get(key)
    if not isinstance(resource, Mapping):
        raise CloudInventoryTransientError(f"AWS returned an invalid {context} resource")
    return resource


def _first_resource(response, key, context):
    values = require_collection(response, key, context)
    if not values:
        raise _ProviderNotFound()
    for value in values:
        if isinstance(value, Mapping):
            return value
    raise CloudInventoryTransientError(f"AWS returned an invalid {context} resource")


def _find_by(resources, field, expected, context):
    for resource in resources:
        if isinstance(resource, Mapping) and resource.get(field) == expected:
            return resource
    if resources and isinstance(resources[0], Mapping):
        # AWS normally returns exactly the requested object.  Accepting the
        # first object also keeps checks compatible with older regional API
        # responses that omit the echoed identifier.
        return resources[0]
    raise _ProviderNotFound()


def _safe_fields(resource, fields, context):
    if not isinstance(resource, Mapping):
        raise CloudInventoryTransientError(f"AWS returned an invalid {context} resource")
    return {field: resource[field] for field in fields if field in resource}


def _safe_task_definition(resource):
    safe = _safe_fields(
        resource,
        (
            "taskDefinitionArn",
            "family",
            "revision",
            "status",
            "requiresAttributes",
            "compatibilities",
            "requiresCompatibilities",
            "runtimePlatform",
            "cpu",
            "memory",
            "networkMode",
            "executionRoleArn",
            "taskRoleArn",
            "pidMode",
            "ipcMode",
            "registeredAt",
            "deregisteredAt",
            "ephemeralStorage",
        ),
        "ECS task definition",
    )
    definitions = resource.get("containerDefinitions")
    if definitions is not None:
        if not isinstance(definitions, list):
            raise CloudInventoryTransientError("AWS returned invalid ECS container definitions")
        safe_definitions = []
        for definition in definitions:
            if not isinstance(definition, Mapping):
                raise CloudInventoryTransientError("AWS returned an invalid ECS container definition")
            item = _safe_fields(
                definition,
                (
                    "name",
                    "image",
                    "essential",
                    "cpu",
                    "memory",
                    "memoryReservation",
                    "portMappings",
                    "healthCheck",
                    "dependsOn",
                    "resourceRequirements",
                    "linuxParameters",
                    "readonlyRootFilesystem",
                    "privileged",
                ),
                "ECS container definition",
            )
            if "portMappings" in item:
                if not isinstance(item["portMappings"], list):
                    raise CloudInventoryTransientError("AWS returned invalid ECS port mappings")
                item["portMappings"] = [
                    _safe_fields(
                        port,
                        ("containerPort", "hostPort", "protocol", "appProtocol"),
                        "ECS port mapping",
                    )
                    for port in item["portMappings"]
                    if isinstance(port, Mapping)
                ]
            if "healthCheck" in item:
                health_check = item["healthCheck"]
                if not isinstance(health_check, Mapping):
                    raise CloudInventoryTransientError("AWS returned an invalid ECS health check")
                item["healthCheck"] = _safe_fields(
                    health_check,
                    ("interval", "timeout", "retries", "startPeriod"),
                    "ECS health check",
                )
            safe_definitions.append(item)
        safe["containerDefinitions"] = safe_definitions
    return safe


def _safe_deployment(resource):
    return _safe_fields(
        resource,
        (
            "id",
            "deploymentId",
            "status",
            "rolloutState",
            "rolloutStateReason",
            "desiredCount",
            "pendingCount",
            "runningCount",
            "failedTasks",
            "createdAt",
            "updatedAt",
            "taskDefinition",
            "launchType",
            "capacityProviderStrategy",
            "platformVersion",
        ),
        "ECS deployment",
    )


def _safe_eks_cluster(resource):
    return _safe_fields(
        resource,
        (
            "name",
            "arn",
            "createdAt",
            "version",
            "endpoint",
            "status",
            "certificateAuthority",
            "platformVersion",
            "roleArn",
            "resourcesVpcConfig",
            "kubernetesNetworkConfig",
            "logging",
            "identity",
            "encryptionConfig",
            "tags",
        ),
        "EKS cluster",
    )


def _safe_eks_node_group(resource):
    return _safe_fields(
        resource,
        (
            "nodegroupName",
            "nodegroupArn",
            "clusterName",
            "version",
            "releaseVersion",
            "createdAt",
            "modifiedAt",
            "status",
            "capacityType",
            "scalingConfig",
            "instanceTypes",
            "subnets",
            "amiType",
            "nodeRole",
            "labels",
            "taints",
            "health",
            "updateConfig",
            "tags",
        ),
        "EKS node group",
    )


def _safe_eks_addon(resource):
    return _safe_fields(
        resource,
        (
            "addonName",
            "addonArn",
            "clusterName",
            "addonVersion",
            "serviceAccountRoleArn",
            "createdAt",
            "modifiedAt",
            "status",
            "health",
            "marketplaceInformation",
            "publisher",
            "owner",
            "tags",
        ),
        "EKS add-on",
    )


def _safe_eks_fargate_profile(resource):
    return _safe_fields(
        resource,
        (
            "fargateProfileName",
            "fargateProfileArn",
            "clusterName",
            "createdAt",
            "podExecutionRoleArn",
            "subnets",
            "selectors",
            "status",
            "tags",
        ),
        "EKS Fargate profile",
    )


def _safe_apprunner_operation(resource):
    return _safe_fields(
        resource,
        (
            "Id",
            "OperationId",
            "Type",
            "Status",
            "TargetArn",
            "StartedAt",
            "EndedAt",
            "UpdatedAt",
        ),
        "App Runner operation",
    )


def _normalize_status(value):
    if value is None:
        raise CloudInventoryTransientError("AWS returned a resource without a status")
    raw = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    if not raw:
        raise CloudInventoryTransientError("AWS returned a resource without a status")

    if raw in {"healthy"}:
        return "healthy"
    if raw in {"running", "in_service", "primary"}:
        return "running"
    if raw in {
        "active",
        "available",
        "ready",
        "enabled",
        "succeeded",
        "success",
        "completed",
        "complete",
        "deployed",
        "create_complete",
        "update_complete",
    }:
        return "active"
    if raw in {
        "creating",
        "provisioning",
        "pending",
        "pending_deployment",
        "in_progress",
        "operation_in_progress",
        "updating",
        "update_in_progress",
        "create_in_progress",
    }:
        return "provisioning"
    if raw in {"degraded"}:
        return "degraded"
    if raw in {
        "failed",
        "error",
        "create_failed",
        "update_failed",
        "delete_failed",
        "delete_failed_resource",
        "rollback_failed",
        "deployment_failed",
    }:
        return "failed"
    if raw in {"stopped", "stopping", "cancelled", "canceled"}:
        return "stopped"
    if raw in {"inactive"}:
        return "inactive"
    if raw in {"deleting", "delete_in_progress", "deleted", "paused"}:
        return "inactive"
    return raw


def _provider_error(error):
    code = aws_error_code(error)
    if code in _NOT_FOUND_CODES or "notfound" in code.lower() or "not_found" in code.lower():
        status = "not_found"
    elif code in _AUTH_CODES or "accessdenied" in code.lower() or "unauthorized" in code.lower():
        status = "invalid_access_token"
    else:
        status = "error"
    return status, {"errorCode": code}


def _malformed_error(error):
    return "error", {"errorCode": type(error).__name__[:128]}


def _check_ecr_repository(unique_id, credentials, region, metadata):
    repository_name = metadata.get("_cloudmoo_repository_name") or str(unique_id).rsplit(":", 1)[-1]
    client = _client(credentials, "ecr", region)
    response = client.describe_repositories(repositoryNames=[repository_name])
    resource = _first_resource(response, "repositories", "ECR repository")
    resource = _find_by([resource], "repositoryName", repository_name, "ECR repository")
    safe = _safe_fields(
        resource,
        (
            "repositoryArn",
            "registryId",
            "repositoryName",
            "repositoryUri",
            "createdAt",
            "imageTagMutability",
            "imageScanningConfiguration",
            "encryptionConfiguration",
        ),
        "ECR repository",
    )
    return "active", {"aws_ecr_repository": serialize_aws(safe)}


def _check_ecr_image(unique_id, credentials, region, metadata):
    repository_name = metadata.get("_cloudmoo_repository_name")
    digest = metadata.get("_cloudmoo_image_digest")
    tags = metadata.get("_cloudmoo_image_tags")
    if not repository_name:
        raise ValueError("ECR image repository context is missing")
    image_id = {"imageDigest": digest} if digest else None
    if image_id is None and isinstance(tags, list) and tags:
        image_id = {"imageTag": str(tags[0])}
    if image_id is None:
        raise ValueError("ECR image identity is missing")
    client = _client(credentials, "ecr", region)
    response = client.describe_images(repositoryName=repository_name, imageIds=[image_id])
    resource = _first_resource(response, "imageDetails", "ECR image")
    safe = _safe_fields(
        resource,
        (
            "registryId",
            "repositoryName",
            "imageDigest",
            "imageTags",
            "imageSizeInBytes",
            "imagePushedAt",
            "imageScanStatus",
            "imageScanFindingsSummary",
            "artifactMediaType",
            "imageManifestMediaType",
            "lastRecordedPullTime",
        ),
        "ECR image",
    )
    return "active", {"aws_ecr_image": serialize_aws(safe)}


def _check_task_definition(unique_id, credentials, region, metadata):
    task_definition_arn = metadata.get("_cloudmoo_task_definition_arn")
    if not task_definition_arn:
        raise ValueError("ECS task definition context is missing")
    client = _client(credentials, "ecs", region)
    response = client.describe_task_definition(taskDefinition=task_definition_arn)
    resource = _required_resource(response, "taskDefinition", "ECS task definition")
    safe = _safe_task_definition(resource)
    return _normalize_status(resource.get("status", "ACTIVE")), {
        "aws_ecs_task_definition": serialize_aws(safe),
    }


def _check_ecs_deployment(unique_id, credentials, region, metadata):
    cluster_arn = metadata.get("_cloudmoo_cluster_arn")
    service_arn = metadata.get("_cloudmoo_service_arn")
    deployment_id = metadata.get("_cloudmoo_deployment_id")
    if not cluster_arn or not service_arn or not deployment_id:
        raise ValueError("ECS deployment context is missing")
    client = _client(credentials, "ecs", region)
    response = client.describe_services(cluster=cluster_arn, services=[service_arn])
    services = require_collection(response, "services", "ECS service")
    service = _find_by(services, "serviceArn", service_arn, "ECS service")
    deployments = service.get("deployments")
    if not isinstance(deployments, list):
        raise CloudInventoryTransientError("AWS returned invalid ECS deployments")
    deployment = None
    for candidate in deployments:
        if isinstance(candidate, Mapping) and (
            candidate.get("id") == deployment_id or candidate.get("deploymentId") == deployment_id
        ):
            deployment = candidate
            break
    if deployment is None:
        raise _ProviderNotFound()
    rollout = deployment.get("rolloutState") or deployment.get("status")
    return _normalize_status(rollout), {
        "aws_ecs_deployment": serialize_aws(_safe_deployment(deployment)),
    }


def _check_eks_cluster(unique_id, credentials, region, metadata):
    cluster_name = metadata.get("_cloudmoo_cluster_name")
    if not cluster_name:
        raise ValueError("EKS cluster context is missing")
    client = _client(credentials, "eks", region)
    resource = _required_resource(client.describe_cluster(name=cluster_name), "cluster", "EKS cluster")
    return _normalize_status(resource.get("status")), {
        "aws_eks_cluster": serialize_aws(_safe_eks_cluster(resource)),
    }


def _check_eks_node_group(unique_id, credentials, region, metadata):
    cluster_name = metadata.get("_cloudmoo_cluster_name")
    nodegroup_name = metadata.get("_cloudmoo_nodegroup_name")
    if not cluster_name or not nodegroup_name:
        raise ValueError("EKS node group context is missing")
    client = _client(credentials, "eks", region)
    resource = _required_resource(
        client.describe_nodegroup(clusterName=cluster_name, nodegroupName=nodegroup_name),
        "nodegroup",
        "EKS node group",
    )
    return _normalize_status(resource.get("status")), {
        "aws_eks_node_group": serialize_aws(_safe_eks_node_group(resource)),
    }


def _check_eks_addon(unique_id, credentials, region, metadata):
    cluster_name = metadata.get("_cloudmoo_cluster_name")
    addon_name = metadata.get("_cloudmoo_addon_name")
    if not cluster_name or not addon_name:
        raise ValueError("EKS add-on context is missing")
    client = _client(credentials, "eks", region)
    resource = _required_resource(
        client.describe_addon(clusterName=cluster_name, addonName=addon_name),
        "addon",
        "EKS add-on",
    )
    return _normalize_status(resource.get("status")), {
        "aws_eks_addon": serialize_aws(_safe_eks_addon(resource)),
    }


def _check_eks_fargate_profile(unique_id, credentials, region, metadata):
    cluster_name = metadata.get("_cloudmoo_cluster_name")
    profile_name = metadata.get("_cloudmoo_fargate_profile_name")
    if not cluster_name or not profile_name:
        raise ValueError("EKS Fargate profile context is missing")
    client = _client(credentials, "eks", region)
    resource = _required_resource(
        client.describe_fargate_profile(
            clusterName=cluster_name,
            fargateProfileName=profile_name,
        ),
        "fargateProfile",
        "EKS Fargate profile",
    )
    return _normalize_status(resource.get("status")), {
        "aws_eks_fargate_profile": serialize_aws(_safe_eks_fargate_profile(resource)),
    }


def _check_apprunner_service(unique_id, credentials, region, metadata):
    service_arn = metadata.get("_cloudmoo_service_arn")
    if not service_arn:
        raise ValueError("App Runner service context is missing")
    client = _client(credentials, "apprunner", region)
    resource = _required_resource(
        client.describe_service(ServiceArn=service_arn),
        "Service",
        "App Runner service",
    )
    return _normalize_status(resource.get("Status")), {
        "aws_apprunner_service": serialize_aws(_safe_apprunner_service(resource)),
    }


def _check_apprunner_deployment(unique_id, credentials, region, metadata):
    service_arn = metadata.get("_cloudmoo_service_arn")
    operation_id = metadata.get("_cloudmoo_operation_id")
    if not service_arn or not operation_id:
        raise ValueError("App Runner operation context is missing")
    client = _client(credentials, "apprunner", region)
    resource = _required_resource(
        client.describe_operation(ServiceArn=service_arn, OperationId=operation_id),
        "Operation",
        "App Runner operation",
    )
    return _normalize_status(resource.get("Status")), {
        "aws_apprunner_deployment": serialize_aws(_safe_apprunner_operation(resource)),
    }


_CHECK_HANDLERS = {
    "aws_ecr_repository": _check_ecr_repository,
    "aws_ecr_image": _check_ecr_image,
    "aws_ecs_task_definition": _check_task_definition,
    "aws_ecs_deployment": _check_ecs_deployment,
    "aws_eks_cluster": _check_eks_cluster,
    "aws_eks_node_group": _check_eks_node_group,
    "aws_eks_addon": _check_eks_addon,
    "aws_eks_fargate_profile": _check_eks_fargate_profile,
    "aws_apprunner_service": _check_apprunner_service,
    "aws_apprunner_deployment": _check_apprunner_deployment,
}


def check_aws_container_asset_status(asset_type, unique_id, credentials):
    """Check one AWS container/platform asset using read-only calls only."""
    handler = _CHECK_HANDLERS.get(asset_type)
    if handler is None:
        return "error", {"errorCode": "UnsupportedAssetType"}
    try:
        region, metadata = _context(credentials)
        return handler(unique_id, credentials, region, metadata)
    except _ProviderNotFound:
        return "not_found", {"errorCode": "ResourceNotFound"}
    except (ClientError, BotoCoreError) as error:
        return _provider_error(error)
    except (CloudInventoryTransientError, KeyError, TypeError, ValueError) as error:
        return _malformed_error(error)
    except Exception as error:
        return _malformed_error(error)


def _make_check(asset_type):
    def check(unique_id, credentials):
        return check_aws_container_asset_status(asset_type, unique_id, credentials)

    check.__name__ = f"check_aws_{asset_type}_status"
    return check


for _asset_type in AWS_CONTAINER_ASSET_TYPES:
    globals()[f"check_aws_{_asset_type}_status"] = _make_check(_asset_type)

# The integration lane consumes canonical, fully namespaced asset types.  The
# shorter aliases keep direct callers ergonomic without changing that map.
AWS_CONTAINER_STATUS_CHECKS = {
    asset_type: globals()[f"check_aws_{asset_type}_status"]
    for asset_type in AWS_CONTAINER_ASSET_TYPES
}
AWS_CONTAINER_CHECKS = AWS_CONTAINER_STATUS_CHECKS
AWS_CONTAINER_ASSET_TYPE_CHECKS = AWS_CONTAINER_STATUS_CHECKS
AWS_CONTAINER_CHECK_FUNCTIONS = AWS_CONTAINER_STATUS_CHECKS
AWS_CONTAINER_CHECK_REGISTRY = AWS_CONTAINER_STATUS_CHECKS
CHECK_REGISTRATION = AWS_CONTAINER_STATUS_CHECKS

for _asset_type in AWS_CONTAINER_ASSET_TYPES:
    _short_name = _asset_type.removeprefix("aws_")
    globals()[f"check_aws_{_short_name}_status"] = AWS_CONTAINER_STATUS_CHECKS[_asset_type]


__all__ = [
    "AWS_CONTAINER_ASSET_TYPES",
    "AWS_CONTAINER_STATUS_CHECKS",
    "AWS_CONTAINER_CHECKS",
    "AWS_CONTAINER_ASSET_TYPE_CHECKS",
    "AWS_CONTAINER_CHECK_FUNCTIONS",
    "AWS_CONTAINER_CHECK_REGISTRY",
    "CHECK_REGISTRATION",
    "check_aws_container_asset_status",
    *[f"check_aws_{asset_type}_status" for asset_type in AWS_CONTAINER_ASSET_TYPES],
    *[f"check_aws_{asset_type.removeprefix('aws_')}_status" for asset_type in AWS_CONTAINER_ASSET_TYPES],
]
