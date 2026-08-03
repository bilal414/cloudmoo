import json
from pathlib import Path
from unittest.mock import Mock, patch

from django.apps import apps as django_apps
from django.contrib import admin
from django.test import SimpleTestCase

from apps.console.cloud.aws import (
    account_operations,
    credentials_config,
    security_governance,
)
from apps.console.cloud.aws.models import CoreAWSAccount
from apps.console.cloud.models import CoreCloud
from apps.console.utils.models import UtilAsset
from apps.monitoring.checks import get_check_function


REPO_ROOT = Path(__file__).resolve().parents[1]
PRIORITY2_MAPS = (
    security_governance.AWS_SECURITY_GOVERNANCE_ASSET_MODELS,
    credentials_config.AWS_CREDENTIALS_CONFIG_ASSET_MODELS,
    account_operations.AWS_ACCOUNT_OPERATIONS_ASSET_MODELS,
)
PRIORITY2_TYPES = {
    "aws_iam_user",
    "aws_iam_role",
    "aws_iam_policy",
    "aws_kms_key",
    "aws_kms_alias",
    "aws_cloudtrail_trail",
    "aws_config_rule",
    "aws_config_recorder",
    "aws_guardduty_detector",
    "aws_security_hub",
    "aws_inspector",
    "aws_macie",
    "aws_firewall_manager_policy",
    "aws_secrets_manager_secret",
    "aws_ssm_parameter",
    "aws_health_event",
    "aws_trusted_advisor_check",
    "aws_cost_explorer_signal",
    "aws_cost_anomaly_monitor",
    "aws_cost_anomaly_subscription",
    "aws_cost_anomaly",
}


class AWSPriority2ModelIntegrationTests(SimpleTestCase):
    def test_models_types_maps_admin_and_ui_registration_are_complete(self):
        registered_names = {
            model.__name__
            for model in django_apps.get_app_config("apps").get_models()
        }
        registered_priority2_names = {
            model.__name__
            for model_map in PRIORITY2_MAPS
            for model in model_map.values()
        }
        self.assertTrue(registered_priority2_names.issubset(registered_names))

        values = {value for value, _label in UtilAsset.Type.choices}
        self.assertTrue(PRIORITY2_TYPES.issubset(values))
        self.assertEqual(
            set().union(*(set(model_map) for model_map in PRIORITY2_MAPS)),
            PRIORITY2_TYPES,
        )
        expected_map_types = (
            security_governance.AWS_SECURITY_GOVERNANCE_ASSET_TYPES,
            credentials_config.AWS_CREDENTIALS_CONFIG_ASSET_TYPES,
            account_operations.AWS_ACCOUNT_OPERATIONS_ASSET_TYPES,
        )
        for model_map, expected_types in zip(PRIORITY2_MAPS, expected_map_types):
            self.assertEqual(set(model_map), set(expected_types))
            for model in model_map.values():
                self.assertIn(model, admin.site._registry)

        from apps.console.asset import views as asset_views
        from apps.console.home import views as home_views

        self.assertEqual(set(asset_views._AWS_PRIORITY2_ASSET_MODELS), PRIORITY2_TYPES)
        self.assertEqual(
            {asset_type for asset_type, _model in home_views._AWS_PRIORITY2_ASSET_MODELS},
            PRIORITY2_TYPES,
        )

    def test_cloud_relations_use_actual_class_qualified_related_names(self):
        relations = dict(CoreCloud.ASSET_RELATIONS)
        for model_map in PRIORITY2_MAPS:
            for asset_type, model in model_map.items():
                accessor = f"{model.__name__.lower()}_assets"
                with self.subTest(asset_type=asset_type):
                    self.assertEqual(relations.get(accessor), asset_type)

    def test_monitoring_dispatch_uses_each_priority2_module_and_fails_closed(self):
        expected_modules = {
            **{
                asset_type: "apps.monitoring.checks.aws_security_governance"
                for asset_type in security_governance.AWS_SECURITY_GOVERNANCE_ASSET_TYPES
            },
            **{
                asset_type: "apps.monitoring.checks.aws_credentials_config"
                for asset_type in credentials_config.AWS_CREDENTIALS_CONFIG_ASSET_TYPES
            },
            **{
                asset_type: "apps.monitoring.checks.aws_account_operations"
                for asset_type in account_operations.AWS_ACCOUNT_OPERATIONS_ASSET_TYPES
            },
        }
        self.assertEqual(set(expected_modules), PRIORITY2_TYPES)
        for asset_type, expected_module in expected_modules.items():
            with self.subTest(asset_type=asset_type):
                check = get_check_function("aws", asset_type)
                self.assertTrue(callable(check))
                self.assertEqual(check.__module__, expected_module)

        with self.assertRaises(ValueError):
            get_check_function("aws", "aws_priority2_unsupported")


