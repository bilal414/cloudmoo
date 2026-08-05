from types import SimpleNamespace
from unittest.mock import patch

from botocore.exceptions import ClientError
from django.test import SimpleTestCase

from apps.console.cloud.aws import backup
from apps.console.cloud.aws.backup import (
    AWS_BACKUP_COPY_JOB,
    AWS_BACKUP_JOB,
    AWS_BACKUP_PLAN,
    AWS_BACKUP_RECOVERY_POINT,
    AWS_BACKUP_VAULT,
    sync_aws_backup_assets,
    sync_aws_snapshots,
)
from apps.console.utils.models import UtilAsset
from apps.monitoring.checks import aws_backup


def _aws_error(code):
    return ClientError(
        {"Error": {"Code": code, "Message": "test provider error"}},
        "MockOperation",
    )


class MemoryAsset:
    def __init__(self, owner, unique_id, defaults):
        self.owner = owner
        self.unique_id = unique_id
        self.__dict__.update(defaults)
        self.metadata = self.__dict__.get("metadata") or {}
        self.monitoring = self.__dict__.get("monitoring", UtilAsset.Monitoring.ACTIVE)

    def save(self, **_kwargs):
        return None


class MemoryManager:
    def __init__(self):
        self.rows = []

    def get_or_create(self, owner, unique_id, defaults):
        for row in self.rows:
            if row.owner is owner and row.unique_id == unique_id:
                return row, False
        row = MemoryAsset(owner, unique_id, defaults)
        self.rows.append(row)
        return row, True

    def filter(self, owner=None, region=None):
        return [
            row
            for row in self.rows
            if (owner is None or row.owner is owner)
            and (region is None or getattr(row, "region", None) == region)
        ]


def _memory_model():
    return SimpleNamespace(
        objects=MemoryManager(),
        Type=SimpleNamespace(SNAPSHOT=UtilAsset.Type.SNAPSHOT),
    )


class FakeAWSClient:
    """Only exposes read APIs used by the adapter; any mutation is a failure."""

    READ_OPERATIONS = {
        "describe_backup_vault",
        "get_backup_plan",
        "get_backup_selection",
        "describe_backup_job",
        "describe_copy_job",
        "describe_recovery_point",
        "describe_snapshots",
        "describe_db_snapshots",
        "describe_db_cluster_snapshots",
    }

    def __init__(self, region):
        self.region = region
        self.calls = []

    def _detail(self, operation, **kwargs):
        self.calls.append((operation, kwargs))
        if operation == "describe_backup_vault":
            return {
                "BackupVaultName": kwargs["BackupVaultName"],
                "VaultState": "AVAILABLE",
                "MinRetentionDays": 7,
                "MaxRetentionDays": 30,
            }
        if operation == "get_backup_plan":
            return {
                "BackupPlan": {
                    "BackupPlanName": "nightly",
                    "Rules": [{"RuleName": "daily", "ScheduleExpression": "cron(0 0 * * ? *)"}],
                }
            }
        if operation == "get_backup_selection":
            return {
                "BackupSelection": {
                    "SelectionName": "all-volumes",
                    "Resources": ["arn:aws:ec2:*:*:volume/*"],
                    "IamRoleArn": "arn:aws:iam::123456789012:role/backup",
                }
            }
        if operation == "describe_backup_job":
            return {"BackupJob": {"BackupJobId": kwargs["BackupJobId"], "State": "COMPLETED"}}
        if operation == "describe_copy_job":
            return {"CopyJob": {"CopyJobId": kwargs["CopyJobId"], "State": "RUNNING"}}
        raise AssertionError(f"Unexpected detail operation: {operation}")

    def __getattr__(self, operation):
        if operation not in self.READ_OPERATIONS:
            raise AssertionError(f"Unexpected mutation or unsupported operation: {operation}")
        return lambda **kwargs: self._detail(operation, **kwargs)


