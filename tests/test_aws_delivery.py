from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError
from django.test import SimpleTestCase

from apps.console.cloud.aws import delivery
from apps.console.cloud.aws.delivery import (
    AWS_CODEBUILD_BUILD,
    AWS_CODEBUILD_PROJECT,
    AWS_CODEPIPELINE_EXECUTION,
    AWS_CODEPIPELINE_PIPELINE,
    AWS_ELASTIC_BEANSTALK_APPLICATION,
    AWS_ELASTIC_BEANSTALK_ENVIRONMENT,
    AWS_DELIVERY_ASSET_TYPES,
    CoreAWSCodeBuildBuild,
    CoreAWSCodeBuildProject,
    CoreAWSCodePipelineExecution,
    CoreAWSCodePipelinePipeline,
    CoreAWSElasticBeanstalkApplication,
    CoreAWSElasticBeanstalkEnvironment,
    sync_aws_delivery_assets,
)
from apps.console.cloud.aws.models import CoreAWSAccount
from apps.console.cloud.models import CloudInventoryTransientError
from apps.console.utils.models import UtilAsset
from apps.monitoring.checks import aws_delivery
from apps.console.cloud.aws.discovery import iter_pages


class _Paginator:
    def __init__(self, client, operation):
        self.client = client
        self.operation = operation

    def paginate(self, **kwargs):
        self.client.calls.append((self.operation, kwargs))
        if self.operation in self.client.failures:
            raise self.client.failures[self.operation]
        pages = self.client.pages.get(self.operation, [])
        if callable(pages):
            pages = pages(**kwargs)
        return iter(pages)


class _ReadOnlyClient:
    def __init__(self, *, pages=None, responses=None, failures=None):
        self.pages = pages or {}
        self.responses = responses or {}
        self.failures = failures or {}
        self.calls = []

    def get_paginator(self, operation):
        return _Paginator(self, operation)

    def __getattr__(self, operation):
        if operation not in self.responses:
            raise AssertionError(f"Unexpected AWS operation: {operation}")

        def call(**kwargs):
            self.calls.append((operation, kwargs))
            if operation in self.failures:
                raise self.failures[operation]
            response = self.responses[operation]
            return response(**kwargs) if callable(response) else response

        return call


class _MemoryQuerySet:
    def __init__(self, rows):
        self.rows = list(rows)

    def exclude(self, **kwargs):
        current = set(kwargs.get("unique_id__in", []))
        return _MemoryQuerySet([row for row in self.rows if row.unique_id not in current])

    def update(self, **kwargs):
        for row in self.rows:
            for key, value in kwargs.items():
                setattr(row, key, value)
        return len(self.rows)


class _MemoryAsset:
    def __init__(self, owner, unique_id, defaults):
        self.owner = owner
        self.unique_id = unique_id
        self.__dict__.update(defaults)

    def save(self):
        return None


class _MemoryManager:
    def __init__(self, model):
        self.model = model
        self.rows = []

    def get_or_create(self, owner, unique_id, defaults):
        for row in self.rows:
            if row.owner is owner and row.unique_id == unique_id:
                return row, False
        row = _MemoryAsset(owner, unique_id, defaults)
        self.rows.append(row)
        return row, True

    def filter(self, **kwargs):
        rows = self.rows
        for key, value in kwargs.items():
            rows = [row for row in rows if getattr(row, key) == value]
        return _MemoryQuerySet(rows)


def _error(code):
    return ClientError(
        {"Error": {"Code": code, "Message": "provider secret-token=must-not-return"}},
        "MockOperation",
    )


