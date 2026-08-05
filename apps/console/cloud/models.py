import logging
import uuid
from django.utils import timezone

from django.db import models
from django.utils.text import slugify
from model_utils.models import TimeStampedModel

from apps.console.account.models import CoreAccount
from apps.monitoring.models import AssetMonitoringState, AssetStatusEmail, AssetStatusLog
from apps.monitoring.schedules import (
    asset_schedule_delete,
    asset_schedule_update,
    cloud_schedule_create,
    cloud_schedule_delete,
    cloud_schedule_update,
)

logger = logging.getLogger(__name__)


class CloudValidationTransientError(Exception):
    """The provider could not be reached or returned a transient failure."""


class CloudInventoryTransientError(Exception):
    """The provider returned an incomplete inventory response."""


def validate_provider_response(response, provider_name):
    """Normalize provider validation responses without exposing credentials."""
    status_code = getattr(response, 'status_code', None)
    if status_code is not None and 200 <= status_code < 300:
        try:
            response.json()
        except (AttributeError, ValueError) as error:
            raise CloudValidationTransientError(
                f"{provider_name} returned an invalid validation response"
            ) from error
        return True

    # Client errors indicate invalid or insufficient credentials. Rate limits
    # and server errors are transient and must not disable monitoring.
    if status_code is not None and 400 <= status_code < 500 and status_code != 429:
        return False

    raise CloudValidationTransientError(
        f"{provider_name} validation temporarily unavailable"
    )


def require_inventory_list(payload, path, provider_name):
    """Return a provider inventory list or fail closed for a retry.

    An omitted collection is not equivalent to an empty inventory. Treating a
    malformed response as empty would mark every locally known asset as gone.
    """
    current = payload
    for key in path:
        if not isinstance(current, dict) or key not in current:
            raise CloudInventoryTransientError(
                f"{provider_name} returned an incomplete inventory response"
            )
        current = current[key]

    if not isinstance(current, list):
        raise CloudInventoryTransientError(
            f"{provider_name} returned an invalid inventory collection"
        )
    return current



