from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.console.cloud.aws import containers
from apps.monitoring.checks import aws_containers as checks


class FakePaginator:
    def __init__(self, client, operation, source):
        self.client = client
        self.operation = operation
        self.source = source

    def paginate(self, **kwargs):
        self.client.calls.append((self.operation, kwargs))
        pages = self.source(kwargs) if callable(self.source) else self.source
        return iter(pages)


class FakeAWSClient:
    MUTATING_PREFIXES = (
        "allocate",
        "attach",
        "create",
        "delete",
        "detach",
        "modify",
        "put",
        "release",
        "restore",
        "start",
        "stop",
        "update",
    )

    def __init__(self, pages=None, responses=None):
        self.pages = pages or {}
        self.responses = responses or {}
        self.calls = []

    def get_paginator(self, operation):
        self.calls.append(("get_paginator", {"operation": operation}))
        if operation not in self.pages:
            raise AttributeError(operation)
        return FakePaginator(self, operation, self.pages[operation])

    def __getattr__(self, operation):
        if operation.startswith(self.MUTATING_PREFIXES):
            raise AssertionError(f"unexpected mutating AWS call: {operation}")

        def call(**kwargs):
            self.calls.append((operation, kwargs))
            if operation not in self.responses:
                raise AssertionError(f"unexpected AWS call: {operation}")
            response = self.responses[operation]
            return response(kwargs) if callable(response) else response

        return call


def _empty_client(service):
    pages = {
        "describe_repositories": [{"repositories": []}],
        "list_images": [{"imageIds": []}],
        "describe_images": [{"imageDetails": []}],
        "list_task_definitions": [{"taskDefinitionArns": []}],
        "list_clusters": [{"clusterArns": []}],
        "list_services": [{"serviceArns": []}],
        "list_nodegroups": [{"nodegroups": []}],
        "list_addons": [{"addons": []}],
        "list_fargate_profiles": [{"fargateProfileNames": []}],
        "list_operations": [{"OperationSummaryList": []}],
    }
    if service == "apprunner":
        pages["list_services"] = [{"ServiceSummaryList": []}]
    return FakeAWSClient(pages=pages)


