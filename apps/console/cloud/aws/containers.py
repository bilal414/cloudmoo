"""Read-only inventory for AWS container and application platforms.

The existing AWS adapter already owns ECS services and tasks.  This module
deliberately adds only the adjacent resource records: ECR repositories and
images, ECS task definitions and service deployments, EKS resources, and App
Runner services and operations.

All provider responses are reduced to allow-listed metadata before they reach
the model boundary.  In particular, task/container environment variables,
secret references, and log data are never part of an inventory record.
"""

import hashlib
import logging
from types import SimpleNamespace

from botocore.exceptions import BotoCoreError, ClientError
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


AWS_ECR_REPOSITORY = "aws_ecr_repository"
AWS_ECR_IMAGE = "aws_ecr_image"
AWS_ECS_TASK_DEFINITION = "aws_ecs_task_definition"
AWS_ECS_DEPLOYMENT = "aws_ecs_deployment"
AWS_EKS_CLUSTER = "aws_eks_cluster"
AWS_EKS_NODE_GROUP = "aws_eks_node_group"
AWS_EKS_ADDON = "aws_eks_addon"
AWS_EKS_FARGATE_PROFILE = "aws_eks_fargate_profile"
AWS_APPRUNNER_SERVICE = "aws_apprunner_service"
AWS_APPRUNNER_DEPLOYMENT = "aws_apprunner_deployment"

AWS_CONTAINER_ASSET_TYPES = (
    AWS_ECR_REPOSITORY,
    AWS_ECR_IMAGE,
    AWS_ECS_TASK_DEFINITION,
    AWS_ECS_DEPLOYMENT,
    AWS_EKS_CLUSTER,
    AWS_EKS_NODE_GROUP,
    AWS_EKS_ADDON,
    AWS_EKS_FARGATE_PROFILE,
    AWS_APPRUNNER_SERVICE,
    AWS_APPRUNNER_DEPLOYMENT,
)

# These limits are intentionally below the service APIs' maximum page sizes.
# A provider response larger than a limit is treated as incomplete rather than
# silently marking older local records as gone.
MAX_COLLECTION_ITEMS = 10_000
MAX_ECR_IMAGES_PER_REPOSITORY = 2_000
MAX_TASK_DEFINITIONS_PER_REGION = 2_000
MAX_ECS_CLUSTERS_PER_REGION = 1_000
MAX_ECS_SERVICES_PER_CLUSTER = 2_000
MAX_EKS_CLUSTERS_PER_REGION = 1_000
MAX_EKS_CHILDREN_PER_CLUSTER = 500
MAX_APPRUNNER_SERVICES_PER_REGION = 1_000
MAX_APPRUNNER_OPERATIONS_PER_SERVICE = 20


class _InventoryIncomplete(CloudInventoryTransientError):
    """An AWS collection or child expansion exceeded its safe boundary."""


def _owner_identifier_constraint(name):
    return models.UniqueConstraint(
        fields=("owner", "unique_id"),
        name=f"aws_{name}_owner_uid_uniq",
    )


class CoreAWSContainerAsset(UtilAsset):
    """Common fields and monitoring context for the container asset models."""

    region = models.CharField(max_length=32, db_index=True)
    asset_type = None

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
        return {
            "access_key": self.owner.access_key,
            "secret_key": self.owner.secret_key,
            "region": self.region,
            "resource_region": self.region,
            "resource_name": self.unique_id,
            "asset_type": self.type or self.asset_type,
            "provider_type": self.asset_type,
            "metadata": self.metadata if isinstance(self.metadata, dict) else {},
        }

    @property
    def provider_url(self):
        return f"https://{self.region}.console.aws.amazon.com/"

    def check_status(self):
        from apps.monitoring.checks.aws_containers import check_aws_container_asset_status

        return check_aws_container_asset_status(
            self.type,
            self.unique_id,
            self.monitoring_credentials,
        )


class CoreAWSECRRepository(CoreAWSContainerAsset):
    asset_type = AWS_ECR_REPOSITORY
    owner = models.ForeignKey(
        CoreAWSAccount,
        on_delete=models.CASCADE,
        related_name="aws_ecr_repositories",
    )

    class Meta:
        db_table = "core_aws_ecr_repository"
        constraints = [_owner_identifier_constraint("ecr_repo")]


class CoreAWSECRImage(CoreAWSContainerAsset):
    asset_type = AWS_ECR_IMAGE
    owner = models.ForeignKey(
        CoreAWSAccount,
        on_delete=models.CASCADE,
        related_name="aws_ecr_images",
    )

    class Meta:
        db_table = "core_aws_ecr_image"
        constraints = [_owner_identifier_constraint("ecr_image")]


