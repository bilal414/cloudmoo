import json
from pathlib import Path
from unittest.mock import Mock, patch

from django.apps import apps
from django.test import SimpleTestCase

from apps.console.cloud.aws import application_services, data_services, delivery
from apps.console.cloud.aws.models import CoreAWSAccount
from apps.console.cloud.models import CoreCloud
from apps.console.utils.models import UtilAsset
from apps.monitoring.checks import get_check_function


PRIORITY1_TYPES = {
    "aws_rds_cluster",
    "aws_elasticache_cluster",
    "aws_elasticache_replication_group",
    "aws_elasticache_serverless_cache",
    "aws_memorydb_cluster",
    "aws_opensearch_domain",
    "aws_efs_file_system",
    "aws_fsx_file_system",
    "aws_apigateway_rest_api",
    "aws_apigateway_v2_api",
    "aws_eventbridge_bus",
    "aws_eventbridge_rule",
    "aws_eventbridge_schedule",
    "aws_eventbridge_pipe",
    "aws_sns_topic",
    "aws_sqs_queue",
    "aws_stepfunctions_state_machine",
    "aws_athena_workgroup",
    "aws_athena_data_catalog",
    "aws_cloudformation_stack",
    "aws_elastic_beanstalk_application",
    "aws_elastic_beanstalk_environment",
    "aws_codebuild_project",
    "aws_codebuild_build",
    "aws_codepipeline_pipeline",
    "aws_codepipeline_execution",
}


class AWSPriority1ModelIntegrationTests(SimpleTestCase):
    def test_models_and_asset_choices_cover_every_priority1_family(self):
        registered_names = {
            model.__name__
            for model in apps.get_app_config("apps").get_models()
        }
        model_maps = (
            data_services.AWS_DATA_SERVICE_ASSET_MODELS,
            application_services.AWS_APPLICATION_ASSET_MODELS,
            delivery.AWS_DELIVERY_ASSET_MODELS,
        )
        registered_priority1_names = {
            model.__name__
            for model_map in model_maps
            for model in model_map.values()
        }
        self.assertTrue(registered_priority1_names.issubset(registered_names))
        values = {value for value, _label in UtilAsset.Type.choices}
        self.assertTrue(PRIORITY1_TYPES.issubset(values))
        self.assertTrue(
            PRIORITY1_TYPES.issubset(
                set().union(*(set(model_map) for model_map in model_maps))
            )
        )

    def test_cloud_relations_and_monitoring_dispatch_are_explicit(self):
        relations = dict(CoreCloud.ASSET_RELATIONS)
        for model_map in (
            data_services.AWS_DATA_SERVICE_ASSET_MODELS,
            application_services.AWS_APPLICATION_ASSET_MODELS,
            delivery.AWS_DELIVERY_ASSET_MODELS,
        ):
            for asset_type, model in model_map.items():
                accessor = f"{model.__name__.lower()}_assets"
                self.assertEqual(relations.get(accessor), asset_type, asset_type)

        expected_modules = {
            **{
                asset_type: "apps.monitoring.checks.aws_data_services"
                for asset_type in data_services.AWS_DATA_SERVICE_ASSET_TYPES
            },
            **{
                asset_type: "apps.monitoring.checks.aws_application_services"
                for asset_type in application_services.AWS_APPLICATION_ASSET_TYPES
            },
            **{
                asset_type: "apps.monitoring.checks.aws_delivery"
                for asset_type in delivery.AWS_DELIVERY_ASSET_TYPES
            },
        }
        for asset_type, expected_module in expected_modules.items():
            with self.subTest(asset_type=asset_type):
                check = get_check_function("aws", asset_type)
                self.assertTrue(callable(check))
                self.assertEqual(check.__module__, expected_module)

    @patch("apps.console.cloud.aws.models.timezone.now")
    def test_account_sync_calls_all_priority1_adapters(self, now):
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
        with patch.object(account, "save"), patch.multiple(account, **legacy), patch(
            "apps.console.cloud.aws.network.sync_aws_network_assets"
        ), patch("apps.console.cloud.aws.observability.sync_aws_observability_assets"), patch(
            "apps.console.cloud.aws.containers.sync_aws_container_assets"
        ), patch("apps.console.cloud.aws.edge.sync_aws_edge_assets"), patch(
            "apps.console.cloud.aws.edge.sync_aws_regional_certificates"
        ), patch("apps.console.cloud.aws.backup.sync_aws_backup_assets"), patch(
            "apps.console.cloud.aws.backup.sync_aws_snapshots"
        ), patch(
            "apps.console.cloud.aws.data_services.sync_aws_data_service_assets"
        ) as data_sync, patch(
            "apps.console.cloud.aws.application_services.sync_aws_application_service_assets"
        ) as application_sync, patch(
            "apps.console.cloud.aws.delivery.sync_aws_delivery_assets"
        ) as delivery_sync, patch(
            "apps.console.cloud.aws.security_governance.sync_aws_security_governance_assets"
        ) as security_sync, patch(
            "apps.console.cloud.aws.credentials_config.sync_aws_credentials_config_assets"
        ) as credentials_sync, patch(
            "apps.console.cloud.aws.account_operations.sync_aws_account_operations_assets"
        ) as account_operations_sync:
            account.sync_assets()

        data_sync.assert_called_once_with(account)
        application_sync.assert_called_once_with(account)
        delivery_sync.assert_called_once_with(account)
        security_sync.assert_called_once_with(account)
        credentials_sync.assert_called_once_with(account)
        account_operations_sync.assert_called_once_with(account)


