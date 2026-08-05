"""Read-only inventory for AWS delivery and hosting services.

This adapter owns the delivery-oriented AWS families that are not covered by
the container adapter: Elastic Beanstalk applications and environments,
CodeBuild projects and recent builds, and CodePipeline pipelines and recent
executions.  CodeBuild builds and CodePipeline executions are the AWS-native
deployment events represented here.  CodeDeploy is intentionally not added as
another family: it is an independent optional service, while the requested
delivery event coverage is already represented without increasing the scope of
the integration lane.

App Runner services and deployments remain owned by ``containers.py``.  The
``APP_RUNNER_INTEGRATION_NOTE`` export is provided for callers wiring the
provider registry; this module does not define duplicate App Runner models or
make App Runner calls.

Every provider payload is reduced to an allow-listed shape, serialized through
the shared AWS boundary, and bounded before persistence.  Full resource
families are reconciled only after the complete family/region response is
validated.  Historical build and execution windows are deliberately
upsert-only because absence from a recent window is not evidence that an old
event was deleted.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
import re
from urllib.parse import quote

from botocore.exceptions import BotoCoreError, ClientError
from django.db import models

from apps.console.cloud.aws.discovery import (
    aws_client,
    aws_error_code,
    get_enabled_regions,
    is_transient_aws_error,
    iter_pages,
    require_collection,
    serialize_aws,
)
from apps.console.cloud.aws.models import CoreAWSAccount
from apps.console.cloud.models import CloudInventoryTransientError
from apps.console.utils.models import UtilAsset
from apps.monitoring.metadata import redact_sensitive_metadata


logger = logging.getLogger(__name__)


AWS_ELASTIC_BEANSTALK_APPLICATION = "aws_elastic_beanstalk_application"
AWS_ELASTIC_BEANSTALK_ENVIRONMENT = "aws_elastic_beanstalk_environment"
AWS_CODEBUILD_PROJECT = "aws_codebuild_project"
AWS_CODEBUILD_BUILD = "aws_codebuild_build"
AWS_CODEPIPELINE_PIPELINE = "aws_codepipeline_pipeline"
AWS_CODEPIPELINE_EXECUTION = "aws_codepipeline_execution"

AWS_DELIVERY_ASSET_TYPES = (
    AWS_ELASTIC_BEANSTALK_APPLICATION,
    AWS_ELASTIC_BEANSTALK_ENVIRONMENT,
    AWS_CODEBUILD_PROJECT,
    AWS_CODEBUILD_BUILD,
    AWS_CODEPIPELINE_PIPELINE,
    AWS_CODEPIPELINE_EXECUTION,
)

# The bounds protect both provider calls and the local JSON boundary.  The
# recent event limits are intentionally much smaller than the full collection
# limit because event history is useful as a monitoring summary, not as an
# unbounded archive.
MAX_COLLECTION_ITEMS = 5_000
MAX_ELASTIC_BEANSTALK_APPLICATIONS = 2_000
MAX_ELASTIC_BEANSTALK_ENVIRONMENTS = 5_000
MAX_CODEBUILD_PROJECTS = 2_000
MAX_CODEBUILD_BUILDS_PER_PROJECT = 25
MAX_CODEPIPELINE_PIPELINES = 1_000
MAX_CODEPIPELINE_EXECUTIONS_PER_PIPELINE = 25
MAX_BATCH_ITEMS = 100
MAX_COLLECTION_PAGES = 100

MAX_METADATA_DEPTH = 5
MAX_METADATA_ITEMS = 80
MAX_METADATA_LIST_ITEMS = 100
MAX_METADATA_STRING = 2_048
MAX_REGION_LENGTH = 64

APP_RUNNER_INTEGRATION_NOTE = (
    "App Runner services and deployments are owned by "
    "apps.console.cloud.aws.containers; do not register duplicate delivery "
    "models from this module."
)

_REGION_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)+$")


class _InventoryIncomplete(CloudInventoryTransientError):
    """The provider response exceeded a safe boundary or lacked required data."""


def _owner_identifier_constraint(name):
    return models.UniqueConstraint(
        fields=("owner", "unique_id"),
        name=f"aws_delivery_{name}_owner_uid_uniq",
    )


class CoreAWSDeliveryAsset(UtilAsset):
    """Common regional identity and monitoring behavior for delivery assets."""

    # UtilAsset historically limits this field to 100 characters.  AWS ARNs
    # and composite event identities can exceed that, so long values are
    # replaced with a stable digest while the raw provider ID remains in safe
    # metadata.
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
        """Return check context without persisting credentials in metadata."""
        metadata = self.metadata if isinstance(self.metadata, dict) else {}
        provider_id = metadata.get("_cloudmoo_raw_id") or self.unique_id
        return {
            "access_key": self.owner.access_key,
            "secret_key": self.owner.secret_key,
            "region": self.region,
            "resource_region": self.region,
            "provider_id": provider_id,
            "resource_name": metadata.get("_cloudmoo_provider_name") or self.name,
            "asset_type": self.type or self.asset_type,
            "provider_type": self.provider_type or self.asset_type,
            "metadata": metadata,
        }

    @property
    def provider_url(self):
        metadata = self.metadata if isinstance(self.metadata, dict) else {}
        region = quote(str(self.region), safe="-._~")
        raw_id = str(metadata.get("_cloudmoo_raw_id") or self.unique_id)
        raw = quote(raw_id, safe="-._~")
        asset_type = self.type or self.asset_type

        if asset_type == AWS_ELASTIC_BEANSTALK_APPLICATION:
            return (
                f"https://{region}.console.aws.amazon.com/elasticbeanstalk/home"
                f"?region={region}#/applications/{raw}"
            )
        if asset_type == AWS_ELASTIC_BEANSTALK_ENVIRONMENT:
            return (
                f"https://{region}.console.aws.amazon.com/elasticbeanstalk/home"
                f"?region={region}#/environment/dashboard?environmentId={raw}"
            )
        if asset_type in {AWS_CODEBUILD_PROJECT, AWS_CODEBUILD_BUILD}:
            name = quote(
                str(metadata.get("_cloudmoo_project_name") or raw_id),
                safe="-._~",
            )
            return (
                f"https://{region}.console.aws.amazon.com/codesuite/codebuild"
                f"/{name}/history?region={region}"
            )
        if asset_type in {AWS_CODEPIPELINE_PIPELINE, AWS_CODEPIPELINE_EXECUTION}:
            name = quote(
                str(metadata.get("_cloudmoo_pipeline_name") or raw_id),
                safe="-._~",
            )
            return (
                f"https://{region}.console.aws.amazon.com/codesuite/codepipeline"
                f"/pipelines/{name}/view?region={region}"
            )
        return f"https://{region}.console.aws.amazon.com/"

    def check_status(self):
        from apps.monitoring.checks.aws_delivery import check_aws_delivery_asset_status

        return check_aws_delivery_asset_status(
            self.type or self.asset_type,
            self.unique_id,
            self.monitoring_credentials,
        )


class CoreAWSElasticBeanstalkApplication(CoreAWSDeliveryAsset):
    asset_type = AWS_ELASTIC_BEANSTALK_APPLICATION
    provider_type = AWS_ELASTIC_BEANSTALK_APPLICATION

    class Meta:
        db_table = "core_aws_elastic_beanstalk_application"
        constraints = [_owner_identifier_constraint("elastic_beanstalk_application")]


class CoreAWSElasticBeanstalkEnvironment(CoreAWSDeliveryAsset):
    asset_type = AWS_ELASTIC_BEANSTALK_ENVIRONMENT
    provider_type = AWS_ELASTIC_BEANSTALK_ENVIRONMENT

    class Meta:
        db_table = "core_aws_elastic_beanstalk_environment"
        constraints = [_owner_identifier_constraint("elastic_beanstalk_environment")]


class CoreAWSCodeBuildProject(CoreAWSDeliveryAsset):
    asset_type = AWS_CODEBUILD_PROJECT
    provider_type = AWS_CODEBUILD_PROJECT

    class Meta:
        db_table = "core_aws_codebuild_project"
        constraints = [_owner_identifier_constraint("codebuild_project")]


class CoreAWSCodeBuildBuild(CoreAWSDeliveryAsset):
    asset_type = AWS_CODEBUILD_BUILD
    provider_type = AWS_CODEBUILD_BUILD

    class Meta:
        db_table = "core_aws_codebuild_build"
        constraints = [_owner_identifier_constraint("codebuild_build")]


class CoreAWSCodePipelinePipeline(CoreAWSDeliveryAsset):
    asset_type = AWS_CODEPIPELINE_PIPELINE
    provider_type = AWS_CODEPIPELINE_PIPELINE

    class Meta:
        db_table = "core_aws_codepipeline_pipeline"
        constraints = [_owner_identifier_constraint("codepipeline_pipeline")]


class CoreAWSCodePipelineExecution(CoreAWSDeliveryAsset):
    asset_type = AWS_CODEPIPELINE_EXECUTION
    provider_type = AWS_CODEPIPELINE_EXECUTION

    class Meta:
        db_table = "core_aws_codepipeline_execution"
        constraints = [_owner_identifier_constraint("codepipeline_execution")]


AWS_DELIVERY_MODELS = {
    AWS_ELASTIC_BEANSTALK_APPLICATION: CoreAWSElasticBeanstalkApplication,
    AWS_ELASTIC_BEANSTALK_ENVIRONMENT: CoreAWSElasticBeanstalkEnvironment,
    AWS_CODEBUILD_PROJECT: CoreAWSCodeBuildProject,
    AWS_CODEBUILD_BUILD: CoreAWSCodeBuildBuild,
    AWS_CODEPIPELINE_PIPELINE: CoreAWSCodePipelinePipeline,
    AWS_CODEPIPELINE_EXECUTION: CoreAWSCodePipelineExecution,
}
AWS_DELIVERY_ASSET_MODELS = AWS_DELIVERY_MODELS


def _require_mapping(value, context):
    if not isinstance(value, Mapping):
        raise _InventoryIncomplete(f"AWS returned an invalid {context} resource")
    return value


def _require_identifier(value, context):
    if not isinstance(value, str) or not value.strip():
        raise _InventoryIncomplete(f"AWS returned an invalid {context} identifier")
    return value.strip()


def _bound_value(value, depth=0):
    """Bound an already serialized, allow-listed value."""
    if depth > MAX_METADATA_DEPTH:
        return "<truncated>"
    if isinstance(value, str):
        return value[:MAX_METADATA_STRING]
    if isinstance(value, Mapping):
        result = {}
        for index, (key, child) in enumerate(value.items()):
            if index >= MAX_METADATA_ITEMS:
                result["_cloudmoo_truncated_items"] = True
                break
            result[str(key)[:128]] = _bound_value(child, depth + 1)
        return result
    if isinstance(value, list):
        result = [_bound_value(item, depth + 1) for item in value[:MAX_METADATA_LIST_ITEMS]]
        if len(value) > MAX_METADATA_LIST_ITEMS:
            result.append("<truncated>")
        return result
    return value


def _bounded_metadata(value):
    return _bound_value(redact_sensitive_metadata(serialize_aws(value)))


def _copy_fields(item, fields, context, required=()):
    item = _require_mapping(item, context)
    for field in required:
        if field not in item or item[field] in (None, ""):
            raise _InventoryIncomplete(f"AWS returned an incomplete {context} object")
    return {field: item[field] for field in fields if field in item}


def _safe_nested_mapping(item, key, fields, context):
    value = item.get(key)
    if value is None:
        return None
    return _bounded_metadata(_copy_fields(value, fields, context))


def _safe_summary_list(value, fields, context, required=()):
    if not isinstance(value, list):
        raise _InventoryIncomplete(f"AWS returned an invalid {context} collection")
    if len(value) > MAX_METADATA_LIST_ITEMS:
        raise _InventoryIncomplete(f"AWS {context} exceeded its safe metadata limit")
    return [
        _bounded_metadata(_copy_fields(item, fields, context, required=required))
        for item in value
    ]


def _stable_id(region, raw_id):
    """Return a stable, region-scoped local ID and retain the raw provider ID."""
    value = f"{str(region).strip()}|{str(raw_id).strip()}"
    if len(value) <= 255:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"{str(region).strip()}|sha256:{digest}"[:255]


def _display_name(value, fallback="AWS delivery resource"):
    return str(value or fallback)[:100]


def _resource_metadata(
    safe,
    *,
    region,
    asset_type,
    raw_id,
    provider_id=None,
    provider_name=None,
    provider_status=None,
    **extra,
):
    metadata = dict(safe) if isinstance(safe, Mapping) else {}
    metadata.update(
        {
            "_cloudmoo_region": region,
            "_cloudmoo_raw_id": raw_id,
            "_cloudmoo_provider_id": provider_id or raw_id,
            "_cloudmoo_resource_type": asset_type,
        }
    )
    if provider_name:
        metadata["_cloudmoo_provider_name"] = provider_name
    if provider_status not in (None, ""):
        metadata["_cloudmoo_provider_status"] = str(provider_status)[:128]
    metadata.update(extra)
    return _bounded_metadata(metadata)


def _record(
    asset_type,
    region,
    raw_id,
    name,
    safe,
    *,
    provider_id=None,
    provider_name=None,
    provider_status=None,
    identity=None,
    **extra,
):
    return {
        "unique_id": _stable_id(region, identity or raw_id),
        "name": _display_name(name, raw_id),
        "metadata": _resource_metadata(
            safe,
            region=region,
            asset_type=asset_type,
            raw_id=raw_id,
            provider_id=provider_id,
            provider_name=provider_name,
            provider_status=provider_status,
            **extra,
        ),
    }


def _bounded_collection(client, operation, response_key, context, limit=MAX_COLLECTION_ITEMS, **kwargs):
    values = []
    for page_number, page in enumerate(iter_pages(client, operation, **kwargs), start=1):
        if page_number > MAX_COLLECTION_PAGES:
            raise _InventoryIncomplete(f"AWS {context} pagination exceeded its safe page limit")
        page_values = require_collection(page, response_key, context)
        values.extend(page_values)
        if len(values) > limit:
            raise _InventoryIncomplete(f"AWS {context} exceeded its safe inventory limit")
    return values


def _bounded_recent_collection(client, operation, response_key, context, limit, **kwargs):
    """Read only the newest bounded window from an ordered history API."""
    values = []
    for page_number, page in enumerate(iter_pages(client, operation, **kwargs), start=1):
        if page_number > MAX_COLLECTION_PAGES:
            raise _InventoryIncomplete(f"AWS {context} pagination exceeded its safe page limit")
        page_values = require_collection(page, response_key, context)
        values.extend(page_values)
        if len(values) >= limit:
            return values[:limit]
    return values


def _batch_get_collection(
    client,
    operation,
    request_key,
    response_key,
    missing_key,
    identifiers,
    context,
):
    """Batch-read all requested objects and fail closed for partial results."""
    result = []
    for offset in range(0, len(identifiers), MAX_BATCH_ITEMS):
        chunk = identifiers[offset : offset + MAX_BATCH_ITEMS]
        response = getattr(client, operation)(**{request_key: chunk})
        if not isinstance(response, Mapping):
            raise _InventoryIncomplete(f"AWS returned an invalid {context} response")
        values = require_collection(response, response_key, context)
        missing = response.get(missing_key, [])
        if not isinstance(missing, list):
            raise _InventoryIncomplete(f"AWS returned an invalid {context} missing collection")
        if missing:
            raise _InventoryIncomplete(f"AWS returned an incomplete {context} response")

        seen = set()
        for value in values:
            value = _require_mapping(value, context)
            identifier = value.get("name") or value.get("id")
            if identifier is not None:
                seen.add(str(identifier))
        if not set(chunk).issubset(seen):
            raise _InventoryIncomplete(f"AWS returned an incomplete {context} response")
        result.extend(values)
    return result


def _safe_beanstalk_application(application):
    safe = _copy_fields(
        application,
        (
            "ApplicationName",
            "ApplicationArn",
            "Description",
            "DateCreated",
            "DateUpdated",
            "OperationsRole",
        ),
        "Elastic Beanstalk application",
    )
    lifecycle = application.get("ResourceLifecycleConfig")
    if lifecycle is not None:
        lifecycle = _copy_fields(
            lifecycle,
            ("ServiceRole", "VersionLifecycleConfig"),
            "Elastic Beanstalk resource lifecycle configuration",
        )
        if "VersionLifecycleConfig" in lifecycle:
            rules = lifecycle["VersionLifecycleConfig"]
            if not isinstance(rules, list) or len(rules) > MAX_METADATA_LIST_ITEMS:
                raise _InventoryIncomplete(
                    "AWS returned an invalid Elastic Beanstalk version lifecycle collection"
                )
            safe_rules = []
            for rule in rules:
                rule = _copy_fields(
                    rule,
                    ("MaxCountRule", "MaxAgeRule", "DeleteSourceFromS3"),
                    "Elastic Beanstalk version lifecycle rule",
                )
                if isinstance(rule.get("MaxCountRule"), Mapping):
                    rule["MaxCountRule"] = _copy_fields(
                        rule["MaxCountRule"],
                        ("Enabled", "MaxCount"),
                        "Elastic Beanstalk max count rule",
                    )
                if isinstance(rule.get("MaxAgeRule"), Mapping):
                    rule["MaxAgeRule"] = _copy_fields(
                        rule["MaxAgeRule"],
                        ("Enabled", "MaxAgeInDays"),
                        "Elastic Beanstalk max age rule",
                    )
                safe_rules.append(rule)
            lifecycle["VersionLifecycleConfig"] = safe_rules
        safe["ResourceLifecycleConfig"] = lifecycle
    if "Versions" in application:
        safe["Versions"] = _safe_summary_list(
            application["Versions"],
            ("VersionLabel", "VersionArn", "Status", "DateCreated", "DateUpdated"),
            "Elastic Beanstalk application versions",
        )
    if "ConfigurationTemplates" in application:
        safe["ConfigurationTemplates"] = _safe_summary_list(
            application["ConfigurationTemplates"],
            ("TemplateName", "DateCreated", "DateUpdated", "Description"),
            "Elastic Beanstalk configuration templates",
        )
    return _bounded_metadata(safe)


def _safe_beanstalk_environment(environment):
    safe = _copy_fields(
        environment,
        (
            "EnvironmentId",
            "EnvironmentName",
            "EnvironmentArn",
            "ApplicationName",
            "ApplicationArn",
            "Description",
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
            "TemplateName",
            "AbortableOperationInProgress",
            "OperationsRole",
        ),
        "Elastic Beanstalk environment",
    )
    if environment.get("Tier") is not None:
        safe["Tier"] = _copy_fields(
            environment["Tier"],
            ("Name", "Type", "Version"),
            "Elastic Beanstalk environment tier",
        )
    return _bounded_metadata(safe)


def _safe_codebuild_project(project):
    safe = _copy_fields(
        project,
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
        "CodeBuild project",
    )

    source = project.get("source")
    if source is not None:
        safe["source"] = _copy_fields(
            source,
            ("type", "gitCloneDepth", "gitSubmodulesConfig", "insecureSsl", "reportBuildStatus"),
            "CodeBuild source",
        )
        if isinstance(source.get("gitSubmodulesConfig"), Mapping):
            safe["source"]["gitSubmodulesConfig"] = _copy_fields(
                source["gitSubmodulesConfig"],
                ("fetchSubmodules",),
                "CodeBuild source submodules",
            )

    environment = project.get("environment")
    if environment is not None:
        safe["environment"] = _copy_fields(
            environment,
            ("type", "computeType", "image", "privilegedMode", "imagePullCredentialsType"),
            "CodeBuild environment",
        )

    artifacts = project.get("artifacts")
    if artifacts is not None:
        safe["artifacts"] = _copy_fields(
            artifacts,
            ("type", "packaging", "name", "namespaceType", "overrideArtifactName", "encryptionDisabled"),
            "CodeBuild artifacts",
        )

    logs_config = project.get("logsConfig")
    if logs_config is not None:
        logs_config = _require_mapping(logs_config, "CodeBuild logs configuration")
        safe_logs = {}
        if logs_config.get("cloudWatchLogs") is not None:
            safe_logs["cloudWatchLogs"] = _copy_fields(
                logs_config["cloudWatchLogs"],
                ("status", "groupName"),
                "CodeBuild CloudWatch logs configuration",
            )
        if logs_config.get("s3Logs") is not None:
            safe_logs["s3Logs"] = _copy_fields(
                logs_config["s3Logs"],
                ("status", "encryptionDisabled"),
                "CodeBuild S3 logs configuration",
            )
        safe["logsConfig"] = safe_logs

    vpc_config = project.get("vpcConfig")
    if vpc_config is not None:
        safe["vpcConfig"] = _copy_fields(
            vpc_config,
            ("vpcId", "subnets", "securityGroupIds"),
            "CodeBuild VPC configuration",
        )
    return _bounded_metadata(safe)


def _safe_codebuild_build(build):
    safe = _copy_fields(
        build,
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
            "timeoutInMinutes",
            "queuedTimeoutInMinutes",
            "reportArns",
        ),
        "CodeBuild build",
    )
    if "phases" in build:
        safe["phases"] = _safe_summary_list(
            build["phases"],
            ("phaseType", "phaseStatus", "startTime", "endTime", "durationInSeconds"),
            "CodeBuild build phases",
        )
    if "artifacts" in build and isinstance(build["artifacts"], Mapping):
        safe["artifacts"] = _copy_fields(
            build["artifacts"],
            ("type", "name", "artifactIdentifier", "encryptionDisabled", "sha256sum", "md5sum"),
            "CodeBuild build artifacts",
        )
    if "logs" in build:
        safe["logs"] = _copy_fields(
            build["logs"],
            ("groupName", "streamName", "cloudWatchLogsArn", "s3LogsArn", "cloudWatchLogsStatus", "s3LogsStatus"),
            "CodeBuild build logs",
        )
    return _bounded_metadata(safe)


def _safe_codepipeline_pipeline(pipeline):
    safe = _copy_fields(
        pipeline,
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
        "CodePipeline pipeline",
    )
    if "artifactStore" in pipeline:
        artifact_store = _require_mapping(pipeline["artifactStore"], "CodePipeline artifact store")
        safe["artifactStore"] = _copy_fields(
            artifact_store,
            ("type", "location", "encryptionKey"),
            "CodePipeline artifact store",
        )
        if isinstance(artifact_store.get("encryptionKey"), Mapping):
            safe["artifactStore"]["encryptionKey"] = _copy_fields(
                artifact_store["encryptionKey"],
                ("id", "type"),
                "CodePipeline artifact encryption key",
            )
    if "stages" in pipeline:
        stages = pipeline["stages"]
        if not isinstance(stages, list) or len(stages) > MAX_METADATA_LIST_ITEMS:
            raise _InventoryIncomplete("AWS returned an invalid CodePipeline stages collection")
        safe_stages = []
        for stage in stages:
            stage = _require_mapping(stage, "CodePipeline stage")
            safe_stage = _copy_fields(stage, ("name",), "CodePipeline stage")
            if "actions" in stage:
                actions = stage["actions"]
                if not isinstance(actions, list) or len(actions) > MAX_METADATA_LIST_ITEMS:
                    raise _InventoryIncomplete("AWS returned an invalid CodePipeline actions collection")
                safe_actions = []
                for action in actions:
                    action = _require_mapping(action, "CodePipeline action")
                    safe_action = _copy_fields(
                        action,
                        ("name", "runOrder", "region", "namespace"),
                        "CodePipeline action",
                    )
                    if isinstance(action.get("actionTypeId"), Mapping):
                        safe_action["actionTypeId"] = _copy_fields(
                            action["actionTypeId"],
                            ("category", "owner", "provider", "version"),
                            "CodePipeline action type",
                        )
                    safe_actions.append(safe_action)
                safe_stage["actions"] = safe_actions
            safe_stages.append(safe_stage)
        safe["stages"] = safe_stages
    return _bounded_metadata(safe)


def _safe_codepipeline_execution(execution):
    safe = _copy_fields(
        execution,
        (
            "pipelineExecutionId",
            "status",
            "startTime",
            "lastUpdateTime",
        ),
        "CodePipeline execution",
    )
    if "sourceRevisions" in execution:
        safe["sourceRevisions"] = _safe_summary_list(
            execution["sourceRevisions"],
            ("actionName", "revisionId", "revisionType"),
            "CodePipeline source revisions",
        )
    if isinstance(execution.get("trigger"), Mapping):
        safe["trigger"] = _copy_fields(
            execution["trigger"],
            ("triggerType",),
            "CodePipeline execution trigger",
        )
    return _bounded_metadata(safe)


def _elastic_beanstalk_application_records(client, region):
    applications = _bounded_collection(
        client,
        "describe_applications",
        "Applications",
        "Elastic Beanstalk applications",
        limit=MAX_ELASTIC_BEANSTALK_APPLICATIONS,
    )
    records = []
    for application in applications:
        application = _require_mapping(application, "Elastic Beanstalk application")
        name = _require_identifier(application.get("ApplicationName"), "Elastic Beanstalk application")
        safe = _safe_beanstalk_application(application)
        records.append(
            _record(
                AWS_ELASTIC_BEANSTALK_APPLICATION,
                region,
                name,
                name,
                safe,
                provider_id=application.get("ApplicationArn") or name,
                provider_name=name,
                provider_status="available",
            )
        )
    return records


def _elastic_beanstalk_environment_records(client, region):
    environments = _bounded_collection(
        client,
        "describe_environments",
        "Environments",
        "Elastic Beanstalk environments",
        limit=MAX_ELASTIC_BEANSTALK_ENVIRONMENTS,
    )
    records = []
    for environment in environments:
        environment = _require_mapping(environment, "Elastic Beanstalk environment")
        environment_id = environment.get("EnvironmentId")
        environment_name = _require_identifier(
            environment.get("EnvironmentName"),
            "Elastic Beanstalk environment",
        )
        raw_id = _require_identifier(environment_id or environment_name, "Elastic Beanstalk environment")
        application_name = _require_identifier(
            environment.get("ApplicationName") or "unknown-application",
            "Elastic Beanstalk application context",
        )
        safe = _safe_beanstalk_environment(environment)
        records.append(
            _record(
                AWS_ELASTIC_BEANSTALK_ENVIRONMENT,
                region,
                raw_id,
                f"{application_name}/{environment_name}",
                safe,
                provider_id=environment.get("EnvironmentArn") or raw_id,
                provider_name=environment_name,
                provider_status=environment.get("Status") or environment.get("Health"),
                _cloudmoo_application_name=application_name,
                _cloudmoo_environment_id=environment_id or "",
                _cloudmoo_environment_name=environment_name,
            )
        )
    return records


def _codebuild_project_records(client, region):
    project_names = _bounded_collection(
        client,
        "list_projects",
        "projects",
        "CodeBuild projects",
        limit=MAX_CODEBUILD_PROJECTS,
    )
    names = []
    for name in project_names:
        names.append(_require_identifier(name, "CodeBuild project"))
    names = list(dict.fromkeys(names))
    if not names:
        return [], []
    projects = _batch_get_collection(
        client,
        "batch_get_projects",
        "names",
        "projects",
        "projectsNotFound",
        names,
        "CodeBuild projects",
    )
    records = []
    for project in projects:
        project = _require_mapping(project, "CodeBuild project")
        name = _require_identifier(project.get("name"), "CodeBuild project")
        safe = _safe_codebuild_project(project)
        records.append(
            _record(
                AWS_CODEBUILD_PROJECT,
                region,
                name,
                name,
                safe,
                provider_id=project.get("arn") or name,
                provider_name=name,
                provider_status="available",
                _cloudmoo_project_name=name,
            )
        )
    return records, names


def _codebuild_build_records(client, region, project_name):
    build_ids = _bounded_recent_collection(
        client,
        "list_builds_for_project",
        "ids",
        "CodeBuild builds",
        limit=MAX_CODEBUILD_BUILDS_PER_PROJECT,
        projectName=project_name,
        sortOrder="DESCENDING",
    )
    ids = list(dict.fromkeys(_require_identifier(value, "CodeBuild build") for value in build_ids))
    if not ids:
        return []
    builds = _batch_get_collection(
        client,
        "batch_get_builds",
        "ids",
        "builds",
        "buildsNotFound",
        ids,
        "CodeBuild builds",
    )
    records = []
    for build in builds:
        build = _require_mapping(build, "CodeBuild build")
        build_id = _require_identifier(build.get("id"), "CodeBuild build")
        actual_project = _require_identifier(
            build.get("projectName") or project_name,
            "CodeBuild project context",
        )
        build_number = build.get("buildNumber") or build_id
        safe = _safe_codebuild_build(build)
        records.append(
            _record(
                AWS_CODEBUILD_BUILD,
                region,
                build_id,
                f"{actual_project}/{build_number}",
                safe,
                provider_id=build.get("arn") or build_id,
                provider_name=str(build_number),
                provider_status=build.get("buildStatus"),
                identity=f"{actual_project}|{build_id}",
                _cloudmoo_project_name=actual_project,
                _cloudmoo_build_id=build_id,
            )
        )
    return records


def _codepipeline_pipeline_records(client, region):
    summaries = _bounded_collection(
        client,
        "list_pipelines",
        "pipelines",
        "CodePipeline pipelines",
        limit=MAX_CODEPIPELINE_PIPELINES,
    )
    records = []
    names = []
    for summary in summaries:
        summary = _require_mapping(summary, "CodePipeline pipeline summary")
        listed_name = _require_identifier(summary.get("name"), "CodePipeline pipeline")
        response = client.get_pipeline(name=listed_name)
        if not isinstance(response, Mapping):
            raise _InventoryIncomplete("AWS returned an invalid CodePipeline pipeline response")
        pipeline = _require_mapping(response.get("pipeline"), "CodePipeline pipeline")
        name = _require_identifier(pipeline.get("name") or listed_name, "CodePipeline pipeline")
        safe = _safe_codepipeline_pipeline(pipeline)
        records.append(
            _record(
                AWS_CODEPIPELINE_PIPELINE,
                region,
                name,
                name,
                safe,
                provider_id=pipeline.get("pipelineArn") or summary.get("pipelineArn") or name,
                provider_name=name,
                provider_status="available",
                _cloudmoo_pipeline_name=name,
            )
        )
        names.append(name)
    return records, names


def _codepipeline_execution_records(client, region, pipeline_name):
    summaries = _bounded_recent_collection(
        client,
        "list_pipeline_executions",
        "pipelineExecutionSummaries",
        "CodePipeline executions",
        limit=MAX_CODEPIPELINE_EXECUTIONS_PER_PIPELINE,
        pipelineName=pipeline_name,
        maxResults=MAX_CODEPIPELINE_EXECUTIONS_PER_PIPELINE,
    )
    records = []
    for summary in summaries:
        summary = _require_mapping(summary, "CodePipeline execution")
        execution_id = _require_identifier(
            summary.get("pipelineExecutionId"),
            "CodePipeline execution",
        )
        safe = _safe_codepipeline_execution(summary)
        records.append(
            _record(
                AWS_CODEPIPELINE_EXECUTION,
                region,
                execution_id,
                f"{pipeline_name}/{execution_id}",
                safe,
                provider_id=execution_id,
                provider_name=execution_id,
                provider_status=summary.get("status"),
                identity=f"{pipeline_name}|{execution_id}",
                _cloudmoo_pipeline_name=pipeline_name,
                _cloudmoo_execution_id=execution_id,
            )
        )
    return records


def _client_for(account, region, service, cache):
    key = (service, region)
    if key not in cache:
        cache[key] = aws_client(account, service, region=region)
    return cache[key]


def _error_summary(error):
    code = aws_error_code(error)
    if isinstance(error, CloudInventoryTransientError):
        kind = "incomplete_inventory"
    elif is_transient_aws_error(error):
        kind = "transient"
    elif isinstance(error, (ClientError, BotoCoreError)):
        kind = "provider"
    else:
        kind = "adapter"
    return {"code": str(code)[:128], "kind": kind}


def _record_error(summary, region, asset_type, error):
    safe = _error_summary(error)
    summary["errors"].append(
        {
            "region": region,
            "asset_type": asset_type,
            **safe,
        }
    )
    logger.warning(
        "AWS delivery inventory skipped %s in %s (%s)",
        asset_type,
        region,
        safe["code"],
    )
    return safe


def _family_success(summary, asset_type, region, count, reconciled):
    result = {
        "status": "ok",
        "complete": True,
        "reconciled": bool(reconciled),
        "count": count,
    }
    summary["families"][asset_type][region] = result
    summary[asset_type][region] = result
    summary["counts"][asset_type][region] = count
    summary["synced"][asset_type] += count


def _family_failure(summary, asset_type, region, error, count=None, errors=None):
    safe_errors = list(errors or ())
    if not safe_errors:
        safe_errors.append(_record_error(summary, region, asset_type, error))
    else:
        for safe in safe_errors:
            summary["errors"].append(
                {
                    "region": region,
                    "asset_type": asset_type,
                    **safe,
                }
            )
    result = {
        "status": "error",
        "complete": False,
        "reconciled": False,
        "count": count,
        "errors": safe_errors,
    }
    summary["families"][asset_type][region] = result
    summary[asset_type][region] = result
    summary["counts"][asset_type][region] = count


def _upsert_asset(model, account, region, asset_type, record):
    defaults = {
        "region": region,
        "name": record["name"],
        "type": asset_type,
        "monitoring": model.Monitoring.ACTIVE,
        "metadata": record["metadata"],
    }
    asset, created = model.objects.get_or_create(
        owner=account,
        unique_id=record["unique_id"],
        defaults=defaults,
    )
    asset.region = region
    asset.name = record["name"]
    asset.type = asset_type
    asset.metadata = record["metadata"]
    if not created and getattr(asset, "monitoring", None) == model.Monitoring.NO_LONGER_EXISTS:
        asset.monitoring = model.Monitoring.ACTIVE
    asset.save()
    return asset


def _reconcile(model, account, region, asset_type, records):
    if not isinstance(records, list):
        raise _InventoryIncomplete(f"AWS returned an invalid {asset_type} collection")
    current_ids = []
    seen_ids = set()
    for record in records:
        if not isinstance(record, Mapping) or not record.get("unique_id"):
            raise _InventoryIncomplete(f"AWS returned an invalid {asset_type} record")
        unique_id = str(record["unique_id"])
        if unique_id in seen_ids:
            raise _InventoryIncomplete(f"AWS returned a duplicate {asset_type} identifier")
        seen_ids.add(unique_id)
        current_ids.append(unique_id)
        _upsert_asset(model, account, region, asset_type, record)

    model.objects.filter(owner=account, region=region).exclude(
        unique_id__in=current_ids
    ).update(monitoring=model.Monitoring.NO_LONGER_EXISTS)
    return len(current_ids)


def _run_full_family(summary, account, region, asset_type, model, records_factory):
    try:
        records = records_factory()
        count = _reconcile(model, account, region, asset_type, records)
        _family_success(summary, asset_type, region, count, reconciled=True)
        return records
    except Exception as error:
        _family_failure(summary, asset_type, region, error)
        return None


def _run_recent_family(summary, account, region, asset_type, model, parent_names, records_factory):
    """Upsert a bounded event window without deleting older event rows."""
    records = []
    failures = []
    for parent_name in parent_names:
        try:
            records.extend(records_factory(parent_name))
        except Exception as error:
            failures.append(_error_summary(error))

    try:
        seen_ids = set()
        for record in records:
            if not isinstance(record, Mapping) or not record.get("unique_id"):
                raise _InventoryIncomplete(f"AWS returned an invalid {asset_type} record")
            unique_id = str(record["unique_id"])
            if unique_id in seen_ids:
                raise _InventoryIncomplete(f"AWS returned a duplicate {asset_type} identifier")
            seen_ids.add(unique_id)
            _upsert_asset(model, account, region, asset_type, record)
    except Exception as error:
        failures.append(_error_summary(error))

    if failures:
        _family_failure(
            summary,
            asset_type,
            region,
            CloudInventoryTransientError("AWS delivery event inventory was incomplete"),
            count=len(records),
            errors=failures,
        )
    else:
        _family_success(summary, asset_type, region, len(records), reconciled=False)
    return records


def _sync_beanstalk(summary, account, region, clients):
    try:
        client = _client_for(account, region, "elasticbeanstalk", clients)
    except Exception as error:
        _family_failure(summary, AWS_ELASTIC_BEANSTALK_APPLICATION, region, error)
        _family_failure(summary, AWS_ELASTIC_BEANSTALK_ENVIRONMENT, region, error)
        return

    _run_full_family(
        summary,
        account,
        region,
        AWS_ELASTIC_BEANSTALK_APPLICATION,
        CoreAWSElasticBeanstalkApplication,
        lambda: _elastic_beanstalk_application_records(client, region),
    )
    _run_full_family(
        summary,
        account,
        region,
        AWS_ELASTIC_BEANSTALK_ENVIRONMENT,
        CoreAWSElasticBeanstalkEnvironment,
        lambda: _elastic_beanstalk_environment_records(client, region),
    )


def _sync_codebuild(summary, account, region, clients):
    try:
        client = _client_for(account, region, "codebuild", clients)
    except Exception as error:
        _family_failure(summary, AWS_CODEBUILD_PROJECT, region, error)
        _family_failure(summary, AWS_CODEBUILD_BUILD, region, error)
        return

    try:
        project_records, project_names = _codebuild_project_records(client, region)
        _reconcile(
            CoreAWSCodeBuildProject,
            account,
            region,
            AWS_CODEBUILD_PROJECT,
            project_records,
        )
        _family_success(
            summary,
            AWS_CODEBUILD_PROJECT,
            region,
            len(project_records),
            reconciled=True,
        )
    except Exception as error:
        _family_failure(summary, AWS_CODEBUILD_PROJECT, region, error)
        _family_failure(summary, AWS_CODEBUILD_BUILD, region, error)
        return

    _run_recent_family(
        summary,
        account,
        region,
        AWS_CODEBUILD_BUILD,
        CoreAWSCodeBuildBuild,
        project_names,
        lambda project_name: _codebuild_build_records(client, region, project_name),
    )


def _sync_codepipeline(summary, account, region, clients):
    try:
        client = _client_for(account, region, "codepipeline", clients)
    except Exception as error:
        _family_failure(summary, AWS_CODEPIPELINE_PIPELINE, region, error)
        _family_failure(summary, AWS_CODEPIPELINE_EXECUTION, region, error)
        return

    try:
        pipeline_records, pipeline_names = _codepipeline_pipeline_records(client, region)
        _reconcile(
            CoreAWSCodePipelinePipeline,
            account,
            region,
            AWS_CODEPIPELINE_PIPELINE,
            pipeline_records,
        )
        _family_success(
            summary,
            AWS_CODEPIPELINE_PIPELINE,
            region,
            len(pipeline_records),
            reconciled=True,
        )
    except Exception as error:
        _family_failure(summary, AWS_CODEPIPELINE_PIPELINE, region, error)
        _family_failure(summary, AWS_CODEPIPELINE_EXECUTION, region, error)
        return

    _run_recent_family(
        summary,
        account,
        region,
        AWS_CODEPIPELINE_EXECUTION,
        CoreAWSCodePipelineExecution,
        pipeline_names,
        lambda pipeline_name: _codepipeline_execution_records(client, region, pipeline_name),
    )


def sync_aws_delivery_assets(account):
    """Synchronize AWS delivery assets using read-only regional calls."""
    raw_regions = get_enabled_regions(account)
    if not isinstance(raw_regions, (list, tuple, set, frozenset)):
        raise CloudInventoryTransientError("AWS enabled regions are invalid")
    regions = set()
    for region in raw_regions:
        if (
            not isinstance(region, str)
            or not region.strip()
            or len(region.strip()) > MAX_REGION_LENGTH
            or _REGION_RE.fullmatch(region.strip()) is None
        ):
            raise CloudInventoryTransientError("AWS enabled regions are invalid")
        regions.add(region.strip())
    regions = sorted(regions)

    summary = {
        "regions": regions,
        "families": {asset_type: {} for asset_type in AWS_DELIVERY_ASSET_TYPES},
        "counts": {asset_type: {} for asset_type in AWS_DELIVERY_ASSET_TYPES},
        "synced": {asset_type: 0 for asset_type in AWS_DELIVERY_ASSET_TYPES},
        "errors": [],
    }
    for asset_type in AWS_DELIVERY_ASSET_TYPES:
        summary[asset_type] = summary["families"][asset_type]

    for region in regions:
        clients = {}
        _sync_beanstalk(summary, account, region, clients)
        _sync_codebuild(summary, account, region, clients)
        _sync_codepipeline(summary, account, region, clients)
    return summary


# Integration-friendly aliases; no App Runner resources are duplicated here.
sync_aws_delivery_inventory = sync_aws_delivery_assets
sync_aws_hosting_assets = sync_aws_delivery_assets


__all__ = [
    "AWS_ELASTIC_BEANSTALK_APPLICATION",
    "AWS_ELASTIC_BEANSTALK_ENVIRONMENT",
    "AWS_CODEBUILD_PROJECT",
    "AWS_CODEBUILD_BUILD",
    "AWS_CODEPIPELINE_PIPELINE",
    "AWS_CODEPIPELINE_EXECUTION",
    "AWS_DELIVERY_ASSET_TYPES",
    "AWS_DELIVERY_MODELS",
    "AWS_DELIVERY_ASSET_MODELS",
    "APP_RUNNER_INTEGRATION_NOTE",
    "CoreAWSDeliveryAsset",
    "CoreAWSElasticBeanstalkApplication",
    "CoreAWSElasticBeanstalkEnvironment",
    "CoreAWSCodeBuildProject",
    "CoreAWSCodeBuildBuild",
    "CoreAWSCodePipelinePipeline",
    "CoreAWSCodePipelineExecution",
    "sync_aws_delivery_assets",
    "sync_aws_delivery_inventory",
    "sync_aws_hosting_assets",
]
