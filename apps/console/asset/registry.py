"""Provider/asset-type model registry shared by the console and the API.

Every lookup that resolves ``(provider_code, asset_type)`` to a concrete
asset model goes through this module so the console detail view, CSV export,
and the mobile API can never silently diverge.
"""
from apps.console.cloud.aws.account_operations import (
    AWS_ACCOUNT_OPERATIONS_ASSET_MODELS,
)
from apps.console.cloud.aws.application_services import AWS_APPLICATION_ASSET_MODELS
from apps.console.cloud.aws.backup import AWS_BACKUP_ASSET_MODELS
from apps.console.cloud.aws.containers import AWS_CONTAINER_ASSET_MODELS
from apps.console.cloud.aws.credentials_config import (
    AWS_CREDENTIALS_CONFIG_ASSET_MODELS,
)
from apps.console.cloud.aws.data_services import AWS_DATA_SERVICE_ASSET_MODELS
from apps.console.cloud.aws.delivery import AWS_DELIVERY_ASSET_MODELS
from apps.console.cloud.aws.edge import AWS_EDGE_ASSET_MODELS
from apps.console.cloud.aws.lightsail import (
    CoreAWSLightsailAlarm,
    CoreAWSLightsailAutoSnapshot,
    CoreAWSLightsailBucket,
    CoreAWSLightsailCertificate,
    CoreAWSLightsailContainerDeployment,
    CoreAWSLightsailContainerImage,
    CoreAWSLightsailContainerService,
    CoreAWSLightsailDatabase,
    CoreAWSLightsailDatabaseSnapshot,
    CoreAWSLightsailDisk,
    CoreAWSLightsailDiskSnapshot,
    CoreAWSLightsailDistribution,
    CoreAWSLightsailDNSRecord,
    CoreAWSLightsailDomain,
    CoreAWSLightsailInstance,
    CoreAWSLightsailInstanceSnapshot,
    CoreAWSLightsailLoadBalancer,
    CoreAWSLightsailOperation,
    CoreAWSLightsailStaticIP,
)
from apps.console.cloud.aws.models import (
    CoreAWSACMCertificate,
    CoreAWSDynamoDB,
    CoreAWSECSService,
    CoreAWSECSTask,
    CoreAWSElasticIP,
    CoreAWSInstance,
    CoreAWSLambda,
    CoreAWSLoadBalancer,
    CoreAWSRDSDatabase,
    CoreAWSS3Bucket,
    CoreAWSSecurityGroup,
    CoreAWSSnapshot,
    CoreAWSVolume,
)
from apps.console.cloud.aws.network import AWS_NETWORK_COLLECTION_SPECS
from apps.console.cloud.aws.observability import AWS_OBSERVABILITY_ASSET_MODELS
from apps.console.cloud.aws.security_governance import (
    AWS_SECURITY_GOVERNANCE_ASSET_MODELS,
)
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
from apps.console.cloud.hetzner.resources import HETZNER_RESOURCE_MODELS
from apps.console.cloud.linode.models import CoreLinodeServer, CoreLinodeVolume
from apps.console.cloud.oracle.models import CoreOracleInstance, CoreOracleVolume
from apps.console.cloud.upcloud.models import CoreUpCloudServer, CoreUpCloudVolume
from apps.console.cloud.vultr.integration import get_vultr_resource_models
from apps.console.cloud.vultr.models import (
    CoreVultrDatabase,
    CoreVultrServer,
    CoreVultrVolume,
)

_AWS_NETWORK_ASSET_MODELS = {
    spec['provider_type'].removeprefix('aws_'): spec['model']
    for spec in AWS_NETWORK_COLLECTION_SPECS
}
_AWS_PRIORITY0_ASSET_MODELS = {
    **_AWS_NETWORK_ASSET_MODELS,
    **AWS_OBSERVABILITY_ASSET_MODELS,
    **AWS_CONTAINER_ASSET_MODELS,
    **AWS_EDGE_ASSET_MODELS,
    **AWS_BACKUP_ASSET_MODELS,
}
_AWS_PRIORITY1_ASSET_MODELS = {
    **AWS_DATA_SERVICE_ASSET_MODELS,
    **AWS_APPLICATION_ASSET_MODELS,
    **AWS_DELIVERY_ASSET_MODELS,
}
_AWS_PRIORITY2_ASSET_MODELS = {
    **AWS_SECURITY_GOVERNANCE_ASSET_MODELS,
    **AWS_CREDENTIALS_CONFIG_ASSET_MODELS,
    **AWS_ACCOUNT_OPERATIONS_ASSET_MODELS,
}

