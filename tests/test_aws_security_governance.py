"""Credential-free contract tests for the AWS security/governance lane."""

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from botocore.exceptions import ClientError
from django.test import SimpleTestCase

from apps.console.cloud.aws import security_governance as inventory
from apps.console.cloud.aws.models import CoreAWSAccount
from apps.console.utils.models import UtilAsset
from apps.monitoring.checks import aws_security_governance as checks


def _aws_error(code):
    return ClientError(
        {"Error": {"Code": code, "Message": "secret=must-not-escape"}},
        "ReadOnlyOperation",
    )


class _Paginator:
    def __init__(self, client, operation):
        self.client = client
        self.operation = operation

    def paginate(self, **kwargs):
        self.client.calls.append((self.operation, kwargs))
        value = self.client.pages.get(self.operation, [])
        if callable(value):
            value = value(kwargs)
        if isinstance(value, Exception):
            raise value
        return iter(value)


class _ReadOnlyClient:
    """Fake client that rejects every operation outside read prefixes."""

    def __init__(self, *, pages=None, details=None):
        self.pages = pages or {}
        self.details = details or {}
        self.calls = []

    def get_paginator(self, operation):
        return _Paginator(self, operation)

    def __getattr__(self, operation):
        if not operation.startswith(("batch_get_", "describe_", "get_", "list_")):
            raise AssertionError(f"Unexpected AWS mutation or operation: {operation}")

        def call(**kwargs):
            self.calls.append((operation, kwargs))
            value = self.details.get(operation)
            if callable(value):
                value = value(kwargs)
            if isinstance(value, Exception):
                raise value
            if value is None:
                raise AssertionError(f"Missing read fixture for {operation}")
            return value

        return call


def _fixture_client():
    key_arn = "arn:aws:kms:us-east-1:123456789012:key/key-1"
    trail_arn = "arn:aws:cloudtrail:us-east-1:123456789012:trail/main"
    fms_arn = "arn:aws:fms:us-east-1:123456789012:policy/fms-1"
    pages = {
        "list_users": [{"Users": [{"UserName": "alice", "UserId": "AIDALICE", "Arn": "arn:aws:iam::123:user/alice", "SecretToken": "omit"}]}],
        "list_roles": [{"Roles": [{"RoleName": "reader", "RoleId": "AROA1", "Arn": "arn:aws:iam::123:role/reader", "AssumeRolePolicyDocument": {"Secret": "omit"}}]}],
        "list_policies": lambda kwargs: (
            [{"Policies": [{"PolicyName": "local-read", "PolicyId": "ANPA1", "Arn": "arn:aws:iam::123:policy/local-read", "Document": {"Secret": "omit"}}]}]
            if "Scope" in kwargs
            else [{"PolicyList": [{"PolicyId": "fms-1", "PolicyName": "baseline", "PolicyArn": fms_arn}]}]
        ),
        "list_keys": [{"Keys": [{"KeyId": "key-1", "KeyArn": key_arn}]}],
        "list_aliases": [{"Aliases": [{"AliasName": "alias/app", "AliasArn": "arn:aws:kms:us-east-1:123:alias/app", "TargetKeyId": "key-1"}]}],
        "describe_trails": [{"trailList": [{"Name": "main", "TrailARN": trail_arn, "S3BucketName": "audit-bucket"}]}],
        "describe_config_rules": [{"ConfigRules": [{"ConfigRuleName": "encrypted-volumes", "ConfigRuleArn": "arn:aws:config:us-east-1:123:config-rule/encrypted", "ConfigRuleState": "ACTIVE", "InputParameters": {"Secret": "omit"}}]}],
        "describe_compliance_by_config_rule": [{"ComplianceByConfigRules": [{"ConfigRuleName": "encrypted-volumes", "Compliance": {"ComplianceType": "COMPLIANT", "ComplianceContributorCount": {"CappedCount": 0}}}]}],
        "describe_configuration_recorders": [{"ConfigurationRecorders": [{"name": "default", "roleARN": "arn:aws:iam::123:role/config"}]}],
        "list_detectors": [{"DetectorIds": ["detector-1"]}],
        "get_enabled_standards": [{"StandardsSubscriptions": [{"StandardsArn": "arn:aws:securityhub:::ruleset/cis-aws-foundations-benchmark", "StandardsStatus": "READY"}]}],
        "list_compliance_status": [{"PolicyComplianceStatusList": [{"PolicyId": "fms-1", "MemberAccount": "123456789012", "ComplianceStatus": "COMPLIANT", "IssueInfoMap": {"Secret": "omit"}}]}],
    }
    details = {
        "get_user": {"User": {"UserName": "alice", "UserId": "AIDALICE", "Arn": "arn:aws:iam::123:user/alice", "PasswordLastUsed": "2026-08-03T12:00:00Z"}},
        "get_role": {"Role": {"RoleName": "reader", "RoleId": "AROA1", "Arn": "arn:aws:iam::123:role/reader", "MaxSessionDuration": 3600}},
        "get_policy": {"Policy": {"PolicyName": "local-read", "PolicyId": "ANPA1", "Arn": "arn:aws:iam::123:policy/local-read", "DefaultVersionId": "v1", "IsAttachable": True}},
        "describe_key": {"KeyMetadata": {"KeyId": "key-1", "KeyArn": key_arn, "KeyState": "Enabled", "KeyUsage": "ENCRYPT_DECRYPT", "Description": "application key", "SecretToken": "omit"}},
        "get_trail": {"Name": "main", "TrailARN": trail_arn, "IncludeGlobalServiceEvents": True, "IsMultiRegionTrail": True, "KMSKeyId": key_arn},
        "get_trail_status": {"IsLogging": True, "LatestDeliveryTime": "2026-08-03T12:00:00Z", "LatestDeliveryError": "secret=omit"},
        "describe_configuration_recorder_status": {"ConfigurationRecordersStatus": [{"name": "default", "recording": True, "lastStatus": "SUCCESS"}]},
        "get_detector": {"DetectorId": "detector-1", "Status": "ENABLED", "FindingPublishingFrequency": "FIFTEEN_MINUTES", "Features": [{"Name": "EKS_AUDIT_LOGS", "Status": "ENABLED"}], "SecretToken": "omit"},
        "describe_hub": {"HubArn": "arn:aws:securityhub:us-east-1:123:hub/default", "SubscribedAt": "2026-08-03T12:00:00Z", "AutoEnableControls": True},
        "batch_get_account_status": {"accounts": [{"accountId": "123456789012", "state": "ENABLED", "resourceState": {"ec2": "ENABLED"}}], "failedAccounts": []},
        "get_configuration": {"ec2": {"ec2ScanMode": "EC2_SSM_AGENT"}, "SecretToken": "omit"},
        "get_macie_session": {"accountId": "123456789012", "status": "ENABLED", "findingPublishingFrequency": "FIFTEEN_MINUTES", "serviceRole": "arn:aws:iam::123:role/macie"},
        "get_policy": lambda kwargs: (
            {"Policy": {"PolicyName": "local-read", "PolicyId": "ANPA1", "Arn": "arn:aws:iam::123:policy/local-read", "DefaultVersionId": "v1", "IsAttachable": True}}
            if "PolicyArn" in kwargs
            else {"Policy": {"PolicyId": "fms-1", "PolicyName": "baseline", "PolicyArn": fms_arn, "PolicyStatus": "ACTIVE", "ManagedServiceData": "{\"SecretToken\":\"omit\"}"}}
        ),
    }
    return _ReadOnlyClient(pages=pages, details=details)


