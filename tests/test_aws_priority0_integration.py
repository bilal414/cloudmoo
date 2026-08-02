import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.apps import apps
from django.test import SimpleTestCase

from apps.console.cloud.aws.models import CoreAWSACMCertificate, CoreAWSAccount, CoreAWSSnapshot
from apps.console.cloud.models import CoreCloud
from apps.console.utils.models import UtilAsset
from apps.monitoring.checks import get_check_function


PRIORITY0_TYPES = {
    "aws_cloudwatch_alarm",
    "aws_cloudwatch_metric",
    "aws_log_group",
    "aws_ecr_repository",
    "aws_ecr_image",
    "aws_ecs_task_definition",
    "aws_ecs_deployment",
    "aws_eks_cluster",
    "aws_eks_node_group",
    "aws_eks_addon",
    "aws_eks_fargate_profile",
    "aws_apprunner_service",
    "aws_apprunner_deployment",
    "aws_route53_zone",
    "aws_route53_record",
    "aws_cloudfront_distribution",
    "aws_cloudfront_origin_access_control",
    "aws_waf_web_acl",
    "aws_global_accelerator",
    "aws_backup_vault",
    "aws_backup_plan",
    "aws_backup_recovery_point",
    "aws_backup_job",
    "aws_backup_copy_job",
    "vpc",
    "subnet",
    "route_table",
    "internet_gateway",
    "nat_gateway",
    "network_acl",
    "network_interface",
    "vpc_peering",
    "transit_gateway_attachment",
    "vpn_connection",
    "flow_log",
    "auto_scaling_group",
    "launch_template",
    "ami",
    "ebs_attachment",
}


MODEL_NAMES = {
    "CoreAWSCloudWatchAlarm",
    "CoreAWSCloudWatchMetric",
    "CoreAWSLogGroup",
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
    "CoreAWSRoute53Zone",
    "CoreAWSRoute53Record",
    "CoreAWSCloudFrontDistribution",
    "CoreAWSCloudFrontOriginAccessControl",
    "CoreAWSWAFWebACL",
    "CoreAWSGlobalAccelerator",
    "CoreAWSBackupVault",
    "CoreAWSBackupPlan",
    "CoreAWSBackupRecoveryPoint",
    "CoreAWSBackupJob",
    "CoreAWSBackupCopyJob",
    "CoreAWSVPC",
    "CoreAWSSubnet",
    "CoreAWSRouteTable",
    "CoreAWSInternetGateway",
    "CoreAWSNATGateway",
    "CoreAWSNetworkACL",
    "CoreAWSNetworkInterface",
    "CoreAWSVPCPeering",
    "CoreAWSTransitGatewayAttachment",
    "CoreAWSVPNConnection",
    "CoreAWSFlowLog",
    "CoreAWSAutoScalingGroup",
    "CoreAWSLaunchTemplate",
    "CoreAWSAMI",
    "CoreAWSEBSVolumeAttachment",
}


