"""
Metadata filtering and comparison for asset status checks.

Ported from the cloudMooCheckAssetStatus Lambda. Previous metadata now comes
from PostgreSQL JSON fields, so the DynamoDB wire-format handling
({'S': ..., 'N': ..., 'M': ..., 'L': ...}) from the original has been dropped.
"""
import logging
import re

logger = logging.getLogger(__name__)

# Provider APIs can return credential-like values as part of otherwise useful
# configuration payloads. Status metadata is persisted in PostgreSQL and can
# be included in notification emails, so those values must not be retained.
SENSITIVE_METADATA_KEY_PARTS = (
    'password',
    'secret',
    'token',
    'accesskey',
    'privatekey',
    'credential',
    'authorization',
    'apikey',
)

SENSITIVE_ERROR_PATTERN = re.compile(
    r"(?i)(['\"]?(?:password|secret|token|access[_-]?key|private[_-]?key|credential|authorization|api[_-]?key)['\"]?\s*[:=]\s*['\"]?)([^'\",;\s}]+)"
)
BEARER_ERROR_PATTERN = re.compile(
    r"(?i)\b(bearer|basic)\s+[^\s,;]+"
)
MAX_ERROR_MESSAGE_LENGTH = 2048


def redact_sensitive_metadata(value):
    """Recursively redact credential-like metadata values before persistence."""
    if isinstance(value, dict):
        redacted = {}
        for key, child in value.items():
            normalized_key = ''.join(
                character for character in str(key).lower() if character.isalnum()
            )
            if normalized_key == 'environment':
                # Container APIs commonly return environment variables as a
                # direct name/value map.  Keep the legacy
                # Environment -> Variables shape traversable so its nested
                # values retain the established redaction behavior.
                if isinstance(child, dict) and not any(
                    ''.join(character for character in str(child_key).lower() if character.isalnum())
                    == 'variables'
                    for child_key in child
                ):
                    redacted[key] = {variable: '[REDACTED]' for variable in child}
                elif isinstance(child, dict):
                    redacted[key] = redact_sensitive_metadata(child)
                else:
                    redacted[key] = '[REDACTED]'
                continue
            if (
                any(part in normalized_key for part in SENSITIVE_METADATA_KEY_PARTS)
                or normalized_key == 'variables'
            ):
                if isinstance(child, dict):
                    redacted[key] = {variable: '[REDACTED]' for variable in child}
                else:
                    redacted[key] = '[REDACTED]'
            else:
                redacted[key] = redact_sensitive_metadata(child)
        return redacted
    if isinstance(value, list):
        return [redact_sensitive_metadata(item) for item in value]
    return value


def redact_error_message(value):
    """Bound and redact provider error text before storing or returning it."""
    redacted = redact_sensitive_metadata(value)
    message = str(redacted)
    message = BEARER_ERROR_PATTERN.sub(r'\1 [REDACTED]', message)
    message = SENSITIVE_ERROR_PATTERN.sub(r'\1[REDACTED]', message)
    return message[:MAX_ERROR_MESSAGE_LENGTH]