class _MemoryQuerySet:
    def __init__(self, rows):
        self.rows = rows

    def exclude(self, **kwargs):
        excluded = set(kwargs.get("unique_id__in", []))
        return _MemoryQuerySet([row for row in self.rows if row.unique_id not in excluded])

    def update(self, **kwargs):
        for row in self.rows:
            for key, value in kwargs.items():
                setattr(row, key, value)
        return len(self.rows)


class _MemoryManager:
    def __init__(self):
        self.rows = []

    def get_or_create(self, owner, region, unique_id, defaults):
        for row in self.rows:
            if row.owner is owner and row.region == region and row.unique_id == unique_id:
                return row, False
        row = SimpleNamespace(owner=owner, region=region, unique_id=unique_id, save=lambda: None, **defaults)
        self.rows.append(row)
        return row, True

    def filter(self, **kwargs):
        rows = self.rows
        for key, value in kwargs.items():
            rows = [row for row in rows if getattr(row, key) == value]
        return _MemoryQuerySet(rows)


class AWSSecurityGovernanceInventoryTests(SimpleTestCase):
    def setUp(self):
        self.account = SimpleNamespace(access_key="ACCESS", secret_key="SECRET", region="us-east-1")

    def _patch_managers(self, stack):
        managers = {}
        for asset_type, model in inventory.AWS_SECURITY_GOVERNANCE_ASSET_MODELS.items():
            manager = _MemoryManager()
            managers[asset_type] = manager
            stack.enter_context(patch.object(model, "objects", manager))
        return managers

    def test_registry_identity_and_global_regional_contract(self):
        self.assertEqual(set(inventory.AWS_SECURITY_GOVERNANCE_ASSET_MODELS), set(inventory.AWS_SECURITY_GOVERNANCE_ASSET_TYPES))
        self.assertEqual(set(inventory.AWS_SECURITY_GOVERNANCE_GLOBAL_ASSET_TYPES), {inventory.AWS_IAM_USER, inventory.AWS_IAM_ROLE, inventory.AWS_IAM_POLICY})
        self.assertEqual(inventory.AWS_SECURITY_GOVERNANCE_ENDPOINTS[inventory.AWS_IAM_USER]["scope"], "global")
        self.assertEqual(inventory.AWS_SECURITY_GOVERNANCE_ENDPOINTS[inventory.AWS_KMS_KEY]["scope"], "regional")
        for model in inventory.AWS_SECURITY_GOVERNANCE_ASSET_MODELS.values():
            constraint = model._meta.constraints[0]
            self.assertEqual(tuple(constraint.fields), ("owner", "region", "unique_id"))

    def test_all_requested_families_sync_with_bounds_redaction_and_regional_identity(self):
        clients = {}

        def client_for(_account, service, region=None):
            key = (service, region)
            clients.setdefault(key, _fixture_client())
            return clients[key]

        with patch.object(inventory, "get_enabled_regions", return_value=["eu-west-1", "us-east-1"]), \
             patch.object(inventory, "aws_client", side_effect=client_for), \
             ExitStack() as stack:
            managers = self._patch_managers(stack)
            summary = inventory.sync_aws_security_governance_assets(self.account)

        self.assertEqual(summary["regions"], ["eu-west-1", "us-east-1"])
        self.assertEqual(summary["globalRegion"], "global")
        self.assertFalse(summary["errors"], summary["errors"])
        for asset_type in inventory.AWS_SECURITY_GOVERNANCE_ASSET_TYPES:
            self.assertEqual(summary["counts"][asset_type], 1 if asset_type in inventory.AWS_SECURITY_GOVERNANCE_GLOBAL_ASSET_TYPES else 2, asset_type)
            scope = summary["families"][asset_type]
            expected_regions = {"global"} if asset_type in inventory.AWS_SECURITY_GOVERNANCE_GLOBAL_ASSET_TYPES else {"eu-west-1", "us-east-1"}
            self.assertEqual(set(scope), expected_regions, asset_type)
            self.assertTrue(all(item["complete"] and item["reconciled"] for item in scope.values()), asset_type)

        key_rows = managers[inventory.AWS_KMS_KEY].rows
        self.assertEqual({row.unique_id for row in key_rows}, {"eu-west-1|" + "arn:aws:kms:us-east-1:123456789012:key/key-1", "us-east-1|" + "arn:aws:kms:us-east-1:123456789012:key/key-1"})
        for manager in managers.values():
            for row in manager.rows:
                text = str(row.metadata)
                self.assertNotIn("ACCESS", text)
                self.assertNotIn("SECRET", text)
                self.assertNotIn("omit", text)
        self.assertTrue(all(call[0].startswith(("batch_get_", "describe_", "get_", "list_")) for client in clients.values() for call in client.calls))

    def test_malformed_collection_does_not_reconcile_existing_assets(self):
        clients = {}

        def client_for(_account, service, region=None):
            key = (service, region)
            clients.setdefault(key, _fixture_client())
            if service == "kms":
                clients[key].pages["list_keys"] = [{}]
            return clients[key]

        with patch.object(inventory, "aws_client", side_effect=client_for), \
             ExitStack() as stack:
            managers = self._patch_managers(stack)
            old = SimpleNamespace(
                owner=self.account,
                region="us-east-1",
                unique_id="us-east-1|old-key",
                monitoring=UtilAsset.Monitoring.ACTIVE,
                save=lambda: None,
                metadata={},
                name="old-key",
                type=inventory.AWS_KMS_KEY,
                provider_status="available",
            )
            managers[inventory.AWS_KMS_KEY].rows.append(old)
            summary = inventory.sync_aws_security_governance_assets(self.account, regions=["us-east-1"])

        family = summary["families"][inventory.AWS_KMS_KEY]["us-east-1"]
        self.assertFalse(family["complete"])
        self.assertFalse(family["reconciled"])
        self.assertEqual(old.monitoring, UtilAsset.Monitoring.ACTIVE)
        self.assertTrue(any(error["assetType"] == inventory.AWS_KMS_KEY for error in summary["errors"]))

    def test_access_denied_service_is_visible_not_empty_complete(self):
        def client_for(_account, service, region=None):
            client = _fixture_client()
            if service == "securityhub":
                client.details["describe_hub"] = _aws_error("AccessDeniedException")
            return client

        with patch.object(inventory, "aws_client", side_effect=client_for), \
             patch.object(inventory, "get_enabled_regions", return_value=["us-east-1"]), \
             ExitStack() as stack:
            self._patch_managers(stack)
            summary = inventory.sync_aws_security_governance_assets(self.account)

        family = summary["families"][inventory.AWS_SECURITY_HUB]["us-east-1"]
        self.assertFalse(family["complete"])
        self.assertFalse(family["reconciled"])
        self.assertEqual(family["status"], "provider_error")
        self.assertTrue(any(error["errorCode"] == "AccessDeniedException" for error in summary["errors"]))
        self.assertNotIn("must-not-escape", str(summary))

    def test_pagination_bounds_and_mutation_guard(self):
        client = _ReadOnlyClient(pages={"list_keys": [{"Keys": []}] * (inventory.MAX_PAGES_PER_COLLECTION + 1)})
        with self.assertRaises(inventory._InventoryIncomplete):
            inventory._collection(client, "list_keys", "Keys", "KMS key inventory")
        with self.assertRaises(inventory._InventoryIncomplete):
            inventory._collection(_ReadOnlyClient(pages={"list_keys": [{}]}), "list_keys", "Keys", "KMS key inventory")
        for operation in ("create_key", "put_key_policy", "delete_alias", "update_trail", "start_logging"):
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                inventory.assert_read_only_operation(operation)
        for spec in inventory.AWS_SECURITY_GOVERNANCE_COLLECTION_SPECS:
            for operation in spec["operations"]:
                inventory.assert_read_only_operation(operation)