class CoreAWSECSTaskDefinition(CoreAWSContainerAsset):
    asset_type = AWS_ECS_TASK_DEFINITION
    owner = models.ForeignKey(
        CoreAWSAccount,
        on_delete=models.CASCADE,
        related_name="aws_ecs_task_definitions",
    )

    class Meta:
        db_table = "core_aws_ecs_task_definition"
        constraints = [_owner_identifier_constraint("ecs_td")]


class CoreAWSECSDeployment(CoreAWSContainerAsset):
    asset_type = AWS_ECS_DEPLOYMENT
    owner = models.ForeignKey(
        CoreAWSAccount,
        on_delete=models.CASCADE,
        related_name="aws_ecs_deployments",
    )

    class Meta:
        db_table = "core_aws_ecs_deployment"
        constraints = [_owner_identifier_constraint("ecs_deploy")]


class CoreAWSEKSCluster(CoreAWSContainerAsset):
    asset_type = AWS_EKS_CLUSTER
    owner = models.ForeignKey(
        CoreAWSAccount,
        on_delete=models.CASCADE,
        related_name="aws_eks_clusters",
    )

    class Meta:
        db_table = "core_aws_eks_cluster"
        constraints = [_owner_identifier_constraint("eks_cluster")]


class CoreAWSEKSNodeGroup(CoreAWSContainerAsset):
    asset_type = AWS_EKS_NODE_GROUP
    owner = models.ForeignKey(
        CoreAWSAccount,
        on_delete=models.CASCADE,
        related_name="aws_eks_node_groups",
    )

    class Meta:
        db_table = "core_aws_eks_node_group"
        constraints = [_owner_identifier_constraint("eks_node")]


class CoreAWSEKSAddon(CoreAWSContainerAsset):
    asset_type = AWS_EKS_ADDON
    owner = models.ForeignKey(
        CoreAWSAccount,
        on_delete=models.CASCADE,
        related_name="aws_eks_addons",
    )

    class Meta:
        db_table = "core_aws_eks_addon"
        constraints = [_owner_identifier_constraint("eks_addon")]


class CoreAWSEKSFargateProfile(CoreAWSContainerAsset):
    asset_type = AWS_EKS_FARGATE_PROFILE
    owner = models.ForeignKey(
        CoreAWSAccount,
        on_delete=models.CASCADE,
        related_name="aws_eks_fargate_profiles",
    )

    class Meta:
        db_table = "core_aws_eks_fargate_profile"
        constraints = [_owner_identifier_constraint("eks_fargate")]


class CoreAWSAppRunnerService(CoreAWSContainerAsset):
    asset_type = AWS_APPRUNNER_SERVICE
    owner = models.ForeignKey(
        CoreAWSAccount,
        on_delete=models.CASCADE,
        related_name="aws_apprunner_services",
    )

    class Meta:
        db_table = "core_aws_apprunner_service"
        constraints = [_owner_identifier_constraint("apprunner_svc")]


class CoreAWSAppRunnerDeployment(CoreAWSContainerAsset):
    asset_type = AWS_APPRUNNER_DEPLOYMENT
    owner = models.ForeignKey(
        CoreAWSAccount,
        on_delete=models.CASCADE,
        related_name="aws_apprunner_deployments",
    )

    class Meta:
        db_table = "core_aws_apprunner_deployment"
        constraints = [_owner_identifier_constraint("apprunner_dep")]


AWS_CONTAINER_MODELS = {
    "aws_ecr_repository": CoreAWSECRRepository,
    "aws_ecr_image": CoreAWSECRImage,
    "aws_ecs_task_definition": CoreAWSECSTaskDefinition,
    "aws_ecs_deployment": CoreAWSECSDeployment,
    "aws_eks_cluster": CoreAWSEKSCluster,
    "aws_eks_node_group": CoreAWSEKSNodeGroup,
    "aws_eks_addon": CoreAWSEKSAddon,
    "aws_eks_fargate_profile": CoreAWSEKSFargateProfile,
    "aws_apprunner_service": CoreAWSAppRunnerService,
    "aws_apprunner_deployment": CoreAWSAppRunnerDeployment,
}
AWS_CONTAINER_ASSET_MODELS = AWS_CONTAINER_MODELS