ASSET_CLASS_REGISTRY = {
    'digitalocean': {
        'server': CoreDigitalOceanServer,
        'database': CoreDigitalOceanDatabase,
        'volume': CoreDigitalOceanVolume,
        'snapshot': CoreDigitalOceanSnapshot,
        'backup': CoreDigitalOceanBackup,
        'reserved_ip': CoreDigitalOceanReservedIP,
        'firewall': CoreDigitalOceanFirewall,
        'load_balancer': CoreDigitalOceanLoadBalancer,
        'app_platform': CoreDigitalOceanApp,
        'object_storage': CoreDigitalOceanSpace,
        'container_registry': CoreDigitalOceanContainerRegistry,
        'kubernetes_cluster': CoreDigitalOceanKubernetesCluster,
        'kubernetes_node_pool': CoreDigitalOceanKubernetesNodePool,
        'vpc': CoreDigitalOceanVPC,
        'vpc_peering': CoreDigitalOceanVPCPeering,
        'nat_gateway': CoreDigitalOceanVPCNATGateway,
        'domain': CoreDigitalOceanDomain,
        'dns_record': CoreDigitalOceanDNSRecord,
        'cdn_endpoint': CoreDigitalOceanCDNEndpoint,
        'certificate': CoreDigitalOceanCertificate,
    },
    'vultr': {
        'server': CoreVultrServer,
        'volume': CoreVultrVolume,
        'database': CoreVultrDatabase,
        **get_vultr_resource_models(),
    },
    'hetzner': {
        'server': CoreHetznerServer,
        'volume': CoreHetznerVolume,
        **HETZNER_RESOURCE_MODELS,
    },
    'aws': {
        'server': CoreAWSInstance,
        'volume': CoreAWSVolume,
        'rds_database': CoreAWSRDSDatabase,
        'lambda': CoreAWSLambda,
        'dynamodb': CoreAWSDynamoDB,
        's3_bucket': CoreAWSS3Bucket,
        'acm_certificate': CoreAWSACMCertificate,
        'snapshot': CoreAWSSnapshot,
        'elastic_ip': CoreAWSElasticIP,
        'load_balancer': CoreAWSLoadBalancer,
        'security_group': CoreAWSSecurityGroup,
        'ecs_service': CoreAWSECSService,
        'ecs_task': CoreAWSECSTask,
        'lightsail_instance': CoreAWSLightsailInstance,
        'lightsail_disk': CoreAWSLightsailDisk,
        'lightsail_instance_snapshot': CoreAWSLightsailInstanceSnapshot,
        'lightsail_disk_snapshot': CoreAWSLightsailDiskSnapshot,
        'lightsail_static_ip': CoreAWSLightsailStaticIP,
        'lightsail_database': CoreAWSLightsailDatabase,
        'lightsail_database_snapshot': CoreAWSLightsailDatabaseSnapshot,
        'lightsail_load_balancer': CoreAWSLightsailLoadBalancer,
        'lightsail_certificate': CoreAWSLightsailCertificate,
        'lightsail_bucket': CoreAWSLightsailBucket,
        'lightsail_distribution': CoreAWSLightsailDistribution,
        'lightsail_domain': CoreAWSLightsailDomain,
        'lightsail_dns_record': CoreAWSLightsailDNSRecord,
        'lightsail_container_service': CoreAWSLightsailContainerService,
        'lightsail_container_deployment': CoreAWSLightsailContainerDeployment,
        'lightsail_container_image': CoreAWSLightsailContainerImage,
        'lightsail_alarm': CoreAWSLightsailAlarm,
        'lightsail_operation': CoreAWSLightsailOperation,
        'lightsail_auto_snapshot': CoreAWSLightsailAutoSnapshot,
        **_AWS_PRIORITY0_ASSET_MODELS,
        **_AWS_PRIORITY1_ASSET_MODELS,
        **_AWS_PRIORITY2_ASSET_MODELS,
    },
    'upcloud': {
        'server': CoreUpCloudServer,
        'volume': CoreUpCloudVolume,
    },
    'linode': {
        'server': CoreLinodeServer,
        'volume': CoreLinodeVolume,
    },
    'oracle': {
        'server': CoreOracleInstance,
        'volume': CoreOracleVolume,
    },
}


def get_asset_model(provider_code, asset_type):
    """Return the concrete asset model for a provider/type pair, or None."""
    return ASSET_CLASS_REGISTRY.get(provider_code, {}).get(asset_type)
