from django.views.generic import TemplateView
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Sum, Case, When, IntegerField
from django.db.models import Count, Q

from apps.console.cloud.aws.models import CoreAWSInstance, CoreAWSVolume, CoreAWSRDSDatabase, CoreAWSLambda, CoreAWSDynamoDB, CoreAWSS3Bucket, CoreAWSACMCertificate, CoreAWSSnapshot, CoreAWSElasticIP, CoreAWSLoadBalancer, CoreAWSSecurityGroup, CoreAWSECSService, CoreAWSECSTask
from apps.console.cloud.aws.lightsail import (
    CoreAWSLightsailAlarm,
    CoreAWSLightsailAutoSnapshot,
    CoreAWSLightsailBucket,
    CoreAWSLightsailCertificate,
    CoreAWSLightsailContainerDeployment,
    CoreAWSLightsailContainerImage,
    CoreAWSLightsailContainerService,
    CoreAWSLightsailDNSRecord,
    CoreAWSLightsailDatabase,
    CoreAWSLightsailDatabaseSnapshot,
    CoreAWSLightsailDisk,
    CoreAWSLightsailDiskSnapshot,
    CoreAWSLightsailDistribution,
    CoreAWSLightsailDomain,
    CoreAWSLightsailInstance,
    CoreAWSLightsailInstanceSnapshot,
    CoreAWSLightsailLoadBalancer,
    CoreAWSLightsailOperation,
    CoreAWSLightsailStaticIP,
)
from apps.console.cloud.aws.network import AWS_NETWORK_COLLECTION_SPECS
from apps.console.cloud.aws.observability import AWS_OBSERVABILITY_ASSET_MODELS
from apps.console.cloud.aws.containers import AWS_CONTAINER_ASSET_MODELS
from apps.console.cloud.aws.edge import AWS_EDGE_ASSET_MODELS
from apps.console.cloud.aws.backup import AWS_BACKUP_ASSET_MODELS
from apps.console.cloud.aws.data_services import AWS_DATA_SERVICE_ASSET_MODELS
from apps.console.cloud.aws.application_services import AWS_APPLICATION_ASSET_MODELS
from apps.console.cloud.aws.delivery import AWS_DELIVERY_ASSET_MODELS
from apps.console.cloud.aws.security_governance import AWS_SECURITY_GOVERNANCE_ASSET_MODELS
from apps.console.cloud.aws.credentials_config import AWS_CREDENTIALS_CONFIG_ASSET_MODELS
from apps.console.cloud.aws.account_operations import AWS_ACCOUNT_OPERATIONS_ASSET_MODELS
from apps.console.cloud.linode.models import CoreLinodeVolume, CoreLinodeServer
from apps.console.cloud.oracle.models import CoreOracleInstance, CoreOracleVolume
from apps.console.cloud.models import CoreCloud
from apps.console.cloud.digitalocean.models import (
    CoreDigitalOceanApp,
    CoreDigitalOceanBackup,
    CoreDigitalOceanCDNEndpoint,
    CoreDigitalOceanCertificate,
    CoreDigitalOceanContainerRegistry,
    CoreDigitalOceanDatabase,
    CoreDigitalOceanDNSRecord,
    CoreDigitalOceanDomain,
    CoreDigitalOceanFirewall,
    CoreDigitalOceanKubernetesCluster,
    CoreDigitalOceanKubernetesNodePool,
    CoreDigitalOceanLoadBalancer,
    CoreDigitalOceanReservedIP,
    CoreDigitalOceanServer,
    CoreDigitalOceanSnapshot,
    CoreDigitalOceanSpace,
    CoreDigitalOceanVolume,
    CoreDigitalOceanVPC,
    CoreDigitalOceanVPCNATGateway,
    CoreDigitalOceanVPCPeering,
)
from apps.console.cloud.hetzner.models import CoreHetznerServer, CoreHetznerVolume
from apps.console.cloud.upcloud.models import CoreUpCloudServer, CoreUpCloudVolume
from apps.console.cloud.vultr.models import CoreVultrServer, CoreVultrVolume, CoreVultrDatabase
from apps.console.utils.models import UtilAsset


