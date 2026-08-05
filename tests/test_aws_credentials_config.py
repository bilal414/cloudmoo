from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from botocore.exceptions import ClientError
from django.test import SimpleTestCase

from apps.console.cloud.aws import credentials_config as inventory
from apps.console.cloud.aws.credentials_config import (
    AWS_CREDENTIALS_CONFIG_ASSET_MODELS,
    AWS_CREDENTIALS_CONFIG_ASSET_TYPES,
    AWS_SECRETS_MANAGER_SECRET,
    AWS_SSM_PARAMETER,
    CoreAWSAccount,
    CoreAWSSecretsManagerSecret,
    CoreAWSSSMParameter,
    normalize_credentials_config_status,
    sync_aws_credentials_config_assets,
)
from apps.console.cloud.models import CloudInventoryTransientError
from apps.console.utils.models import UtilAsset
from apps.monitoring.checks import aws_credentials_config as checks
from apps.console.cloud.aws.discovery import iter_pages


def _error(code):
    return ClientError(
        {
            "Error": {
                "Code": code,
                "Message": "provider secret-token=must-not-return",
            }
        },
        "MockOperation",
    )


class _Paginator:
    def __init__(self, client, operation):
        self.client = client
        self.operation = operation

    def paginate(self, **kwargs):
        self.client.calls.append((self.operation, kwargs))
        failure = self.client.failures.get(self.operation)
        if failure:
            raise failure
        pages = self.client.pages.get(self.operation, [])
        if callable(pages):
            pages = pages(**kwargs)
        return iter(pages)


class _ReadOnlyClient:
    """Fake provider that rejects mutations and records every read."""

    MUTATION_PREFIXES = (
        "abort",
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

    def __init__(self, *, pages=None, responses=None, failures=None):
        self.pages = dict(pages or {})
        self.responses = dict(responses or {})
        self.failures = dict(failures or {})
        self.calls = []

    def get_paginator(self, operation):
        if operation not in self.pages:
            raise AttributeError(operation)
        return _Paginator(self, operation)

    def __getattr__(self, operation):
        if operation.startswith(self.MUTATION_PREFIXES):
            raise AssertionError(f"mutating AWS operation attempted: {operation}")
        if operation not in self.responses:
            raise AttributeError(operation)

        def call(**kwargs):
            self.calls.append((operation, kwargs))
            failure = self.failures.get(operation)
            if failure:
                raise failure
            response = self.responses[operation]
            if callable(response):
                return response(**kwargs)
            return response

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


class _MemoryManager:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def get_or_create(self, *, owner, region, unique_id, defaults):
        for row in self.rows:
            if row.owner is owner and row.region == region and row.unique_id == unique_id:
                return row, False
        row = _MemoryAsset(owner, region, unique_id, defaults)
        self.rows.append(row)
        return row, True

    def filter(self, **kwargs):
        rows = self.rows
        for key, expected in kwargs.items():
            rows = [row for row in rows if getattr(row, key) == expected]
        return _MemoryQuerySet(rows)


def _secret(region, name):
    arn = f"arn:aws:secretsmanager:{region}:123456789012:secret:{name}-abc"
    changed = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    rotated = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)
    secret_string_key = "Secret" + "String"
    version_map_key = "Secret" + "VersionsToStages"
    summary = {
        "ARN": arn,
        "Name": name,
        "LastChangedDate": changed,
        "LastRotatedDate": rotated,
        "RotationEnabled": True,
        "KmsKeyId": f"arn:aws:kms:{region}:123456789012:key/key-1",
        "Description": "password=description-must-not-leave",
        secret_string_key: "secret-string-must-not-leave",
        version_map_key: {"version-id": ["AWSCURRENT"]},
    }
    detail = {
        **summary,
        "NextRotationDate": datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc),
        "OwningService": "app.example",
        "PrimaryRegion": region,
        "RotationRules": {"AutomaticallyAfterDays": 30},
    }
    tags = [
        {"Key": "Environment", "Value": "prod"},
        {"Key": "Application", "Value": "password=tag-must-be-redacted"},
        {"Key": "SecretToken", "Value": "tag-must-not-leave"},
    ]
    return summary, detail, tags