class AWSPriority1PolicyTests(SimpleTestCase):
    def test_policy_contains_priority1_read_actions_and_no_mutations(self):
        policy_path = Path(__file__).resolve().parents[1] / "aws-cloudmoo-readonly-policy.json"
        policy = json.loads(policy_path.read_text())
        actions = {
            action.lower()
            for statement in policy["Statement"]
            for action in statement["Action"]
        }
        required = {
            "rds:describedb*",
            "elasticache:describecacheclusters",
            "elasticache:describereplicationgroups",
            "elasticache:describeserverlesscaches",
            "memorydb:describeclusters",
            "es:listdomainnames",
            "es:describedomain",
            "elasticfilesystem:describefilesystems",
            "elasticfilesystem:describemounttargets",
            "fsx:describefilesystems",
            "apigateway:get",
            "events:listeventbuses",
            "events:describeeventbus",
            "events:listrules",
            "events:describerule",
            "scheduler:listschedules",
            "scheduler:getschedule",
            "pipes:listpipes",
            "pipes:describepipe",
            "sns:listtopics",
            "sns:gettopicattributes",
            "sqs:listqueues",
            "sqs:getqueueattributes",
            "states:liststatemachines",
            "states:describestatemachine",
            "athena:listworkgroups",
            "athena:getworkgroup",
            "athena:listdatacatalogs",
            "athena:getdatacatalog",
            "cloudformation:liststacks",
            "cloudformation:describestacks",
            "cloudformation:liststackresources",
            "elasticbeanstalk:describeapplications",
            "elasticbeanstalk:describeenvironments",
            "codebuild:listprojects",
            "codebuild:batchgetprojects",
            "codebuild:listbuildsforproject",
            "codebuild:batchgetbuilds",
            "codepipeline:listpipelines",
            "codepipeline:getpipeline",
            "codepipeline:listpipelineexecutions",
            "codepipeline:getpipelineexecution",
        }
        self.assertTrue(required.issubset(actions))

        forbidden_prefixes = (
            "create",
            "put",
            "update",
            "delete",
            "start",
            "stop",
            "attach",
            "detach",
            "modify",
            "allocate",
            "release",
            "restore",
        )
        for action in actions:
            self.assertFalse(
                action.split(":", 1)[1].startswith(forbidden_prefixes),
                action,
            )