# Fields worth tracking per provider/asset type, mapped to display names.
# Only changes to these fields trigger "configuration change" notifications.
PROVIDER_METADATA_FIELDS = {
    'digitalocean': {
        'server': {
            'droplet.name': 'Name',
            'droplet.status': 'Status',
            'droplet.memory': 'Memory',
            'droplet.vcpus': 'vCPUs',
            'droplet.disk': 'Disk',
            'droplet.size_slug': 'Size',
            'droplet.locked': 'Locked Status',
            'droplet.networks.v4.[].ip_address': 'IP Address',
            'droplet.features': 'Features',
            'droplet.region.name': 'Region'
        },
        'volume': {
            'volume.name': 'Name',
            'volume.size_gigabytes': 'Size (GB)',
            'volume.region.name': 'Region',
            'volume.droplet_ids': 'Attached Droplets',
            'volume.filesystem_type': 'Filesystem Type'
        },
        'database': {
            'database.name': 'Name',
            'database.status': 'Status',
            'database.engine': 'Engine',
            'database.version': 'Version',
            'database.region.slug': 'Region',
            'database.size': 'Size',
            'database.num_nodes': 'Nodes',
            'database.storage_size_mib': 'Storage (MiB)',
            'database.tags': 'Tags'
        },
        'load_balancer': {
            'load_balancer.name': 'Name',
            'load_balancer.status': 'Status',
            'load_balancer.region.slug': 'Region',
            'load_balancer.algorithm.type': 'Algorithm',
            'load_balancer.droplet_ids': 'Attached Droplets',
            'load_balancer.forwarding_rules': 'Forwarding Rules',
            'load_balancer.health_check': 'Health Check',
            'load_balancer.sticky_sessions': 'Sticky Sessions'
        },
        'snapshot': {
            'snapshot.name': 'Name',
            'snapshot.resource_type': 'Resource Type',
            'snapshot.resource_id': 'Source Resource ID',
            'snapshot.size_gigabytes': 'Size (GB)',
            'snapshot.min_disk_size': 'Minimum Disk Size (GB)',
            'snapshot.regions': 'Regions',
            'snapshot.tags': 'Tags'
        },
        'backup': {
            'backup.id': 'Backup ID',
            'backup.droplet_id': 'Droplet ID',
            'backup.created_at': 'Created At',
            'backup.size_gigabytes': 'Size (GB)',
            'backup.distribution': 'Distribution',
            'backup.slug': 'Image Slug'
        },
        'reserved_ip': {
            'reserved_ip.ip': 'IP Address',
            'reserved_ip.ip_version': 'IP Version',
            'reserved_ip.region.slug': 'Region',
            'reserved_ip.region_slug': 'Region',
            'reserved_ip.droplet.id': 'Attached Droplet',
            'reserved_ip.locked': 'Locked Status',
            'reserved_ip.project_id': 'Project ID'
        },
        'firewall': {
            'firewall.name': 'Name',
            'firewall.status': 'Status',
            'firewall.droplet_ids': 'Attached Droplets',
            'firewall.inbound_rules': 'Inbound Rules',
            'firewall.outbound_rules': 'Outbound Rules',
            'firewall.pending_changes': 'Pending Changes',
            'firewall.tags': 'Tags'
        },
        'app_platform': {
            'app.id': 'App ID',
            'app.spec.name': 'Name',
            'app.spec.region': 'Region',
            'app.active_deployment.phase': 'Active Deployment',
            'app.in_progress_deployment.phase': 'In-progress Deployment',
            'app.live_url': 'Live URL',
            'app.tier_slug': 'Tier',
            'app.default_ingress': 'Default Ingress'
        },
        'object_storage': {
            'bucket.name': 'Name',
            'bucket.region': 'Region'
        },
        'container_registry': {
            'registry.name': 'Name',
            'registry.region': 'Region',
            'registry.endpoint': 'Endpoint',
            'registry.storage_usage_bytes': 'Storage Usage (bytes)',
            'registry.subscription': 'Subscription'
        },
        'kubernetes_cluster': {
            'kubernetes_cluster.name': 'Name',
            'kubernetes_cluster.status.state': 'Status',
            'kubernetes_cluster.region.slug': 'Region',
            'kubernetes_cluster.version': 'Version',
            'kubernetes_cluster.node_pools.[].name': 'Node Pools',
            'kubernetes_cluster.node_pools.[].count': 'Node Counts',
            'kubernetes_cluster.ha': 'High Availability',
            'kubernetes_cluster.auto_upgrade': 'Automatic Upgrades',
            'kubernetes_cluster.registry_enabled': 'Container Registry',
            'kubernetes_cluster.tags': 'Tags',
        },
        'kubernetes_node_pool': {
            'kubernetes_node_pool.name': 'Name',
            'kubernetes_node_pool.size': 'Droplet Size',
            'kubernetes_node_pool.count': 'Node Count',
            'kubernetes_node_pool.auto_scale': 'Autoscaling',
            'kubernetes_node_pool.min_nodes': 'Minimum Nodes',
            'kubernetes_node_pool.max_nodes': 'Maximum Nodes',
            'kubernetes_node_pool.nodes.[].status.state': 'Node States',
            'kubernetes_node_pool.tags': 'Tags',
            'kubernetes_node_pool.labels': 'Labels',
            'kubernetes_node_pool.taints': 'Taints',
        },
        'vpc': {
            'vpc.name': 'Name',
            'vpc.region.slug': 'Region',
            'vpc.ip_range': 'IP Range',
            'vpc.default': 'Default VPC',
            'vpc.description': 'Description',
        },
        'vpc_peering': {
            'vpc_peering.name': 'Name',
            'vpc_peering.status': 'Status',
            'vpc_peering.vpc_ids': 'VPCs',
        },
        'nat_gateway': {
            'vpc_nat_gateway.name': 'Name',
            'vpc_nat_gateway.state': 'State',
            'vpc_nat_gateway.region.slug': 'Region',
            'vpc_nat_gateway.type': 'Type',
            'vpc_nat_gateway.size': 'Size',
            'vpc_nat_gateway.vpcs': 'VPCs',
            'vpc_nat_gateway.egresses.public_gateways': 'Public Gateways',
        },
        'domain': {
            'domain.name': 'Name',
            'domain.ttl': 'TTL',
            'domain.zone_file': 'Zone File',
            'domain.state': 'State',
            'domain.ip_address': 'IP Address',
            'domain.public': 'Public',
        },
        'dns_record': {
            'domain_record.type': 'Type',
            'domain_record.name': 'Name',
            'domain_record.data': 'Data',
            'domain_record.ttl': 'TTL',
            'domain_record.priority': 'Priority',
            'domain_record.port': 'Port',
            'domain_record.weight': 'Weight',
            'domain_record.flags': 'Flags',
            'domain_record.tag': 'CAA Tag',
            'domain_record.domain_name': 'Domain',
        },
        'cdn_endpoint': {
            'endpoint.origin': 'Origin',
            'endpoint.endpoint': 'CDN URL',
            'endpoint.custom_domain': 'Custom Domain',
            'endpoint.certificate_id': 'Certificate ID',
            'endpoint.ttl': 'TTL',
            'endpoint.created_at': 'Created At',
        },
        'certificate': {
            'certificate.name': 'Name',
            'certificate.type': 'Type',
            'certificate.state': 'State',
            'certificate.domains': 'Domains',
            'certificate.not_after': 'Expiration',
            'certificate.expires_at': 'Expiration',
            'certificate.expiration': 'Expiration',
            'certificate.sha1_fingerprint': 'SHA-1 Fingerprint',
        }
    },
    'vultr': {
        'server': {
            'instance.label': 'Name',
            'instance.power_status': 'Power Status',
            'instance.server_status': 'Server Status',
            'instance.status': 'Status',
            'instance.ram': 'Memory',
            'instance.vcpu_count': 'vCPUs',
            'instance.disk': 'Disk',
            'instance.plan': 'Plan',
            'instance.main_ip': 'Main IP',
            'instance.region': 'Region',
            'instance.features': 'Features'
        },
        'volume': {
            'block.label': 'Name',
            'block.size_gb': 'Size (GB)',
            'block.region': 'Region',
            'block.attached_to_instance': 'Attached To',
            'block.mount_id': 'Mount ID'
        }
    },
    'hetzner': {
        'server': {
            'server.status': 'Status',
            'server.name': 'Name',
            'server.server_type.cores': 'CPU Cores',
            'server.server_type.memory': 'Memory',
            'server.public_net.ipv4.ip': 'IPv4',
            'server.datacenter.location.name': 'Location',
            'server.rescue_enabled': 'Rescue Mode',
            'server.locked': 'Locked Status'
        },
        'volume': {
            'volume.name': 'Name',
            'volume.size': 'Size (GB)',
            'volume.location.name': 'Location',
            'volume.server': 'Attached Server',
            'volume.linux_device': 'Device Path',
            'volume.protection.delete': 'Delete Protection'
        },
        'primary_ip': {
            'primary_ip.ip': 'IP Address',
            'primary_ip.type': 'Address Family',
            'primary_ip.blocked': 'Blocked',
            'primary_ip.assignee_id': 'Assignee',
            'primary_ip.protection': 'Protection',
        },
        'floating_ip': {
            'floating_ip.ip': 'IP Address',
            'floating_ip.type': 'Address Family',
            'floating_ip.blocked': 'Blocked',
            'floating_ip.assignee_id': 'Assignee',
            'floating_ip.protection': 'Protection',
        },
        'network': {
            'network.name': 'Name',
            'network.ip_range': 'IP Range',
            'network.subnets': 'Subnets',
            'network.routes': 'Routes',
            'network.attached_servers': 'Attached Servers',
            'network.protection': 'Protection',
        },
        'firewall': {
            'firewall.name': 'Name',
            'firewall.rules': 'Rules',
            'firewall.applied_to': 'Applied Resources',
            'firewall.protection': 'Protection',
            'firewall.labels': 'Labels',
        },
        'load_balancer': {
            'load_balancer.name': 'Name',
            'load_balancer.status': 'Status',
            'load_balancer.targets': 'Targets',
            'load_balancer.services': 'Services',
            'load_balancer.protection': 'Protection',
        },
        'placement_group': {
            'placement_group.name': 'Name',
            'placement_group.type': 'Type',
            'placement_group.servers': 'Servers',
            'placement_group.protection': 'Protection',
        },
        'image': {
            'image.name': 'Name',
            'image.type': 'Type',
            'image.status': 'Status',
            'image.description': 'Description',
            'image.os_flavor': 'OS Flavor',
            'image.protection': 'Protection',
        },
        'certificate': {
            'certificate.name': 'Name',
            'certificate.type': 'Type',
            'certificate.status': 'Status',
            'certificate.domains': 'Domains',
            'certificate.not_valid_after': 'Expiration',
            'certificate.expires_at': 'Expiration',
            'certificate.sha1_fingerprint': 'SHA-1 Fingerprint',
        },
        'location': {
            'location.name': 'Name',
            'location.city': 'City',
            'location.country': 'Country',
            'location.latitude': 'Latitude',
            'location.longitude': 'Longitude',
        },
        'datacenter': {
            'datacenter.name': 'Name',
            'datacenter.description': 'Description',
            'datacenter.location.name': 'Location',
            'datacenter.server_types': 'Server Types',
        },
        'server_type': {
            'server_type.name': 'Name',
            'server_type.description': 'Description',
            'server_type.cores': 'CPU Cores',
            'server_type.memory': 'Memory',
            'server_type.disk': 'Disk',
        },
        'load_balancer_type': {
            'load_balancer_type.name': 'Name',
            'load_balancer_type.description': 'Description',
            'load_balancer_type.max_connections': 'Maximum Connections',
            'load_balancer_type.max_services': 'Maximum Services',
        },
        'iso': {
            'iso.name': 'Name',
            'iso.description': 'Description',
            'iso.type': 'Type',
            'iso.deprecated': 'Deprecated',
        },
        'ssh_key': {
            'ssh_key.name': 'Name',
            'ssh_key.fingerprint': 'Fingerprint',
            'ssh_key.labels': 'Labels',
        },
        'zone': {
            'zone.name': 'Name',
            'zone.mode': 'Mode',
            'zone.ttl': 'TTL',
            'zone.nameservers': 'Nameservers',
            'zone.primary_nameservers': 'Primary Nameservers',
        },
        'rrset': {
            'rrset.name': 'Name',
            'rrset.type': 'Type',
            'rrset.ttl': 'TTL',
            'rrset.records': 'Records',
        },
        'object_storage': {
            'bucket.name': 'Name',
            'bucket.region': 'Region',
            'bucket.CreationDate': 'Created At',
        },
        'action': {
            'action.status': 'Status',
            'action.progress': 'Progress',
            'action.command': 'Command',
            'action.error': 'Error',
        },
    },
    'aws': {
        'server': {
            'instance.InstanceType': 'Instance Type',
            'instance.State.Name': 'State',
            'instance.PublicIpAddress': 'Public IP',
            'instance.PrivateIpAddress': 'Private IP',
            'instance.VpcId': 'VPC ID',
            'instance.SubnetId': 'Subnet ID',
            'instance.Tags': 'Tags',
            'instance.KeyName': 'Key Pair',
            'instance.SecurityGroups.[].GroupName': 'Security Groups',
            'instance.BlockDeviceMappings.[].DeviceName': 'Block Devices'
        },
        'volume': {
            'volume.Size': 'Size (GB)',
            'volume.VolumeType': 'Volume Type',
            'volume.State': 'State',
            'volume.Iops': 'IOPS',
            'volume.Encrypted': 'Encrypted',
            'volume.SnapshotId': 'Snapshot ID',
            'volume.CreateTime': 'Creation Time',
            'volume.Tags': 'Tags',
            'volume.Attachments.[].InstanceId': 'Attached Instances'
        },
        'rds_database': {
            'database.DBInstanceStatus': 'Status',
            'database.Engine': 'Engine',
            'database.EngineVersion': 'Engine Version',
            'database.DBInstanceClass': 'Instance Class',
            'database.MasterUsername': 'Master Username',
            'database.DBName': 'Database Name',
            'database.AllocatedStorage': 'Allocated Storage (GB)',
            'database.StorageType': 'Storage Type',
            'database.StorageEncrypted': 'Storage Encrypted',
            'database.MultiAZ': 'Multi-AZ',
            'database.PubliclyAccessible': 'Publicly Accessible',
            'database.BackupRetentionPeriod': 'Backup Retention (Days)',
            'database.AvailabilityZone': 'Availability Zone',
            'database.PreferredBackupWindow': 'Backup Window',
            'database.PreferredMaintenanceWindow': 'Maintenance Window',
            'database.VpcSecurityGroups.[].VpcSecurityGroupId': 'Security Groups',
            'database.DBSubnetGroup.DBSubnetGroupName': 'Subnet Group',
            'database.Endpoint.Address': 'Endpoint Address',
            'database.Endpoint.Port': 'Port'
        },
        'lambda': {
            'function.FunctionName': 'Function Name',
            'function.Runtime': 'Runtime',
            'function.State': 'State',
            'function.StateReason': 'State Reason',
            'function.MemorySize': 'Memory Size (MB)',
            'function.Timeout': 'Timeout (seconds)',
            'function.Handler': 'Handler',
            'function.CodeSize': 'Code Size (bytes)',
            'function.Description': 'Description',
            'function.LastModified': 'Last Modified',
            'function.Environment.Variables': 'Environment Variables',
            'function.DeadLetterConfig.TargetArn': 'Dead Letter Queue',
            'function.VpcConfig.VpcId': 'VPC ID',
            'function.VpcConfig.SubnetIds': 'Subnet IDs',
            'function.VpcConfig.SecurityGroupIds': 'Security Group IDs',
            'function.ReservedConcurrencyExecutions': 'Reserved Concurrency',
            'function.Layers.[].Arn': 'Layers',
            'function.PackageType': 'Package Type',
            'function.Architectures': 'Architectures'
        },
        'dynamodb': {
            'table.TableName': 'Table Name',
            'table.TableStatus': 'Status',
            'table.TableSizeBytes': 'Table Size (bytes)',
            'table.ItemCount': 'Item Count',
            'table.BillingModeSummary.BillingMode': 'Billing Mode',
            'table.ProvisionedThroughput.ReadCapacityUnits': 'Read Capacity Units',
            'table.ProvisionedThroughput.WriteCapacityUnits': 'Write Capacity Units',
            'table.TableClass': 'Table Class',
            'table.TableArn': 'Table ARN',
            'table.CreationDateTime': 'Creation Date',
            'table.DeletionProtectionEnabled': 'Deletion Protection',
            'table.PointInTimeRecoveryDescription.PointInTimeRecoveryStatus': 'Point-in-Time Recovery',
            'table.ArchivalSummary.ArchivalDateTime': 'Archival Date',
            'table.SSEDescription.Status': 'Server-Side Encryption',
            'table.StreamSpecification.StreamEnabled': 'DynamoDB Streams',
            'table.StreamSpecification.StreamViewType': 'Stream View Type',
            'table.GlobalSecondaryIndexes.[].IndexName': 'Global Secondary Indexes',
            'table.LocalSecondaryIndexes.[].IndexName': 'Local Secondary Indexes',
            'table.Tags.[].Key': 'Tags'
        },
        's3_bucket': {
            'bucket.Name': 'Bucket Name',
            'bucket.LocationConstraint': 'Region',
            'bucket.Versioning.Status': 'Versioning Status',
            'bucket.Versioning.MfaDelete': 'MFA Delete',
            'bucket.Encryption.Rules.[].ApplyServerSideEncryptionByDefault.SSEAlgorithm': 'Encryption Algorithm',
            'bucket.Encryption.Rules.[].ApplyServerSideEncryptionByDefault.KMSMasterKeyID': 'KMS Key ID',
            'bucket.Encryption.Rules.[].BucketKeyEnabled': 'Bucket Key Enabled',
            'bucket.PublicAccessBlock.BlockPublicAcls': 'Block Public ACLs',
            'bucket.PublicAccessBlock.IgnorePublicAcls': 'Ignore Public ACLs',
            'bucket.PublicAccessBlock.BlockPublicPolicy': 'Block Public Policy',
            'bucket.PublicAccessBlock.RestrictPublicBuckets': 'Restrict Public Buckets',
            'bucket.Cors.CORSRules.[].AllowedMethods': 'CORS Allowed Methods',
            'bucket.Cors.CORSRules.[].AllowedOrigins': 'CORS Allowed Origins',
            'bucket.Cors.CORSRules.[].AllowedHeaders': 'CORS Allowed Headers',
            'bucket.Lifecycle.Rules.[].Status': 'Lifecycle Rules Status',
            'bucket.Notification.Configurations': 'Notification Configurations',
            'bucket.Website.IndexDocument.Suffix': 'Website Index Document',
            'bucket.Logging.LoggingEnabled.TargetBucket': 'Access Logging Target',
            'bucket.RequestPayment.Payer': 'Request Payment Configuration',
            'bucket.Replication.Rules.[].Status': 'Replication Rules Status'
        },
        'acm_certificate': {
            'certificate.DomainName': 'Domain Name',
            'certificate.Status': 'Status',
            'certificate.Type': 'Certificate Type',
            'certificate.KeyAlgorithm': 'Key Algorithm',
            'certificate.KeyUsages.[].Name': 'Key Usages',
            'certificate.ExtendedKeyUsages.[].Name': 'Extended Key Usages',
            'certificate.CreatedAt': 'Created Date',
            'certificate.IssuedAt': 'Issued Date',
            'certificate.NotBefore': 'Not Before',
            'certificate.NotAfter': 'Expiration Date',
            'certificate.Serial': 'Serial Number',
            'certificate.Subject': 'Subject',
            'certificate.Issuer': 'Issuer',
            'certificate.DomainValidationOptions.[].DomainName': 'Domain Validation',
            'certificate.DomainValidationOptions.[].ValidationStatus': 'Validation Status',
            'certificate.DomainValidationOptions.[].ValidationMethod': 'Validation Method',
            'certificate.SubjectAlternativeNames': 'Subject Alternative Names',
            'certificate.InUseBy': 'In Use By',
            'certificate.FailureReason': 'Failure Reason',
            'certificate.Options.CertificateTransparencyLoggingPreference': 'Certificate Transparency',
            'certificate.RenewalEligibility': 'Renewal Eligibility'
        },
        'snapshot': {
            'snapshot.SnapshotId': 'Snapshot ID',
            'snapshot.Description': 'Description',
            'snapshot.State': 'State',
            'snapshot.StateMessage': 'State Message',
            'snapshot.Progress': 'Progress',
            'snapshot.StartTime': 'Start Time',
            'snapshot.VolumeId': 'Source Volume ID',
            'snapshot.VolumeSize': 'Volume Size (GB)',
            'snapshot.OwnerId': 'Owner ID',
            'snapshot.OwnerAlias': 'Owner Alias',
            'snapshot.Encrypted': 'Encrypted',
            'snapshot.KmsKeyId': 'KMS Key ID',
            'snapshot.DataEncryptionKeyId': 'Data Encryption Key ID',
            'snapshot.StorageTier': 'Storage Tier',
            'snapshot.RestoreExpiryTime': 'Restore Expiry Time',
            'snapshot.Tags.[].Key': 'Tag Keys',
            'snapshot.Tags.[].Value': 'Tag Values',
            'snapshot.OutpostArn': 'Outpost ARN',
            'snapshot.SseType': 'Server-Side Encryption Type'
        },
        'elastic_ip': {
            'elastic_ip.PublicIp': 'Public IP Address',
            'elastic_ip.AllocationId': 'Allocation ID',
            'elastic_ip.AssociationId': 'Association ID',
            'elastic_ip.Domain': 'Domain',
            'elastic_ip.InstanceId': 'Instance ID',
            'elastic_ip.PublicIpv4Pool': 'Public IPv4 Pool',
            'elastic_ip.NetworkBorderGroup': 'Network Border Group',
            'elastic_ip.NetworkInterfaceId': 'Network Interface ID',
            'elastic_ip.NetworkInterfaceOwnerId': 'Network Interface Owner ID',
            'elastic_ip.PrivateIpAddress': 'Private IP Address',
            'elastic_ip.CarrierIp': 'Carrier IP',
            'elastic_ip.CustomerOwnedIp': 'Customer Owned IP',
            'elastic_ip.CustomerOwnedIpv4Pool': 'Customer Owned IPv4 Pool',
            'elastic_ip.Tags.[].Key': 'Tag Keys',
            'elastic_ip.Tags.[].Value': 'Tag Values'
        },
        'load_balancer': {
            'load_balancer.LoadBalancerName': 'Load Balancer Name',
            'load_balancer.LoadBalancerArn': 'Load Balancer ARN',
            'load_balancer.DNSName': 'DNS Name',
            'load_balancer.CanonicalHostedZoneId': 'Hosted Zone ID',
            'load_balancer.CreatedTime': 'Created Time',
            'load_balancer.State.Code': 'State',
            'load_balancer.State.Reason': 'State Reason',
            'load_balancer.Type': 'Type',
            'load_balancer.Scheme': 'Scheme',
            'load_balancer.VpcId': 'VPC ID',
            'load_balancer.AvailabilityZones.[].ZoneName': 'Availability Zones',
            'load_balancer.AvailabilityZones.[].SubnetId': 'Subnet IDs',
            'load_balancer.SecurityGroups': 'Security Groups',
            'load_balancer.IpAddressType': 'IP Address Type',
            'load_balancer.CustomerOwnedIpv4Pool': 'Customer Owned IPv4 Pool',
            'load_balancer.Listeners.[].Protocol': 'Listener Protocols',
            'load_balancer.Listeners.[].Port': 'Listener Ports',
            'load_balancer.Listeners.[].SslPolicy': 'SSL Policies',
            'load_balancer.TargetGroups.[].TargetGroupName': 'Target Group Names',
            'load_balancer.TargetGroups.[].Protocol': 'Target Group Protocols',
            'load_balancer.TargetGroups.[].Port': 'Target Group Ports',
            'load_balancer.TargetGroups.[].HealthCheckProtocol': 'Health Check Protocols',
            'load_balancer.TargetGroups.[].HealthCheckPath': 'Health Check Paths',
            'load_balancer.InstanceStates.[].InstanceId': 'Classic LB Instance IDs',
            'load_balancer.InstanceStates.[].State': 'Classic LB Instance States'
        },
        'security_group': {
            'security_group.GroupId': 'Group ID',
            'security_group.GroupName': 'Group Name',
            'security_group.Description': 'Description',
            'security_group.VpcId': 'VPC ID',
            'security_group.OwnerId': 'Owner ID',
            'security_group.IpPermissions.[].IpProtocol': 'Inbound Protocols',
            'security_group.IpPermissions.[].FromPort': 'Inbound From Port',
            'security_group.IpPermissions.[].ToPort': 'Inbound To Port',
            'security_group.IpPermissions.[].IpRanges.[].CidrIp': 'Inbound CIDR Blocks',
            'security_group.IpPermissions.[].IpRanges.[].Description': 'Inbound CIDR Descriptions',
            'security_group.IpPermissions.[].UserIdGroupPairs.[].GroupId': 'Inbound Referenced Security Groups',
            'security_group.IpPermissions.[].UserIdGroupPairs.[].Description': 'Inbound SG Descriptions',
            'security_group.IpPermissions.[].PrefixListIds.[].PrefixListId': 'Inbound Prefix Lists',
            'security_group.IpPermissionsEgress.[].IpProtocol': 'Outbound Protocols',
            'security_group.IpPermissionsEgress.[].FromPort': 'Outbound From Port',
            'security_group.IpPermissionsEgress.[].ToPort': 'Outbound To Port',
            'security_group.IpPermissionsEgress.[].IpRanges.[].CidrIp': 'Outbound CIDR Blocks',
            'security_group.IpPermissionsEgress.[].IpRanges.[].Description': 'Outbound CIDR Descriptions',
            'security_group.IpPermissionsEgress.[].UserIdGroupPairs.[].GroupId': 'Outbound Referenced Security Groups',
            'security_group.IpPermissionsEgress.[].UserIdGroupPairs.[].Description': 'Outbound SG Descriptions',
            'security_group.IpPermissionsEgress.[].PrefixListIds.[].PrefixListId': 'Outbound Prefix Lists',
            'security_group.Tags.[].Key': 'Tag Keys',
            'security_group.Tags.[].Value': 'Tag Values'
        },
        'ecs_service': {
            'service.serviceName': 'Service Name',
            'service.serviceArn': 'Service ARN',
            'service.clusterArn': 'Cluster ARN',
            'service.taskDefinition': 'Task Definition ARN',
            'service.status': 'Status',
            'service.runningCount': 'Running Count',
            'service.pendingCount': 'Pending Count',
            'service.desiredCount': 'Desired Count',
            'service.platformVersion': 'Platform Version',
            'service.platformFamily': 'Platform Family',
            'service.launchType': 'Launch Type',
            'service.capacityProviderStrategy.[].capacityProvider': 'Capacity Providers',
            'service.capacityProviderStrategy.[].weight': 'Capacity Provider Weights',
            'service.loadBalancers.[].targetGroupArn': 'Target Group ARNs',
            'service.loadBalancers.[].containerName': 'Container Names',
            'service.loadBalancers.[].containerPort': 'Container Ports',
            'service.serviceRegistries.[].registryArn': 'Service Registry ARNs',
            'service.networkConfiguration.awsvpcConfiguration.subnets': 'VPC Subnets',
            'service.networkConfiguration.awsvpcConfiguration.securityGroups': 'VPC Security Groups',
            'service.networkConfiguration.awsvpcConfiguration.assignPublicIp': 'Assign Public IP',
            'service.healthCheckGracePeriodSeconds': 'Health Check Grace Period',
            'service.schedulingStrategy': 'Scheduling Strategy',
            'service.deploymentController.type': 'Deployment Controller Type',
            'service.tags.[].key': 'Tag Keys',
            'service.tags.[].value': 'Tag Values'
        },
        'ecs_task': {
            'task.taskArn': 'Task ARN',
            'task.clusterArn': 'Cluster ARN',
            'task.taskDefinitionArn': 'Task Definition ARN',
            'task.lastStatus': 'Last Status',
            'task.desiredStatus': 'Desired Status',
            'task.healthStatus': 'Health Status',
            'task.stopCode': 'Stop Code',
            'task.stopReason': 'Stop Reason',
            'task.connectivity': 'Connectivity',
            'task.connectivityAt': 'Connectivity At',
            'task.pullStartedAt': 'Pull Started At',
            'task.pullStoppedAt': 'Pull Stopped At',
            'task.startedAt': 'Started At',
            'task.stoppedAt': 'Stopped At',
            'task.stoppedReason': 'Stopped Reason',
            'task.stoppingAt': 'Stopping At',
            'task.createdAt': 'Created At',
            'task.group': 'Task Group',
            'task.launchType': 'Launch Type',
            'task.platformVersion': 'Platform Version',
            'task.platformFamily': 'Platform Family',
            'task.cpu': 'CPU',
            'task.memory': 'Memory',
            'task.containers.[].containerArn': 'Container ARNs',
            'task.containers.[].name': 'Container Names',
            'task.containers.[].lastStatus': 'Container Statuses',
            'task.containers.[].healthStatus': 'Container Health',
            'task.containers.[].cpu': 'Container CPU',
            'task.containers.[].memory': 'Container Memory',
            'task.attachments.[].type': 'Attachment Types',
            'task.attachments.[].status': 'Attachment Status',
            'task.tags.[].key': 'Tag Keys',
            'task.tags.[].value': 'Tag Values'
        }
    },
    'upcloud': {
        'server': {
            'server.title': 'Name',
            'server.hostname': 'Hostname',
            'server.state': 'State',
            'server.zone': 'Zone',
            'server.plan': 'Plan',
            'server.memory_amount': 'Memory',
            'server.core_number': 'CPU Cores',
            'server.storage_devices.storage_device.[].address': 'Storage Devices',
            'server.ip_addresses.ip_address.[].address': 'IP Addresses',
            'server.firewall_state': 'Firewall State',
            'server.boot_mode': 'Boot Mode',
            'server.tags.tag': 'Tags'
        },
        'volume': {
            'volume.title': 'Name',
            'volume.size': 'Size (GB)',
            'volume.state': 'State',
            'volume.type': 'Type',
            'volume.zone': 'Zone',
            'volume.license': 'License',
            'volume.tier': 'Tier',
            'volume.servers': 'Attached Servers',
            'volume.tags.tag': 'Tags'
        }
    },
    'linode': {
        'server': {
            'id': 'ID',
            'label': 'Name',
            'status': 'Status',
            'region': 'Region',
            'type': 'Type',
            'hypervisor': 'Hypervisor',
            'specs.memory': 'Memory',
            'specs.vcpus': 'vCPUs',
            'specs.disk': 'Disk',
            'ipv4': 'IPv4 Addresses',
            'ipv6': 'IPv6 Address',
            'backups.enabled': 'Backups Enabled',
            'backups.schedule.day': 'Backup Day',
            'backups.schedule.window': 'Backup Window',
            'tags': 'Tags',
            'group': 'Group',
            'watchdog_enabled': 'Watchdog Enabled',
            'alerts.cpu': 'CPU Alert Threshold',
            'alerts.network_in': 'Network In Alert Threshold',
            'alerts.network_out': 'Network Out Alert Threshold',
            'alerts.transfer_quota': 'Transfer Quota Alert Threshold',
            'alerts.io': 'IO Alert Threshold'
        },
        'volume': {
            'id': 'ID',
            'label': 'Name',
            'size': 'Size (GB)',
            'region': 'Region',
            'linode_id': 'Attached Linode ID',
            'filesystem_path': 'Filesystem Path',
            'status': 'Status',
            'created': 'Created Date',
            'updated': 'Updated Date',
            'tags': 'Tags'
        }
    }
}