class AWSSecurityGovernanceCheckTests(SimpleTestCase):
    def test_registry_status_dispatch_is_read_only_and_normalized(self):
        client = _fixture_client()
        credentials = {
            "access_key": "ACCESS",
            "secret_key": "SECRET",
            "region": "us-east-1",
            "resource_region": "us-east-1",
            "metadata": {},
        }
        metadata_by_type = {
            inventory.AWS_IAM_USER: {"UserName": "alice"},
            inventory.AWS_IAM_ROLE: {"RoleName": "reader"},
            inventory.AWS_IAM_POLICY: {"Arn": "arn:aws:iam::123:policy/local-read"},
            inventory.AWS_KMS_KEY: {"KeyId": "key-1"},
            inventory.AWS_KMS_ALIAS: {"AliasName": "alias/app", "TargetKeyId": "key-1"},
            inventory.AWS_CLOUDTRAIL_TRAIL: {"Name": "main"},
            inventory.AWS_CONFIG_RULE: {"ConfigRuleName": "encrypted-volumes"},
            inventory.AWS_CONFIG_RECORDER: {"name": "default"},
            inventory.AWS_GUARDDUTY_DETECTOR: {"DetectorId": "detector-1"},
            inventory.AWS_SECURITY_HUB: {},
            inventory.AWS_INSPECTOR: {},
            inventory.AWS_MACIE: {},
            inventory.AWS_FIREWALL_MANAGER_POLICY: {"PolicyId": "fms-1"},
        }

        def client_for(_account, _service, region=None):
            return client

        with patch.object(checks, "aws_client", side_effect=client_for):
            for asset_type, checker in checks.AWS_SECURITY_GOVERNANCE_CHECKS.items():
                check_credentials = dict(credentials)
                check_credentials["metadata"] = metadata_by_type[asset_type]
                status, payload = checker("us-east-1|provider-id", check_credentials)
                with self.subTest(asset_type=asset_type):
                    self.assertIn(status, {"available", "compliant"}, (asset_type, payload))
                    self.assertIn(asset_type, payload)
                    self.assertNotIn("omit", str(payload))
                    self.assertNotIn("SECRET", str(payload))

        with patch.object(checks, "aws_client", side_effect=client_for):
            self.assertEqual(
                checks.check_aws_security_governance_asset_status(
                    inventory.AWS_SECURITY_HUB,
                    "us-east-1|hub",
                    credentials,
                )[0],
                "available",
            )
        self.assertTrue(all(operation.startswith(("batch_get_", "describe_", "get_", "list_")) for operation, _kwargs in client.calls))

    def test_model_monitoring_context_is_regional_and_does_not_include_credentials(self):
        owner = CoreAWSAccount(access_key="ACCESS", secret_key="SECRET", region="us-east-1")
        asset = inventory.CoreAWSKMSKey(
            owner=owner,
            region="eu-west-1",
            unique_id="eu-west-1|key-1",
            type=inventory.AWS_KMS_KEY,
            metadata={"_cloudmoo_raw_id": "key-1", "Description": "application key"},
        )
        context = asset.monitoring_credentials
        self.assertEqual(context["resource_region"], "eu-west-1")
        self.assertEqual(context["provider_id"], "key-1")
        self.assertNotIn("ACCESS", context["metadata"])
        self.assertNotIn("SECRET", context["metadata"])
