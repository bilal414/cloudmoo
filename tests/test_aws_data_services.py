from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from botocore.exceptions import ClientError
from django.test import SimpleTestCase

from apps.console.cloud.aws import data_services
from apps.console.cloud.aws.data_services import (
    AWS_DATA_SERVICE_ASSET_MODELS,
    AWS_DATA_SERVICE_ASSET_TYPES,
    AWS_EFS_FILE_SYSTEM,
    AWS_ELASTICACHE_CLUSTER,
    AWS_ELASTICACHE_REPLICATION_GROUP,
    AWS_ELASTICACHE_SERVERLESS_CACHE,
    AWS_FSX_FILE_SYSTEM,
    AWS_MEMORYDB_CLUSTER,
    AWS_OPENSEARCH_DOMAIN,
    AWS_RDS_CLUSTER,
    CoreAWSAccount,
    CoreAWSRDSCluster,
    EFS_MOUNT_TARGETS_SUPPORTED,
    ELASTICACHE_SERVERLESS_SUPPORTED,
    normalize_data_service_status,
    sync_aws_data_service_assets,
)
from apps.console.cloud.models import CloudInventoryTransientError
from apps.console.utils.models import UtilAsset
from apps.monitoring.checks import aws_data_services as data_service_checks


class FakePaginator:
    def __init__(self, client, operation):
        self.client = client
        self.operation = operation

    def paginate(self, **kwargs):
        self.client.calls.append((self.operation, kwargs))
        pages = self.client.pages[self.operation]
        if isinstance(pages, BaseException):
            raise pages
        if callable(pages):
            pages = pages(**kwargs)
        return iter(pages)


class ReadOnlyFakeClient:
    """A fake that fails loudly if an implementation tries an AWS mutation."""

    MUTATION_PREFIXES = (
        "abort",
        "accept",
        "allocate",
        "associate",
        "attach",
        "authorize",
        "cancel",
        "change",
        "complete",
        "copy",
        "create",
        "delete",
        "deregister",
        "detach",
        "disable",
        "disassociate",
        "enable",
        "execute",
        "import",
        "invite",
        "modify",
        "publish",
        "put",
        "reboot",
        "register",
        "release",
        "remove",
        "replace",
        "reset",
        "restore",
        "resume",
        "revoke",
        "run",
        "send",
        "set",
        "start",
        "stop",
        "suspend",
        "tag",
        "terminate",
        "untag",
        "update",
        "upload",
    )

    def __init__(self, *, pages=None, responses=None):
        self.pages = dict(pages or {})
        self.responses = dict(responses or {})
        self.calls = []

    def get_paginator(self, operation):
        if operation not in self.pages:
            raise AttributeError(operation)
        return FakePaginator(self, operation)

    def __getattr__(self, operation):
        if operation.startswith(self.MUTATION_PREFIXES):
            raise AssertionError(f"mutating AWS operation attempted: {operation}")
        if operation not in self.responses and operation not in self.pages:
            raise AttributeError(operation)

        def call(**kwargs):
            self.calls.append((operation, kwargs))
            value = self.responses.get(operation)
            if value is None:
                pages = self.pages[operation]
                if callable(pages):
                    pages = pages(**kwargs)
                return next(iter(pages))
            if callable(value):
                return value(**kwargs)
            if isinstance(value, BaseException):
                raise value
            return value

        return call


class FakeAsset:
    def __init__(self, owner, region, unique_id, defaults):
        self.owner = owner
        self.region = region
        self.unique_id = unique_id
        self.name = defaults.get("name", unique_id)
        self.type = defaults.get("type")
        self.metadata = defaults.get("metadata", {})
        self.monitoring = defaults.get("monitoring", UtilAsset.Monitoring.ACTIVE)
        self.save_count = 0

    def save(self):
        self.save_count += 1