def _fixture_clients():
    repository_one = {
        "repositoryArn": "arn:aws:ecr:us-east-1:123456789012:repository/api",
        "repositoryName": "api",
        "repositoryUri": "123456789012.dkr.ecr.us-east-1.amazonaws.com/api",
        "imageScanningConfiguration": {"scanOnPush": True},
    }
    repository_two = {
        "repositoryArn": "arn:aws:ecr:us-east-1:123456789012:repository/web",
        "repositoryName": "web",
    }
    image = {
        "repositoryName": "api",
        "imageDigest": "sha256:abc",
        "imageTags": ["latest"],
        "imageSizeInBytes": 123,
        "imageScanStatus": {"status": "COMPLETE"},
        "imageScanFindingsSummary": {
            "findingSeverityCounts": {"HIGH": 2, "MEDIUM": 1},
            "findingCount": 3,
        },
        "imagePushedAt": "2026-08-02T12:00:00Z",
    }

    ecr = FakeAWSClient(
        pages={
            "describe_repositories": [
                {"repositories": [repository_one]},
                {"repositories": [repository_two]},
            ],
            "list_images": lambda kwargs: [
                {"imageIds": [{"imageDigest": "sha256:abc"}]}
                if kwargs["repositoryName"] == "api"
                else {"imageIds": []}
            ],
            "describe_images": [{"imageDetails": [image]}],
        }
    )

    ecs = FakeAWSClient(
        pages={
            "list_task_definitions": [{"taskDefinitionArns": [
                "arn:aws:ecs:us-east-1:123456789012:task-definition/api:7",
            ]}],
            "list_clusters": [{"clusterArns": [
                "arn:aws:ecs:us-east-1:123456789012:cluster/prod",
            ]}],
            "list_services": [{"serviceArns": [
                "arn:aws:ecs:us-east-1:123456789012:service/prod/api",
            ]}],
        },
        responses={
            "describe_task_definition": {
                "taskDefinition": {
                    "taskDefinitionArn": "arn:aws:ecs:us-east-1:123456789012:task-definition/api:7",
                    "family": "api",
                    "revision": 7,
                    "status": "ACTIVE",
                    "executionRoleArn": "arn:aws:iam::123456789012:role/ecsTaskExecutionRole",
                    "containerDefinitions": [{
                        "name": "api",
                        "image": "123456789012.dkr.ecr.us-east-1.amazonaws.com/api:latest",
                        "environment": [{"name": "PASSWORD", "value": "must-not-persist"}],
                        "secrets": [{"name": "TOKEN", "valueFrom": "arn:aws:secretsmanager:secret"}],
                        "portMappings": [{"containerPort": 8080, "protocol": "tcp"}],
                    }],
                }
            },
            "describe_services": {
                "services": [{
                    "serviceArn": "arn:aws:ecs:us-east-1:123456789012:service/prod/api",
                    "serviceName": "api",
                    "deployments": [{
                        "id": "ecs-svc/1234567890",
                        "status": "PRIMARY",
                        "rolloutState": "COMPLETED",
                        "desiredCount": 2,
                        "pendingCount": 0,
                        "runningCount": 2,
                        "failedTasks": 0,
                        "taskDefinition": "arn:aws:ecs:us-east-1:123456789012:task-definition/api:7",
                    }],
                }],
                "failures": [],
            },
        },
    )

    eks = FakeAWSClient(
        pages={
            "list_clusters": [{"clusters": ["prod"]}],
            "list_nodegroups": [{"nodegroups": ["blue"]}],
            "list_addons": [{"addons": ["vpc-cni"]}],
            "list_fargate_profiles": [{"fargateProfileNames": ["jobs"]}],
        },
        responses={
            "describe_cluster": {"cluster": {
                "name": "prod",
                "arn": "arn:aws:eks:us-east-1:123456789012:cluster/prod",
                "status": "ACTIVE",
                "version": "1.30",
            }},
            "describe_nodegroup": {"nodegroup": {
                "nodegroupName": "blue",
                "nodegroupArn": "arn:aws:eks:us-east-1:123456789012:nodegroup/prod/blue",
                "clusterName": "prod",
                "status": "ACTIVE",
                "scalingConfig": {"desiredSize": 2},
            }},
            "describe_addon": {"addon": {
                "addonName": "vpc-cni",
                "addonArn": "arn:aws:eks:us-east-1:123456789012:addon/prod/vpc-cni/1",
                "clusterName": "prod",
                "status": "DEGRADED",
            }},
            "describe_fargate_profile": {"fargateProfile": {
                "fargateProfileName": "jobs",
                "fargateProfileArn": "arn:aws:eks:us-east-1:123456789012:fargateprofile/prod/jobs",
                "clusterName": "prod",
                "status": "ACTIVE",
                "selectors": [{"namespace": "jobs"}],
            }},
        },
    )

    apprunner = FakeAWSClient(
        pages={
            "list_services": [{"ServiceSummaryList": [{
                "ServiceArn": "arn:aws:apprunner:us-east-1:123456789012:service/api/abc",
                "ServiceName": "api",
            }]}],
            "list_operations": [{"OperationSummaryList": [{
                "Id": "op-1",
                "Type": "START_DEPLOYMENT",
                "Status": "IN_PROGRESS",
            }]}],
        },
        responses={
            "describe_service": {"Service": {
                "ServiceArn": "arn:aws:apprunner:us-east-1:123456789012:service/api/abc",
                "ServiceName": "api",
                "Status": "RUNNING",
                "ServiceUrl": "api.us-east-1.awsapprunner.com",
                "SourceConfiguration": {
                    "AutoDeploymentsEnabled": True,
                    "ImageRepository": {
                        "ImageIdentifier": "api:latest",
                        "ImageRepositoryType": "ECR",
                        "ImageConfiguration": {
                            "Port": "8080",
                            "RuntimeEnvironmentVariables": {"PASSWORD": "must-not-persist"},
                            "RuntimeEnvironmentSecrets": {"TOKEN": "arn:aws:secretsmanager:secret"},
                        },
                    },
                },
            }},
            "describe_operation": {"Operation": {
                "Id": "op-1",
                "Type": "START_DEPLOYMENT",
                "Status": "SUCCEEDED",
                "TargetArn": "arn:aws:apprunner:us-east-1:123456789012:service/api/abc",
            }},
        },
    )

    return {"ecr": ecr, "ecs": ecs, "eks": eks, "apprunner": apprunner}