class AWSPriority0ModelIntegrationTests(SimpleTestCase):
    def test_models_are_registered_and_asset_values_are_exact(self):
        registered_names = {
            model.__name__
            for model in apps.get_app_config("apps").get_models()
        }
        self.assertTrue(MODEL_NAMES.issubset(registered_names))

        values = {value for value, _label in UtilAsset.Type.choices}
        self.assertTrue(PRIORITY0_TYPES.issubset(values))
        self.assertIn("vpc", values)
        self.assertIn("nat_gateway", values)
        self.assertIn("vpc_peering", values)
        self.assertIn("snapshot", values)
        self.assertIn("acm_certificate", values)

    def test_cloud_asset_relations_use_resolved_provider_accessors(self):
        relations = dict(CoreCloud.ASSET_RELATIONS)
        self.assertEqual(relations["coreawsvpc_assets"], "vpc")
        self.assertEqual(relations["coreawsbackupvault_assets"], "aws_backup_vault")
        self.assertEqual(relations["cloudwatch_alarms"], "aws_cloudwatch_alarm")
        self.assertEqual(relations["aws_ecr_repositories"], "aws_ecr_repository")
        self.assertEqual(relations["route53_zones"], "aws_route53_zone")

    @patch("apps.console.cloud.aws.models.timezone.now")
    def test_account_sync_orchestrates_core_priority0_and_lightsail(self, now):
        now.return_value = SimpleNamespace()
        account = CoreAWSAccount()

        legacy_syncs = (
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
            "sync_acm_certificates",
            "sync_snapshots",
        )
        legacy_mocks = {name: Mock() for name in legacy_syncs}
        with patch.object(account, "save") as save, patch.multiple(
            account,
            **legacy_mocks,
        ), patch("apps.console.cloud.aws.network.sync_aws_network_assets") as network, patch(
            "apps.console.cloud.aws.observability.sync_aws_observability_assets"
        ) as observability, patch(
            "apps.console.cloud.aws.containers.sync_aws_container_assets"
        ) as containers, patch(
            "apps.console.cloud.aws.edge.sync_aws_edge_assets"
        ) as edge, patch(
            "apps.console.cloud.aws.edge.sync_aws_regional_certificates"
        ) as certificates, patch(
            "apps.console.cloud.aws.backup.sync_aws_backup_assets"
        ) as backups, patch(
            "apps.console.cloud.aws.backup.sync_aws_snapshots"
        ) as snapshots:
            account.sync_assets()

        for sync in (
            network,
            observability,
            containers,
            edge,
            certificates,
            backups,
            snapshots,
        ):
            sync.assert_called_once_with(account)
        legacy_mocks["sync_acm_certificates"].assert_not_called()
        legacy_mocks["sync_snapshots"].assert_not_called()
        legacy_mocks["sync_lightsail_assets"].assert_called_once_with()
        save.assert_called_once_with()

    def test_monitoring_dispatch_routes_each_aws_family_explicitly(self):
        expected_modules = {
            "vpc": "apps.monitoring.checks.aws_network",
            "subnet": "apps.monitoring.checks.aws_network",
            "aws_cloudwatch_alarm": "apps.monitoring.checks.aws_observability",
            "aws_ecr_repository": "apps.monitoring.checks.aws_containers",
            "aws_eks_cluster": "apps.monitoring.checks.aws_containers",
            "aws_route53_zone": "apps.monitoring.checks.aws_edge",
            "aws_waf_web_acl": "apps.monitoring.checks.aws_edge",
            "aws_backup_vault": "apps.monitoring.checks.aws_backup",
            "snapshot": "apps.monitoring.checks.aws_backup",
            "lightsail_alarm": "apps.monitoring.checks.aws_lightsail",
            "acm_certificate": "apps.monitoring.checks.aws",
            "server": "apps.monitoring.checks.aws",
        }
        for asset_type, expected_module in expected_modules.items():
            with self.subTest(asset_type=asset_type):
                check = get_check_function("aws", asset_type)
                self.assertTrue(callable(check))
                self.assertEqual(check.__module__, expected_module)

        self.assertEqual(
            get_check_function("digitalocean", "server").__module__,
            "apps.monitoring.checks.digitalocean",
        )

    def test_regional_acm_and_snapshot_context_is_not_persisted_as_credentials(self):
        owner = CoreAWSAccount(
            access_key="ACCESS-KEY",
            secret_key="SECRET-KEY",
            region="us-east-1",
        )
        certificate = CoreAWSACMCertificate(
            owner=owner,
            unique_id="arn:aws:acm:eu-west-1:123:certificate/example",
            type=UtilAsset.Type.ACM_CERTIFICATE,
            metadata={"_cloudmoo_region": "eu-west-1", "_cloudmoo_name": "example.test"},
        )
        snapshot = CoreAWSSnapshot(
            owner=owner,
            unique_id="eu-west-1|snap-123",
            type=UtilAsset.Type.SNAPSHOT,
            metadata={
                "_cloudmoo_region": "eu-west-1",
                "_cloudmoo_provider_id": "snap-123",
                "_cloudmoo_snapshot_kind": "ebs",
            },
        )

        self.assertEqual(certificate.monitoring_credentials["resource_region"], "eu-west-1")
        self.assertEqual(snapshot.monitoring_credentials["resource_region"], "eu-west-1")
        self.assertEqual(snapshot.monitoring_credentials["provider_id"], "snap-123")
        self.assertNotIn("ACCESS-KEY", certificate.metadata)
        self.assertNotIn("SECRET-KEY", certificate.metadata)
        self.assertNotIn("ACCESS-KEY", snapshot.metadata)
        self.assertNotIn("SECRET-KEY", snapshot.metadata)

        with patch(
            "apps.monitoring.checks.aws.check_aws_acm_certificate_status",
            return_value=("ISSUED", {"certificate": {}}),
        ) as acm_check, patch(
            "apps.monitoring.checks.aws_backup.check_aws_snapshot_status",
            return_value=("completed", {"snapshot": {}}),
        ) as snapshot_check:
            certificate.check_status()
            snapshot.check_status()

        acm_credentials = acm_check.call_args.args[1]
        snapshot_credentials = snapshot_check.call_args.args[1]
        self.assertEqual(snapshot_check.call_args.args[0], "snap-123")
        self.assertEqual(acm_credentials["region"], "eu-west-1")
        self.assertEqual(snapshot_credentials["region"], "eu-west-1")
        self.assertEqual(snapshot_credentials["snapshot_kind"], "ebs")

        rds_snapshot = CoreAWSSnapshot(
            owner=owner,
            unique_id="eu-west-1|rds-snapshot-123",
            type=UtilAsset.Type.SNAPSHOT,
            metadata={
                "_cloudmoo_region": "eu-west-1",
                "_cloudmoo_provider_id": "rds-snapshot-123",
                "_cloudmoo_snapshot_kind": "rds_instance",
            },
        )
        with patch(
            "apps.monitoring.checks.aws_backup.check_aws_rds_snapshot_status",
            return_value=("completed", {"snapshot": {}}),
        ) as rds_check:
            rds_snapshot.check_status()
        self.assertEqual(rds_check.call_args.args[0], "rds-snapshot-123")