class FakeQuerySet:
    def __init__(self, manager, rows):
        self.manager = manager
        self.rows = list(rows)

    def exclude(self, **kwargs):
        excluded = set(kwargs.get("unique_id__in", []))
        return FakeQuerySet(
            self.manager,
            [row for row in self.rows if row.unique_id not in excluded],
        )

    def update(self, **kwargs):
        self.manager.update_calls.append((self.rows[:], kwargs))
        for row in self.rows:
            for key, value in kwargs.items():
                setattr(row, key, value)
        return len(self.rows)


class FakeManager:
    def __init__(self, owner, rows=()):
        self.owner = owner
        self.rows = list(rows)
        self.filter_calls = []
        self.update_calls = []

    def get_or_create(self, *, owner, region, unique_id, defaults):
        for row in self.rows:
            if row.owner is owner and row.region == region and row.unique_id == unique_id:
                return row, False
        row = FakeAsset(owner, region, unique_id, defaults)
        self.rows.append(row)
        return row, True

    def filter(self, *, owner, region):
        self.filter_calls.append((owner, region))
        return FakeQuerySet(
            self,
            [row for row in self.rows if row.owner is owner and row.region == region],
        )


def _resource_for_region(region, prefix):
    return f"{prefix}-{region.replace('-', '')}"