class AWSContainersInventoryTestCase(SimpleTestCase):
    def setUp(self):
        self.account = SimpleNamespace(
            access_key="access-key",
            secret_key="secret-key",
            region="us-east-1",
        )

    def test_inventory_covers_scans_deployments_eks_children_and_apprunner_operations(self):
        clients = _fixture_clients()
        reconciled = []

        def capture(model, account, region, asset_type, records):
            reconciled.append((asset_type, region, records))
            return len(records)

        def client_for(account, service, region=None):
            return clients[service]

        with patch.object(containers, "get_enabled_regions", return_value=["us-east-1"]), \
             patch.object(containers, "aws_client", side_effect=client_for), \
             patch.object(containers, "_reconcile", side_effect=capture):
            summary = containers.sync_aws_container_assets(self.account)

        self.assertFalse(summary["errors"])
        self.assertEqual(set(summary["synced"]), set(containers.AWS_CONTAINER_ASSET_TYPES))
        by_type = {asset_type: records for asset_type, _region, records in reconciled}

        image = by_type["aws_ecr_image"][0]
        self.assertEqual(image["metadata"]["imageScanStatus"]["status"], "COMPLETE")
        self.assertEqual(image["metadata"]["imageScanFindingsSummary"]["findingCount"], 3)

        deployment = by_type["aws_ecs_deployment"][0]
        self.assertEqual(deployment["metadata"]["rolloutState"], "COMPLETED")
        self.assertEqual(deployment["metadata"]["runningCount"], 2)

        self.assertEqual(by_type["aws_eks_node_group"][0]["metadata"]["nodegroupName"], "blue")
        self.assertEqual(by_type["aws_eks_addon"][0]["metadata"]["status"], "DEGRADED")
        self.assertEqual(by_type["aws_eks_fargate_profile"][0]["metadata"]["status"], "ACTIVE")
        self.assertEqual(by_type["aws_apprunner_deployment"][0]["metadata"]["Id"], "op-1")

        task_metadata = by_type["aws_ecs_task_definition"][0]["metadata"]
        self.assertNotIn("environment", str(task_metadata).lower())
        self.assertNotIn("secrets", str(task_metadata).lower())

        for client in clients.values():
            mutation_calls = [
                operation for operation, _kwargs in client.calls
                if operation.startswith(FakeAWSClient.MUTATING_PREFIXES)
            ]
            self.assertEqual(mutation_calls, [])

    def test_pagination_and_partial_region_safety(self):
        good_clients = {service: _empty_client(service) for service in ("ecr", "ecs", "eks", "apprunner")}
        broken_ecr = FakeAWSClient(
            pages={
                "describe_repositories": [{}],
            }
        )
        region_clients = {
            ("us-east-1", "ecr"): broken_ecr,
            **{("us-east-1", service): good_clients[service] for service in ("ecs", "eks", "apprunner")},
            **{("us-west-2", service): good_clients[service] for service in ("ecr", "ecs", "eks", "apprunner")},
        }
        reconciled = []

        def client_for(account, service, region=None):
            return region_clients[(region, service)]

        def capture(model, account, region, asset_type, records):
            reconciled.append((asset_type, region, records))
            return len(records)

        with patch.object(containers, "get_enabled_regions", return_value=["us-east-1", "us-west-2"]), \
             patch.object(containers, "aws_client", side_effect=client_for), \
             patch.object(containers, "_reconcile", side_effect=capture):
            summary = containers.sync_aws_container_assets(self.account)

        self.assertTrue(any(
            error["region"] == "us-east-1" and error["asset_type"] == "aws_ecr_repository"
            for error in summary["errors"]
        ))
        self.assertNotIn(
            ("aws_ecr_repository", "us-east-1"),
            {(asset_type, region) for asset_type, region, _records in reconciled},
        )
        self.assertIn(
            ("aws_ecr_repository", "us-west-2"),
            {(asset_type, region) for asset_type, region, _records in reconciled},
        )

    def test_status_checks_normalize_states_and_keep_scan_metadata(self):
        image_client = FakeAWSClient(responses={
            "describe_images": {"imageDetails": [{
                "imageDigest": "sha256:abc",
                "imageScanStatus": {"status": "COMPLETE"},
                "imageScanFindingsSummary": {"findingSeverityCounts": {"HIGH": 1}},
                "environment": "must-not-return",
            }]}
        })
        node_client = FakeAWSClient(responses={
            "describe_nodegroup": {"nodegroup": {
                "nodegroupName": "blue",
                "clusterName": "prod",
                "status": "CREATING",
            }}
        })
        operation_client = FakeAWSClient(responses={
            "describe_operation": {"Operation": {
                "Id": "op-1",
                "Status": "FAILED",
                "Type": "START_DEPLOYMENT",
            }}
        })
        credentials = {
            "access_key": "access-key",
            "secret_key": "secret-key",
            "region": "us-east-1",
            "metadata": {
                "_cloudmoo_repository_name": "api",
                "_cloudmoo_image_digest": "sha256:abc",
                "_cloudmoo_cluster_name": "prod",
                "_cloudmoo_nodegroup_name": "blue",
                "_cloudmoo_service_arn": "arn:service",
                "_cloudmoo_operation_id": "op-1",
            },
        }

        def client_for(account, service, region=None):
            return {"ecr": image_client, "eks": node_client, "apprunner": operation_client}[service]

        with patch.object(checks, "aws_client", side_effect=client_for):
            image_status, image_metadata = checks.check_aws_aws_ecr_image_status(
                "aws_ecr_image:us-east-1:api:sha256:abc",
                credentials,
            )
            node_status, _node_metadata = checks.check_aws_aws_eks_node_group_status(
                "aws_eks_node_group:us-east-1:prod:blue",
                credentials,
            )
            operation_status, _operation_metadata = checks.check_aws_aws_apprunner_deployment_status(
                "aws_apprunner_deployment:us-east-1:arn:service:op-1",
                credentials,
            )

        self.assertEqual(image_status, "active")
        self.assertEqual(image_metadata["aws_ecr_image"]["imageScanFindingsSummary"]["findingSeverityCounts"]["HIGH"], 1)
        self.assertNotIn("environment", str(image_metadata).lower())
        self.assertEqual(node_status, "provisioning")
        self.assertEqual(operation_status, "failed")

    def test_provider_not_found_is_distinguished_from_malformed_response(self):
        client = FakeAWSClient(responses={
            "describe_repositories": {"repositories": []},
        })
        credentials = {
            "access_key": "access-key",
            "secret_key": "secret-key",
            "region": "us-east-1",
            "metadata": {"_cloudmoo_repository_name": "missing"},
        }
        with patch.object(checks, "aws_client", return_value=client):
            status, metadata = checks.check_aws_aws_ecr_repository_status("id", credentials)

        self.assertEqual(status, "not_found")
        self.assertEqual(metadata, {"errorCode": "ResourceNotFound"})