_AWS_PRIORITY0_ASSET_MODELS = tuple(
    (spec['provider_type'].removeprefix('aws_'), spec['model'])
    for spec in AWS_NETWORK_COLLECTION_SPECS
)
_AWS_PRIORITY0_ASSET_MODELS += tuple(AWS_OBSERVABILITY_ASSET_MODELS.items())
_AWS_PRIORITY0_ASSET_MODELS += tuple(AWS_CONTAINER_ASSET_MODELS.items())
_AWS_PRIORITY0_ASSET_MODELS += tuple(AWS_EDGE_ASSET_MODELS.items())
_AWS_PRIORITY0_ASSET_MODELS += tuple(AWS_BACKUP_ASSET_MODELS.items())
_AWS_PRIORITY1_ASSET_MODELS = (
    tuple(AWS_DATA_SERVICE_ASSET_MODELS.items())
    + tuple(AWS_APPLICATION_ASSET_MODELS.items())
    + tuple(AWS_DELIVERY_ASSET_MODELS.items())
)
_AWS_PRIORITY2_ASSET_MODELS = (
    tuple(AWS_SECURITY_GOVERNANCE_ASSET_MODELS.items())
    + tuple(AWS_CREDENTIALS_CONFIG_ASSET_MODELS.items())
    + tuple(AWS_ACCOUNT_OPERATIONS_ASSET_MODELS.items())
)