def _fixture_client(region, *, empty=False):
    if empty:
        return {
            "rds": ReadOnlyFakeClient(
                pages={"describe_db_clusters": [{"DBClusters": []}]}
            ),
            "elasticache": ReadOnlyFakeClient(
                pages={
                    "describe_cache_clusters": [{"CacheClusters": []}],
                    "describe_replication_groups": [{"ReplicationGroups": []}],
                    **(
                        {"describe_serverless_caches": [{"ServerlessCaches": []}]}
                        if ELASTICACHE_SERVERLESS_SUPPORTED
                        else {}
                    ),
                }
            ),
            "memorydb": ReadOnlyFakeClient(
                pages={"describe_clusters": [{"Clusters": []}]}
            ),
            "opensearch": ReadOnlyFakeClient(
                pages={"list_domain_names": [{"DomainNames": []}]}
            ),
            "efs": ReadOnlyFakeClient(
                pages={"describe_file_systems": [{"FileSystems": []}]}
            ),
            "fsx": ReadOnlyFakeClient(
                pages={"describe_file_systems": [{"FileSystems": []}]}
            ),
        }

    cluster_id = _resource_for_region(region, "aurora")
    cache_id = _resource_for_region(region, "cache")
    replication_id = _resource_for_region(region, "replication")
    serverless_id = _resource_for_region(region, "serverless")
    memorydb_id = _resource_for_region(region, "memorydb")
    domain_id = _resource_for_region(region, "search")
    efs_id = _resource_for_region(region, "fs")
    fsx_id = _resource_for_region(region, "fsx")

    rds = {
        "DBClusterIdentifier": cluster_id,
        "DBClusterArn": f"arn:aws:rds:{region}:123456789012:cluster:{cluster_id}",
        "Status": "available",
        "Engine": "aurora-postgresql",
        "Endpoint": f"{cluster_id}.example.rds.amazonaws.com",
        "MasterUserSecret": {"SecretArn": "must-not-persist"},
        "TagList": [
            {"Key": "Environment", "Value": "test"},
            {"Key": "Password", "Value": "do-not-persist"},
        ],
    }
    cache = {
        "CacheClusterId": cache_id,
        "ARN": f"arn:aws:elasticache:{region}:123456789012:cluster:{cache_id}",
        "CacheClusterStatus": "available",
        "Engine": "redis",
        "CacheNodeType": "cache.t4g.small",
        "AuthToken": "cache-secret-must-not-persist",
        "CacheNodes": [{"CacheNodeId": "0001", "Endpoint": {"Address": "cache.example"}}],
    }
    replication = {
        "ReplicationGroupId": replication_id,
        "ARN": f"arn:aws:elasticache:{region}:123456789012:replicationgroup:{replication_id}",
        "Status": "degraded",
        "Description": "read-only fixture",
        "AuthToken": "replication-secret-must-not-persist",
        "NodeGroups": [{"NodeGroupId": "0001", "Status": "available"}],
    }
    serverless = {
        "ServerlessCacheName": serverless_id,
        "ServerlessCacheArn": f"arn:aws:elasticache:{region}:123456789012:serverlesscache:{serverless_id}",
        "Status": "creating",
        "Engine": "redis",
        "Endpoint": {"Address": "serverless.example"},
        "AuthToken": "serverless-secret-must-not-persist",
    }
    memorydb = {
        "Name": memorydb_id,
        "ARN": f"arn:aws:memorydb:{region}:123456789012:cluster/{memorydb_id}",
        "Status": "active",
        "Engine": "redis",
        "TLSEnabled": True,
    }
    domain = {
        "DomainId": f"123456789012/{domain_id}",
        "DomainName": domain_id,
        "ARN": f"arn:aws:es:{region}:123456789012:domain/{domain_id}",
        "EngineVersion": "OpenSearch_2.11",
        "Processing": False,
        "Deleted": False,
        "AccessPolicies": "secret-policy-text-must-not-persist",
        "Tags": [{"Key": "Password", "Value": "do-not-persist"}],
    }
    efs = {
        "FileSystemId": efs_id,
        "FileSystemArn": f"arn:aws:elasticfilesystem:{region}:123456789012:file-system/{efs_id}",
        "Name": "shared-data",
        "LifeCycleState": "available",
        "Encrypted": True,
        "NumberOfMountTargets": 1,
        "PerformanceMode": "generalPurpose",
        "ThroughputMode": "bursting",
        "Tags": [{"Key": "Environment", "Value": "test"}],
    }
    mount_target = {
        "MountTargetId": f"mt-{efs_id}",
        "FileSystemId": efs_id,
        "SubnetId": "subnet-123",
        "LifeCycleState": "available",
        "IpAddress": "10.0.1.20",
        "SecretToken": "mount-target-secret-must-not-persist",
    }
    fsx = {
        "FileSystemId": fsx_id,
        "ResourceARN": f"arn:aws:fsx:{region}:123456789012:file-system/{fsx_id}",
        "FileSystemType": "WINDOWS",
        "Lifecycle": "AVAILABLE",
        "StorageCapacity": 32,
        "WindowsConfiguration": {
            "DeploymentType": "MULTI_AZ_1",
            "SelfManagedActiveDirectoryConfiguration": {
                "UserName": "readonly-user",
                "Password": "fsx-password-must-not-persist",
            },
        },
    }

    responses = {
        "describe_domain": lambda DomainName: {"DomainStatus": domain},
    }
    pages = {
        "describe_db_clusters": [{"DBClusters": [rds]}, {"DBClusters": []}],
        "describe_cache_clusters": [{"CacheClusters": [cache]}, {"CacheClusters": []}],
        "describe_replication_groups": [{"ReplicationGroups": [replication]}],
        "describe_clusters": [{"Clusters": [memorydb]}],
        "list_domain_names": [{"DomainNames": [{"DomainName": domain_id}]}, {"DomainNames": []}],
        "describe_file_systems": [{"FileSystems": [efs]}],
        "describe_mount_targets": [{"MountTargets": [mount_target]}],
    }
    if ELASTICACHE_SERVERLESS_SUPPORTED:
        pages["describe_serverless_caches"] = [{"ServerlessCaches": [serverless]}]

    # FSx has a separate client from EFS, so its same-named operation is
    # supplied by the service-specific factory below.
    return {
        "rds": ReadOnlyFakeClient(pages={"describe_db_clusters": pages["describe_db_clusters"]}),
        "elasticache": ReadOnlyFakeClient(
            pages={
                key: value
                for key, value in pages.items()
                if key in {
                    "describe_cache_clusters",
                    "describe_replication_groups",
                    "describe_serverless_caches",
                }
            }
        ),
        "memorydb": ReadOnlyFakeClient(pages={"describe_clusters": pages["describe_clusters"]}),
        "opensearch": ReadOnlyFakeClient(
            pages={"list_domain_names": pages["list_domain_names"]},
            responses=responses,
        ),
        "efs": ReadOnlyFakeClient(
            pages={
                "describe_file_systems": pages["describe_file_systems"],
                "describe_mount_targets": pages["describe_mount_targets"],
            }
        ),
        "fsx": ReadOnlyFakeClient(pages={"describe_file_systems": [{"FileSystems": [fsx]}]}),
    }