# Lightsail resources use the same AWS credential context but expose a
# separate control-plane payload. Keep their lifecycle, configuration,
# firewall, tag, backup, and metric changes visible to the monitoring engine.
PROVIDER_METADATA_FIELDS['aws'].update({
    'lightsail_instance': {
        'lightsail_instance.name': 'Name',
        'lightsail_instance.state': 'State',
        'lightsail_instance.publicIpAddress': 'Public IP',
        'lightsail_instance.privateIpAddress': 'Private IP',
        'lightsail_instance.blueprintName': 'Blueprint',
        'lightsail_instance.bundleId': 'Bundle',
        'lightsail_instance.tags': 'Tags',
        'lightsailDetails.portStates': 'Firewall Ports',
        'lightsailDetails.autoSnapshots': 'Auto Snapshots',
        'lightsailDetails.metric.metricData': 'CPU Utilization',
    },
    'lightsail_disk': {
        'lightsail_disk.name': 'Name',
        'lightsail_disk.state': 'State',
        'lightsail_disk.sizeInGb': 'Size (GB)',
        'lightsail_disk.attachedTo': 'Attached Instance',
        'lightsail_disk.tags': 'Tags',
        'lightsailDetails.autoSnapshots': 'Auto Snapshots',
    },
    'lightsail_instance_snapshot': {
        'lightsail_instance_snapshot.name': 'Name',
        'lightsail_instance_snapshot.state': 'State',
        'lightsail_instance_snapshot.fromInstanceName': 'Source Instance',
        'lightsail_instance_snapshot.sizeInGb': 'Size (GB)',
        'lightsail_instance_snapshot.tags': 'Tags',
    },
    'lightsail_disk_snapshot': {
        'lightsail_disk_snapshot.name': 'Name',
        'lightsail_disk_snapshot.state': 'State',
        'lightsail_disk_snapshot.fromDiskName': 'Source Disk',
        'lightsail_disk_snapshot.sizeInGb': 'Size (GB)',
        'lightsail_disk_snapshot.tags': 'Tags',
    },
    'lightsail_static_ip': {
        'lightsail_static_ip.name': 'Name',
        'lightsail_static_ip.ipAddress': 'IP Address',
        'lightsail_static_ip.attachedTo': 'Attached Instance',
        'lightsail_static_ip.tags': 'Tags',
    },
    'lightsail_database': {
        'lightsail_database.name': 'Name',
        'lightsail_database.state': 'State',
        'lightsail_database.engine': 'Engine',
        'lightsail_database.engineVersion': 'Engine Version',
        'lightsail_database.masterDatabaseName': 'Database Name',
        'lightsail_database.tags': 'Tags',
        'lightsailDetails.metric.metricData': 'CPU Utilization',
    },
    'lightsail_database_snapshot': {
        'lightsail_database_snapshot.name': 'Name',
        'lightsail_database_snapshot.state': 'State',
        'lightsail_database_snapshot.fromRelationalDatabaseName': 'Source Database',
        'lightsail_database_snapshot.engine': 'Engine',
        'lightsail_database_snapshot.tags': 'Tags',
    },
    'lightsail_load_balancer': {
        'lightsail_load_balancer.name': 'Name',
        'lightsail_load_balancer.state': 'State',
        'lightsail_load_balancer.publicPorts': 'Public Ports',
        'lightsail_load_balancer.instanceHealthSummary': 'Instance Health',
        'lightsail_load_balancer.tags': 'Tags',
        'lightsailDetails.tlsCertificates': 'TLS Certificates',
        'lightsailDetails.metric.metricData': 'Request Count',
    },
    'lightsail_certificate': {
        'lightsail_certificate.certificateName': 'Name',
        'lightsail_certificate.certificateDetail.status': 'Status',
        'lightsail_certificate.domainName': 'Domain Name',
        'lightsail_certificate.certificateDetail.subjectAlternativeNames': 'Subject Alternative Names',
        'lightsail_certificate.tags': 'Tags',
    },
    'lightsail_bucket': {
        'lightsail_bucket.name': 'Name',
        'lightsail_bucket.state': 'State',
        'lightsail_bucket.bundleId': 'Storage Bundle',
        'lightsail_bucket.objectVersioning': 'Object Versioning',
        'lightsail_bucket.cors': 'CORS Rules',
        'lightsail_bucket.resourcesReceivingAccess': 'Connected Resources',
        'lightsail_bucket.tags': 'Tags',
        'lightsailDetails.metric.metricData': 'Object Count',
    },
    'lightsail_distribution': {
        'lightsail_distribution.name': 'Name',
        'lightsail_distribution.status': 'Status',
        'lightsail_distribution.isEnabled': 'Enabled',
        'lightsail_distribution.origin': 'Origin',
        'lightsail_distribution.tags': 'Tags',
        'lightsailDetails.metric.metricData': 'Requests',
    },
    'lightsail_domain': {
        'lightsail_domain.name': 'Domain Name',
        'lightsail_domain.domainEntries': 'DNS Records',
        'lightsail_domain.tags': 'Tags',
    },
    'lightsail_dns_record': {
        'lightsail_dns_record.name': 'Record Name',
        'lightsail_dns_record.type': 'Record Type',
        'lightsail_dns_record.target': 'Target',
        'lightsail_dns_record.ttl': 'TTL',
        'lightsail_dns_record.isAlias': 'Alias Record',
    },
    'lightsail_container_service': {
        'lightsail_container_service.containerServiceName': 'Service Name',
        'lightsail_container_service.state': 'State',
        'lightsail_container_service.power': 'Power',
        'lightsail_container_service.scale': 'Scale',
        'lightsail_container_service.currentDeployment': 'Current Deployment',
        'lightsail_container_service.tags': 'Tags',
        'lightsailDetails.metric.metricData': 'CPU Utilization',
        'lightsailDetails.containerLogs': 'Container Logs',
    },
    'lightsail_container_deployment': {
        'lightsail_container_deployment.version': 'Version',
        'lightsail_container_deployment.state': 'State',
        'lightsail_container_deployment.containers': 'Containers',
        'lightsail_container_deployment.createdAt': 'Created At',
    },
    'lightsail_container_image': {
        'lightsail_container_image.image': 'Image',
        'lightsail_container_image.digest': 'Digest',
        'lightsail_container_image.createdAt': 'Created At',
    },
    'lightsail_alarm': {
        'lightsail_alarm.name': 'Name',
        'lightsail_alarm.state': 'State',
        'lightsail_alarm.resourceName': 'Monitored Resource',
        'lightsail_alarm.metricName': 'Metric',
        'lightsail_alarm.threshold': 'Threshold',
        'lightsail_alarm.tags': 'Tags',
    },
    'lightsail_operation': {
        'lightsail_operation.id': 'Operation ID',
        'lightsail_operation.resourceName': 'Resource',
        'lightsail_operation.resourceType': 'Resource Type',
        'lightsail_operation.status': 'Status',
        'lightsail_operation.errorCode': 'Error Code',
        'lightsail_operation.createdAt': 'Created At',
    },
    'lightsail_auto_snapshot': {
        'lightsail_auto_snapshot.date': 'Snapshot Date',
        'lightsail_auto_snapshot.status': 'Status',
        'lightsail_auto_snapshot.fromInstanceName': 'Source Instance',
        'lightsail_auto_snapshot.fromDiskName': 'Source Disk',
        'lightsail_auto_snapshot.sizeInGb': 'Size (GB)',
    },
})