class AWSPriority2SyncIntegrationTests(SimpleTestCase):
    @patch("apps.console.cloud.aws.models.timezone.now")
    def test_account_sync_calls_all_priority2_adapters_after_existing_lanes(self, now):
        now.return_value = object()
        account = CoreAWSAccount()
        legacy_methods = (
            "sync_servers",
            "sync_volumes",
            "sync_rds_databases",
            "sync_lambda_functions",
            "sync_dynamodb_tables",
            "sync_s3_buckets",
            "sync_elastic_ips",
            "sync_load_balancers",
            "sync_security_groups",
            "sync_ecs_services",
            "sync_ecs_tasks",
            "sync_lightsail_assets",
        )
        legacy = {name: Mock() for name in legacy_methods}
        with patch.object(account, "save") as save, patch.multiple(
            account,
            **legacy,
        ), patch("apps.console.cloud.aws.network.sync_aws_network_assets"), patch(
            "apps.console.cloud.aws.observability.sync_aws_observability_assets"
        ), patch("apps.console.cloud.aws.containers.sync_aws_container_assets"), patch(
            "apps.console.cloud.aws.edge.sync_aws_edge_assets"
        ), patch("apps.console.cloud.aws.edge.sync_aws_regional_certificates"), patch(
            "apps.console.cloud.aws.backup.sync_aws_backup_assets"
        ), patch("apps.console.cloud.aws.backup.sync_aws_snapshots"), patch(
            "apps.console.cloud.aws.data_services.sync_aws_data_service_assets"
        ), patch(
            "apps.console.cloud.aws.application_services.sync_aws_application_service_assets"
        ), patch("apps.console.cloud.aws.delivery.sync_aws_delivery_assets"), patch(
            "apps.console.cloud.aws.security_governance.sync_aws_security_governance_assets"
        ) as security_sync, patch(
            "apps.console.cloud.aws.credentials_config.sync_aws_credentials_config_assets"
        ) as credentials_sync, patch(
            "apps.console.cloud.aws.account_operations.sync_aws_account_operations_assets"
        ) as account_operations_sync:
            account.sync_assets()

        security_sync.assert_called_once_with(account)
        credentials_sync.assert_called_once_with(account)
        account_operations_sync.assert_called_once_with(account)
        save.assert_called_once_with()


class AWSPriority2PolicyAndBoundaryTests(SimpleTestCase):
    def test_policy_contains_exact_priority2_reads_and_no_mutations(self):
        policy_path = Path(__file__).resolve().parents[1] / "aws-cloudmoo-readonly-policy.json"
        policy = json.loads(policy_path.read_text())
        actions = {
            action.lower()
            for statement in policy["Statement"]
            for action in statement["Action"]
        }
        required = {
            "iam:listusers",
            "iam:getuser",
            "iam:listroles",
            "iam:getrole",
            "iam:listpolicies",
            "iam:getpolicy",
            "kms:listkeys",
            "kms:describekey",
            "kms:listaliases",
            "cloudtrail:describetrails",
            "cloudtrail:gettrail",
            "cloudtrail:gettrailstatus",
            "config:describeconfigrules",
            "config:describecompliancebyconfigrule",
            "config:describeconfigurationrecorders",
            "config:describeconfigurationrecorderstatus",
            "guardduty:listdetectors",
            "guardduty:getdetector",
            "securityhub:describehub",
            "securityhub:getenabledstandards",
            "inspector2:batchgetaccountstatus",
            "inspector2:getconfiguration",
            "macie2:getmaciesession",
            "fms:listpolicies",
            "fms:getpolicy",
            "fms:listcompliancestatus",
            "secretsmanager:listsecrets",
            "secretsmanager:describesecret",
            "secretsmanager:listtagsforresource",
            "ssm:describeparameters",
            "ssm:listtagsforresource",
            "health:describeevents",
            "health:describeeventdetails",
            "support:describetrustedadvisorchecks",
            "support:describetrustedadvisorcheckresult",
            "support:describetrustedadvisorchecksummaries",
            "ce:getcostandusage",
            "ce:getcostforecast",
            "ce:getdimensionvalues",
            "ce:getanomalymonitors",
            "ce:getanomalysubscriptions",
            "ce:getanomalies",
        }
        self.assertTrue(required.issubset(actions))

        forbidden_prefixes = (
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
        for action in actions:
            self.assertFalse(action.split(":", 1)[1].startswith(forbidden_prefixes), action)

    def test_credential_and_parameter_value_operations_are_not_used(self):
        source_paths = (
            REPO_ROOT / "apps/console/cloud/aws/credentials_config.py",
            REPO_ROOT / "apps/monitoring/checks/aws_credentials_config.py",
        )
        forbidden_call_names = (
            "get_secret_value",
            "get_parameter",
            "get_parameters",
            "get_parameters_by_path",
        )
        for source_path in source_paths:
            source = source_path.read_text()
            for call_name in forbidden_call_names:
                with self.subTest(source=str(source_path), call_name=call_name):
                    self.assertNotRegex(source, rf"\.{call_name}\b")

        policy_path = Path(__file__).resolve().parents[1] / "aws-cloudmoo-readonly-policy.json"
        actions = {
            action.lower()
            for statement in json.loads(policy_path.read_text())["Statement"]
            for action in statement["Action"]
        }
        self.assertNotIn("secretsmanager:getsecretvalue", actions)
        self.assertNotIn("ssm:getparameter", actions)
        self.assertNotIn("ssm:getparameters", actions)
        self.assertNotIn("ssm:getparametersbypath", actions)