def _empty_service_clients(region):
    return _fixture_client(region, empty=True)


def _credentials(provider_id):
    return {
        "access_key": "test-access-key",
        "secret_key": "test-secret-key",
        "region": "us-east-1",
        "resource_region": "us-east-1",
        "provider_id": provider_id,
        "metadata": {"_cloudmoo_provider_id": provider_id},
    }


class AWSDataServiceInventoryTests(SimpleTestCase):
    def setUp(self):
        self.account = SimpleNamespace(
            access_key="test-access-key",
            secret_key="test-secret-key",
            region="us-east-1",
        )

    def test_all_families_are_paginated_regional_and_read_only(self):
        clients = {}
        factory_calls = []

        def client_factory(_account, service, region=None):
            factory_calls.append((service, region))
            key = (service, region)
            clients.setdefault(key, _fixture_client(region)[service])
            return clients[key]

        reconciled = []

        def capture(model, account, region, asset_type, records):
            reconciled.append((model, account, region, asset_type, records))
            return len(records)

        with patch.object(data_services, "get_enabled_regions", return_value=[
            "us-west-2",
            "us-east-1",
            "us-east-1",
        ]), patch.object(data_services, "aws_client", side_effect=client_factory), patch.object(
            data_services, "_reconcile", side_effect=capture
        ):
            summary = sync_aws_data_service_assets(self.account)

        self.assertEqual(summary["regions"], ["us-east-1", "us-west-2"])
        self.assertEqual(summary["errors"], [])
        self.assertEqual(set(summary["counts"]), set(AWS_DATA_SERVICE_ASSET_TYPES))
        self.assertEqual(
            {(region, asset_type) for _model, _account, region, asset_type, _records in reconciled},
            {
                (region, asset_type)
                for region in ("us-east-1", "us-west-2")
                for asset_type in AWS_DATA_SERVICE_ASSET_TYPES
            },
        )
        self.assertEqual(
            set(factory_calls),
            {
                (service, region)
                for service in ("rds", "elasticache", "memorydb", "opensearch", "efs", "fsx")
                for region in ("us-east-1", "us-west-2")
            },
        )

        records_by_type = {}
        for _model, _account, region, asset_type, records in reconciled:
            records_by_type.setdefault(asset_type, []).append((region, records[0]))

        expected_statuses = {
            AWS_RDS_CLUSTER: "available",
            AWS_ELASTICACHE_CLUSTER: "available",
            AWS_ELASTICACHE_REPLICATION_GROUP: "degraded",
            AWS_MEMORYDB_CLUSTER: "active",
            AWS_OPENSEARCH_DOMAIN: "active",
            AWS_EFS_FILE_SYSTEM: "available",
            AWS_FSX_FILE_SYSTEM: "available",
        }
        if ELASTICACHE_SERVERLESS_SUPPORTED:
            expected_statuses[AWS_ELASTICACHE_SERVERLESS_CACHE] = "pending"

        for asset_type, expected_status in expected_statuses.items():
            with self.subTest(asset_type=asset_type):
                record = records_by_type[asset_type][0][1]
                self.assertEqual(record["status"], expected_status)
                self.assertEqual(record["metadata"]["normalized_status"], expected_status)
                self.assertEqual(record["metadata"]["provider_id"], record["provider_id"])

        rds_record = records_by_type[AWS_RDS_CLUSTER][0][1]
        self.assertEqual(
            rds_record["unique_id"],
            "aws_rds_cluster:us-east-1:aurora-useast1",
        )
        self.assertIn("arn:aws:rds:us-east-1", rds_record["metadata"]["_cloudmoo_raw_id"])
        self.assertNotIn("must-not-persist", str(rds_record["metadata"]))
        self.assertEqual(
            rds_record["metadata"]["TagList"][1]["Value"],
            "[REDACTED]",
        )

        efs_record = records_by_type[AWS_EFS_FILE_SYSTEM][0][1]
        self.assertEqual(efs_record["metadata"]["mount_targets"][0]["MountTargetId"], "mt-fs-useast1")
        self.assertNotIn("mount-target-secret", str(efs_record["metadata"]))

        opensearch_record = records_by_type[AWS_OPENSEARCH_DOMAIN][0][1]
        self.assertNotIn("AccessPolicies", opensearch_record["metadata"])
        self.assertNotIn("do-not-persist", str(opensearch_record["metadata"]))

        for client in clients.values():
            self.assertFalse(
                [
                    operation
                    for operation, _kwargs in client.calls
                    if operation.startswith(ReadOnlyFakeClient.MUTATION_PREFIXES)
                ]
            )

    def test_incomplete_region_is_not_reconciled_and_does_not_break_other_regions(self):
        managers = {}
        old_id = "aws_rds_cluster:us-west-2:old-cluster"
        old = FakeAsset(
            self.account,
            "us-west-2",
            old_id,
            {
                "name": "old-cluster",
                "type": AWS_RDS_CLUSTER,
                "monitoring": UtilAsset.Monitoring.ACTIVE,
            },
        )
        for asset_type, model in AWS_DATA_SERVICE_ASSET_MODELS.items():
            managers[asset_type] = FakeManager(
                self.account,
                [old] if asset_type == AWS_RDS_CLUSTER else [],
            )

        clients = {}

        def client_factory(_account, service, region=None):
            key = (service, region)
            if key not in clients:
                if service == "rds" and region == "us-west-2":
                    clients[key] = ReadOnlyFakeClient(pages={"describe_db_clusters": [{}]})
                else:
                    clients[key] = _fixture_client(region, empty=True)[service]
            return clients[key]

        with ExitStack() as stack:
            stack.enter_context(patch.object(data_services, "get_enabled_regions", return_value=[
                "us-east-1",
                "us-west-2",
            ]))
            stack.enter_context(patch.object(data_services, "aws_client", side_effect=client_factory))
            for asset_type, model in AWS_DATA_SERVICE_ASSET_MODELS.items():
                stack.enter_context(patch.object(model, "objects", managers[asset_type]))
            summary = sync_aws_data_service_assets(self.account)

        self.assertEqual(summary["counts"][AWS_RDS_CLUSTER], None)
        self.assertTrue(any(
            error["region"] == "us-west-2" and error["asset_type"] == AWS_RDS_CLUSTER
            for error in summary["errors"]
        ))
        self.assertEqual(old.monitoring, UtilAsset.Monitoring.ACTIVE)
        self.assertNotIn((self.account, "us-west-2"), managers[AWS_RDS_CLUSTER].filter_calls)
        self.assertIn((self.account, "us-east-1"), managers[AWS_RDS_CLUSTER].filter_calls)

    def test_pagination_safety_bound_prevents_reconciliation(self):
        clients = {}
        reconciled = []

        def client_factory(_account, service, region=None):
            key = (service, region)
            if key not in clients:
                clients[key] = _fixture_client(region, empty=True)[service]
            if service == "rds":
                clients[key] = ReadOnlyFakeClient(
                    pages={
                        "describe_db_clusters": [
                            {"DBClusters": []},
                            {"DBClusters": []},
                        ]
                    }
                )
            return clients[key]

        def capture(_model, _account, _region, asset_type, records):
            reconciled.append((asset_type, records))
            return len(records)

        with patch.object(data_services, "get_enabled_regions", return_value=["us-east-1"]), patch.object(
            data_services, "aws_client", side_effect=client_factory
        ), patch.object(data_services, "_reconcile", side_effect=capture), patch.object(
            data_services, "MAX_COLLECTION_PAGES", 1
        ):
            summary = sync_aws_data_service_assets(self.account)

        self.assertTrue(any(error["asset_type"] == AWS_RDS_CLUSTER for error in summary["errors"]))
        self.assertNotIn(AWS_RDS_CLUSTER, {asset_type for asset_type, _records in reconciled})

    def test_client_failure_in_one_region_keeps_aggregate_fail_closed(self):
        clients = {}
        reconciled = []

        def client_factory(_account, service, region=None):
            if service == "rds" and region == "us-west-2":
                raise RuntimeError("regional provider outage")
            key = (service, region)
            clients.setdefault(key, _fixture_client(region, empty=True)[service])
            return clients[key]

        def capture(_model, _account, region, asset_type, records):
            reconciled.append((region, asset_type, records))
            return len(records)

        with patch.object(data_services, "get_enabled_regions", return_value=[
            "us-east-1",
            "us-west-2",
        ]), patch.object(data_services, "aws_client", side_effect=client_factory), patch.object(
            data_services, "_reconcile", side_effect=capture
        ):
            summary = sync_aws_data_service_assets(self.account)

        self.assertIsNone(summary["counts"][AWS_RDS_CLUSTER])
        self.assertFalse(summary["families"][AWS_RDS_CLUSTER]["complete"])
        self.assertIn(
            ("us-east-1", AWS_RDS_CLUSTER, []),
            reconciled,
        )
        self.assertNotIn(
            ("us-west-2", AWS_RDS_CLUSTER, []),
            reconciled,
        )
        self.assertTrue(any(
            error["region"] == "us-west-2" and error["asset_type"] == AWS_RDS_CLUSTER
            for error in summary["errors"]
        ))

    def test_model_contracts_keep_credentials_out_of_metadata(self):
        owner = CoreAWSAccount(
            access_key="ACCESS-KEY",
            secret_key="SECRET-KEY",
            region="us-east-1",
        )
        asset = CoreAWSRDSCluster(
            owner=owner,
            region="eu-west-1",
            unique_id="aws_rds_cluster:eu-west-1:cluster-1",
            name="cluster-1",
            metadata={
                "_cloudmoo_provider_id": "cluster-1",
                "normalized_status": "available",
            },
        )

        self.assertEqual(set(AWS_DATA_SERVICE_ASSET_MODELS), set(AWS_DATA_SERVICE_ASSET_TYPES))
        for asset_type, model in AWS_DATA_SERVICE_ASSET_MODELS.items():
            with self.subTest(asset_type=asset_type):
                self.assertEqual(model.asset_type, asset_type)
                self.assertIs(model._meta.get_field("owner").remote_field.model, CoreAWSAccount)
                self.assertIsNotNone(model._meta.get_field("region"))

        credentials = asset.monitoring_credentials
        self.assertEqual(credentials["provider_id"], "cluster-1")
        self.assertEqual(credentials["access_key"], "ACCESS-KEY")
        self.assertEqual(credentials["secret_key"], "SECRET-KEY")
        self.assertNotIn("access_key", asset.metadata)
        self.assertNotIn("secret_key", asset.metadata)
        self.assertIn("eu-west-1", asset.provider_url)
        self.assertIn("cluster-1", asset.provider_url)

        with patch.dict(
            data_service_checks.AWS_DATA_SERVICE_STATUS_CHECKS,
            {AWS_RDS_CLUSTER: lambda _unique_id, _credentials: ("available", {"ok": True})},
            clear=False,
        ):
            self.assertEqual(asset.check_status(), ("available", {"ok": True}))