class CoreCloudServiceProvider(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        DISABLED = "disabled", "Disabled"

    name = models.CharField(max_length=100, unique=True)
    code = models.CharField(max_length=64, unique=True)
    position = models.IntegerField(null=True)
    url = models.URLField(null=True)
    image = models.CharField(null=True, max_length=2048)
    status = models.CharField(max_length=64, choices=Status.choices, default=Status.ACTIVE)

    class Meta:
        db_table = "core_cloud_service_provider"

    def __str__(self):
        return self.name


class CoreCloudManager(models.Manager):
    def for_user(self, user):
        if user.is_superuser:
            return self.get_queryset()
        return self.get_queryset().filter(account=user.member.active_account)

class CoreCloud(TimeStampedModel):
    objects = CoreCloudManager()

    # All provider asset relations are registered here so inventory, cleanup,
    # scheduling, the asset list, and the dashboard cannot silently diverge
    # when a provider adds a new resource type.
    ASSET_RELATIONS = (
        ('servers', 'server'),
        ('volumes', 'volume'),
        # Hetzner Cloud's additional read-only inventory families use the
        # abstract resource base's class-qualified related names.
        ('corehetznerprimaryip_assets', 'primary_ip'),
        ('corehetznerfloatingip_assets', 'floating_ip'),
        ('corehetznernetwork_assets', 'network'),
        ('corehetznerfirewall_assets', 'firewall'),
        ('corehetznerloadbalancer_assets', 'load_balancer'),
        ('corehetznerplacementgroup_assets', 'placement_group'),
        ('corehetznerimage_assets', 'image'),
        ('corehetznercertificate_assets', 'certificate'),
        ('corehetznerlocation_assets', 'location'),
        ('corehetznerdatacenter_assets', 'datacenter'),
        ('corehetznerservertype_assets', 'server_type'),
        ('corehetzneriso_assets', 'iso'),
        ('corehetznersshkey_assets', 'ssh_key'),
        ('corehetznerloadbalancertype_assets', 'load_balancer_type'),
        ('corehetznerzone_assets', 'zone'),
        ('corehetznerrset_assets', 'rrset'),
        ('corehetzneraction_assets', 'action'),
        ('corehetznerobjectstoragebucket_assets', 'object_storage'),
        ('databases', 'database'),
        ('rds_databases', 'rds_database'),
        ('lambda_functions', 'lambda'),
        ('dynamodb_tables', 'dynamodb'),
        ('s3_buckets', 's3_bucket'),
        ('acm_certificates', 'acm_certificate'),
        ('snapshots', 'snapshot'),
        ('backups', 'backup'),
        ('elastic_ips', 'elastic_ip'),
        ('reserved_ips', 'reserved_ip'),
        ('load_balancers', 'load_balancer'),
        ('security_groups', 'security_group'),
        ('firewalls', 'firewall'),
        ('apps', 'app_platform'),
        ('spaces', 'object_storage'),
        ('container_registries', 'container_registry'),
        ('ecs_services', 'ecs_service'),
        ('ecs_tasks', 'ecs_task'),
        ('kubernetes_clusters', 'kubernetes_cluster'),
        ('kubernetes_node_pools', 'kubernetes_node_pool'),
        ('vpcs', 'vpc'),
        ('vpc_peerings', 'vpc_peering'),
        ('vpc_nat_gateways', 'nat_gateway'),
        ('domains', 'domain'),
        ('dns_records', 'dns_record'),
        ('cdn_endpoints', 'cdn_endpoint'),
        ('certificates', 'certificate'),
        ('lightsail_instances', 'lightsail_instance'),
        ('lightsail_disks', 'lightsail_disk'),
        ('lightsail_instance_snapshots', 'lightsail_instance_snapshot'),
        ('lightsail_disk_snapshots', 'lightsail_disk_snapshot'),
        ('lightsail_static_ips', 'lightsail_static_ip'),
        ('lightsail_databases', 'lightsail_database'),
        ('lightsail_database_snapshots', 'lightsail_database_snapshot'),
        ('lightsail_load_balancers', 'lightsail_load_balancer'),
        ('lightsail_certificates', 'lightsail_certificate'),
        ('lightsail_buckets', 'lightsail_bucket'),
        ('lightsail_distributions', 'lightsail_distribution'),
        ('lightsail_domains', 'lightsail_domain'),
        ('lightsail_dns_records', 'lightsail_dns_record'),
        ('lightsail_container_services', 'lightsail_container_service'),
        ('lightsail_container_deployments', 'lightsail_container_deployment'),
        ('lightsail_container_images', 'lightsail_container_image'),
        ('lightsail_alarms', 'lightsail_alarm'),
        ('lightsail_operations', 'lightsail_operation'),
        ('lightsail_auto_snapshots', 'lightsail_auto_snapshot'),

        # AWS Priority 0 networking/EC2 dependencies.  These models use the
        # abstract base's ``%(class)s_assets`` related-name pattern, so the
        # resolved Django accessors are intentionally class-qualified.
        ('coreawsvpc_assets', 'vpc'),
        ('coreawssubnet_assets', 'subnet'),
        ('coreawsroutetable_assets', 'route_table'),
        ('coreawsinternetgateway_assets', 'internet_gateway'),
        ('coreawsnatgateway_assets', 'nat_gateway'),
        ('coreawsnetworkacl_assets', 'network_acl'),
        ('coreawsnetworkinterface_assets', 'network_interface'),
        ('coreawsvpcpeering_assets', 'vpc_peering'),
        ('coreawstransitgatewayattachment_assets', 'transit_gateway_attachment'),
        ('coreawsvpnconnection_assets', 'vpn_connection'),
        ('coreawsflowlog_assets', 'flow_log'),
        ('coreawsautoscalinggroup_assets', 'auto_scaling_group'),
        ('coreawslaunchtemplate_assets', 'launch_template'),
        ('coreawsami_assets', 'ami'),
        ('coreawsebsvolumeattachment_assets', 'ebs_attachment'),

        # AWS Priority 0 observability, container, edge, and backup models
        # use explicit related names in their provider modules.
        ('cloudwatch_alarms', 'aws_cloudwatch_alarm'),
        ('cloudwatch_metrics', 'aws_cloudwatch_metric'),
        ('log_groups', 'aws_log_group'),
        ('aws_ecr_repositories', 'aws_ecr_repository'),
        ('aws_ecr_images', 'aws_ecr_image'),
        ('aws_ecs_task_definitions', 'aws_ecs_task_definition'),
        ('aws_ecs_deployments', 'aws_ecs_deployment'),
        ('aws_eks_clusters', 'aws_eks_cluster'),
        ('aws_eks_node_groups', 'aws_eks_node_group'),
        ('aws_eks_addons', 'aws_eks_addon'),
        ('aws_eks_fargate_profiles', 'aws_eks_fargate_profile'),
        ('aws_apprunner_services', 'aws_apprunner_service'),
        ('aws_apprunner_deployments', 'aws_apprunner_deployment'),
        ('route53_zones', 'aws_route53_zone'),
        ('route53_records', 'aws_route53_record'),
        ('cloudfront_distributions', 'aws_cloudfront_distribution'),
        ('cloudfront_origin_access_controls', 'aws_cloudfront_origin_access_control'),
        ('waf_web_acls', 'aws_waf_web_acl'),
        ('global_accelerators', 'aws_global_accelerator'),
        ('coreawsbackupvault_assets', 'aws_backup_vault'),
        ('coreawsbackupplan_assets', 'aws_backup_plan'),
        ('coreawsbackuprecoverypoint_assets', 'aws_backup_recovery_point'),
        ('coreawsbackupjob_assets', 'aws_backup_job'),
        ('coreawsbackupcopyjob_assets', 'aws_backup_copy_job'),

        # AWS Priority 1 data-service models use the concrete class-qualified
        # related_name generated by the provider module's abstract owner FK.
        ('coreawsrdscluster_assets', 'aws_rds_cluster'),
        ('coreawselasticachecluster_assets', 'aws_elasticache_cluster'),
        ('coreawselasticachereplicationgroup_assets', 'aws_elasticache_replication_group'),
        ('coreawselasticacheserverlesscache_assets', 'aws_elasticache_serverless_cache'),
        ('coreawsmemorydbcluster_assets', 'aws_memorydb_cluster'),
        ('coreawsopensearchdomain_assets', 'aws_opensearch_domain'),
        ('coreawsefsfilesystem_assets', 'aws_efs_file_system'),
        ('coreawsfsxfilesystem_assets', 'aws_fsx_file_system'),

        # AWS Priority 1 application/integration models use the same
        # class-qualified related_name convention.
        ('coreawsapigatewayrestapi_assets', 'aws_apigateway_rest_api'),
        ('coreawsapigatewayv2api_assets', 'aws_apigateway_v2_api'),
        ('coreawseventbridgebus_assets', 'aws_eventbridge_bus'),
        ('coreawseventbridgerule_assets', 'aws_eventbridge_rule'),
        ('coreawseventbridgeschedule_assets', 'aws_eventbridge_schedule'),
        ('coreawseventbridgepipe_assets', 'aws_eventbridge_pipe'),
        ('coreawssnstopic_assets', 'aws_sns_topic'),
        ('coreawssqsqueue_assets', 'aws_sqs_queue'),
        ('coreawsstepfunctionsstatemachine_assets', 'aws_stepfunctions_state_machine'),
        ('coreawsathenaworkgroup_assets', 'aws_athena_workgroup'),
        ('coreawsathenadatacatalog_assets', 'aws_athena_data_catalog'),
        ('coreawscloudformationstack_assets', 'aws_cloudformation_stack'),

        # AWS Priority 1 delivery models use the same class-qualified
        # related_name convention.
        ('coreawselasticbeanstalkapplication_assets', 'aws_elastic_beanstalk_application'),
        ('coreawselasticbeanstalkenvironment_assets', 'aws_elastic_beanstalk_environment'),
        ('coreawscodebuildproject_assets', 'aws_codebuild_project'),
        ('coreawscodebuildbuild_assets', 'aws_codebuild_build'),
        ('coreawscodepipelinepipeline_assets', 'aws_codepipeline_pipeline'),
        ('coreawscodepipelineexecution_assets', 'aws_codepipeline_execution'),

        # AWS Priority 2 security and governance models use the concrete
        # class-qualified related name generated by their abstract owner FK.
        ('coreawsiamuser_assets', 'aws_iam_user'),
        ('coreawsiamrole_assets', 'aws_iam_role'),
        ('coreawsiampolicy_assets', 'aws_iam_policy'),
        ('coreawskmskey_assets', 'aws_kms_key'),
        ('coreawskmsalias_assets', 'aws_kms_alias'),
        ('coreawscloudtrailtrail_assets', 'aws_cloudtrail_trail'),
        ('coreawsconfigrule_assets', 'aws_config_rule'),
        ('coreawsconfigrecorder_assets', 'aws_config_recorder'),
        ('coreawsguarddutydetector_assets', 'aws_guardduty_detector'),
        ('coreawssecurityhub_assets', 'aws_security_hub'),
        ('coreawsinspector_assets', 'aws_inspector'),
        ('coreawsmacie_assets', 'aws_macie'),
        ('coreawsfirewallmanagerpolicy_assets', 'aws_firewall_manager_policy'),

        # AWS credential/configuration metadata remains regional and stores
        # identifiers and posture only; values are never persisted.
        ('coreawssecretsmanagersecret_assets', 'aws_secrets_manager_secret'),
        ('coreawsssmparameter_assets', 'aws_ssm_parameter'),

        # AWS account operations and FinOps signals are account-scoped assets
        # persisted at the control-plane Region for endpoint/audit context.
        ('coreawshealthevent_assets', 'aws_health_event'),
        ('coreawstrustedadvisorcheck_assets', 'aws_trusted_advisor_check'),
        ('coreawscostexplorersignal_assets', 'aws_cost_explorer_signal'),
        ('coreawscostanomalymonitor_assets', 'aws_cost_anomaly_monitor'),
        ('coreawscostanomalysubscription_assets', 'aws_cost_anomaly_subscription'),
        ('coreawscostanomaly_assets', 'aws_cost_anomaly'),
    )

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        PAUSED = "paused", "Paused"
        SUSPENDED = "suspended", "Suspended"
        DELETE = "delete", "Delete"
        INVALID_AUTH = "invalid_auth", "Invalid Authentication"

    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    status = models.CharField(max_length=64, choices=Status.choices, default=Status.ACTIVE)
    account = models.ForeignKey(CoreAccount, on_delete=models.CASCADE, related_name="clouds")
    provider = models.ForeignKey(CoreCloudServiceProvider, on_delete=models.CASCADE, related_name="clouds")
    last_synced = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "core_cloud"
        verbose_name = "Cloud"
        verbose_name_plural = "Clouds"
    def __str__(self):
        return self.name

    @property
    def key(self):
        return slugify(f"cm__{self.account.id}__{self.provider.code}__{self.uuid}")

    @property
    def name(self):
        """
        Get the name from the associated provider account
        """
        provider_code = self.provider.code.lower()
        try:
            provider_account = getattr(self, provider_code).first()
            if provider_account:
                return provider_account.name
            return f"Unnamed {self.provider.name} Account"
        except AttributeError:
            return f"Unnamed {self.provider.name} Account"


    def save(self, *args, **kwargs):
        is_new = self._state.adding
        status_changed = False
        update_fields = kwargs.get('update_fields')
        if not is_new:
            try:
                original_status = type(self).objects.only('status').get(pk=self.pk).status
                status_changed = original_status != self.status
            except type(self).DoesNotExist:
                pass

        super().save(*args, **kwargs)
        if is_new:
            try:
                cloud_schedule_create(self)
            except Exception as e:
                logger.warning(
                    f"Could not create asset sync schedule for cloud {self.pk}: {e}. "
                    f"Run 'python manage.py create_all_cloud_schedules --confirm' to recreate schedules."
                )
        elif status_changed and (not update_fields or 'status' in update_fields):
            try:
                cloud_schedule_update(self)
            except Exception as e:
                logger.warning(
                    f"Could not update asset sync schedule for cloud {self.pk}: {e}. "
                    f"Schedules can be recreated with 'python manage.py create_all_cloud_schedules --confirm'."
                )

    def delete(self, *args, **kwargs):
        """
        Override delete method to handle cleanup before deletion.
        Schedule/monitoring-data cleanup failures are logged but never block
        local deletion.
        """
        try:
            # Delete monitoring data while the asset rows (and their keys) still exist
            self.delete_monitoring_data()

            # Delete all assets; each asset removes its own schedule on delete
            self.delete_all_assets()

            # Delete the cloud's own sync schedule
            cloud_schedule_delete(self)

        except Exception as e:
            logger.warning(
                f"Schedule cleanup for cloud {self.pk} failed: {e}. "
                f"Continuing with local deletion; check for orphaned periodic tasks."
            )

        # Finally, perform the actual deletion
        super().delete(*args, **kwargs)

    @property
    def provider_account(self):
        provider_code = self.provider.code.lower()
        try:
            return getattr(self, f"{provider_code}").first()
        except AttributeError:
            raise NotImplementedError(f"Provider {provider_code} not implemented for this cloud")

    def validate(self):
        try:
            return self.provider_account.validate()
        except NotImplementedError:
            raise NotImplementedError("Validation not implemented for this cloud provider")

    def _asset_relations_for_provider(self):
        """Return the shared and provider-specific asset relations.

        Vultr's resource families are intentionally split across modules.  A
        dynamic hook keeps the inventory list, monitoring scheduler, cleanup,
        and dashboard in sync without making the core model import every
        provider model during Django app initialization.
        """
        relations = list(self.ASSET_RELATIONS)
        try:
            provider_code = self.provider.code.lower()
        except AttributeError:
            return tuple(relations)
        if provider_code == "vultr":
            from apps.console.cloud.vultr.integration import get_vultr_asset_relations

            for relation in get_vultr_asset_relations():
                if relation not in relations:
                    relations.append(relation)
        return tuple(relations)

    def get_all_assets(self):
        """
        Gets all assets associated with this cloud.
        Returns a list of tuples containing (asset, asset_type) pairs.
        """
        provider_account = self.provider_account
        assets = []
        for relation_name, asset_type in self._asset_relations_for_provider():
            manager = getattr(provider_account, relation_name, None)
            if manager is not None:
                assets.extend((asset, asset_type) for asset in manager.all())
        return assets

    def get_active_assets(self):
        from apps.console.utils.models import UtilAsset

        """
        Gets all active assets (excluding NO_LONGER_EXISTS) associated with this cloud.
        Returns a list of tuples containing (asset, asset_type) pairs.
        """
        provider_account = self.provider_account
        assets = []
        for relation_name, asset_type in self._asset_relations_for_provider():
            manager = getattr(provider_account, relation_name, None)
            if manager is not None:
                active_assets = manager.exclude(
                    monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
                )
                assets.extend((asset, asset_type) for asset in active_assets)
        return assets

    def get_monitored_assets(self):
        from apps.console.utils.models import UtilAsset

        """
        Gets all actively monitored assets associated with this cloud.
        Returns a list of tuples containing (asset, asset_type) pairs.
        """
        provider_account = self.provider_account
        assets = []
        for relation_name, asset_type in self._asset_relations_for_provider():
            manager = getattr(provider_account, relation_name, None)
            if manager is not None:
                monitored_assets = manager.filter(
                    monitoring=UtilAsset.Monitoring.ACTIVE
                )
                assets.extend((asset, asset_type) for asset in monitored_assets)
        return assets

    def delete_all_assets(self):
        """
        Deletes all assets associated with this cloud.
        Each asset removes its own schedule and monitoring data on delete.
        """
        try:
            provider_account = self.provider_account
            for relation_name, _asset_type in self._asset_relations_for_provider():
                manager = getattr(provider_account, relation_name, None)
                if manager is None:
                    continue
                for asset in manager.all():
                    try:
                        asset.delete()
                    except Exception as e:
                        logger.warning(
                            "Error deleting %s asset %s: %s",
                            relation_name,
                            asset.name,
                            e,
                        )

        except Exception as e:
            logger.warning("Error during asset deletion for cloud %s: %s", self.name, e)
            raise

    def delete_all_asset_schedules(self):
        """
        Deletes the status-check schedules for all assets associated with this cloud
        """
        try:
            # Remove any existing status task, including one left behind for an
            # asset that a provider no longer returns.
            monitored_assets = self.get_all_assets()

            # Delete schedules for all monitored assets
            for asset, asset_type in monitored_assets:
                asset_schedule_delete(asset)

        except Exception as e:
            print(f"Error deleting asset schedules for cloud {self.name}: {str(e)}")
            raise

    def create_all_asset_schedules(self):
        from apps.console.utils.models import UtilAsset

        """
        Reconcile schedules for all locally known assets.
        This is useful when reactivating a cloud or fixing authentication issues.
        """
        try:
            # Reconcile every locally known asset. This repairs stale task
            # definitions and removes tasks for assets that disappeared from
            # the provider, while preserving disabled assets as disabled.
            for asset, asset_type in self.get_all_assets():
                if asset.monitoring == UtilAsset.Monitoring.NO_LONGER_EXISTS:
                    asset_schedule_delete(asset)
                else:
                    asset_schedule_update(asset)
        except Exception as e:
            print(f"Error creating asset schedules for cloud {self.name}: {str(e)}")
            raise

    def delete_monitoring_data(self):
        """
        Deletes all monitoring data (status logs and notification emails)
        related to this cloud's assets.
        """
        try:
            # Get all assets for this cloud
            asset_keys = [asset.key for asset, _ in self.get_all_assets()]

            AssetStatusLog.objects.filter(asset_key__in=asset_keys).delete()
            AssetStatusEmail.objects.filter(asset_key__in=asset_keys).delete()
            AssetMonitoringState.objects.filter(asset_key__in=asset_keys).delete()

        except Exception as e:
            print(f"Error deleting monitoring data for cloud {self.name}: {str(e)}")
            pass

    def sync_assets(self):
        try:
            self.provider_account.sync_assets()
            self.last_synced = timezone.now()
            self.save()
        except NotImplementedError:
            raise NotImplementedError("Asset synchronization not implemented for this cloud provider")