class AWSPriority0PolicyTests(SimpleTestCase):
    def test_policy_contains_required_read_actions_only(self):
        policy_path = Path(__file__).resolve().parents[1] / "aws-cloudmoo-readonly-policy.json"
        policy = json.loads(policy_path.read_text())
        actions = {
            action.lower()
            for statement in policy["Statement"]
            for action in statement["Action"]
        }

        required = {
            "ec2:describe*",
            "autoscaling:describeautoscalinggroups",
            "rds:describedb*",
            "cloudwatch:describealarms",
            "cloudwatch:listmetrics",
            "cloudwatch:getmetricdata",
            "cloudwatch:getmetricstatistics",
            "logs:describeloggroups",
            "ecr:describe*",
            "ecr:list*",
            "ecr:scan*",
            "ecs:list*",
            "ecs:describe*",
            "eks:list*",
            "eks:describe*",
            "apprunner:list*",
            "apprunner:describe*",
            "route53:list*",
            "route53:get*",
            "cloudfront:list*",
            "cloudfront:get*",
            "wafv2:list*",
            "wafv2:get*",
            "globalaccelerator:list*",
            "globalaccelerator:describe*",
            "acm:list*",
            "acm:describe*",
            "backup:list*",
            "backup:describe*",
            "backup:get*",
            "sts:getcalleridentity",
        }
        self.assertTrue(required.issubset(actions))

        forbidden_prefixes = (
            "create",
            "update",
            "delete",
            "terminate",
            "reboot",
            "modify",
            "associate",
            "disassociate",
            "put",
        )
        for action in actions:
            self.assertFalse(
                action.split(":", 1)[1].startswith(forbidden_prefixes),
                action,
            )