class BackupPaginationClient(FakeAWSClient):
    def __init__(self, region):
        super().__init__(region)
        self.pages = {
            "list_backup_vaults": [
                {"BackupVaultList": [{"BackupVaultName": "vault-one"}], "NextToken": "vault-page-2"},
                {"BackupVaultList": [{"BackupVaultName": "vault-two"}]},
            ],
            "list_recovery_points_by_backup_vault": {
                "vault-one": [
                    {"RecoveryPoints": [{"RecoveryPointArn": "arn:recovery:one", "Status": "COMPLETED"}], "NextToken": "rp-2"},
                    {"RecoveryPoints": [{"RecoveryPointArn": "arn:recovery:two", "Status": "CREATING"}]},
                ],
                "vault-two": [_aws_error("AccessDeniedException")],
            },
            "list_backup_plans": [
                {"BackupPlansList": [{"BackupPlanId": "plan-one", "BackupPlanName": "nightly"}], "NextToken": "plan-page-2"},
                {"BackupPlansList": []},
            ],
            "list_backup_selections": [{"BackupSelectionsList": [{"SelectionId": "selection-one"}]}],
            "list_backup_jobs": [
                {"BackupJobs": [{"BackupJobId": "job-one", "State": "COMPLETED", "ResourceType": "EBS"}]},
            ],
            "list_copy_jobs": [
                {"CopyJobs": [{"CopyJobId": "copy-one", "State": "RUNNING"}]},
            ],
            "describe_snapshots": [{"Snapshots": [{"SnapshotId": "snap-one", "State": "completed"}]}],
            "describe_db_snapshots": [{"DBSnapshots": [{"DBSnapshotIdentifier": "db-one", "Status": "available"}]}],
            "describe_db_cluster_snapshots": [{"DBClusterSnapshots": [{"DBClusterSnapshotIdentifier": "cluster-one", "Status": "creating"}]}],
        }

    def page_stream(self, operation, collection_key=None, **kwargs):
        self.calls.append((operation, kwargs))
        if operation == "list_recovery_points_by_backup_vault":
            pages = self.pages[operation][kwargs["BackupVaultName"]]
        else:
            pages = self.pages.get(operation, [])
        for page in pages:
            if isinstance(page, Exception):
                raise page
            yield page

    def get_paginator(self, operation):
        return SimpleNamespace(paginate=lambda **kwargs: self.page_stream(operation, **kwargs))


class SnapshotStatusClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error

    def describe_backup_job(self, **kwargs):
        return self.response

    def describe_copy_job(self, **kwargs):
        return self.response

    def describe_backup_vault(self, **kwargs):
        return self.response

    def get_backup_plan(self, **kwargs):
        return self.response

    def list_recovery_points_by_backup_vault(self, **kwargs):
        if self.error:
            raise self.error
        return self.response

    def describe_recovery_point(self, **kwargs):
        if self.error:
            raise self.error
        return self.response

    def describe_snapshots(self, **kwargs):
        if self.error:
            raise self.error
        return self.response

    def describe_db_snapshots(self, **kwargs):
        if self.error:
            raise self.error
        return self.response

    def describe_db_cluster_snapshots(self, **kwargs):
        if self.error:
            raise self.error
        return self.response