def _parameter(region, name):
    arn = f"arn:aws:ssm:{region}:123456789012:parameter{name}"
    modified = datetime(2026, 7, 3, 10, 30, tzinfo=timezone.utc)
    policy_text_key = "Policy" + "Text"
    parameter_value_key = "Value"
    return {
        "Name": name,
        "ARN": arn,
        "Type": "SecureString",
        "KeyId": f"alias/cloudmoo-{region}",
        "LastModifiedDate": modified,
        "Tier": "Advanced",
        "DataType": "text",
        "Description": "token=description-must-not-leave",
        "Policies": [
            {
                "PolicyType": "Expiration",
                "PolicyStatus": "Pending",
                policy_text_key: "policy-content-must-not-leave",
            }
        ],
        "Version": 9,
        parameter_value_key: "parameter-value-must-not-leave",
    }


def _clients_for_regions(regions):
    clients = {}
    for region in regions:
        summary, detail, secret_tags = _secret(region, f"secret-{region}")
        parameter = _parameter(region, f"/app/{region}/database")
        secret_arn = summary["ARN"]
        clients[("secretsmanager", region)] = _ReadOnlyClient(
            pages={"list_secrets": [{"SecretList": [summary]}]},
            responses={
                "describe_secret": detail,
                "list_tags_for_resource": {"Tags": secret_tags},
            },
        )
        clients[("ssm", region)] = _ReadOnlyClient(
            pages={"describe_parameters": [{"Parameters": [parameter]}]},
            responses={
                "list_tags_for_resource": {
                    "TagList": [
                        {"Key": "Environment", "Value": "prod"},
                        {"Key": "Owner", "Value": "platform"},
                    ]
                }
            },
        )
        # Exercise the real provider identifier in checker tests without
        # retaining the local variable in metadata.
        assert secret_arn.startswith("arn:aws:secretsmanager:")
    return clients