def _stable_id(asset_type, region, *identifiers):
    """Return a bounded, region-scoped identifier stable across syncs."""
    raw = ":".join(
        str(value).strip()
        for value in (asset_type, region, *identifiers)
        if value is not None and str(value).strip()
    )
    if len(raw) <= 100:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:48]
    prefix = f"{asset_type}:{region}:"
    return f"{prefix}{digest}"[:100]


def _display_name(value, fallback="AWS container resource"):
    text = str(value or fallback)
    return text[:100]


def _require_mapping(value, context):
    if not isinstance(value, dict):
        raise _InventoryIncomplete(f"AWS returned an invalid {context} object")
    return value


def _require_identifier(value, context):
    if not isinstance(value, str) or not value.strip():
        raise _InventoryIncomplete(f"AWS returned an invalid {context} identifier")
    return value.strip()


def _copy_fields(item, fields, context, required=()):
    item = _require_mapping(item, context)
    for field in required:
        if field not in item or item[field] in (None, ""):
            raise _InventoryIncomplete(f"AWS returned an incomplete {context} object")
    return {field: item[field] for field in fields if field in item}


def _bounded_collection(client, operation, response_key, context, limit=MAX_COLLECTION_ITEMS, **kwargs):
    values = []
    for page in iter_pages(client, operation, **kwargs):
        page_values = require_collection(page, response_key, context)
        values.extend(page_values)
        if len(values) > limit:
            raise _InventoryIncomplete(f"AWS {context} exceeded its safe inventory limit")
    return values


def _describe_collection(response, response_key, context):
    return require_collection(response, response_key, context)


def _with_context(item, region, **context):
    metadata = dict(item)
    metadata["_cloudmoo_region"] = region
    metadata.update(context)
    return serialize_aws(metadata)


def _upsert_asset(model, account, region, unique_id, name, asset_type, metadata):
    asset, created = model.objects.get_or_create(
        owner=account,
        unique_id=unique_id,
        defaults={
            "region": region,
            "name": _display_name(name),
            "type": asset_type,
            "metadata": metadata,
            "monitoring": UtilAsset.Monitoring.ACTIVE,
        },
    )
    asset.region = region
    asset.name = _display_name(name)
    asset.type = asset_type
    asset.metadata = metadata
    if not created and asset.monitoring == UtilAsset.Monitoring.NO_LONGER_EXISTS:
        asset.monitoring = UtilAsset.Monitoring.ACTIVE
    asset.save()
    return asset


def _reconcile(model, account, region, asset_type, records):
    current_ids = []
    for record in records:
        unique_id = _require_identifier(record["unique_id"], f"{asset_type} unique")
        _upsert_asset(
            model,
            account,
            region,
            unique_id,
            record["name"],
            asset_type,
            record["metadata"],
        )
        current_ids.append(unique_id)

    model.objects.filter(owner=account, region=region).exclude(
        unique_id__in=current_ids
    ).update(monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS)
    return len(current_ids)