class IndexView(LoginRequiredMixin, TemplateView):
    template_name = "console/home/index.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        active_account = user.member.active_account

        clouds = CoreCloud.objects.filter(account=active_account)

        if not clouds.exists():
            context['show_welcome'] = True
            return context

        # Prepare a dictionary to store asset counts for each cloud
        cloud_asset_counts = {cloud.id: {
            'servers': 0,
            'volumes': 0,
            'databases': 0,
            'lambda_functions': 0,
            'dynamodb_tables': 0,
            's3_buckets': 0,
            'acm_certificates': 0,
            'snapshots': 0,
            'backups': 0,
            'elastic_ips': 0,
            'reserved_ips': 0,
            'load_balancers': 0,
            'security_groups': 0,
            'firewalls': 0,
            'apps': 0,
            'spaces': 0,
            'container_registries': 0,
            'ecs_services': 0,
            'ecs_tasks': 0,
            'kubernetes_clusters': 0,
            'kubernetes_node_pools': 0,
            'vpcs': 0,
            'vpc_peerings': 0,
            'vpc_nat_gateways': 0,
            'domains': 0,
            'dns_records': 0,
            'cdn_endpoints': 0,
            'certificates': 0,
            'lightsail_misc': 0,
            **{asset_type: 0 for asset_type, _model in _AWS_PRIORITY0_ASSET_MODELS},
            **{asset_type: 0 for asset_type, _model in _AWS_PRIORITY1_ASSET_MODELS},
            **{asset_type: 0 for asset_type, _model in _AWS_PRIORITY2_ASSET_MODELS},
        } for cloud in clouds}

        # Count DigitalOcean assets
        do_counts = CoreDigitalOceanServer.objects.filter(
            owner__cloud__account=active_account
        ).exclude(
            monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
        ).values('owner__cloud').annotate(count=Count('id'))
        for item in do_counts:
            cloud_asset_counts[item['owner__cloud']]['servers'] += item['count']

        do_volume_counts = CoreDigitalOceanVolume.objects.filter(
            owner__cloud__account=active_account
        ).exclude(
            monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
        ).values('owner__cloud').annotate(count=Count('id'))
        for item in do_volume_counts:
            cloud_asset_counts[item['owner__cloud']]['volumes'] += item['count']

        do_db_counts = CoreDigitalOceanDatabase.objects.filter(
            owner__cloud__account=active_account
        ).exclude(
            monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
        ).values('owner__cloud').annotate(count=Count('id'))
        for item in do_db_counts:
            cloud_asset_counts[item['owner__cloud']]['databases'] += item['count']

        # Count DigitalOcean resources beyond Droplets and Volumes
        digitalocean_resource_models = (
            ('snapshots', CoreDigitalOceanSnapshot),
            ('backups', CoreDigitalOceanBackup),
            ('reserved_ips', CoreDigitalOceanReservedIP),
            ('firewalls', CoreDigitalOceanFirewall),
            ('load_balancers', CoreDigitalOceanLoadBalancer),
            ('apps', CoreDigitalOceanApp),
            ('spaces', CoreDigitalOceanSpace),
            ('container_registries', CoreDigitalOceanContainerRegistry),
            ('kubernetes_clusters', CoreDigitalOceanKubernetesCluster),
            ('kubernetes_node_pools', CoreDigitalOceanKubernetesNodePool),
            ('vpcs', CoreDigitalOceanVPC),
            ('vpc_peerings', CoreDigitalOceanVPCPeering),
            ('vpc_nat_gateways', CoreDigitalOceanVPCNATGateway),
            ('domains', CoreDigitalOceanDomain),
            ('dns_records', CoreDigitalOceanDNSRecord),
            ('cdn_endpoints', CoreDigitalOceanCDNEndpoint),
            ('certificates', CoreDigitalOceanCertificate),
        )
        for resource_name, resource_model in digitalocean_resource_models:
            resource_counts = resource_model.objects.filter(
                owner__cloud__account=active_account
            ).exclude(
                monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
            ).values('owner__cloud').annotate(count=Count('id'))
            for item in resource_counts:
                cloud_asset_counts[item['owner__cloud']][resource_name] += item['count']

        # Count Hetzner assets
        hetzner_counts = CoreHetznerServer.objects.filter(
            owner__cloud__account=active_account
        ).exclude(
            monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
        ).values('owner__cloud').annotate(count=Count('id'))
        for item in hetzner_counts:
            cloud_asset_counts[item['owner__cloud']]['servers'] += item['count']

        hetzner_volume_counts = CoreHetznerVolume.objects.filter(
            owner__cloud__account=active_account
        ).exclude(
            monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
        ).values('owner__cloud').annotate(count=Count('id'))
        for item in hetzner_volume_counts:
            cloud_asset_counts[item['owner__cloud']]['volumes'] += item['count']

        # Count Vultr assets
        vultr_counts = CoreVultrServer.objects.filter(
            owner__cloud__account=active_account
        ).exclude(
            monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
        ).values('owner__cloud').annotate(count=Count('id'))
        for item in vultr_counts:
            cloud_asset_counts[item['owner__cloud']]['servers'] += item['count']

        vultr_volume_counts = CoreVultrVolume.objects.filter(
            owner__cloud__account=active_account
        ).exclude(
            monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
        ).values('owner__cloud').annotate(count=Count('id'))
        for item in vultr_volume_counts:
            cloud_asset_counts[item['owner__cloud']]['volumes'] += item['count']

        vultr_db_counts = CoreVultrDatabase.objects.filter(
            owner__cloud__account=active_account
        ).exclude(
            monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
        ).values('owner__cloud').annotate(count=Count('id'))
        for item in vultr_db_counts:
            cloud_asset_counts[item['owner__cloud']]['databases'] += item['count']

        # Update the counts for AWS
        aws_counts = CoreAWSInstance.objects.filter(
            owner__cloud__account=active_account
        ).exclude(
            monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
        ).values('owner__cloud').annotate(count=Count('id'))
        for item in aws_counts:
            cloud_asset_counts[item['owner__cloud']]['servers'] += item['count']

        # Update the counts for AWS
        aws_volume_counts = CoreAWSVolume.objects.filter(
            owner__cloud__account=active_account
        ).exclude(
            monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
        ).values('owner__cloud').annotate(count=Count('id'))
        for item in aws_volume_counts:
            cloud_asset_counts[item['owner__cloud']]['volumes'] += item['count']

        # Count AWS RDS databases
        aws_rds_db_counts = CoreAWSRDSDatabase.objects.filter(
            owner__cloud__account=active_account
        ).exclude(
            monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
        ).values('owner__cloud').annotate(count=Count('id'))
        for item in aws_rds_db_counts:
            cloud_asset_counts[item['owner__cloud']]['databases'] += item['count']

        # Count AWS Lambda functions
        aws_lambda_counts = CoreAWSLambda.objects.filter(
            owner__cloud__account=active_account
        ).exclude(
            monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
        ).values('owner__cloud').annotate(count=Count('id'))
        for item in aws_lambda_counts:
            cloud_asset_counts[item['owner__cloud']]['lambda_functions'] += item['count']

        # Count AWS DynamoDB tables
        aws_dynamodb_counts = CoreAWSDynamoDB.objects.filter(
            owner__cloud__account=active_account
        ).exclude(
            monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
        ).values('owner__cloud').annotate(count=Count('id'))
        for item in aws_dynamodb_counts:
            cloud_asset_counts[item['owner__cloud']]['dynamodb_tables'] += item['count']

        # Count AWS S3 buckets
        aws_s3_counts = CoreAWSS3Bucket.objects.filter(
            owner__cloud__account=active_account
        ).exclude(
            monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
        ).values('owner__cloud').annotate(count=Count('id'))
        for item in aws_s3_counts:
            cloud_asset_counts[item['owner__cloud']]['s3_buckets'] += item['count']

        # Count AWS ACM certificates
        aws_acm_counts = CoreAWSACMCertificate.objects.filter(
            owner__cloud__account=active_account
        ).exclude(
            monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
        ).values('owner__cloud').annotate(count=Count('id'))
        for item in aws_acm_counts:
            cloud_asset_counts[item['owner__cloud']]['acm_certificates'] += item['count']

        # Count AWS snapshots
        aws_snapshot_counts = CoreAWSSnapshot.objects.filter(
            owner__cloud__account=active_account
        ).exclude(
            monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
        ).values('owner__cloud').annotate(count=Count('id'))
        for item in aws_snapshot_counts:
            cloud_asset_counts[item['owner__cloud']]['snapshots'] += item['count']

        # Count AWS Elastic IPs
        aws_eip_counts = CoreAWSElasticIP.objects.filter(
            owner__cloud__account=active_account
        ).exclude(
            monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
        ).values('owner__cloud').annotate(count=Count('id'))
        for item in aws_eip_counts:
            cloud_asset_counts[item['owner__cloud']]['elastic_ips'] += item['count']

        # Count AWS Load Balancers
        aws_lb_counts = CoreAWSLoadBalancer.objects.filter(
            owner__cloud__account=active_account
        ).exclude(
            monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
        ).values('owner__cloud').annotate(count=Count('id'))
        for item in aws_lb_counts:
            cloud_asset_counts[item['owner__cloud']]['load_balancers'] += item['count']

        # Count AWS Security Groups
        aws_sg_counts = CoreAWSSecurityGroup.objects.filter(
            owner__cloud__account=active_account
        ).exclude(
            monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
        ).values('owner__cloud').annotate(count=Count('id'))
        for item in aws_sg_counts:
            cloud_asset_counts[item['owner__cloud']]['security_groups'] += item['count']

        # Count AWS ECS Services
        aws_ecs_service_counts = CoreAWSECSService.objects.filter(
            owner__cloud__account=active_account
        ).exclude(
            monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
        ).values('owner__cloud').annotate(count=Count('id'))
        for item in aws_ecs_service_counts:
            cloud_asset_counts[item['owner__cloud']]['ecs_services'] += item['count']

        # Count AWS ECS Tasks
        aws_ecs_task_counts = CoreAWSECSTask.objects.filter(
            owner__cloud__account=active_account
        ).exclude(
            monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
        ).values('owner__cloud').annotate(count=Count('id'))
        for item in aws_ecs_task_counts:
            cloud_asset_counts[item['owner__cloud']]['ecs_tasks'] += item['count']

        # Lightsail assets use their own models, but contribute to the same
        # dashboard categories so the AWS total remains complete.
        lightsail_resource_models = (
            ('servers', CoreAWSLightsailInstance),
            ('volumes', CoreAWSLightsailDisk),
            ('snapshots', CoreAWSLightsailInstanceSnapshot),
            ('snapshots', CoreAWSLightsailDiskSnapshot),
            ('elastic_ips', CoreAWSLightsailStaticIP),
            ('databases', CoreAWSLightsailDatabase),
            ('snapshots', CoreAWSLightsailDatabaseSnapshot),
            ('load_balancers', CoreAWSLightsailLoadBalancer),
            ('certificates', CoreAWSLightsailCertificate),
            ('spaces', CoreAWSLightsailBucket),
            ('cdn_endpoints', CoreAWSLightsailDistribution),
            ('domains', CoreAWSLightsailDomain),
            ('dns_records', CoreAWSLightsailDNSRecord),
            ('ecs_services', CoreAWSLightsailContainerService),
            ('ecs_tasks', CoreAWSLightsailContainerDeployment),
            ('ecs_tasks', CoreAWSLightsailContainerImage),
            ('lightsail_misc', CoreAWSLightsailAlarm),
            ('lightsail_misc', CoreAWSLightsailOperation),
            ('snapshots', CoreAWSLightsailAutoSnapshot),
        )
        for category, resource_model in lightsail_resource_models:
            resource_counts = resource_model.objects.filter(
                owner__cloud__account=active_account
            ).exclude(
                monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
            ).values('owner__cloud').annotate(count=Count('id'))
            for item in resource_counts:
                cloud_asset_counts[item['owner__cloud']][category] += item['count']

        # Priority 0 AWS resources retain their exact persisted type in the
        # dashboard counts.  This keeps regional/network resources distinct
        # from the older provider-agnostic categories above and prevents a
        # newly registered model from disappearing from inventory totals.
        for asset_type, resource_model in _AWS_PRIORITY0_ASSET_MODELS:
            resource_counts = resource_model.objects.filter(
                owner__cloud__account=active_account
            ).exclude(
                monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
            ).values('owner__cloud').annotate(count=Count('id'))
            for item in resource_counts:
                cloud_asset_counts[item['owner__cloud']][asset_type] += item['count']

        # Priority 1 AWS platform and data-service resources retain their
        # exact persisted type so the dashboard does not collapse distinct
        # databases, integrations, or delivery events into generic totals.
        for asset_type, resource_model in _AWS_PRIORITY1_ASSET_MODELS:
            resource_counts = resource_model.objects.filter(
                owner__cloud__account=active_account
            ).exclude(
                monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
            ).values('owner__cloud').annotate(count=Count('id'))
            for item in resource_counts:
                cloud_asset_counts[item['owner__cloud']][asset_type] += item['count']

        # Priority 2 security, credential/configuration, and account-operation
        # resources are counted from their model maps so new families do not
        # require one-off dashboard fields.
        for asset_type, resource_model in _AWS_PRIORITY2_ASSET_MODELS:
            resource_counts = resource_model.objects.filter(
                owner__cloud__account=active_account
            ).exclude(
                monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
            ).values('owner__cloud').annotate(count=Count('id'))
            for item in resource_counts:
                cloud_asset_counts[item['owner__cloud']][asset_type] += item['count']

        # Update the counts for UpCloud
        upcloud_counts = CoreUpCloudServer.objects.filter(
            owner__cloud__account=active_account
        ).exclude(
            monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
        ).values('owner__cloud').annotate(count=Count('id'))
        for item in upcloud_counts:
            cloud_asset_counts[item['owner__cloud']]['servers'] += item['count']

        upcloud_volume_counts = CoreUpCloudVolume.objects.filter(
            owner__cloud__account=active_account
        ).exclude(
            monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
        ).values('owner__cloud').annotate(count=Count('id'))
        for item in upcloud_volume_counts:
            cloud_asset_counts[item['owner__cloud']]['volumes'] += item['count']

        # Update the counts for Linode
        linode_counts = CoreLinodeServer.objects.filter(
            owner__cloud__account=active_account
        ).exclude(
            monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
        ).values('owner__cloud').annotate(count=Count('id'))
        for item in linode_counts:
            cloud_asset_counts[item['owner__cloud']]['servers'] += item['count']

        linode_volume_counts = CoreLinodeVolume.objects.filter(
            owner__cloud__account=active_account
        ).exclude(
            monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
        ).values('owner__cloud').annotate(count=Count('id'))
        for item in linode_volume_counts:
            cloud_asset_counts[item['owner__cloud']]['volumes'] += item['count']

        # Update the counts for Oracle Cloud
        oracle_counts = CoreOracleInstance.objects.filter(
            owner__cloud__account=active_account
        ).exclude(
            monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
        ).values('owner__cloud').annotate(count=Count('id'))
        for item in oracle_counts:
            cloud_asset_counts[item['owner__cloud']]['servers'] += item['count']

        oracle_volume_counts = CoreOracleVolume.objects.filter(
            owner__cloud__account=active_account
        ).exclude(
            monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
        ).values('owner__cloud').annotate(count=Count('id'))
        for item in oracle_volume_counts:
            cloud_asset_counts[item['owner__cloud']]['volumes'] += item['count']

        # Calculate totals
        total_servers = sum(cloud['servers'] for cloud in cloud_asset_counts.values())
        total_volumes = sum(cloud['volumes'] for cloud in cloud_asset_counts.values())
        total_databases = sum(cloud['databases'] for cloud in cloud_asset_counts.values())
        total_lambda_functions = sum(cloud['lambda_functions'] for cloud in cloud_asset_counts.values())
        total_dynamodb_tables = sum(cloud['dynamodb_tables'] for cloud in cloud_asset_counts.values())
        total_s3_buckets = sum(cloud['s3_buckets'] for cloud in cloud_asset_counts.values())
        total_acm_certificates = sum(cloud['acm_certificates'] for cloud in cloud_asset_counts.values())
        total_snapshots = sum(cloud['snapshots'] for cloud in cloud_asset_counts.values())
        total_backups = sum(cloud['backups'] for cloud in cloud_asset_counts.values())
        total_elastic_ips = sum(cloud['elastic_ips'] for cloud in cloud_asset_counts.values())
        total_reserved_ips = sum(cloud['reserved_ips'] for cloud in cloud_asset_counts.values())
        total_load_balancers = sum(cloud['load_balancers'] for cloud in cloud_asset_counts.values())
        total_security_groups = sum(cloud['security_groups'] for cloud in cloud_asset_counts.values())
        total_firewalls = sum(cloud['firewalls'] for cloud in cloud_asset_counts.values())
        total_apps = sum(cloud['apps'] for cloud in cloud_asset_counts.values())
        total_spaces = sum(cloud['spaces'] for cloud in cloud_asset_counts.values())
        total_container_registries = sum(cloud['container_registries'] for cloud in cloud_asset_counts.values())
        total_ecs_services = sum(cloud['ecs_services'] for cloud in cloud_asset_counts.values())
        total_ecs_tasks = sum(cloud['ecs_tasks'] for cloud in cloud_asset_counts.values())
        total_kubernetes_clusters = sum(cloud['kubernetes_clusters'] for cloud in cloud_asset_counts.values())
        total_kubernetes_node_pools = sum(cloud['kubernetes_node_pools'] for cloud in cloud_asset_counts.values())
        total_vpcs = sum(cloud['vpcs'] for cloud in cloud_asset_counts.values())
        total_vpc_peerings = sum(cloud['vpc_peerings'] for cloud in cloud_asset_counts.values())
        total_vpc_nat_gateways = sum(cloud['vpc_nat_gateways'] for cloud in cloud_asset_counts.values())
        total_domains = sum(cloud['domains'] for cloud in cloud_asset_counts.values())
        total_dns_records = sum(cloud['dns_records'] for cloud in cloud_asset_counts.values())
        total_cdn_endpoints = sum(cloud['cdn_endpoints'] for cloud in cloud_asset_counts.values())
        total_certificates = sum(cloud['certificates'] for cloud in cloud_asset_counts.values())
        total_lightsail_misc = sum(cloud['lightsail_misc'] for cloud in cloud_asset_counts.values())
        total_priority0_counts = {
            asset_type: sum(
                cloud[asset_type] for cloud in cloud_asset_counts.values()
            )
            for asset_type, _model in _AWS_PRIORITY0_ASSET_MODELS
        }
        total_priority0_assets = sum(total_priority0_counts.values())
        total_priority1_counts = {
            asset_type: sum(
                cloud[asset_type] for cloud in cloud_asset_counts.values()
            )
            for asset_type, _model in _AWS_PRIORITY1_ASSET_MODELS
        }
        total_priority1_assets = sum(total_priority1_counts.values())
        total_priority2_counts = {
            asset_type: sum(
                cloud[asset_type] for cloud in cloud_asset_counts.values()
            )
            for asset_type, _model in _AWS_PRIORITY2_ASSET_MODELS
        }
        total_priority2_assets = sum(total_priority2_counts.values())
        total_assets = (
            total_servers + total_volumes + total_databases
            + total_lambda_functions + total_dynamodb_tables + total_s3_buckets
            + total_acm_certificates + total_snapshots + total_backups
            + total_elastic_ips + total_reserved_ips + total_load_balancers
            + total_security_groups + total_firewalls + total_apps + total_spaces
            + total_container_registries + total_ecs_services + total_ecs_tasks
            + total_kubernetes_clusters + total_kubernetes_node_pools + total_vpcs
            + total_vpc_peerings + total_vpc_nat_gateways + total_domains
            + total_dns_records + total_cdn_endpoints + total_certificates
            + total_lightsail_misc + total_priority0_assets + total_priority1_assets
            + total_priority2_assets
        )

        # Attach counts to cloud objects
        for cloud in clouds:
            cloud.server_count = cloud_asset_counts[cloud.id]['servers']
            cloud.volume_count = cloud_asset_counts[cloud.id]['volumes']
            cloud.database_count = cloud_asset_counts[cloud.id]['databases']
            cloud.lambda_function_count = cloud_asset_counts[cloud.id]['lambda_functions']
            cloud.dynamodb_table_count = cloud_asset_counts[cloud.id]['dynamodb_tables']
            cloud.s3_bucket_count = cloud_asset_counts[cloud.id]['s3_buckets']
            cloud.acm_certificate_count = cloud_asset_counts[cloud.id]['acm_certificates']
            cloud.snapshot_count = cloud_asset_counts[cloud.id]['snapshots']
            cloud.backup_count = cloud_asset_counts[cloud.id]['backups']
            cloud.elastic_ip_count = cloud_asset_counts[cloud.id]['elastic_ips']
            cloud.reserved_ip_count = cloud_asset_counts[cloud.id]['reserved_ips']
            cloud.load_balancer_count = cloud_asset_counts[cloud.id]['load_balancers']
            cloud.security_group_count = cloud_asset_counts[cloud.id]['security_groups']
            cloud.firewall_count = cloud_asset_counts[cloud.id]['firewalls']
            cloud.app_count = cloud_asset_counts[cloud.id]['apps']
            cloud.space_count = cloud_asset_counts[cloud.id]['spaces']
            cloud.container_registry_count = cloud_asset_counts[cloud.id]['container_registries']
            cloud.ecs_service_count = cloud_asset_counts[cloud.id]['ecs_services']
            cloud.ecs_task_count = cloud_asset_counts[cloud.id]['ecs_tasks']
            cloud.kubernetes_cluster_count = cloud_asset_counts[cloud.id]['kubernetes_clusters']
            cloud.kubernetes_node_pool_count = cloud_asset_counts[cloud.id]['kubernetes_node_pools']
            cloud.vpc_count = cloud_asset_counts[cloud.id]['vpcs']
            cloud.vpc_peering_count = cloud_asset_counts[cloud.id]['vpc_peerings']
            cloud.vpc_nat_gateway_count = cloud_asset_counts[cloud.id]['vpc_nat_gateways']
            cloud.domain_count = cloud_asset_counts[cloud.id]['domains']
            cloud.dns_record_count = cloud_asset_counts[cloud.id]['dns_records']
            cloud.cdn_endpoint_count = cloud_asset_counts[cloud.id]['cdn_endpoints']
            cloud.certificate_count = cloud_asset_counts[cloud.id]['certificates']
            cloud.lightsail_misc_count = cloud_asset_counts[cloud.id]['lightsail_misc']
            cloud.priority0_asset_counts = {
                asset_type: cloud_asset_counts[cloud.id][asset_type]
                for asset_type, _model in _AWS_PRIORITY0_ASSET_MODELS
            }
            cloud.priority1_asset_counts = {
                asset_type: cloud_asset_counts[cloud.id][asset_type]
                for asset_type, _model in _AWS_PRIORITY1_ASSET_MODELS
            }
            cloud.priority2_asset_counts = {
                asset_type: cloud_asset_counts[cloud.id][asset_type]
                for asset_type, _model in _AWS_PRIORITY2_ASSET_MODELS
            }
            cloud.total_assets = (
                cloud.server_count + cloud.volume_count + cloud.database_count
                + cloud.lambda_function_count + cloud.dynamodb_table_count
                + cloud.s3_bucket_count + cloud.acm_certificate_count
                + cloud.snapshot_count + cloud.backup_count + cloud.elastic_ip_count
                + cloud.reserved_ip_count + cloud.load_balancer_count
                + cloud.security_group_count + cloud.firewall_count
                + cloud.app_count + cloud.space_count
                + cloud.container_registry_count + cloud.ecs_service_count
                + cloud.ecs_task_count
                + cloud.kubernetes_cluster_count + cloud.kubernetes_node_pool_count
                + cloud.vpc_count + cloud.vpc_peering_count
                + cloud.vpc_nat_gateway_count + cloud.domain_count
                + cloud.dns_record_count + cloud.cdn_endpoint_count
                + cloud.certificate_count + cloud.lightsail_misc_count
                + sum(cloud.priority0_asset_counts.values())
                + sum(cloud.priority1_asset_counts.values())
                + sum(cloud.priority2_asset_counts.values())
            )

        # Calculate asset breakdown percentages
        asset_breakdown = {
            'servers': {'count': total_servers, 'percentage': round((total_servers / total_assets * 100) if total_assets > 0 else 0, 1)},
            'volumes': {'count': total_volumes, 'percentage': round((total_volumes / total_assets * 100) if total_assets > 0 else 0, 1)},
            'databases': {'count': total_databases, 'percentage': round((total_databases / total_assets * 100) if total_assets > 0 else 0, 1)},
            'lambda_functions': {'count': total_lambda_functions, 'percentage': round((total_lambda_functions / total_assets * 100) if total_assets > 0 else 0, 1)},
            'dynamodb_tables': {'count': total_dynamodb_tables, 'percentage': round((total_dynamodb_tables / total_assets * 100) if total_assets > 0 else 0, 1)},
            's3_buckets': {'count': total_s3_buckets, 'percentage': round((total_s3_buckets / total_assets * 100) if total_assets > 0 else 0, 1)},
            'acm_certificates': {'count': total_acm_certificates, 'percentage': round((total_acm_certificates / total_assets * 100) if total_assets > 0 else 0, 1)},
            'snapshots': {'count': total_snapshots, 'percentage': round((total_snapshots / total_assets * 100) if total_assets > 0 else 0, 1)},
            'backups': {'count': total_backups, 'percentage': round((total_backups / total_assets * 100) if total_assets > 0 else 0, 1)},
            'elastic_ips': {'count': total_elastic_ips, 'percentage': round((total_elastic_ips / total_assets * 100) if total_assets > 0 else 0, 1)},
            'reserved_ips': {'count': total_reserved_ips, 'percentage': round((total_reserved_ips / total_assets * 100) if total_assets > 0 else 0, 1)},
            'load_balancers': {'count': total_load_balancers, 'percentage': round((total_load_balancers / total_assets * 100) if total_assets > 0 else 0, 1)},
            'security_groups': {'count': total_security_groups, 'percentage': round((total_security_groups / total_assets * 100) if total_assets > 0 else 0, 1)},
            'firewalls': {'count': total_firewalls, 'percentage': round((total_firewalls / total_assets * 100) if total_assets > 0 else 0, 1)},
            'apps': {'count': total_apps, 'percentage': round((total_apps / total_assets * 100) if total_assets > 0 else 0, 1)},
            'spaces': {'count': total_spaces, 'percentage': round((total_spaces / total_assets * 100) if total_assets > 0 else 0, 1)},
            'container_registries': {'count': total_container_registries, 'percentage': round((total_container_registries / total_assets * 100) if total_assets > 0 else 0, 1)},
            'ecs_services': {'count': total_ecs_services, 'percentage': round((total_ecs_services / total_assets * 100) if total_assets > 0 else 0, 1)},
            'ecs_tasks': {'count': total_ecs_tasks, 'percentage': round((total_ecs_tasks / total_assets * 100) if total_assets > 0 else 0, 1)},
            'kubernetes_clusters': {'count': total_kubernetes_clusters, 'percentage': round((total_kubernetes_clusters / total_assets * 100) if total_assets > 0 else 0, 1)},
            'kubernetes_node_pools': {'count': total_kubernetes_node_pools, 'percentage': round((total_kubernetes_node_pools / total_assets * 100) if total_assets > 0 else 0, 1)},
            'vpcs': {'count': total_vpcs, 'percentage': round((total_vpcs / total_assets * 100) if total_assets > 0 else 0, 1)},
            'vpc_peerings': {'count': total_vpc_peerings, 'percentage': round((total_vpc_peerings / total_assets * 100) if total_assets > 0 else 0, 1)},
            'vpc_nat_gateways': {'count': total_vpc_nat_gateways, 'percentage': round((total_vpc_nat_gateways / total_assets * 100) if total_assets > 0 else 0, 1)},
            'domains': {'count': total_domains, 'percentage': round((total_domains / total_assets * 100) if total_assets > 0 else 0, 1)},
            'dns_records': {'count': total_dns_records, 'percentage': round((total_dns_records / total_assets * 100) if total_assets > 0 else 0, 1)},
            'cdn_endpoints': {'count': total_cdn_endpoints, 'percentage': round((total_cdn_endpoints / total_assets * 100) if total_assets > 0 else 0, 1)},
            'certificates': {'count': total_certificates, 'percentage': round((total_certificates / total_assets * 100) if total_assets > 0 else 0, 1)},
        }
        asset_breakdown.update({
            asset_type: {
                'count': count,
                'percentage': round((count / total_assets * 100) if total_assets > 0 else 0, 1),
            }
            for asset_type, count in total_priority0_counts.items()
        })
        asset_breakdown.update({
            asset_type: {
                'count': count,
                'percentage': round((count / total_assets * 100) if total_assets > 0 else 0, 1),
            }
            for asset_type, count in total_priority1_counts.items()
        })
        asset_breakdown.update({
            asset_type: {
                'count': count,
                'percentage': round((count / total_assets * 100) if total_assets > 0 else 0, 1),
            }
            for asset_type, count in total_priority2_counts.items()
        })

        priority0_asset_counts = {
            cloud_id: {
                asset_type: cloud_counts[asset_type]
                for asset_type, _model in _AWS_PRIORITY0_ASSET_MODELS
            }
            for cloud_id, cloud_counts in cloud_asset_counts.items()
        }
        priority1_asset_counts = {
            cloud_id: {
                asset_type: cloud_counts[asset_type]
                for asset_type, _model in _AWS_PRIORITY1_ASSET_MODELS
            }
            for cloud_id, cloud_counts in cloud_asset_counts.items()
        }
        priority2_asset_counts = {
            cloud_id: {
                asset_type: cloud_counts[asset_type]
                for asset_type, _model in _AWS_PRIORITY2_ASSET_MODELS
            }
            for cloud_id, cloud_counts in cloud_asset_counts.items()
        }

        context.update({
            'active_account': active_account,
            'clouds': clouds,
            'total_servers': total_servers,
            'total_volumes': total_volumes,
            'total_databases': total_databases,
            'total_lambda_functions': total_lambda_functions,
            'total_dynamodb_tables': total_dynamodb_tables,
            'total_s3_buckets': total_s3_buckets,
            'total_acm_certificates': total_acm_certificates,
            'total_snapshots': total_snapshots,
            'total_backups': total_backups,
            'total_elastic_ips': total_elastic_ips,
            'total_reserved_ips': total_reserved_ips,
            'total_load_balancers': total_load_balancers,
            'total_security_groups': total_security_groups,
            'total_firewalls': total_firewalls,
            'total_apps': total_apps,
            'total_spaces': total_spaces,
            'total_container_registries': total_container_registries,
            'total_ecs_services': total_ecs_services,
            'total_ecs_tasks': total_ecs_tasks,
            'total_kubernetes_clusters': total_kubernetes_clusters,
            'total_kubernetes_node_pools': total_kubernetes_node_pools,
            'total_vpcs': total_vpcs,
            'total_vpc_peerings': total_vpc_peerings,
            'total_vpc_nat_gateways': total_vpc_nat_gateways,
            'total_domains': total_domains,
            'total_dns_records': total_dns_records,
            'total_cdn_endpoints': total_cdn_endpoints,
            'total_certificates': total_certificates,
            **{
                f'total_{asset_type}': count
                for asset_type, count in total_priority0_counts.items()
            },
            'total_assets': total_assets,
            'asset_breakdown': asset_breakdown,
            'priority0_asset_counts': priority0_asset_counts,
            'priority1_asset_counts': priority1_asset_counts,
            'priority2_asset_counts': priority2_asset_counts,
            'total_priority2_counts': total_priority2_counts,
            'total_priority2_assets': total_priority2_assets,
            **{
                f'total_{asset_type}': count
                for asset_type, count in total_priority1_counts.items()
            },
            'show_welcome': False
        })

        return context