def filter_metadata(metadata, provider, asset_type):
    """Filter metadata to only include fields defined in PROVIDER_METADATA_FIELDS"""
    if not provider in PROVIDER_METADATA_FIELDS or not asset_type in PROVIDER_METADATA_FIELDS[provider]:
        return {}

    filtered_metadata = {}
    important_fields = PROVIDER_METADATA_FIELDS[provider][asset_type]

    # Special handling for Linode which has a flat structure at the root
    if provider == 'linode':
        for field_path, display_name in important_fields.items():
            parts = field_path.split('.')
            current = metadata

            # Navigate through the parts to get the value
            valid_path = True
            for part in parts:
                if part == '[]':
                    # Handle array access
                    if isinstance(current, list):
                        break  # We'll handle the array at the end
                    else:
                        valid_path = False
                        break
                elif isinstance(current, dict) and part in current:
                    current = current[part]
                else:
                    valid_path = False
                    break

            if not valid_path:
                continue

            # If we successfully navigated to a value, add it to filtered metadata
            # Build the nested structure in filtered metadata
            target = filtered_metadata
            for i, part in enumerate(parts[:-1]):
                if part == '[]':
                    break
                if part not in target:
                    target[part] = {}
                target = target[part]

            # Set the final value
            if parts[-1] != '[]' and valid_path:
                target[parts[-1]] = current
            elif parts[-1] == '[]' and isinstance(current, list):
                last_valid_part = parts[-2]
                target[last_valid_part] = current

        return redact_sensitive_metadata(filtered_metadata)

    # Original logic for other providers
    for field_path in important_fields.keys():
        parts = field_path.split('.')
        current = metadata

        # Skip if the root element doesn't exist in metadata
        root = parts[0]
        if root not in metadata:
            continue

        # Initialize the root in filtered metadata if not present
        if root not in filtered_metadata:
            filtered_metadata[root] = {}

        current = metadata[root]
        target = filtered_metadata[root]

        # Navigate through the remaining parts
        for i, part in enumerate(parts[1:], 1):
            if part == '[]':
                # Handle array specially
                if isinstance(current, list):
                    # If the previous part exists in target, make it a list
                    prev_part = parts[i - 1]
                    target[prev_part] = current
                break

            # If we've reached the last part, set the value
            if i == len(parts) - 1:
                if isinstance(current, dict):
                    target[part] = current.get(part)
            else:
                # Create nested structure
                if isinstance(current, dict):
                    if part not in target:
                        target[part] = {}
                    current = current.get(part, {})
                    target = target[part]

    return redact_sensitive_metadata(filtered_metadata)