def _project(name, *, secret=True):
    value = {
        "name": name,
        "arn": f"arn:aws:codebuild:eu-west-1:123:project/{name}",
        "description": "read-only test project",
        "source": {
            "type": "GITHUB",
            "location": "https://token@example.invalid/repo",
            "gitCloneDepth": 1,
        },
        "environment": {
            "type": "LINUX_CONTAINER",
            "computeType": "BUILD_GENERAL1_SMALL",
            "image": "aws/codebuild/standard:7.0",
            "privilegedMode": False,
            "environmentVariables": [
                {"name": "DEPLOY_TOKEN", "value": "secret-value"}
            ],
        },
        "serviceRole": "arn:aws:iam::123:role/codebuild",
        "logsConfig": {
            "cloudWatchLogs": {"status": "ENABLED", "groupName": "/aws/codebuild/" + name},
        },
    }
    if secret:
        value["SecretToken"] = "must-not-persist"
    return value


def _build(build_id, project_name="build-one"):
    return {
        "id": build_id,
        "arn": f"arn:aws:codebuild:eu-west-1:123:build/{build_id}",
        "buildNumber": 7,
        "projectName": project_name,
        "buildStatus": "SUCCEEDED",
        "sourceVersion": "abc123",
        "buildComplete": True,
        "phases": [
            {
                "phaseType": "BUILD",
                "phaseStatus": "SUCCEEDED",
                "contexts": [{"message": "secret-token=hidden"}],
            }
        ],
        "logs": {
            "groupName": "/aws/codebuild/" + project_name,
            "streamName": "7",
            "deepLink": "https://logs.invalid/?token=secret",
        },
    }


def _pipeline(name="pipe-one"):
    return {
        "name": name,
        "version": 3,
        "pipelineType": "V2",
        "executionMode": "SUPERSEDED",
        "roleArn": "arn:aws:iam::123:role/codepipeline",
        "stages": [
            {
                "name": "Source",
                "actions": [
                    {
                        "name": "Checkout",
                        "runOrder": 1,
                        "region": "eu-west-1",
                        "actionTypeId": {
                            "category": "Source",
                            "owner": "AWS",
                            "provider": "CodeStarSourceConnection",
                            "version": "1",
                        },
                        "configuration": {"OAuthToken": "secret-value"},
                    }
                ],
            }
        ],
    }


def _delivery_clients(*, partial=False):
    eb = _ReadOnlyClient(
        pages={
            "describe_applications": [
                {
                    "Applications": [
                        {
                            "ApplicationName": "shop-app",
                            "ApplicationArn": "arn:aws:elasticbeanstalk:eu-west-1:123:application/shop-app",
                            "Description": "test app",
                            "SecretToken": "omit-me",
                            "Versions": [{"VersionLabel": "v1", "Status": "Processed"}],
                            "ResourceLifecycleConfig": {
                                "ServiceRole": "arn:aws:iam::123:role/eb",
                                "VersionLifecycleConfig": [
                                    {"MaxCountRule": {"Enabled": True, "MaxCount": 5}}
                                ],
                            },
                        }
                    ],
                },
                {"Applications": []},
            ],
            "describe_environments": [
                {
                    "Environments": [
                        {
                            "EnvironmentId": "e-abc123",
                            "EnvironmentName": "shop-prod",
                            "ApplicationName": "shop-app",
                            "Status": "Ready",
                            "Health": "Green",
                            "HealthStatus": "Ok",
                            "SolutionStackName": "64bit Amazon Linux 2",
                            "VersionLabel": "v1",
                            "EndpointURL": "shop.example.invalid",
                            "EnvironmentVariables": {"DEPLOY_TOKEN": "secret-value"},
                        }
                    ],
                },
                {"Environments": []},
            ],
        },
    )

    codebuild = _ReadOnlyClient(
        pages={
            "list_projects": [{"projects": ["build-one"]}, {"projects": ["build-two"]}],
            "list_builds_for_project": lambda projectName, sortOrder: [
                {"ids": ["build-one:7"] if projectName == "build-one" else []},
                {"ids": []},
            ],
        },
        responses={
            "batch_get_projects": lambda names: {
                "projects": [_project(name) for name in names],
                "projectsNotFound": [],
            },
            "batch_get_builds": lambda ids: {
                "builds": [_build(build_id) for build_id in ids],
                "buildsNotFound": [],
            },
        },
    )

    codepipeline = _ReadOnlyClient(
        pages={
            "list_pipelines": [{"pipelines": [{"name": "pipe-one"}]}, {"pipelines": []}],
            "list_pipeline_executions": lambda pipelineName, maxResults: [
                {
                    "pipelineExecutionSummaries": [
                        {
                            "pipelineExecutionId": "exec-1",
                            "status": "Succeeded",
                            "sourceRevisions": [
                                {"actionName": "Checkout", "revisionId": "abc123", "revisionType": "CommitId"}
                            ],
                            "trigger": {"triggerType": "StartPipelineExecution", "triggerDetail": "secret-token"},
                        }
                    ]
                },
                {"pipelineExecutionSummaries": []},
            ],
        },
        responses={
            "get_pipeline": lambda name: {"pipeline": _pipeline(name)},
        },
    )
    if partial:
        eb.pages["describe_applications"] = [{}]
        codebuild.pages["list_projects"] = [{"projects": ["build-one"]}]
        codebuild.responses["batch_get_projects"] = lambda names: {
            "projects": [],
            "projectsNotFound": list(names),
        }
        codepipeline.failures["list_pipeline_executions"] = _error("RequestLimitExceeded")
    return eb, codebuild, codepipeline