def _ecr_repository_records(client, region):
    repositories = _bounded_collection(
        client,
        "describe_repositories",
        "repositories",
        "ECR repositories",
    )
    records = []
    repository_names = []
    for repository in repositories:
        repository = _require_mapping(repository, "ECR repository")
        repository_name = repository.get("repositoryName")
        if not repository_name and repository.get("repositoryArn"):
            repository_name = str(repository["repositoryArn"]).rsplit("/", 1)[-1]
        repository_name = _require_identifier(repository_name, "ECR repository")
        repository_names.append(repository_name)
        safe = _copy_fields(
            repository,
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
        records.append({
            "unique_id": _stable_id(
                "aws_ecr_repository",
                region,
                safe.get("repositoryArn") or repository_name,
            ),
            "name": repository_name,
            "metadata": _with_context(
                safe,
                region,
                _cloudmoo_repository_name=repository_name,
                _cloudmoo_resource_type="ecr_repository",
            ),
        })
    return records, repository_names


def _ecr_image_records(client, region, repository_names):
    records = []
    for repository_name in repository_names:
        image_ids = _bounded_collection(
            client,
            "list_images",
            "imageIds",
            "ECR image IDs",
            limit=MAX_ECR_IMAGES_PER_REPOSITORY,
            repositoryName=repository_name,
            filter={"tagStatus": "ANY"},
        )
        for image_id in image_ids:
            image_id = _require_mapping(image_id, "ECR image ID")
            if not image_id.get("imageDigest") and not image_id.get("imageTag"):
                raise _InventoryIncomplete("AWS returned an ECR image without a digest or tag")

        for offset in range(0, len(image_ids), 100):
            details = _bounded_collection(
                client,
                "describe_images",
                "imageDetails",
                "ECR image details",
                limit=MAX_ECR_IMAGES_PER_REPOSITORY,
                repositoryName=repository_name,
                imageIds=image_ids[offset:offset + 100],
            )
            for image in details:
                image = _require_mapping(image, "ECR image")
                digest = image.get("imageDigest")
                tags = image.get("imageTags", [])
                if digest in (None, "") and not tags:
                    raise _InventoryIncomplete("AWS returned an ECR image without identity metadata")
                if "imageTags" in image and not isinstance(tags, list):
                    raise _InventoryIncomplete("AWS returned invalid ECR image tags")
                safe = _copy_fields(
                    image,
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
                identity = digest or ",".join(sorted(str(tag) for tag in tags))
                records.append({
                    "unique_id": _stable_id(
                        "aws_ecr_image",
                        region,
                        repository_name,
                        identity,
                    ),
                    "name": f"{repository_name}@{identity}",
                    "metadata": _with_context(
                        safe,
                        region,
                        _cloudmoo_repository_name=repository_name,
                        _cloudmoo_image_digest=digest,
                        _cloudmoo_image_tags=tags,
                        _cloudmoo_resource_type="ecr_image",
                    ),
                })
    return records


def _task_definition_records(client, region):
    arns = _bounded_collection(
        client,
        "list_task_definitions",
        "taskDefinitionArns",
        "ECS task definitions",
        limit=MAX_TASK_DEFINITIONS_PER_REGION,
        status="ACTIVE",
    )
    records = []
    for arn in arns:
        arn = _require_identifier(arn, "ECS task definition")
        response = client.describe_task_definition(taskDefinition=arn)
        definition = _require_mapping(
            response.get("taskDefinition") if isinstance(response, dict) else None,
            "ECS task definition",
        )
        definition_arn = _require_identifier(
            definition.get("taskDefinitionArn") or arn,
            "ECS task definition",
        )
        safe = _copy_fields(
            definition,
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

        container_definitions = definition.get("containerDefinitions")
        if container_definitions is not None:
            if not isinstance(container_definitions, list):
                raise _InventoryIncomplete("AWS returned invalid ECS container definitions")
            safe_containers = []
            for container in container_definitions:
                container = _require_mapping(container, "ECS container definition")
                safe_container = _copy_fields(
                    container,
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
                if "portMappings" in safe_container:
                    if not isinstance(safe_container["portMappings"], list):
                        raise _InventoryIncomplete("AWS returned invalid ECS port mappings")
                    safe_container["portMappings"] = [
                        _copy_fields(
                            _require_mapping(port, "ECS port mapping"),
                            ("containerPort", "hostPort", "protocol", "appProtocol"),
                            "ECS port mapping",
                        )
                        for port in safe_container["portMappings"]
                    ]
                if "healthCheck" in safe_container:
                    health_check = _require_mapping(safe_container["healthCheck"], "ECS health check")
                    safe_container["healthCheck"] = _copy_fields(
                        health_check,
                        ("interval", "timeout", "retries", "startPeriod"),
                        "ECS health check",
                    )
                safe_containers.append(safe_container)
            safe["containerDefinitions"] = safe_containers

        records.append({
            "unique_id": _stable_id("aws_ecs_task_definition", region, definition_arn),
            "name": definition.get("family") or definition_arn.rsplit("/", 1)[-1],
            "metadata": _with_context(
                safe,
                region,
                _cloudmoo_task_definition_arn=definition_arn,
                _cloudmoo_resource_type="ecs_task_definition",
            ),
        })
    return records


def _ecs_deployment_records(client, region):
    cluster_arns = _bounded_collection(
        client,
        "list_clusters",
        "clusterArns",
        "ECS clusters for deployments",
        limit=MAX_ECS_CLUSTERS_PER_REGION,
    )
    records = []
    for cluster_arn in cluster_arns:
        cluster_arn = _require_identifier(cluster_arn, "ECS cluster")
        service_arns = _bounded_collection(
            client,
            "list_services",
            "serviceArns",
            "ECS services for deployments",
            limit=MAX_ECS_SERVICES_PER_CLUSTER,
            cluster=cluster_arn,
        )
        for offset in range(0, len(service_arns), 10):
            batch = [
                _require_identifier(service_arn, "ECS service")
                for service_arn in service_arns[offset:offset + 10]
            ]
            if not batch:
                continue
            response = client.describe_services(cluster=cluster_arn, services=batch)
            if not isinstance(response, dict):
                raise _InventoryIncomplete("AWS returned an invalid ECS service response")
            failures = response.get("failures", [])
            if not isinstance(failures, list):
                raise _InventoryIncomplete("AWS returned invalid ECS service failures")
            if failures:
                raise _InventoryIncomplete("AWS could not describe every ECS service")
            services = _describe_collection(response, "services", "ECS services")
            for service in services:
                service = _require_mapping(service, "ECS service")
                service_arn = _require_identifier(service.get("serviceArn"), "ECS service")
                deployments = service.get("deployments")
                if not isinstance(deployments, list):
                    raise _InventoryIncomplete("AWS returned invalid ECS deployments")
                service_name = service.get("serviceName") or service_arn.rsplit("/", 1)[-1]
                for deployment in deployments:
                    deployment = _require_mapping(deployment, "ECS deployment")
                    deployment_id = _require_identifier(
                        deployment.get("id") or deployment.get("deploymentId"),
                        "ECS deployment",
                    )
                    safe = _copy_fields(
                        deployment,
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
                    records.append({
                        "unique_id": _stable_id(
                            "aws_ecs_deployment",
                            region,
                            cluster_arn,
                            service_arn,
                            deployment_id,
                        ),
                        "name": f"{service_name}/{deployment_id}",
                        "metadata": _with_context(
                            safe,
                            region,
                            _cloudmoo_cluster_arn=cluster_arn,
                            _cloudmoo_service_arn=service_arn,
                            _cloudmoo_service_name=service_name,
                            _cloudmoo_deployment_id=deployment_id,
                            _cloudmoo_resource_type="ecs_deployment",
                        ),
                    })
    return records


def _eks_cluster_records(client, region):
    names = _bounded_collection(
        client,
        "list_clusters",
        "clusters",
        "EKS clusters",
        limit=MAX_EKS_CLUSTERS_PER_REGION,
    )
    records = []
    contexts = []
    for name in names:
        name = _require_identifier(name, "EKS cluster")
        response = client.describe_cluster(name=name)
        cluster = _require_mapping(
            response.get("cluster") if isinstance(response, dict) else None,
            "EKS cluster",
        )
        cluster_name = _require_identifier(cluster.get("name") or name, "EKS cluster")
        safe = _copy_fields(
            cluster,
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
        records.append({
            "unique_id": _stable_id(
                "aws_eks_cluster",
                region,
                safe.get("arn") or cluster_name,
            ),
            "name": cluster_name,
            "metadata": _with_context(
                safe,
                region,
                _cloudmoo_cluster_name=cluster_name,
                _cloudmoo_cluster_arn=safe.get("arn"),
                _cloudmoo_resource_type="eks_cluster",
            ),
        })
        contexts.append({
            "name": cluster_name,
            "arn": safe.get("arn") or cluster_name,
        })
    return records, contexts


def _eks_child_records(client, region, contexts, child_type):
    specs = {
        "aws_eks_node_group": {
            "list_operation": "list_nodegroups",
            "list_key": "nodegroups",
            "describe_operation": "describe_nodegroup",
            "describe_key": "nodegroup",
            "name_key": "nodegroupName",
            "describe_arg": "nodegroupName",
            "fields": (
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
            "context_key": "_cloudmoo_nodegroup_name",
            "resource_type": "eks_node_group",
        },
        "aws_eks_addon": {
            "list_operation": "list_addons",
            "list_key": "addons",
            "describe_operation": "describe_addon",
            "describe_key": "addon",
            "name_key": "addonName",
            "describe_arg": "addonName",
            "fields": (
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
            "context_key": "_cloudmoo_addon_name",
            "resource_type": "eks_addon",
        },
        "aws_eks_fargate_profile": {
            "list_operation": "list_fargate_profiles",
            "list_key": "fargateProfileNames",
            "describe_operation": "describe_fargate_profile",
            "describe_key": "fargateProfile",
            "name_key": "fargateProfileName",
            "describe_arg": "fargateProfileName",
            "fields": (
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
            "context_key": "_cloudmoo_fargate_profile_name",
            "resource_type": "eks_fargate_profile",
        },
    }
    spec = specs[child_type]
    records = []
    for context in contexts:
        cluster_name = context["name"]
        names = _bounded_collection(
            client,
            spec["list_operation"],
            spec["list_key"],
            f"{child_type} list",
            limit=MAX_EKS_CHILDREN_PER_CLUSTER,
            clusterName=cluster_name,
        )
        for child_name in names:
            child_name = _require_identifier(child_name, child_type)
            response = getattr(client, spec["describe_operation"])(
                clusterName=cluster_name,
                **{spec["describe_arg"]: child_name},
            )
            child = _require_mapping(
                response.get(spec["describe_key"]) if isinstance(response, dict) else None,
                child_type,
            )
            actual_name = _require_identifier(
                child.get(spec["name_key"]) or child_name,
                child_type,
            )
            safe = _copy_fields(child, spec["fields"], child_type)
            provider_id = (
                safe.get("nodegroupArn")
                or safe.get("addonArn")
                or safe.get("fargateProfileArn")
                or actual_name
            )
            records.append({
                "unique_id": _stable_id(child_type, region, cluster_name, provider_id),
                "name": f"{cluster_name}/{actual_name}",
                "metadata": _with_context(
                    safe,
                    region,
                    _cloudmoo_cluster_name=cluster_name,
                    **{
                        spec["context_key"]: actual_name,
                        "_cloudmoo_resource_type": spec["resource_type"],
                    },
                ),
            })
    return records


def _apprunner_service_records(client, region):
    summaries = _bounded_collection(
        client,
        "list_services",
        "ServiceSummaryList",
        "App Runner services",
        limit=MAX_APPRUNNER_SERVICES_PER_REGION,
    )
    records = []
    contexts = []
    for summary in summaries:
        summary = _require_mapping(summary, "App Runner service summary")
        service_arn = _require_identifier(summary.get("ServiceArn"), "App Runner service")
        response = client.describe_service(ServiceArn=service_arn)
        service = _require_mapping(
            response.get("Service") if isinstance(response, dict) else None,
            "App Runner service",
        )
        actual_arn = _require_identifier(service.get("ServiceArn") or service_arn, "App Runner service")
        safe = _safe_apprunner_service(service)
        service_name = service.get("ServiceName") or summary.get("ServiceName") or actual_arn.rsplit("/", 1)[-1]
        records.append({
            "unique_id": _stable_id("aws_apprunner_service", region, actual_arn),
            "name": service_name,
            "metadata": _with_context(
                safe,
                region,
                _cloudmoo_service_arn=actual_arn,
                _cloudmoo_service_name=service_name,
                _cloudmoo_resource_type="apprunner_service",
            ),
        })
        contexts.append({"arn": actual_arn, "name": service_name})
    return records, contexts


def _safe_apprunner_service(service):
    safe = _copy_fields(
        service,
        (
            "ServiceArn",
            "ServiceId",
            "ServiceName",
            "Status",
            "ServiceUrl",
            "CreatedAt",
            "UpdatedAt",
            "DeletedAt",
            "AutoScalingConfigurationSummary",
            "HealthCheckConfiguration",
            "InstanceConfiguration",
            "NetworkConfiguration",
            "ObservabilityConfiguration",
            "Tags",
        ),
        "App Runner service",
    )
    source = service.get("SourceConfiguration")
    if source is not None:
        source = _require_mapping(source, "App Runner source configuration")
        safe_source = _copy_fields(
            source,
            ("AutoDeploymentsEnabled", "AuthenticationConfiguration"),
            "App Runner source configuration",
        )
        image_repository = source.get("ImageRepository")
        if image_repository is not None:
            image_repository = _require_mapping(image_repository, "App Runner image repository")
            safe_source["ImageRepository"] = _copy_fields(
                image_repository,
                ("ImageIdentifier", "ImageRepositoryType"),
                "App Runner image repository",
            )
            image_configuration = image_repository.get("ImageConfiguration")
            if image_configuration is not None:
                image_configuration = _require_mapping(image_configuration, "App Runner image configuration")
                safe_source["ImageRepository"]["ImageConfiguration"] = _copy_fields(
                    image_configuration,
                    ("Port", "Runtime"),
                    "App Runner image configuration",
                )
        code_repository = source.get("CodeRepository")
        if code_repository is not None:
            code_repository = _require_mapping(code_repository, "App Runner code repository")
            safe_source["CodeRepository"] = _copy_fields(
                code_repository,
                ("RepositoryUrl", "SourceDirectory"),
                "App Runner code repository",
            )
            code_configuration = code_repository.get("CodeConfiguration")
            if code_configuration is not None:
                code_configuration = _require_mapping(code_configuration, "App Runner code configuration")
                safe_source["CodeRepository"]["CodeConfiguration"] = _copy_fields(
                    code_configuration,
                    ("ConfigurationSource", "Runtime", "Port"),
                    "App Runner code configuration",
                )
        safe["SourceConfiguration"] = safe_source
    return safe


def _apprunner_deployment_records(client, region, contexts):
    records = []
    for context in contexts:
        summaries = _bounded_collection(
            client,
            "list_operations",
            "OperationSummaryList",
            "App Runner operations",
            limit=MAX_COLLECTION_ITEMS,
            ServiceArn=context["arn"],
        )
        # The service API returns newest operations first.  Keep only a small
        # recent window, as operation history is not an unbounded asset source.
        summaries = summaries[:MAX_APPRUNNER_OPERATIONS_PER_SERVICE]
        for summary in summaries:
            summary = _require_mapping(summary, "App Runner operation summary")
            operation_id = _require_identifier(
                summary.get("Id") or summary.get("OperationId"),
                "App Runner operation",
            )
            response = client.describe_operation(
                ServiceArn=context["arn"],
                OperationId=operation_id,
            )
            operation = _require_mapping(
                response.get("Operation") if isinstance(response, dict) else None,
                "App Runner operation",
            )
            actual_id = _require_identifier(
                operation.get("Id") or operation.get("OperationId") or operation_id,
                "App Runner operation",
            )
            safe = _copy_fields(
                operation,
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
            records.append({
                "unique_id": _stable_id(
                    "aws_apprunner_deployment",
                    region,
                    context["arn"],
                    actual_id,
                ),
                "name": f"{context['name']}/{actual_id}",
                "metadata": _with_context(
                    safe,
                    region,
                    _cloudmoo_service_arn=context["arn"],
                    _cloudmoo_operation_id=actual_id,
                    _cloudmoo_resource_type="apprunner_deployment",
                ),
            })
    return records


def _client_for(account, region, service, cache):
    key = (service, region)
    if key not in cache:
        cache[key] = aws_client(account, service, region=region)
    return cache[key]


def _record_sync_failure(summary, region, asset_type, error):
    code = aws_error_code(error)
    summary["errors"].append({
        "region": region,
        "asset_type": asset_type,
        "error_code": code,
    })
    logger.warning(
        "AWS container inventory skipped %s in %s (%s)",
        asset_type,
        region,
        code,
    )


def _run_family(summary, account, region, asset_type, model, records_factory):
    try:
        records = records_factory()
        summary["synced"][asset_type] += _reconcile(
            model,
            account,
            region,
            asset_type,
            records,
        )
    except (ClientError, BotoCoreError, CloudInventoryTransientError, ValueError, TypeError, KeyError) as error:
        _record_sync_failure(summary, region, asset_type, error)
    except Exception as error:
        # Database/provider adapter bugs must also fail closed for this family;
        # never turn an unexpected response into a mass deletion.
        _record_sync_failure(summary, region, asset_type, error)


def sync_aws_container_assets(account):
    """Synchronize the complete read-only AWS container/platform inventory.

    A family is reconciled only after every page, required child list, and
    required describe in that family/region succeeds.  Other regions and
    families continue independently so a regional permission or outage cannot
    erase healthy inventory elsewhere.
    """
    regions = get_enabled_regions(account)
    if not isinstance(regions, list) or any(not isinstance(region, str) or not region for region in regions):
        raise CloudInventoryTransientError("AWS returned an invalid enabled region list")

    summary = {
        "regions": list(regions),
        "synced": {asset_type: 0 for asset_type in AWS_CONTAINER_ASSET_TYPES},
        "errors": [],
    }

    for region in regions:
        clients = {}
        try:
            ecr = _client_for(account, region, "ecr", clients)
            repository_records, repository_names = _ecr_repository_records(ecr, region)
            _run_family(
                summary,
                account,
                region,
                "aws_ecr_repository",
                CoreAWSECRRepository,
                lambda records=repository_records: records,
            )
            _run_family(
                summary,
                account,
                region,
                "aws_ecr_image",
                CoreAWSECRImage,
                lambda names=repository_names: _ecr_image_records(ecr, region, names),
            )
        except (ClientError, BotoCoreError, CloudInventoryTransientError, ValueError, TypeError, KeyError) as error:
            _record_sync_failure(summary, region, "aws_ecr_repository", error)
            _record_sync_failure(summary, region, "aws_ecr_image", error)
        except Exception as error:
            _record_sync_failure(summary, region, "aws_ecr_repository", error)
            _record_sync_failure(summary, region, "aws_ecr_image", error)

        _run_family(
            summary,
            account,
            region,
            "aws_ecs_task_definition",
            CoreAWSECSTaskDefinition,
            lambda: _task_definition_records(_client_for(account, region, "ecs", clients), region),
        )
        _run_family(
            summary,
            account,
            region,
            "aws_ecs_deployment",
            CoreAWSECSDeployment,
            lambda: _ecs_deployment_records(_client_for(account, region, "ecs", clients), region),
        )

        eks_client = None
        try:
            eks_client = _client_for(account, region, "eks", clients)
            cluster_records, cluster_contexts = _eks_cluster_records(eks_client, region)
            _run_family(
                summary,
                account,
                region,
                "aws_eks_cluster",
                CoreAWSEKSCluster,
                lambda records=cluster_records: records,
            )
            for child_type in (
                "aws_eks_node_group",
                "aws_eks_addon",
                "aws_eks_fargate_profile",
            ):
                _run_family(
                    summary,
                    account,
                    region,
                    child_type,
                    AWS_CONTAINER_MODELS[child_type],
                    lambda child_type=child_type: _eks_child_records(
                        eks_client,
                        region,
                        cluster_contexts,
                        child_type,
                    ),
                )
        except (ClientError, BotoCoreError, CloudInventoryTransientError, ValueError, TypeError, KeyError) as error:
            _record_sync_failure(summary, region, "aws_eks_cluster", error)
            for child_type in (
                "aws_eks_node_group",
                "aws_eks_addon",
                "aws_eks_fargate_profile",
            ):
                _record_sync_failure(summary, region, child_type, error)
        except Exception as error:
            _record_sync_failure(summary, region, "aws_eks_cluster", error)
            for child_type in (
                "aws_eks_node_group",
                "aws_eks_addon",
                "aws_eks_fargate_profile",
            ):
                _record_sync_failure(summary, region, child_type, error)

        apprunner_client = None
        try:
            apprunner_client = _client_for(account, region, "apprunner", clients)
            service_records, service_contexts = _apprunner_service_records(apprunner_client, region)
            _run_family(
                summary,
                account,
                region,
                "aws_apprunner_service",
                CoreAWSAppRunnerService,
                lambda records=service_records: records,
            )
            _run_family(
                summary,
                account,
                region,
                "aws_apprunner_deployment",
                CoreAWSAppRunnerDeployment,
                lambda: _apprunner_deployment_records(apprunner_client, region, service_contexts),
            )
        except (ClientError, BotoCoreError, CloudInventoryTransientError, ValueError, TypeError, KeyError) as error:
            _record_sync_failure(summary, region, "aws_apprunner_service", error)
            _record_sync_failure(summary, region, "aws_apprunner_deployment", error)
        except Exception as error:
            _record_sync_failure(summary, region, "aws_apprunner_service", error)
            _record_sync_failure(summary, region, "aws_apprunner_deployment", error)

    return summary


# This helper is intentionally small and public for the status-check lane.
def resource_context(credentials):
    if not isinstance(credentials, dict):
        return {}
    metadata = credentials.get("metadata")
    if isinstance(metadata, dict):
        return metadata
    return {
        key: value
        for key, value in credentials.items()
        if isinstance(key, str) and key.startswith("_cloudmoo_")
    }


def aws_check_account(credentials, region):
    """Build the attribute-shaped account context expected by discovery.aws_client."""
    if not isinstance(credentials, dict):
        raise ValueError("AWS credentials are not configured")
    access_key = credentials.get("access_key")
    secret_key = credentials.get("secret_key")
    if not access_key or not secret_key:
        raise ValueError("AWS credentials are incomplete")
    return SimpleNamespace(
        access_key=access_key,
        secret_key=secret_key,
        region=region,
    )


__all__ = [
    "AWS_ECR_REPOSITORY",
    "AWS_ECR_IMAGE",
    "AWS_ECS_TASK_DEFINITION",
    "AWS_ECS_DEPLOYMENT",
    "AWS_EKS_CLUSTER",
    "AWS_EKS_NODE_GROUP",
    "AWS_EKS_ADDON",
    "AWS_EKS_FARGATE_PROFILE",
    "AWS_APPRUNNER_SERVICE",
    "AWS_APPRUNNER_DEPLOYMENT",
    "AWS_CONTAINER_ASSET_TYPES",
    "AWS_CONTAINER_MODELS",
    "AWS_CONTAINER_ASSET_MODELS",
    "CoreAWSContainerAsset",
    "CoreAWSECRRepository",
    "CoreAWSECRImage",
    "CoreAWSECSTaskDefinition",
    "CoreAWSECSDeployment",
    "CoreAWSEKSCluster",
    "CoreAWSEKSNodeGroup",
    "CoreAWSEKSAddon",
    "CoreAWSEKSFargateProfile",
    "CoreAWSAppRunnerService",
    "CoreAWSAppRunnerDeployment",
    "aws_check_account",
    "resource_context",
    "sync_aws_container_assets",
]