class AWSBackupInventoryTest(SimpleTestCase):
    def setUp(self):
        self.account = SimpleNamespace(
            access_key="ACCESS",
            secret_key="SECRET",
            region="us-east-1",
        )
        self.vault_model = _memory_model()
        self.plan_model = _memory_model()
        self.recovery_model = _memory_model()
        self.job_model = _memory_model()
        self.copy_model = _memory_model()
        self.snapshot_model = _memory_model()
        self.clients = {}

        def get_client(account, service, region):
            self.clients.setdefault((service, region), BackupPaginationClient(region))
            return self.clients[(service, region)]

        self.patches = [
            patch.object(backup, "get_enabled_regions", return_value=["us-east-1", "eu-west-1"]),
            patch.object(backup, "aws_client", side_effect=get_client),
            patch.object(backup, "CoreAWSBackupVault", self.vault_model),
            patch.object(backup, "CoreAWSBackupPlan", self.plan_model),
            patch.object(backup, "CoreAWSBackupRecoveryPoint", self.recovery_model),
            patch.object(backup, "CoreAWSBackupJob", self.job_model),
            patch.object(backup, "CoreAWSBackupCopyJob", self.copy_model),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()

    def test_backup_inventory_paginates_and_keeps_rows_when_recovery_child_fails(self):
        old_recovery = MemoryAsset(
            self.account,
            "us-east-1:old-recovery",
            {
                "region": "us-east-1",
                "metadata": {"_cloudmoo_backup_vault_name": "vault-two"},
                "monitoring": UtilAsset.Monitoring.ACTIVE,
            },
        )
        self.recovery_model.objects.rows.append(old_recovery)

        result = sync_aws_backup_assets(self.account)

        self.assertEqual(result["regions"], ["us-east-1", "eu-west-1"])
        self.assertEqual(result["synced"][AWS_BACKUP_VAULT], 4)
        self.assertEqual(result["synced"][AWS_BACKUP_PLAN], 2)
        self.assertEqual(result["synced"][AWS_BACKUP_JOB], 2)
        self.assertEqual(result["synced"][AWS_BACKUP_COPY_JOB], 2)
        self.assertEqual(result["synced"][AWS_BACKUP_RECOVERY_POINT], 4)
        self.assertEqual(old_recovery.monitoring, UtilAsset.Monitoring.ACTIVE)
        plan = self.plan_model.objects.rows[0]
        self.assertEqual(plan.metadata["_cloudmoo_rules"][0]["RuleName"], "daily")
        self.assertEqual(plan.metadata["_cloudmoo_selections"][0]["BackupSelection"]["SelectionName"], "all-volumes")

        for client in self.clients.values():
            for operation, _kwargs in client.calls:
                self.assertTrue(
                    operation.startswith(("list_", "get_", "describe_")),
                    operation,
                )
        self.assertTrue(any(error["resource"].startswith("aws_backup_recovery_point:") for error in result["errors"]))

    def test_snapshot_inventory_is_region_scoped_and_uses_existing_model(self):
        with patch.object(backup, "CoreAWSSnapshot", self.snapshot_model):
            result = sync_aws_snapshots(self.account)

        self.assertEqual(result["regions"], ["us-east-1", "eu-west-1"])
        self.assertEqual(result["synced"], 6)
        rows = self.snapshot_model.objects.rows
        self.assertEqual({row.unique_id.split("|", 1)[0] for row in rows}, {"us-east-1", "eu-west-1"})
        self.assertEqual(
            {row.metadata["_cloudmoo_snapshot_kind"] for row in rows},
            {"ebs", "rds_instance", "rds_cluster"},
        )
        self.assertTrue(all("secret" not in str(row.metadata).lower() for row in rows))


class AWSBackupStatusTest(SimpleTestCase):
    def setUp(self):
        self.credentials = {
            "access_key": "ACCESS",
            "secret_key": "SECRET",
            "region": "us-east-1",
            "provider_id": "id-1",
            "backup_vault_name": "vault-one",
        }

    def _patch_client(self, client):
        def pages(_client, operation, **kwargs):
            collection_keys = {
                "describe_snapshots": "Snapshots",
                "describe_db_snapshots": "DBSnapshots",
                "describe_db_cluster_snapshots": "DBClusterSnapshots",
                "list_recovery_points_by_backup_vault": "RecoveryPoints",
            }
            key = collection_keys[operation]
            response = getattr(_client, operation)(**kwargs)
            if isinstance(response, dict) and key in response:
                return iter(response[key])
            return iter([response])

        return [
            patch.object(aws_backup, "aws_client", return_value=client),
            patch.object(aws_backup, "iter_pages", side_effect=pages),
            patch.object(aws_backup, "require_collection", side_effect=lambda response, key, _context: response[key]),
            patch.object(aws_backup, "serialize_aws", side_effect=lambda value: value),
            patch.object(aws_backup, "aws_error_code", side_effect=lambda error: (error.response.get("Error") or {}).get("Code", type(error).__name__)),
        ]

    def test_snapshot_status_normalization_and_deleted_state(self):
        for provider_state, expected in (
            ("completed", "completed"),
            ("pending", "pending"),
            ("error", "error"),
        ):
            client = SnapshotStatusClient({"Snapshots": [{"SnapshotId": "snap-1", "State": provider_state}]})
            patchers = self._patch_client(client)
            for patcher in patchers:
                patcher.start()
            try:
                status, _metadata = aws_backup.check_aws_snapshot_status("us-east-1:snap-1", self.credentials)
            finally:
                for patcher in reversed(patchers):
                    patcher.stop()
            self.assertEqual(status, expected)

        client = SnapshotStatusClient({"Snapshots": []})
        patchers = self._patch_client(client)
        for patcher in patchers:
            patcher.start()
        try:
            status, _metadata = aws_backup.check_aws_snapshot_status("us-east-1:snap-1", self.credentials)
        finally:
            for patcher in reversed(patchers):
                patcher.stop()
        self.assertEqual(status, "deleted")

    def test_backup_job_and_recovery_point_statuses_are_distinct(self):
        cases = (
            ("completed", "completed"),
            ("running", "running"),
            ("failed", "failed"),
            ("expired", "expired"),
        )
        for provider_state, expected in cases:
            client = SnapshotStatusClient({"BackupJob": {"BackupJobId": "job-1", "State": provider_state}})
            patchers = self._patch_client(client)
            for patcher in patchers:
                patcher.start()
            try:
                status, _metadata = aws_backup.check_aws_backup_job_status("us-east-1:job-1", self.credentials)
            finally:
                for patcher in reversed(patchers):
                    patcher.stop()
            self.assertEqual(status, expected)

        client = SnapshotStatusClient({"RecoveryPointArn": "rp-1", "Status": "EXPIRED"})
        patchers = self._patch_client(client)
        for patcher in patchers:
            patcher.start()
        try:
            status, _metadata = aws_backup.check_aws_backup_recovery_point_status(
                "us-east-1:rp-1",
                {**self.credentials, "provider_id": "rp-1"},
            )
        finally:
            for patcher in reversed(patchers):
                patcher.stop()
        self.assertEqual(status, "expired")

        client = SnapshotStatusClient(error=_aws_error("AccessDeniedException"))
        patchers = self._patch_client(client)
        for patcher in patchers:
            patcher.start()
        try:
            status, metadata = aws_backup.check_aws_backup_recovery_point_status(
                "us-east-1:rp-1",
                {**self.credentials, "provider_id": "rp-1"},
            )
        finally:
            for patcher in reversed(patchers):
                patcher.stop()
        self.assertEqual(status, "invalid_access_token")
        self.assertEqual(metadata["error_code"], "AccessDeniedException")