class AWSDeliveryInventoryTests(SimpleTestCase):
    def setUp(self):
        self.account = SimpleNamespace(
            access_key="ACCESS-KEY",
            secret_key="SECRET-KEY",
            region="eu-west-1",
        )

    def _patch_managers(self, stack):
        managers = {}
        for asset_type, model in delivery.AWS_DELIVERY_MODELS.items():
            manager = _MemoryManager(model)
            managers[asset_type] = manager
            stack.enter_context(patch.object(model, "objects", manager))
        return managers

    def test_inventory_uses_pagination_allowlists_region_ids_and_read_only_calls(self):
        eb, codebuild, codepipeline = _delivery_clients()
        clients = {
            "elasticbeanstalk": eb,
            "codebuild": codebuild,
            "codepipeline": codepipeline,
        }

        def client_factory(_account, service, region=None):
            self.assertEqual(region, "eu-west-1")
            return clients[service]

        with patch.object(delivery, "get_enabled_regions", return_value=["eu-west-1"]), \
             patch.object(delivery, "aws_client", side_effect=client_factory), \
             ExitStack() as stack:
            managers = self._patch_managers(stack)
            summary = sync_aws_delivery_assets(self.account)

        self.assertEqual(summary["regions"], ["eu-west-1"])
        self.assertEqual(summary["counts"][AWS_ELASTIC_BEANSTALK_APPLICATION]["eu-west-1"], 1)
        self.assertEqual(summary["counts"][AWS_ELASTIC_BEANSTALK_ENVIRONMENT]["eu-west-1"], 1)
        self.assertEqual(summary["counts"][AWS_CODEBUILD_PROJECT]["eu-west-1"], 2)
        self.assertEqual(summary["counts"][AWS_CODEBUILD_BUILD]["eu-west-1"], 1)
        self.assertEqual(summary["counts"][AWS_CODEPIPELINE_PIPELINE]["eu-west-1"], 1)
        self.assertEqual(summary["counts"][AWS_CODEPIPELINE_EXECUTION]["eu-west-1"], 1)
        self.assertFalse(summary["errors"])

        application = managers[AWS_ELASTIC_BEANSTALK_APPLICATION].rows[0]
        self.assertEqual(application.unique_id, "eu-west-1|shop-app")
        self.assertEqual(application.type, AWS_ELASTIC_BEANSTALK_APPLICATION)
        self.assertNotIn("SecretToken", application.metadata)
        self.assertNotIn("ACCESS-KEY", application.metadata)
        self.assertNotIn("SECRET-KEY", application.metadata)

        project = managers[AWS_CODEBUILD_PROJECT].rows[0]
        self.assertNotIn("environmentVariables", project.metadata.get("environment", {}))
        self.assertNotIn("location", project.metadata.get("source", {}))
        self.assertNotIn("SecretToken", project.metadata)

        pipeline = managers[AWS_CODEPIPELINE_PIPELINE].rows[0]
        action = pipeline.metadata["stages"][0]["actions"][0]
        self.assertNotIn("configuration", action)

        build = managers[AWS_CODEBUILD_BUILD].rows[0]
        self.assertEqual(build.unique_id, "eu-west-1|build-one|build-one:7")
        self.assertEqual(build.metadata["_cloudmoo_raw_id"], "build-one:7")
        self.assertNotIn("contexts", build.metadata["phases"][0])
        self.assertNotIn("deepLink", build.metadata["logs"])

        execution = managers[AWS_CODEPIPELINE_EXECUTION].rows[0]
        self.assertEqual(execution.unique_id, "eu-west-1|pipe-one|exec-1")
        self.assertEqual(execution.metadata["_cloudmoo_raw_id"], "exec-1")
        self.assertNotIn("triggerDetail", execution.metadata["trigger"])

        calls = eb.calls + codebuild.calls + codepipeline.calls
        self.assertTrue(any(operation == "describe_applications" for operation, _ in calls))
        self.assertTrue(any(operation == "list_projects" for operation, _ in calls))
        self.assertTrue(any(operation == "batch_get_builds" for operation, _ in calls))
        self.assertTrue(any(operation == "list_pipeline_executions" for operation, _ in calls))
        self.assertTrue(
            all(
                operation == "get_paginator"
                or operation.startswith(("describe_", "list_", "batch_get_", "get_"))
                for operation, _ in calls
            )
        )

    def test_partial_family_failures_never_mark_existing_rows_missing(self):
        eb, codebuild, codepipeline = _delivery_clients(partial=True)
        clients = {
            "elasticbeanstalk": eb,
            "codebuild": codebuild,
            "codepipeline": codepipeline,
        }

        def client_factory(_account, service, region=None):
            return clients[service]

        with patch.object(delivery, "get_enabled_regions", return_value=["eu-west-1"]), \
             patch.object(delivery, "aws_client", side_effect=client_factory), \
             ExitStack() as stack:
            managers = self._patch_managers(stack)
            old_rows = {
                AWS_ELASTIC_BEANSTALK_APPLICATION: _MemoryAsset(
                    self.account,
                    "eu-west-1|old-app",
                    {"region": "eu-west-1", "monitoring": UtilAsset.Monitoring.ACTIVE},
                ),
                AWS_CODEBUILD_PROJECT: _MemoryAsset(
                    self.account,
                    "eu-west-1|old-project",
                    {"region": "eu-west-1", "monitoring": UtilAsset.Monitoring.ACTIVE},
                ),
                AWS_CODEBUILD_BUILD: _MemoryAsset(
                    self.account,
                    "eu-west-1|old-project|old-build",
                    {"region": "eu-west-1", "monitoring": UtilAsset.Monitoring.ACTIVE},
                ),
                AWS_CODEPIPELINE_EXECUTION: _MemoryAsset(
                    self.account,
                    "eu-west-1|pipe-one|old-exec",
                    {"region": "eu-west-1", "monitoring": UtilAsset.Monitoring.ACTIVE},
                ),
            }
            for asset_type, row in old_rows.items():
                managers[asset_type].rows.append(row)
            summary = sync_aws_delivery_assets(self.account)

        self.assertFalse(
            summary["families"][AWS_ELASTIC_BEANSTALK_APPLICATION]["eu-west-1"]["complete"]
        )
        self.assertFalse(
            summary["families"][AWS_CODEBUILD_PROJECT]["eu-west-1"]["complete"]
        )
        self.assertFalse(
            summary["families"][AWS_CODEBUILD_BUILD]["eu-west-1"]["complete"]
        )
        self.assertFalse(
            summary["families"][AWS_CODEPIPELINE_EXECUTION]["eu-west-1"]["complete"]
        )
        for row in old_rows.values():
            self.assertEqual(row.monitoring, UtilAsset.Monitoring.ACTIVE)
        self.assertTrue(summary["errors"])
        self.assertTrue(all("secret-token" not in str(error).lower() for error in summary["errors"]))

    def test_model_context_and_delivery_registry_are_region_aware(self):
        owner = CoreAWSAccount(
            access_key="ACCESS-KEY",
            secret_key="SECRET-KEY",
            region="us-east-1",
        )
        asset = CoreAWSCodePipelineExecution(
            owner=owner,
            region="eu-west-1",
            unique_id="eu-west-1|pipe-one|exec-1",
            name="pipe-one/exec-1",
            type=AWS_CODEPIPELINE_EXECUTION,
            metadata={
                "_cloudmoo_region": "eu-west-1",
                "_cloudmoo_raw_id": "exec-1",
                "_cloudmoo_pipeline_name": "pipe-one",
            },
        )
        credentials = asset.monitoring_credentials
        self.assertEqual(credentials["resource_region"], "eu-west-1")
        self.assertEqual(credentials["provider_id"], "exec-1")
        self.assertEqual(asset.provider_url.split("?", 1)[0], "https://eu-west-1.console.aws.amazon.com/codesuite/codepipeline/pipelines/pipe-one/view")
        self.assertNotIn("ACCESS-KEY", asset.metadata)
        self.assertNotIn("SECRET-KEY", asset.metadata)
        self.assertEqual(set(aws_delivery.AWS_DELIVERY_STATUS_CHECKS), set(AWS_DELIVERY_ASSET_TYPES))
        self.assertTrue(all(callable(aws_delivery.AWS_DELIVERY_STATUS_CHECKS[asset_type]) for asset_type in AWS_DELIVERY_ASSET_TYPES))

    def test_shared_discovery_rejects_mutating_delivery_operations(self):
        client = Mock()
        with self.assertRaises(ValueError):
            list(iter_pages(client, "start_build"))
        client.get_paginator.assert_not_called()

    def test_empty_codebuild_families_do_not_issue_invalid_batch_requests(self):
        client = _ReadOnlyClient(
            pages={
                "list_projects": [{"projects": []}],
                "list_builds_for_project": [{"ids": []}],
            },
            responses={
                "batch_get_projects": lambda **_kwargs: self.fail(
                    "empty project lists must not call BatchGetProjects"
                ),
                "batch_get_builds": lambda **_kwargs: self.fail(
                    "empty build lists must not call BatchGetBuilds"
                ),
            },
        )

        projects, names = delivery._codebuild_project_records(client, "eu-west-1")
        self.assertEqual(projects, [])
        self.assertEqual(names, [])
        self.assertEqual(
            delivery._codebuild_build_records(client, "eu-west-1", "empty-project"),
            [],
        )
        self.assertEqual(
            [operation for operation, _kwargs in client.calls],
            ["list_projects", "list_builds_for_project"],
        )


