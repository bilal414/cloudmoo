"""
Metadata filtering and comparison for asset status checks.

Ported from the cloudMooCheckAssetStatus Lambda. Previous metadata now comes
from PostgreSQL JSON fields, so the DynamoDB wire-format handling
({'S': ..., 'N': ..., 'M': ..., 'L': ...}) from the original has been dropped.
"""
import logging

logger = logging.getLogger(__name__)

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
        }
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

        return filtered_metadata

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

    return filtered_metadata


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