class AWSCredentialsConfigSyncTests(SimpleTestCase):
    def setUp(self):
        self.account = SimpleNamespace(
            access_key="ACCESS-KEY",
            secret_key="SECRET-KEY",
            region="us-east-1",
        )

    def _patch_managers(self, stack, rows=None):
        rows = rows or {}
        managers = {}
        for asset_type, model in AWS_CREDENTIALS_CONFIG_ASSET_MODELS.items():
            manager = _MemoryManager(rows.get(asset_type, ()))
            managers[asset_type] = manager
            stack.enter_context(patch.object(model, "objects", manager))
        return managers

    def test_registry_models_and_regional_sync_keep_owner_region_identity(self):
        regions = ["eu-west-1", "us-east-1"]
        clients = _clients_for_regions(regions)

        def client_for(_account, service, region=None):
            return clients[(service, region)]

        with patch.object(inventory, "get_enabled_regions", return_value=regions), \
             patch.object(inventory, "aws_client", side_effect=client_for):
            with ExitStack() as stack:
                managers = self._patch_managers(stack)
                result = sync_aws_credentials_config_assets(self.account)

        self.assertEqual(set(AWS_CREDENTIALS_CONFIG_ASSET_MODELS), set(AWS_CREDENTIALS_CONFIG_ASSET_TYPES))
        self.assertEqual(result["regions"], regions)
        for asset_type in AWS_CREDENTIALS_CONFIG_ASSET_TYPES:
            self.assertEqual(result["counts"][asset_type], 2)
            self.assertTrue(all(
                result["families"][asset_type][region]["reconciled"]
                for region in regions
            ))
            self.assertEqual(len(managers[asset_type].rows), 2)
            for row in managers[asset_type].rows:
                self.assertEqual(row.owner, self.account)
                self.assertIn(row.region, regions)
                self.assertTrue(row.unique_id.startswith(row.region + "|"))
                self.assertNotIn("access_key", row.metadata)
                self.assertNotIn("secret_key", row.metadata)

        secret = managers[AWS_SECRETS_MANAGER_SECRET].rows[0]
        self.assertEqual(secret.metadata["rotation_enabled"], True)
        self.assertEqual(secret.metadata["last_rotated_date"], "2026-07-02T12:00:00+00:00")
        self.assertEqual(secret.metadata["last_changed_date"], "2026-07-01T12:00:00+00:00")
        self.assertEqual(secret.metadata["rotation_rules"]["automatically_after_days"], 30)
        secret_tags = {tag["Key"]: tag["Value"] for tag in secret.metadata["tags"]}
        self.assertEqual(secret_tags["Environment"], "prod")
        self.assertNotIn("tag-must-be-redacted", str(secret.metadata))
        self.assertNotIn("description-must-not-leave", str(secret.metadata))

        parameter = managers[AWS_SSM_PARAMETER].rows[0]
        self.assertEqual(parameter.metadata["parameter_type"], "SecureString")
        self.assertEqual(parameter.metadata["tier"], "Advanced")
        self.assertEqual(parameter.metadata["last_changed_date"], "2026-07-03T10:30:00+00:00")
        self.assertEqual(parameter.metadata["policy_posture"]["status"], "configured")
        self.assertEqual(parameter.metadata["policy_posture"]["types"], ["Expiration"])
        self.assertNotIn("policy-content-must-not-leave", str(parameter.metadata))
        self.assertNotIn("parameter-value-must-not-leave", str(parameter.metadata))

        expected_services = {"secretsmanager", "ssm"}
        self.assertEqual({service for service, _region in clients}, expected_services)
        self.assertEqual(
            {
                region
                for client in clients.values()
                for _operation, _kwargs in client.calls
                for region in [client.calls and next(
                    region for (service, region), candidate in clients.items() if candidate is client
                )]
            },
            set(regions),
        )
        for client in clients.values():
            self.assertTrue(client.calls)
            self.assertTrue(all(
                operation.startswith(("list_", "describe_"))
                for operation, _kwargs in client.calls
            ))

    def test_model_monitoring_context_and_checker_contract(self):
        owner = CoreAWSAccount(
            access_key="ACCESS-KEY",
            secret_key="SECRET-KEY",
            region="us-east-1",
        )
        for asset_type, model in AWS_CREDENTIALS_CONFIG_ASSET_MODELS.items():
            with self.subTest(asset_type=asset_type):
                self.assertEqual(model.asset_type, asset_type)
                self.assertIs(model._meta.get_field("owner").remote_field.model, CoreAWSAccount)
                self.assertIsNotNone(model._meta.get_field("region"))
                constraints = {field for constraint in model._meta.constraints for field in constraint.fields}
                self.assertEqual(constraints, {"owner", "region", "unique_id"})

        asset = CoreAWSSecretsManagerSecret(
            owner=owner,
            region="eu-west-1",
            unique_id="eu-west-1|secret-1",
            name="secret-1",
            metadata={"_cloudmoo_raw_id": "secret-1", "normalized_status": "active"},
        )
        context = asset.monitoring_credentials
        self.assertEqual(context["provider_id"], "secret-1")
        self.assertEqual(context["resource_region"], "eu-west-1")
        self.assertEqual(context["access_key"], "ACCESS-KEY")
        self.assertEqual(context["secret_key"], "SECRET-KEY")
        self.assertNotIn("access_key", asset.metadata)
        self.assertNotIn("secret_key", asset.metadata)
        self.assertIn("eu-west-1", asset.provider_url)

        with patch.dict(
            checks.AWS_CREDENTIALS_CONFIG_CHECKS,
            {AWS_SECRETS_MANAGER_SECRET: lambda _id, _credentials: ("active", {"ok": True})},
            clear=False,
        ):
            self.assertEqual(asset.check_status(), ("active", {"ok": True}))

    def test_access_denied_metadata_is_partial_and_does_not_reconcile(self):
        old = SimpleNamespace(
            owner=self.account,
            region="us-east-1",
            unique_id="us-east-1|old-secret",
            name="old-secret",
            metadata={},
            monitoring=UtilAsset.Monitoring.ACTIVE,
            save=lambda: None,
        )
        summary, _detail, tags = _secret("us-east-1", "new-secret")
        client = _ReadOnlyClient(
            pages={"list_secrets": [{"SecretList": [summary]}]},
            responses={
                "describe_secret": {},
                "list_tags_for_resource": {"Tags": tags},
            },
            failures={"describe_secret": _error("AccessDeniedException")},
        )

        with patch.object(inventory, "aws_client", return_value=client), \
             patch.object(inventory, "get_enabled_regions", return_value=["us-east-1"]):
            with ExitStack() as stack:
                managers = self._patch_managers(
                    stack,
                    {AWS_SECRETS_MANAGER_SECRET: [old]},
                )
                result = sync_aws_credentials_config_assets(self.account)

        family = result["families"][AWS_SECRETS_MANAGER_SECRET]["us-east-1"]
        self.assertEqual(family["status"], "partial")
        self.assertFalse(family["complete"])
        self.assertFalse(family["reconciled"])
        self.assertTrue(any(
            error["assetType"] == AWS_SECRETS_MANAGER_SECRET
            and error["errorCode"] == "AccessDeniedException"
            for error in result["errors"]
        ))
        self.assertEqual(old.monitoring, UtilAsset.Monitoring.ACTIVE)
        self.assertEqual(len(managers[AWS_SECRETS_MANAGER_SECRET].rows), 2)

    def test_malformed_page_and_pagination_bound_preserve_existing_rows(self):
        old = SimpleNamespace(
            owner=self.account,
            region="us-east-1",
            unique_id="us-east-1|old-secret",
            name="old-secret",
            metadata={},
            monitoring=UtilAsset.Monitoring.ACTIVE,
            save=lambda: None,
        )
        malformed = _ReadOnlyClient(pages={"list_secrets": [{"SecretList": None}]})
        with patch.object(inventory, "aws_client", return_value=malformed), \
             patch.object(inventory, "get_enabled_regions", return_value=["us-east-1"]):
            with ExitStack() as stack:
                managers = self._patch_managers(stack, {AWS_SECRETS_MANAGER_SECRET: [old]})
                result = sync_aws_credentials_config_assets(self.account)
        family = result["families"][AWS_SECRETS_MANAGER_SECRET]["us-east-1"]
        self.assertEqual(family["status"], "error")
        self.assertFalse(family["reconciled"])
        self.assertEqual(len(managers[AWS_SECRETS_MANAGER_SECRET].rows), 1)
        self.assertEqual(old.monitoring, UtilAsset.Monitoring.ACTIVE)

        summary, detail, tags = _secret("us-east-1", "bounded-secret")
        bounded = _ReadOnlyClient(
            pages={
                "list_secrets": [
                    {"SecretList": [summary]},
                    {"SecretList": []},
                ]
            },
            responses={
                "describe_secret": detail,
                "list_tags_for_resource": {"Tags": tags},
            },
        )
        with patch.object(inventory, "MAX_COLLECTION_PAGES", 1), \
             patch.object(inventory, "aws_client", return_value=bounded), \
             patch.object(inventory, "get_enabled_regions", return_value=["us-east-1"]):
            with ExitStack() as stack:
                managers = self._patch_managers(stack, {AWS_SECRETS_MANAGER_SECRET: [old]})
                result = sync_aws_credentials_config_assets(self.account)
        family = result["families"][AWS_SECRETS_MANAGER_SECRET]["us-east-1"]
        self.assertEqual(family["status"], "error")
        self.assertFalse(family["reconciled"])
        self.assertEqual(len(managers[AWS_SECRETS_MANAGER_SECRET].rows), 1)

    def test_strict_no_value_source_and_output_guarantees(self):
        source_paths = (
            Path(inventory.__file__),
            Path(checks.__file__),
        )
        forbidden_operations = (
            "get_" + "secret_" + "value",
            "batch_" + "get_" + "secret_" + "value",
            "get_" + "parameter",
            "get_" + "parameters",
            "get_" + "parameters_" + "by_" + "path",
            "get_" + "parameter_" + "history",
        )
        forbidden_keys = (
            "Secret" + "String",
            "Secret" + "Binary",
            "Parameter" + "Value",
            "Secret" + "VersionsToStages",
            "Version" + "IdsToStages",
            "Environment" + "Variables",
            "Private" + "Key",
        )
        for path in source_paths:
            source = path.read_text()
            lowered = source.lower()
            for operation in forbidden_operations:
                self.assertNotIn(operation, lowered, path.name)
            for key in forbidden_keys:
                self.assertNotIn(key.lower(), lowered, path.name)

        clients = _clients_for_regions(["us-east-1"])
        with patch.object(inventory, "get_enabled_regions", return_value=["us-east-1"]), \
             patch.object(inventory, "aws_client", side_effect=lambda _a, service, region=None: clients[(service, region)]):
            with ExitStack() as stack:
                managers = self._patch_managers(stack)
                sync_aws_credentials_config_assets(self.account)
        output = str([row.metadata for manager in managers.values() for row in manager.rows])
        for key in forbidden_keys:
            self.assertNotIn(key.lower(), output.lower())
        for value in (
            "secret-string-must-not-leave",
            "parameter-value-must-not-leave",
            "policy-content-must-not-leave",
            "description-must-not-leave",
            "tag-must-not-leave",
        ):
            self.assertNotIn(value, output)

    def test_bounds_redact_tags_and_policy_posture_without_policy_contents(self):
        summary, detail, tags = _secret("us-east-1", "bounded-secret")
        tags = tags + [
            {"Key": "Environment", "Value": f"env-{index}"}
            for index in range(inventory.MAX_TAG_ITEMS + 5)
        ]
        client = _ReadOnlyClient(
            pages={"list_secrets": [{"SecretList": [summary]}]},
            responses={
                "describe_secret": detail,
                "list_tags_for_resource": {"Tags": tags},
            },
        )
        with patch.object(inventory, "aws_client", return_value=client):
            records, warnings = inventory._collect_secrets(self.account, "us-east-1")
        self.assertFalse(warnings)
        metadata = records[0]["metadata"]
        self.assertLessEqual(len(metadata["tags"]), inventory.MAX_TAG_ITEMS + 1)
        self.assertTrue(metadata["tags"][-1]["Key"] == "_cloudmoo_truncated")
        self.assertNotIn("tag-must-be-redacted", str(metadata))
        self.assertNotIn("password=tag-must-be-redacted", str(metadata))

        parameter = _parameter("us-east-1", "/app/bounded")
        parameter["Policies"] = [
            {
                "PolicyType": "Expiration",
                "PolicyStatus": "Pending",
                "Policy" + "Text": "opaque-policy-content",
            }
        ]
        metadata = inventory._parameter_metadata(parameter, {}, "us-east-1", parameter["Name"], parameter["Name"])
        self.assertEqual(metadata["policy_posture"]["types"], ["Expiration"])
        self.assertNotIn("opaque-policy-content", str(metadata))

    def test_public_status_normalization_and_explicit_mutation_rejection(self):
        expected = {
            "ACTIVE": "active",
            "enabled": "active",
            "scheduled-deletion": "pending_deletion",
            "expired": "expired",
            "failed": "error",
            "not-found": "not_found",
            "other": "unknown",
        }
        for raw, normalized in expected.items():
            self.assertEqual(normalize_credentials_config_status(raw), normalized)

        client = _ReadOnlyClient()
        with self.assertRaises(ValueError):
            list(inventory._bounded_pages(client, "delete_secret", "invalid"))
        with self.assertRaises(ValueError):
            inventory._detail(client, "put_parameter", "invalid")
        with self.assertRaises(ValueError):
            list(iter_pages(client, "delete_parameter"))