def get_nested_value(data, path):
    """Get value from nested dictionary using dot notation path"""
    parts = path.split('.')
    current = data

    def normalize_number(value):
        """Normalize number representation to avoid false positives"""
        try:
            num = float(value)
            if num.is_integer():
                return str(int(num))
            return f"{num:.1f}"
        except (ValueError, TypeError):
            return str(value)

    def normalize_list_of_dicts(lst):
        """Normalize a list of dictionaries by sorting them based on their items"""
        if not lst or not isinstance(lst[0], dict):
            return lst
        return sorted([dict(sorted(d.items())) for d in lst], key=lambda x: str(sorted(x.items())))

    for part in parts:
        if part == '[]':
            # Handle array access
            if isinstance(current, list):
                if current and isinstance(current[0], dict):
                    return normalize_list_of_dicts(current)
                return sorted(current) if current else current
            else:
                return None
        else:
            if isinstance(current, dict):
                if isinstance(current.get(part, {}), dict):
                    current = current.get(part, {})
                else:
                    value = current.get(part, '')
                    if isinstance(value, list):
                        if value and isinstance(value[0], dict):
                            current = normalize_list_of_dicts(value)
                        else:
                            current = sorted(value) if value else value
                    elif isinstance(value, (int, float)):
                        current = normalize_number(value)
                    else:
                        current = str(value)
            else:
                if isinstance(current, (int, float)):
                    return normalize_number(current)
                return str(current) if current is not None else ''

    if isinstance(current, (int, float)):
        return normalize_number(current)
    elif isinstance(current, list):
        if current and isinstance(current[0], dict):
            return normalize_list_of_dicts(current)
        return sorted(current) if current else current
    return str(current) if current is not None else ''