class AWSDeliveryCheckTests(SimpleTestCase):
    def setUp(self):
        self.credentials = {
            "access_key": "ACCESS-KEY",
            "secret_key": "SECRET-KEY",
            "region": "eu-west-1",
        }
        self.client = _ReadOnlyClient(
            responses={
                "describe_applications": lambda ApplicationNames: {
                    "Applications": [{"ApplicationName": ApplicationNames[0], "Description": "safe"}]
                },
                "describe_environments": lambda EnvironmentIds: {
                    "Environments": [
                        {
                            "EnvironmentId": EnvironmentIds[0],
                            "EnvironmentName": "shop-prod",
                            "Status": "Updating",
                            "Health": "Yellow",
                        }
                    ]
                },
                "batch_get_projects": lambda names: {
                    "projects": [{"name": names[0], "description": "safe"}],
                    "projectsNotFound": [],
                },
                "batch_get_builds": lambda ids: {
                    "builds": [{"id": ids[0], "buildStatus": "SUCCEEDED", "projectName": "build-one"}],
                    "buildsNotFound": [],
                },
                "get_pipeline": lambda name: {"pipeline": {"name": name, "version": 1}},
                "get_pipeline_execution": lambda pipelineName, pipelineExecutionId: {
                    "pipelineExecution": {
                        "pipelineExecutionId": pipelineExecutionId,
                        "status": "Failed",
                        "pipelineName": pipelineName,
                        "trigger": {"triggerType": "Webhook", "triggerDetail": "secret-token"},
                    }
                },
            }
        )

    def test_checks_use_raw_ids_normalize_service_status_and_stay_read_only(self):
        credentials = {
            **self.credentials,
            "metadata": {"_cloudmoo_region": "eu-west-1", "_cloudmoo_provider_name": "shop-app"},
        }
        with patch.object(aws_delivery, "aws_client", return_value=self.client):
            status, _ = aws_delivery.check_aws_delivery_asset_status(
                AWS_ELASTIC_BEANSTALK_APPLICATION,
                "eu-west-1|shop-app",
                credentials,
            )
            self.assertEqual(status, "active")

            status, _ = aws_delivery.check_aws_delivery_asset_status(
                AWS_ELASTIC_BEANSTALK_ENVIRONMENT,
                "eu-west-1|e-abc123",
                {
                    **self.credentials,
                    "metadata": {
                        "_cloudmoo_region": "eu-west-1",
                        "_cloudmoo_environment_id": "e-abc123",
                    },
                },
            )
            self.assertEqual(status, "provisioning")

            status, _ = aws_delivery.check_aws_delivery_asset_status(
                AWS_CODEBUILD_PROJECT,
                "eu-west-1|build-one",
                {
                    **self.credentials,
                    "metadata": {"_cloudmoo_region": "eu-west-1", "_cloudmoo_project_name": "build-one"},
                },
            )
            self.assertEqual(status, "active")

            status, _ = aws_delivery.check_aws_delivery_asset_status(
                AWS_CODEBUILD_BUILD,
                "eu-west-1|build-one|build-one:7",
                {
                    **self.credentials,
                    "metadata": {
                        "_cloudmoo_region": "eu-west-1",
                        "_cloudmoo_build_id": "build-one:7",
                    },
                },
            )
            self.assertEqual(status, "succeeded")

            status, _ = aws_delivery.check_aws_delivery_asset_status(
                AWS_CODEPIPELINE_PIPELINE,
                "eu-west-1|pipe-one",
                {
                    **self.credentials,
                    "metadata": {"_cloudmoo_region": "eu-west-1", "_cloudmoo_pipeline_name": "pipe-one"},
                },
            )
            self.assertEqual(status, "active")

            status, metadata = aws_delivery.check_aws_delivery_asset_status(
                AWS_CODEPIPELINE_EXECUTION,
                "eu-west-1|pipe-one|exec-1",
                {
                    **self.credentials,
                    "metadata": {
                        "_cloudmoo_region": "eu-west-1",
                        "_cloudmoo_pipeline_name": "pipe-one",
                        "_cloudmoo_execution_id": "exec-1",
                    },
                },
            )
            self.assertEqual(status, "failed")
            self.assertNotIn("triggerDetail", metadata[AWS_CODEPIPELINE_EXECUTION]["trigger"])

        calls = self.client.calls
        self.assertIn(("batch_get_builds", {"ids": ["build-one:7"]}), calls)
        self.assertIn(
            ("get_pipeline_execution", {"pipelineName": "pipe-one", "pipelineExecutionId": "exec-1"}),
            calls,
        )
        self.assertTrue(
            all(
                operation.startswith(("describe_", "batch_get_", "get_"))
                for operation, _ in calls
            )
        )
        self.assertFalse(any(operation.startswith(("start", "stop", "retry", "put", "update")) for operation, _ in calls))

    def test_check_registry_contains_exact_delivery_types(self):
        self.assertEqual(set(aws_delivery.AWS_DELIVERY_STATUS_CHECKS), set(AWS_DELIVERY_ASSET_TYPES))
        for asset_type in AWS_DELIVERY_ASSET_TYPES:
            self.assertTrue(callable(aws_delivery.AWS_DELIVERY_STATUS_CHECKS[asset_type]))