class AWSCredentialsConfigCheckTests(SimpleTestCase):
    def setUp(self):
        self.credentials = {
            "access_key": "ACCESS-KEY",
            "secret_key": "SECRET-KEY",
            "region": "us-east-1",
        }

    def test_checkers_use_only_safe_describes_and_normalize_status(self):
        summary, detail, secret_tags = _secret("us-east-1", "checker-secret")
        secret_client = _ReadOnlyClient(
            responses={"describe_secret": detail},
        )
        parameter = _parameter("us-east-1", "/app/checker")
        parameter_client = _ReadOnlyClient(
            pages={"describe_parameters": [{"Parameters": [parameter]}]},
        )

        with patch.object(checks, "aws_client", side_effect=[secret_client, parameter_client]):
            secret_status, secret_payload = checks.check_aws_secrets_manager_secret_status(
                f"us-east-1|{summary['ARN']}",
                {**self.credentials, "resource_name": "checker-secret"},
            )
            parameter_status, parameter_payload = checks.check_aws_ssm_parameter_status(
                "us-east-1|/app/checker",
                self.credentials,
            )

        self.assertEqual(secret_status, "active")
        self.assertEqual(parameter_status, "active")
        self.assertIn(AWS_SECRETS_MANAGER_SECRET, secret_payload)
        self.assertIn(AWS_SSM_PARAMETER, parameter_payload)
        self.assertEqual(
            secret_payload[AWS_SECRETS_MANAGER_SECRET]["last_changed_date"],
            "2026-07-01T12:00:00+00:00",
        )
        self.assertEqual(
            parameter_payload[AWS_SSM_PARAMETER]["last_changed_date"],
            "2026-07-03T10:30:00+00:00",
        )
        self.assertEqual(secret_client.calls, [("describe_secret", {"SecretId": summary["ARN"]})])
        self.assertEqual(parameter_client.calls[0][0], "describe_parameters")
        self.assertTrue(all(
            operation.startswith("describe_")
            for operation, _kwargs in parameter_client.calls
        ))
        self.assertNotIn("checker-secret", str(secret_payload[AWS_SECRETS_MANAGER_SECRET]["tags"]))
        self.assertNotIn("parameter-value-must-not-leave", str(parameter_payload))

    def test_checker_errors_are_normalized_and_bounded(self):
        denied = _ReadOnlyClient(
            responses={"describe_secret": {}},
            failures={"describe_secret": _error("AccessDeniedException")},
        )
        missing = _ReadOnlyClient(
            responses={"describe_secret": {}},
            failures={"describe_secret": _error("ResourceNotFoundException")},
        )
        with patch.object(checks, "aws_client", side_effect=[denied, missing]):
            status, payload = checks.check_aws_secrets_manager_secret_status(
                "us-east-1|arn:aws:secretsmanager:us-east-1:123:secret:nope",
                self.credentials,
            )
            missing_status, missing_payload = checks.check_aws_secrets_manager_secret_status(
                "us-east-1|arn:aws:secretsmanager:us-east-1:123:secret:nope",
                self.credentials,
            )
        self.assertEqual(status, "invalid_access_token")
        self.assertEqual(payload, {"error_code": "AccessDeniedException"})
        self.assertEqual(missing_status, "not_found")
        self.assertEqual(missing_payload, {"error_code": "ResourceNotFoundException"})
        self.assertNotIn("must-not-return", str(payload))

    def test_invalid_checker_context_does_not_make_a_provider_call(self):
        with patch.object(checks, "aws_client") as client:
            status, payload = checks.check_aws_ssm_parameter_status(
                "us-east-1|/app/parameter",
                {"region": "us-east-1"},
            )
        self.assertEqual(status, "error")
        self.assertEqual(payload["error_code"], "ValueError")
        client.assert_not_called()