def compare_metadata(previous_metadata, current_metadata, provider, asset_type):
    """
    Compare previous and current metadata to find meaningful differences.
    Returns a list of changes in human-readable format.
    """
    if not provider in PROVIDER_METADATA_FIELDS or not asset_type in PROVIDER_METADATA_FIELDS[provider]:
        return []

    important_fields = PROVIDER_METADATA_FIELDS[provider][asset_type]
    changes = []

    for field_path, display_name in important_fields.items():
        try:
            prev_value = get_nested_value(previous_metadata, field_path) if previous_metadata else ''
            curr_value = get_nested_value(current_metadata, field_path) if current_metadata else ''

            if prev_value != curr_value:
                if '[]' in field_path:
                    # Handle array differences
                    if isinstance(prev_value, list) and isinstance(curr_value, list):
                        prev_set = set(str(x) for x in prev_value)
                        curr_set = set(str(x) for x in curr_value)
                        added = curr_set - prev_set
                        removed = prev_set - curr_set
                        if added:
                            changes.append(f"{display_name}: Added {', '.join(added)}")
                        if removed:
                            changes.append(f"{display_name}: Removed {', '.join(removed)}")
                else:
                    # Handle simple value changes
                    changes.append(f"{display_name} changed from '{prev_value}' to '{curr_value}'")
        except Exception as e:
            logger.error(f"Error comparing field {field_path}: {str(e)}")
            continue

    return changes