class AWSDataServiceStatusTests(SimpleTestCase):
    def test_status_normalization_covers_the_monitoring_vocabulary(self):
        expected = {
            "available": "available",
            "ACTIVE": "active",
            "degraded": "degraded",
            "in-progress": "pending",
            "CREATING": "pending",
            "failed": "failed",
            "deleted": "not_found",
            "not found": "not_found",
            "provider-added-state": "error",
            None: "error",
        }
        for raw, normalized in expected.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_data_service_status(raw), normalized)

    def test_all_status_checks_are_read_only_and_return_safe_provider_metadata(self):
        clients = _fixture_client("us-east-1")
        provider_ids = {
            AWS_RDS_CLUSTER: "aurora-useast1",
            AWS_ELASTICACHE_CLUSTER: "cache-useast1",
            AWS_ELASTICACHE_REPLICATION_GROUP: "replication-useast1",
            AWS_MEMORYDB_CLUSTER: "memorydb-useast1",
            AWS_OPENSEARCH_DOMAIN: "search-useast1",
            AWS_EFS_FILE_SYSTEM: "fs-useast1",
            AWS_FSX_FILE_SYSTEM: "fsx-useast1",
        }
        if ELASTICACHE_SERVERLESS_SUPPORTED:
            provider_ids[AWS_ELASTICACHE_SERVERLESS_CACHE] = "serverless-useast1"
        expected = {
            AWS_RDS_CLUSTER: "available",
            AWS_ELASTICACHE_CLUSTER: "available",
            AWS_ELASTICACHE_REPLICATION_GROUP: "degraded",
            AWS_MEMORYDB_CLUSTER: "active",
            AWS_OPENSEARCH_DOMAIN: "active",
            AWS_EFS_FILE_SYSTEM: "available",
            AWS_FSX_FILE_SYSTEM: "available",
        }
        if ELASTICACHE_SERVERLESS_SUPPORTED:
            expected[AWS_ELASTICACHE_SERVERLESS_CACHE] = "pending"

        def client_factory(_account, service, region=None):
            return clients[service]

        with patch.object(data_service_checks, "aws_client", side_effect=client_factory):
            for asset_type, check in data_service_checks.AWS_DATA_SERVICE_STATUS_CHECKS.items():
                with self.subTest(asset_type=asset_type):
                    provider_id = provider_ids[asset_type]
                    status, payload = check(
                        f"{asset_type}:us-east-1:{provider_id}",
                        _credentials(provider_id),
                    )
                    self.assertEqual(status, expected[asset_type])
                    self.assertEqual(payload[asset_type]["provider_id"], provider_id)
                    self.assertNotIn("AuthToken", payload[asset_type])
                    self.assertNotIn("must-not-persist", str(payload))

        for client in clients.values():
            self.assertFalse(
                [
                    operation
                    for operation, _kwargs in client.calls
                    if operation.startswith(ReadOnlyFakeClient.MUTATION_PREFIXES)
                ]
            )

    def test_not_found_errors_are_normalized_without_provider_payloads(self):
        not_found = ClientError(
            {
                "Error": {
                    "Code": "DBClusterNotFoundFault",
                    "Message": "secret-token=must-not-return",
                }
            },
            "DescribeDBClusters",
        )
        client = ReadOnlyFakeClient(
            pages={"describe_db_clusters": not_found},
        )

        with patch.object(data_service_checks, "aws_client", return_value=client):
            status, metadata = data_service_checks.check_aws_rds_cluster_status(
                "aws_rds_cluster:us-east-1:missing",
                _credentials("missing"),
            )

        self.assertEqual(status, "not_found")
        self.assertEqual(metadata, {"error_code": "DBClusterNotFoundFault"})
        self.assertNotIn("secret-token", str(metadata))

    def test_invalid_credentials_fail_closed_as_error(self):
        status, metadata = data_service_checks.check_aws_rds_cluster_status(
            "aws_rds_cluster:us-east-1:cluster",
            {"region": "us-east-1"},
        )
        self.assertEqual(status, "error")
        self.assertEqual(metadata, {"error_code": "invalid_response"})

    def test_shared_discovery_rejects_mutating_operation_names(self):
        client = ReadOnlyFakeClient()
        with self.assertRaises(ValueError):
            list(data_services.iter_pages(client, "delete_db_cluster"))
        self.assertEqual(client.calls, [])
