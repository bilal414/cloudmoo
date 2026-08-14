from django.db import models
from apps.console.cloud.models import CloudValidationTransientError, CoreCloud
from apps.console.utils.helper import _serialize_datetime
from apps.console.utils.models import UtilCloud, UtilAsset
import boto3
import logging
from botocore.config import Config
from botocore.exceptions import ClientError, NoCredentialsError
from django.utils import timezone

logger = logging.getLogger(__name__)


AWS_CLIENT_CONFIG = Config(
    connect_timeout=5,
    read_timeout=15,
    retries={'mode': 'standard', 'max_attempts': 2},
)


def _required_list(payload, key, context):
    """Fail a sync rather than treating an incomplete AWS page as empty."""
    if not isinstance(payload, dict) or key not in payload:
        raise RuntimeError(f"AWS returned an incomplete {context} response")
    value = payload[key]
    if not isinstance(value, list):
        raise RuntimeError(f"AWS returned an invalid {context} collection")
    return value

class CoreAWSAccount(UtilCloud):
    cloud = models.ForeignKey(CoreCloud, on_delete=models.CASCADE, related_name="aws")
    access_key = models.CharField(max_length=255)
    secret_key = models.CharField(max_length=255)
    region = models.CharField(max_length=20)

    class Meta:
        db_table = "core_aws_account"

    def __str__(self):
        return self.name

    @property
    def access_token(self):
        return {'access_key': self.access_key, 'secret_key': self.secret_key, 'region': self.region}

    def validate(self):
        try:
            session = boto3.Session(
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
                region_name=self.region
            )
            ec2 = session.client('ec2', config=AWS_CLIENT_CONFIG)
            ec2.describe_instances()
            return True
        except NoCredentialsError:
            return False
        except ClientError as error:
            code = (error.response.get('Error') or {}).get('Code', '')
            if code in {
                'AccessDenied',
                'AccessDeniedException',
                'AuthFailure',
                'InvalidClientTokenId',
                'InvalidUserID.NotFound',
                'UnauthorizedOperation',
                'InvalidRegion',
            }:
                return False
            raise CloudValidationTransientError(
                'AWS validation temporarily unavailable'
            ) from error
        except Exception as error:
            raise CloudValidationTransientError(
                'AWS validation temporarily unavailable'
            ) from error

    def sync_assets(self):
        self.sync_servers()
        self.sync_volumes()
        self.sync_rds_databases()
        self.sync_lambda_functions()
        self.sync_dynamodb_tables()
        self.sync_s3_buckets()
        self.sync_elastic_ips()
        self.sync_load_balancers()
        self.sync_security_groups()
        self.sync_ecs_services()
        self.sync_ecs_tasks()

        # Priority 0 adapters are imported lazily because each provider module
        # refers back to CoreAWSAccount for its persisted owner relation.
        # Calling them directly is intentional: discovery/authentication
        # failures must remain visible to the cloud sync caller.
        from . import (
            application_services,
            backup,
            containers,
            data_services,
            delivery,
            edge,
            network,
            observability,
            account_operations,
            credentials_config,
            security_governance,
        )

        network.sync_aws_network_assets(self)
        observability.sync_aws_observability_assets(self)
        containers.sync_aws_container_assets(self)
        edge.sync_aws_edge_assets(self)
        backup.sync_aws_backup_assets(self)
        backup.sync_aws_snapshots(self)
        edge.sync_aws_regional_certificates(self)
        data_services.sync_aws_data_service_assets(self)
        application_services.sync_aws_application_service_assets(self)
        delivery.sync_aws_delivery_assets(self)

        self.sync_lightsail_assets()
        security_governance.sync_aws_security_governance_assets(self)
        credentials_config.sync_aws_credentials_config_assets(self)
        account_operations.sync_aws_account_operations_assets(self)
        self.last_synced = timezone.now()
        self.save()

    # Task-sized inventory units for the distributed sync pipeline.  A full
    # AWS pass takes far longer than any single task's time limit, so the
    # periodic sync fans these out into one task per family (and one per
    # region for the CloudWatch metrics walk, which dominates the runtime).
    AWS_SYNC_FAMILIES = (
        'servers',
        'volumes',
        'rds_databases',
        'lambda_functions',
        'dynamodb_tables',
        's3_buckets',
        'elastic_ips',
        'load_balancers',
        'security_groups',
        'ecs_services',
        'ecs_tasks',
        'network',
        'containers',
        'edge',
        'backup',
        'snapshots',
        'certificates',
        'data_services',
        'application_services',
        'delivery',
        'lightsail',
        'security_governance',
        'credentials_config',
        'account_operations',
        'observability.alarms',
        'observability.log_groups',
    )

    def sync_asset_families(self):
        families = [(family_key, None) for family_key in self.AWS_SYNC_FAMILIES]
        from .discovery import get_enabled_regions
        try:
            regions = get_enabled_regions(self)
        except Exception as error:
            # Keep the fan-out alive with a single all-regions metrics shard;
            # the shard itself retries region discovery.
            logger.warning(
                "AWS region discovery failed during sync fan-out for account %s: %s",
                self.pk,
                type(error).__name__,
            )
            families.append(('observability.metrics', None))
        else:
            families.extend(('observability.metrics', region) for region in regions)
        return families

    def sync_asset_family(self, family_key, region=None):
        if family_key.startswith('observability.'):
            from . import observability
            asset_types = {
                'observability.alarms': observability.ASSET_TYPE_CLOUDWATCH_ALARM,
                'observability.metrics': observability.ASSET_TYPE_CLOUDWATCH_METRIC,
                'observability.log_groups': observability.ASSET_TYPE_LOG_GROUP,
            }
            asset_type = asset_types.get(family_key)
            if asset_type is None:
                raise ValueError(f"Unknown AWS sync family: {family_key}")
            return observability.sync_aws_observability_collection(
                self, asset_type, region=region
            )

        # Priority 0 adapters are imported lazily because each provider module
        # refers back to CoreAWSAccount for its persisted owner relation.
        from . import (
            account_operations,
            application_services,
            backup,
            containers,
            credentials_config,
            data_services,
            delivery,
            edge,
            network,
            security_governance,
        )

        handlers = {
            'servers': self.sync_servers,
            'volumes': self.sync_volumes,
            'rds_databases': self.sync_rds_databases,
            'lambda_functions': self.sync_lambda_functions,
            'dynamodb_tables': self.sync_dynamodb_tables,
            's3_buckets': self.sync_s3_buckets,
            'elastic_ips': self.sync_elastic_ips,
            'load_balancers': self.sync_load_balancers,
            'security_groups': self.sync_security_groups,
            'ecs_services': self.sync_ecs_services,
            'ecs_tasks': self.sync_ecs_tasks,
            'network': lambda: network.sync_aws_network_assets(self),
            'containers': lambda: containers.sync_aws_container_assets(self),
            'edge': lambda: edge.sync_aws_edge_assets(self),
            'backup': lambda: backup.sync_aws_backup_assets(self),
            'snapshots': lambda: backup.sync_aws_snapshots(self),
            'certificates': lambda: edge.sync_aws_regional_certificates(self),
            'data_services': lambda: data_services.sync_aws_data_service_assets(self),
            'application_services': lambda: application_services.sync_aws_application_service_assets(self),
            'delivery': lambda: delivery.sync_aws_delivery_assets(self),
            'lightsail': self.sync_lightsail_assets,
            'security_governance': lambda: security_governance.sync_aws_security_governance_assets(self),
            'credentials_config': lambda: credentials_config.sync_aws_credentials_config_assets(self),
            'account_operations': lambda: account_operations.sync_aws_account_operations_assets(self),
        }
        handler = handlers.get(family_key)
        if handler is None:
            raise ValueError(f"Unknown AWS sync family: {family_key}")
        return handler()

    def _get_aws_client(self, service='ec2', region=None):
        return boto3.client(
            service,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            region_name=region or self.region,
            config=AWS_CLIENT_CONFIG,
        )

    def sync_lightsail_assets(self):
        """Synchronize Lightsail resources without invoking mutating APIs."""
        from .lightsail import sync_lightsail_assets

        return sync_lightsail_assets(self)

    def sync_servers(self):
        ec2 = self._get_aws_client()
        paginator = ec2.get_paginator('describe_instances')
        current_instance_ids = []

        for page in paginator.paginate():
            for reservation in page['Reservations']:
                for instance_data in reservation['Instances']:
                    # Convert datetime objects to ISO format strings
                    instance_data = _serialize_datetime(instance_data)

                    instance_id = instance_data['InstanceId']
                    name = next(
                        (tag['Value'] for tag in instance_data.get('Tags', []) if tag['Key'] == 'Name'),
                        instance_id
                    )

                    try:
                        instance = CoreAWSInstance.objects.get(
                            owner=self,
                            unique_id=instance_id
                        )
                        instance.name = name
                        instance.type = CoreAWSInstance.Type.SERVER
                        instance.metadata = instance_data
                        instance.save()
                    except CoreAWSInstance.DoesNotExist:
                        instance = CoreAWSInstance.objects.create(
                            owner=self,
                            unique_id=instance_id,
                            name=name,
                            monitoring=CoreAWSInstance.Monitoring.ACTIVE,
                            type=CoreAWSInstance.Type.SERVER,
                            metadata=instance_data
                        )
                    current_instance_ids.append(instance_id)

        CoreAWSInstance.objects.filter(owner=self).exclude(unique_id__in=current_instance_ids).update(
            monitoring=CoreAWSInstance.Monitoring.NO_LONGER_EXISTS
        )

    def sync_volumes(self):
        ec2 = self._get_aws_client()
        paginator = ec2.get_paginator('describe_volumes')
        current_volume_ids = []

        for page in paginator.paginate():
            for volume_data in page['Volumes']:
                volume_data = _serialize_datetime(volume_data)

                volume_id = volume_data['VolumeId']
                name = next(
                    (tag['Value'] for tag in volume_data.get('Tags', []) if tag['Key'] == 'Name'),
                    volume_id
                )

                try:
                    volume = CoreAWSVolume.objects.get(
                        owner=self,
                        unique_id=volume_id
                    )
                    volume.name = name
                    volume.type = CoreAWSVolume.Type.VOLUME
                    volume.metadata = volume_data
                    volume.save()
                except CoreAWSVolume.DoesNotExist:
                    volume = CoreAWSVolume.objects.create(
                        owner=self,
                        unique_id=volume_id,
                        name=name,
                        monitoring=CoreAWSVolume.Monitoring.ACTIVE,
                        type=CoreAWSVolume.Type.VOLUME,
                        metadata=volume_data
                    )
                current_volume_ids.append(volume_id)

        CoreAWSVolume.objects.filter(owner=self).exclude(unique_id__in=current_volume_ids).update(
            monitoring=CoreAWSVolume.Monitoring.NO_LONGER_EXISTS
        )

    def sync_rds_databases(self):
        rds = self._get_aws_client('rds')
        paginator = rds.get_paginator('describe_db_instances')
        current_database_ids = []

        for page in paginator.paginate():
            for db_data in page['DBInstances']:
                db_data = _serialize_datetime(db_data)

                db_id = db_data['DBInstanceIdentifier']
                name = db_data.get('DBName', db_id)

                try:
                    database = CoreAWSRDSDatabase.objects.get(
                        owner=self,
                        unique_id=db_id
                    )
                    database.name = name
                    database.type = CoreAWSRDSDatabase.Type.RDS_DATABASE
                    database.metadata = db_data
                    database.save()
                except CoreAWSRDSDatabase.DoesNotExist:
                    database = CoreAWSRDSDatabase.objects.create(
                        owner=self,
                        unique_id=db_id,
                        name=name,
                        monitoring=CoreAWSRDSDatabase.Monitoring.ACTIVE,
                        type=CoreAWSRDSDatabase.Type.RDS_DATABASE,
                        metadata=db_data
                    )
                current_database_ids.append(db_id)

        CoreAWSRDSDatabase.objects.filter(owner=self).exclude(unique_id__in=current_database_ids).update(
            monitoring=CoreAWSRDSDatabase.Monitoring.NO_LONGER_EXISTS
        )

    def sync_lambda_functions(self):
        lambda_client = self._get_aws_client('lambda')
        paginator = lambda_client.get_paginator('list_functions')
        current_function_names = []

        for page in paginator.paginate():
            for function_data in page['Functions']:
                function_data = _serialize_datetime(function_data)

                function_name = function_data['FunctionName']
                
                try:
                    lambda_function = CoreAWSLambda.objects.get(
                        owner=self,
                        unique_id=function_name
                    )
                    lambda_function.name = function_name
                    lambda_function.type = CoreAWSLambda.Type.LAMBDA
                    lambda_function.metadata = function_data
                    lambda_function.save()
                except CoreAWSLambda.DoesNotExist:
                    lambda_function = CoreAWSLambda.objects.create(
                        owner=self,
                        unique_id=function_name,
                        name=function_name,
                        monitoring=CoreAWSLambda.Monitoring.ACTIVE,
                        type=CoreAWSLambda.Type.LAMBDA,
                        metadata=function_data
                    )
                current_function_names.append(function_name)

        CoreAWSLambda.objects.filter(owner=self).exclude(unique_id__in=current_function_names).update(
            monitoring=CoreAWSLambda.Monitoring.NO_LONGER_EXISTS
        )

    def sync_dynamodb_tables(self):
        dynamodb = self._get_aws_client('dynamodb')
        paginator = dynamodb.get_paginator('list_tables')
        current_table_names = []

        for page in paginator.paginate():
            for table_name in page['TableNames']:
                # The list response is authoritative for existence even when
                # a detail call for this table fails.
                current_table_names.append(table_name)
                try:
                    table_response = dynamodb.describe_table(TableName=table_name)
                    table_data = _serialize_datetime(table_response['Table'])

                    try:
                        dynamodb_table = CoreAWSDynamoDB.objects.get(
                            owner=self,
                            unique_id=table_name,
                        )
                        dynamodb_table.name = table_name
                        dynamodb_table.type = CoreAWSDynamoDB.Type.DYNAMODB
                        dynamodb_table.metadata = table_data
                        dynamodb_table.save()
                    except CoreAWSDynamoDB.DoesNotExist:
                        CoreAWSDynamoDB.objects.create(
                            owner=self,
                            unique_id=table_name,
                            name=table_name,
                            monitoring=CoreAWSDynamoDB.Monitoring.ACTIVE,
                            type=CoreAWSDynamoDB.Type.DYNAMODB,
                            metadata=table_data,
                        )
                except Exception as e:
                    print(f"Error syncing DynamoDB table {table_name}: {str(e)}")
                    continue

        CoreAWSDynamoDB.objects.filter(owner=self).exclude(unique_id__in=current_table_names).update(
            monitoring=CoreAWSDynamoDB.Monitoring.NO_LONGER_EXISTS
        )

    def sync_s3_buckets(self):
        s3 = self._get_aws_client('s3')
        current_bucket_names = []

        try:
            # List all buckets
            response = s3.list_buckets()
            
            for bucket_info in response['Buckets']:
                bucket_name = bucket_info['Name']
                # A bucket returned by list_buckets exists even if one of its
                # optional detail calls is unavailable.
                current_bucket_names.append(bucket_name)
                
                try:
                    # Get detailed bucket information
                    bucket_data = {
                        'Name': bucket_name,
                        'CreationDate': bucket_info['CreationDate']
                    }
                    
                    # Get bucket location
                    try:
                        location_response = s3.get_bucket_location(Bucket=bucket_name)
                        bucket_data['LocationConstraint'] = location_response.get('LocationConstraint', 'us-east-1')
                    except Exception:
                        bucket_data['LocationConstraint'] = 'unknown'
                    
                    # Get versioning configuration
                    try:
                        versioning_response = s3.get_bucket_versioning(Bucket=bucket_name)
                        bucket_data['Versioning'] = versioning_response
                    except Exception:
                        bucket_data['Versioning'] = {}
                    
                    # Get encryption configuration
                    try:
                        encryption_response = s3.get_bucket_encryption(Bucket=bucket_name)
                        bucket_data['Encryption'] = encryption_response
                    except Exception:
                        bucket_data['Encryption'] = {}
                    
                    # Get public access block
                    try:
                        public_access_response = s3.get_public_access_block(Bucket=bucket_name)
                        bucket_data['PublicAccessBlock'] = public_access_response
                    except Exception:
                        bucket_data['PublicAccessBlock'] = {}
                    
                    # Serialize datetime objects
                    bucket_data = _serialize_datetime(bucket_data)
                    
                    try:
                        s3_bucket = CoreAWSS3Bucket.objects.get(
                            owner=self,
                            unique_id=bucket_name
                        )
                        s3_bucket.name = bucket_name
                        s3_bucket.type = CoreAWSS3Bucket.Type.S3_BUCKET
                        s3_bucket.metadata = bucket_data
                        s3_bucket.save()
                    except CoreAWSS3Bucket.DoesNotExist:
                        s3_bucket = CoreAWSS3Bucket.objects.create(
                            owner=self,
                            unique_id=bucket_name,
                            name=bucket_name,
                            monitoring=CoreAWSS3Bucket.Monitoring.ACTIVE,
                            type=CoreAWSS3Bucket.Type.S3_BUCKET,
                            metadata=bucket_data
                        )
                except Exception as e:
                    print(f"Error syncing S3 bucket {bucket_name}: {str(e)}")
                    continue

        except Exception as e:
            print(f"Error listing S3 buckets: {str(e)}")
            raise

        CoreAWSS3Bucket.objects.filter(owner=self).exclude(unique_id__in=current_bucket_names).update(
            monitoring=CoreAWSS3Bucket.Monitoring.NO_LONGER_EXISTS
        )

    def sync_acm_certificates(self):
        acm = self._get_aws_client('acm')
        current_certificate_arns = []

        try:
            # List all certificates
            paginator = acm.get_paginator('list_certificates')
            
            for page in paginator.paginate():
                for cert_summary in page['CertificateSummaryList']:
                    cert_arn = cert_summary['CertificateArn']
                    current_certificate_arns.append(cert_arn)
                    
                    try:
                        # Get detailed certificate information
                        cert_response = acm.describe_certificate(CertificateArn=cert_arn)
                        cert_data = cert_response['Certificate']
                        cert_data = _serialize_datetime(cert_data)
                        
                        # Use domain name as display name, fallback to ARN
                        cert_name = cert_data.get('DomainName', cert_arn.split('/')[-1])
                        
                        try:
                            acm_certificate = CoreAWSACMCertificate.objects.get(
                                owner=self,
                                unique_id=cert_arn
                            )
                            acm_certificate.name = cert_name
                            acm_certificate.type = CoreAWSACMCertificate.Type.ACM_CERTIFICATE
                            acm_certificate.metadata = cert_data
                            acm_certificate.save()
                        except CoreAWSACMCertificate.DoesNotExist:
                            acm_certificate = CoreAWSACMCertificate.objects.create(
                                owner=self,
                                unique_id=cert_arn,
                                name=cert_name,
                                monitoring=CoreAWSACMCertificate.Monitoring.ACTIVE,
                                type=CoreAWSACMCertificate.Type.ACM_CERTIFICATE,
                                metadata=cert_data
                            )
                    except Exception as e:
                        print(f"Error syncing ACM certificate {cert_arn}: {str(e)}")
                        continue

        except Exception as e:
            print(f"Error listing ACM certificates: {str(e)}")
            raise

        CoreAWSACMCertificate.objects.filter(owner=self).exclude(unique_id__in=current_certificate_arns).update(
            monitoring=CoreAWSACMCertificate.Monitoring.NO_LONGER_EXISTS
        )

    def sync_snapshots(self):
        ec2 = self._get_aws_client('ec2')
        current_snapshot_ids = []

        try:
            # List all snapshots owned by this account
            paginator = ec2.get_paginator('describe_snapshots')
            
            for page in paginator.paginate(OwnerIds=['self']):
                for snapshot_data in page['Snapshots']:
                    snapshot_id = snapshot_data['SnapshotId']
                    current_snapshot_ids.append(snapshot_id)
                    
                    try:
                        # Serialize datetime objects
                        snapshot_data = _serialize_datetime(snapshot_data)
                        
                        # Use description as name if available, otherwise use snapshot ID
                        snapshot_name = snapshot_data.get('Description', snapshot_id)
                        if not snapshot_name or snapshot_name.strip() == '':
                            snapshot_name = snapshot_id
                        
                        try:
                            snapshot = CoreAWSSnapshot.objects.get(
                                owner=self,
                                unique_id=snapshot_id
                            )
                            snapshot.name = snapshot_name
                            snapshot.type = CoreAWSSnapshot.Type.SNAPSHOT
                            snapshot.metadata = snapshot_data
                            snapshot.save()
                        except CoreAWSSnapshot.DoesNotExist:
                            snapshot = CoreAWSSnapshot.objects.create(
                                owner=self,
                                unique_id=snapshot_id,
                                name=snapshot_name,
                                monitoring=CoreAWSSnapshot.Monitoring.ACTIVE,
                                type=CoreAWSSnapshot.Type.SNAPSHOT,
                                metadata=snapshot_data
                            )
                    except Exception as e:
                        print(f"Error syncing snapshot {snapshot_id}: {str(e)}")
                        continue

        except Exception as e:
            print(f"Error listing snapshots: {str(e)}")
            raise

        CoreAWSSnapshot.objects.filter(owner=self).exclude(unique_id__in=current_snapshot_ids).update(
            monitoring=CoreAWSSnapshot.Monitoring.NO_LONGER_EXISTS
        )

    def sync_elastic_ips(self):
        ec2 = self._get_aws_client('ec2')
        current_allocation_ids = []

        try:
            # Describe all Elastic IP addresses
            response = ec2.describe_addresses()
            
            for eip_data in response['Addresses']:
                # Use AllocationId for VPC EIPs, or AssociationId/PublicIp for EC2-Classic
                allocation_id = eip_data.get('AllocationId')
                public_ip = eip_data.get('PublicIp')
                
                # For VPC EIPs, use AllocationId as unique_id, for EC2-Classic use PublicIp
                unique_id = allocation_id or public_ip
                # The list response is authoritative for existence even if
                # metadata normalization or persistence fails.
                current_allocation_ids.append(unique_id)
                
                try:
                    # Serialize datetime objects if any
                    eip_data = _serialize_datetime(eip_data)
                    
                    # Use public IP as name for display
                    eip_name = public_ip
                    
                    try:
                        elastic_ip = CoreAWSElasticIP.objects.get(
                            owner=self,
                            unique_id=unique_id
                        )
                        elastic_ip.name = eip_name
                        elastic_ip.type = CoreAWSElasticIP.Type.ELASTIC_IP
                        elastic_ip.metadata = eip_data
                        elastic_ip.save()
                    except CoreAWSElasticIP.DoesNotExist:
                        elastic_ip = CoreAWSElasticIP.objects.create(
                            owner=self,
                            unique_id=unique_id,
                            name=eip_name,
                            monitoring=CoreAWSElasticIP.Monitoring.ACTIVE,
                            type=CoreAWSElasticIP.Type.ELASTIC_IP,
                            metadata=eip_data
                        )
                except Exception as e:
                    print(f"Error syncing Elastic IP {unique_id}: {str(e)}")
                    continue

        except Exception as e:
            print(f"Error listing Elastic IPs: {str(e)}")
            raise

        CoreAWSElasticIP.objects.filter(owner=self).exclude(unique_id__in=current_allocation_ids).update(
            monitoring=CoreAWSElasticIP.Monitoring.NO_LONGER_EXISTS
        )

    def sync_load_balancers(self):
        current_lb_arns = []

        try:
            # Sync ELBv2 Load Balancers (ALB, NLB, GWLB)
            elbv2 = self._get_aws_client('elbv2')
            
            paginator = elbv2.get_paginator('describe_load_balancers')
            for page in paginator.paginate():
                for lb_data in page['LoadBalancers']:
                    lb_arn = lb_data['LoadBalancerArn']
                    current_lb_arns.append(lb_arn)
                    
                    try:
                        # Serialize datetime objects
                        lb_data = _serialize_datetime(lb_data)
                        
                        # Use load balancer name as display name
                        lb_name = lb_data.get('LoadBalancerName', lb_arn.split('/')[-1])
                        
                        # Get additional details like listeners and target groups
                        try:
                            listeners_response = elbv2.describe_listeners(LoadBalancerArn=lb_arn)
                            lb_data['Listeners'] = _serialize_datetime(listeners_response.get('Listeners', []))
                        except Exception:
                            lb_data['Listeners'] = []
                            
                        try:
                            target_groups_response = elbv2.describe_target_groups(LoadBalancerArn=lb_arn)
                            lb_data['TargetGroups'] = _serialize_datetime(target_groups_response.get('TargetGroups', []))
                        except Exception:
                            lb_data['TargetGroups'] = []
                        
                        try:
                            load_balancer = CoreAWSLoadBalancer.objects.get(
                                owner=self,
                                unique_id=lb_arn
                            )
                            load_balancer.name = lb_name
                            load_balancer.type = CoreAWSLoadBalancer.Type.LOAD_BALANCER
                            load_balancer.metadata = lb_data
                            load_balancer.save()
                        except CoreAWSLoadBalancer.DoesNotExist:
                            load_balancer = CoreAWSLoadBalancer.objects.create(
                                owner=self,
                                unique_id=lb_arn,
                                name=lb_name,
                                monitoring=CoreAWSLoadBalancer.Monitoring.ACTIVE,
                                type=CoreAWSLoadBalancer.Type.LOAD_BALANCER,
                                metadata=lb_data
                            )
                    except Exception as e:
                        print(f"Error syncing Load Balancer {lb_arn}: {str(e)}")
                        continue

            # Sync Classic Load Balancers (ELB)
            elb = self._get_aws_client('elb')
            
            # Get account ID for Classic Load Balancer ARN generation
            sts = self._get_aws_client('sts')
            try:
                account_id = sts.get_caller_identity()['Account']
            except Exception as e:
                print(f"Failed to get account ID: {str(e)}")
                raise
            
            try:
                paginator = elb.get_paginator('describe_load_balancers')
                for page in paginator.paginate():
                    for lb_data in page['LoadBalancerDescriptions']:
                        lb_name = lb_data['LoadBalancerName']
                        # Create a proper ARN for Classic Load Balancers using account ID
                        lb_arn = f"arn:aws:elasticloadbalancing:{self.region}:{account_id}:loadbalancer/{lb_name}"
                        current_lb_arns.append(lb_arn)
                        
                        try:
                            # Serialize datetime objects
                            lb_data = _serialize_datetime(lb_data)
                            
                            # Mark as Classic Load Balancer
                            lb_data['Type'] = 'classic'
                            
                            # Get additional details for Classic LB
                            try:
                                health_response = elb.describe_instance_health(LoadBalancerName=lb_name)
                                lb_data['InstanceStates'] = _serialize_datetime(health_response.get('InstanceStates', []))
                            except Exception:
                                lb_data['InstanceStates'] = []
                            
                            try:
                                load_balancer = CoreAWSLoadBalancer.objects.get(
                                    owner=self,
                                    unique_id=lb_arn
                                )
                                load_balancer.name = lb_name
                                load_balancer.type = CoreAWSLoadBalancer.Type.LOAD_BALANCER
                                load_balancer.metadata = lb_data
                                load_balancer.save()
                            except CoreAWSLoadBalancer.DoesNotExist:
                                load_balancer = CoreAWSLoadBalancer.objects.create(
                                    owner=self,
                                    unique_id=lb_arn,
                                    name=lb_name,
                                    monitoring=CoreAWSLoadBalancer.Monitoring.ACTIVE,
                                    type=CoreAWSLoadBalancer.Type.LOAD_BALANCER,
                                    metadata=lb_data
                                )
                        except Exception as e:
                            print(f"Error syncing Classic Load Balancer {lb_name}: {str(e)}")
                            continue
                            
            except Exception as e:
                print(f"Error listing Classic Load Balancers: {str(e)}")
                raise

        except Exception as e:
            print(f"Error listing Load Balancers: {str(e)}")
            raise

        CoreAWSLoadBalancer.objects.filter(owner=self).exclude(unique_id__in=current_lb_arns).update(
            monitoring=CoreAWSLoadBalancer.Monitoring.NO_LONGER_EXISTS
        )

    def sync_security_groups(self):
        ec2 = self._get_aws_client('ec2')
        current_sg_ids = []

        try:
            # Describe all security groups
            paginator = ec2.get_paginator('describe_security_groups')
            
            for page in paginator.paginate():
                for sg_data in page['SecurityGroups']:
                    sg_id = sg_data['GroupId']
                    current_sg_ids.append(sg_id)
                    
                    try:
                        # Serialize datetime objects
                        sg_data = _serialize_datetime(sg_data)
                        
                        # Use group name if available, otherwise use group ID
                        sg_name = sg_data.get('GroupName', sg_id)
                        
                        try:
                            security_group = CoreAWSSecurityGroup.objects.get(
                                owner=self,
                                unique_id=sg_id
                            )
                            security_group.name = sg_name
                            security_group.type = CoreAWSSecurityGroup.Type.SECURITY_GROUP
                            security_group.metadata = sg_data
                            security_group.save()
                        except CoreAWSSecurityGroup.DoesNotExist:
                            security_group = CoreAWSSecurityGroup.objects.create(
                                owner=self,
                                unique_id=sg_id,
                                name=sg_name,
                                monitoring=CoreAWSSecurityGroup.Monitoring.ACTIVE,
                                type=CoreAWSSecurityGroup.Type.SECURITY_GROUP,
                                metadata=sg_data
                            )
                    except Exception as e:
                        print(f"Error syncing Security Group {sg_id}: {str(e)}")
                        continue

        except Exception as e:
            print(f"Error listing Security Groups: {str(e)}")
            raise

        CoreAWSSecurityGroup.objects.filter(owner=self).exclude(unique_id__in=current_sg_ids).update(
            monitoring=CoreAWSSecurityGroup.Monitoring.NO_LONGER_EXISTS
        )

    def sync_ecs_services(self):
        ecs = self._get_aws_client('ecs')
        current_service_arns = []

        try:
            # First, get all clusters
            clusters_response = ecs.list_clusters()
            cluster_arns = _required_list(clusters_response, 'clusterArns', 'cluster list')
            
            for cluster_arn in cluster_arns:
                try:
                    # List services in this cluster
                    paginator = ecs.get_paginator('list_services')
                    
                    for page in paginator.paginate(cluster=cluster_arn):
                        service_arns = _required_list(page, 'serviceArns', 'service list')
                        # list_services is authoritative for existence even if
                        # a subsequent describe_services call is incomplete.
                        current_service_arns.extend(service_arns)
                        
                        if service_arns:
                            # Describe services in batches (max 10 per call)
                            for i in range(0, len(service_arns), 10):
                                batch_arns = service_arns[i:i+10]
                                services_response = ecs.describe_services(
                                    cluster=cluster_arn,
                                    services=batch_arns
                                )
                                
                                for service_data in _required_list(
                                    services_response,
                                    'services',
                                    'service detail',
                                ):
                                    service_arn = service_data['serviceArn']
                                    
                                    try:
                                        # Serialize datetime objects
                                        service_data = _serialize_datetime(service_data)
                                        
                                        # Use service name as display name
                                        service_name = service_data.get('serviceName', service_arn.split('/')[-1])
                                        
                                        try:
                                            ecs_service = CoreAWSECSService.objects.get(
                                                owner=self,
                                                unique_id=service_arn
                                            )
                                            ecs_service.name = service_name
                                            ecs_service.type = CoreAWSECSService.Type.ECS_SERVICE
                                            ecs_service.metadata = service_data
                                            ecs_service.save()
                                        except CoreAWSECSService.DoesNotExist:
                                            ecs_service = CoreAWSECSService.objects.create(
                                                owner=self,
                                                unique_id=service_arn,
                                                name=service_name,
                                                monitoring=CoreAWSECSService.Monitoring.ACTIVE,
                                                type=CoreAWSECSService.Type.ECS_SERVICE,
                                                metadata=service_data
                                            )
                                    except Exception as e:
                                        print(f"Error syncing ECS Service {service_arn}: {str(e)}")
                                        continue
                                        
                except Exception as e:
                    print(f"Error processing cluster {cluster_arn}: {str(e)}")
                    raise

        except Exception as e:
            print(f"Error listing ECS Services: {str(e)}")
            raise

        CoreAWSECSService.objects.filter(owner=self).exclude(unique_id__in=current_service_arns).update(
            monitoring=CoreAWSECSService.Monitoring.NO_LONGER_EXISTS
        )

    def sync_ecs_tasks(self):
        ecs = self._get_aws_client('ecs')
        current_task_arns = []

        try:
            # First, get all clusters
            clusters_response = ecs.list_clusters()
            cluster_arns = _required_list(clusters_response, 'clusterArns', 'cluster list')
            
            for cluster_arn in cluster_arns:
                try:
                    # List tasks in this cluster
                    paginator = ecs.get_paginator('list_tasks')
                    
                    for page in paginator.paginate(cluster=cluster_arn):
                        task_arns = _required_list(page, 'taskArns', 'task list')
                        # list_tasks is authoritative for existence even if a
                        # subsequent describe_tasks call is incomplete.
                        current_task_arns.extend(task_arns)
                        
                        if task_arns:
                            # Describe tasks in batches (max 100 per call)
                            for i in range(0, len(task_arns), 100):
                                batch_arns = task_arns[i:i+100]
                                tasks_response = ecs.describe_tasks(
                                    cluster=cluster_arn,
                                    tasks=batch_arns
                                )
                                
                                for task_data in _required_list(
                                    tasks_response,
                                    'tasks',
                                    'task detail',
                                ):
                                    task_arn = task_data['taskArn']
                                    
                                    try:
                                        # Serialize datetime objects
                                        task_data = _serialize_datetime(task_data)
                                        
                                        # Use task definition family + revision as display name
                                        task_def_arn = task_data.get('taskDefinitionArn', '')
                                        task_name = task_def_arn.split('/')[-1] if task_def_arn else task_arn.split('/')[-1]
                                        
                                        try:
                                            ecs_task = CoreAWSECSTask.objects.get(
                                                owner=self,
                                                unique_id=task_arn
                                            )
                                            ecs_task.name = task_name
                                            ecs_task.type = CoreAWSECSTask.Type.ECS_TASK
                                            ecs_task.metadata = task_data
                                            ecs_task.save()
                                        except CoreAWSECSTask.DoesNotExist:
                                            ecs_task = CoreAWSECSTask.objects.create(
                                                owner=self,
                                                unique_id=task_arn,
                                                name=task_name,
                                                monitoring=CoreAWSECSTask.Monitoring.ACTIVE,
                                                type=CoreAWSECSTask.Type.ECS_TASK,
                                                metadata=task_data
                                            )
                                    except Exception as e:
                                        print(f"Error syncing ECS Task {task_arn}: {str(e)}")
                                        continue
                                        
                except Exception as e:
                    print(f"Error processing cluster {cluster_arn}: {str(e)}")
                    raise

        except Exception as e:
            print(f"Error listing ECS Tasks: {str(e)}")
            raise

        CoreAWSECSTask.objects.filter(owner=self).exclude(unique_id__in=current_task_arns).update(
            monitoring=CoreAWSECSTask.Monitoring.NO_LONGER_EXISTS
        )

class CoreAWSInstance(UtilAsset):
    owner = models.ForeignKey(CoreAWSAccount, on_delete=models.CASCADE, related_name='servers')

    class Meta:
        db_table = "core_aws_instance"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        return f"https://{self.owner.region}.console.aws.amazon.com/ec2/home?region={self.owner.region}#InstanceDetails:instanceId={self.unique_id}"

    def check_status(self):
        try:
            ec2 = self.owner._get_aws_client()
            response = ec2.describe_instances(InstanceIds=[self.unique_id])
            instance_data = response['Reservations'][0]['Instances'][0]
            current_status = instance_data['State']['Name']
            return current_status, instance_data
        except Exception as e:
            error_status = 'not_found' if 'InvalidInstanceID.NotFound' in str(e) else 'error'
            return error_status, str(e)

class CoreAWSVolume(UtilAsset):
    owner = models.ForeignKey(CoreAWSAccount, on_delete=models.CASCADE, related_name='volumes')

    class Meta:
        db_table = "core_aws_volume"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        return f"https://{self.owner.region}.console.aws.amazon.com/ec2/home?region={self.owner.region}#VolumeDetails:volumeId={self.unique_id}"

    def check_status(self):
        try:
            ec2 = self.owner._get_aws_client()
            response = ec2.describe_volumes(VolumeIds=[self.unique_id])
            volume_data = response['Volumes'][0]
            current_status = volume_data['State']
            return current_status, volume_data
        except Exception as e:
            error_status = 'not_found' if 'InvalidVolume.NotFound' in str(e) else 'error'
            return error_status, str(e)

class CoreAWSRDSDatabase(UtilAsset):
    owner = models.ForeignKey(CoreAWSAccount, on_delete=models.CASCADE, related_name='rds_databases')

    class Meta:
        db_table = "core_aws_rds_database"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        return f"https://{self.owner.region}.console.aws.amazon.com/rds/home?region={self.owner.region}#database:id={self.unique_id};is-cluster=false"

    def check_status(self):
        try:
            rds = self.owner._get_aws_client('rds')
            response = rds.describe_db_instances(DBInstanceIdentifier=self.unique_id)
            db_data = response['DBInstances'][0]
            current_status = db_data['DBInstanceStatus']
            return current_status, db_data
        except Exception as e:
            error_status = 'not_found' if 'DBInstanceNotFound' in str(e) else 'error'
            return error_status, str(e)


class CoreAWSLambda(UtilAsset):
    owner = models.ForeignKey(CoreAWSAccount, on_delete=models.CASCADE, related_name='lambda_functions')

    class Meta:
        db_table = "core_aws_lambda"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        return f"https://{self.owner.region}.console.aws.amazon.com/lambda/home?region={self.owner.region}#/functions/{self.unique_id}"

    def check_status(self):
        try:
            lambda_client = self.owner._get_aws_client('lambda')
            response = lambda_client.get_function(FunctionName=self.unique_id)
            function_data = response['Configuration']
            current_status = function_data['State']
            return current_status, function_data
        except Exception as e:
            error_status = 'not_found' if 'ResourceNotFoundException' in str(e) else 'error'
            return error_status, str(e)


class CoreAWSDynamoDB(UtilAsset):
    owner = models.ForeignKey(CoreAWSAccount, on_delete=models.CASCADE, related_name='dynamodb_tables')

    class Meta:
        db_table = "core_aws_dynamodb"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        return f"https://{self.owner.region}.console.aws.amazon.com/dynamodbv2/home?region={self.owner.region}#table?name={self.unique_id}"

    def check_status(self):
        try:
            dynamodb = self.owner._get_aws_client('dynamodb')
            response = dynamodb.describe_table(TableName=self.unique_id)
            table_data = response['Table']
            current_status = table_data['TableStatus']
            return current_status, table_data
        except Exception as e:
            error_status = 'not_found' if 'ResourceNotFoundException' in str(e) else 'error'
            return error_status, str(e)


class CoreAWSS3Bucket(UtilAsset):
    owner = models.ForeignKey(CoreAWSAccount, on_delete=models.CASCADE, related_name='s3_buckets')

    class Meta:
        db_table = "core_aws_s3_bucket"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        return f"https://s3.console.aws.amazon.com/s3/buckets/{self.unique_id}?region={self.owner.region}"

    def check_status(self):
        try:
            s3 = self.owner._get_aws_client('s3')
            # Check if bucket exists by getting bucket location
            response = s3.get_bucket_location(Bucket=self.unique_id)
            current_status = 'available'  # S3 buckets don't have complex statuses like other services
            
            # Get additional bucket details for comprehensive monitoring
            bucket_data = {
                'Name': self.unique_id,
                'LocationConstraint': response.get('LocationConstraint', 'us-east-1')
            }
            
            # Get versioning, encryption, and public access block settings
            try:
                versioning = s3.get_bucket_versioning(Bucket=self.unique_id)
                bucket_data['Versioning'] = versioning
            except Exception:
                bucket_data['Versioning'] = {}
                
            try:
                encryption = s3.get_bucket_encryption(Bucket=self.unique_id)
                bucket_data['Encryption'] = encryption
            except Exception:
                bucket_data['Encryption'] = {}
                
            try:
                public_access = s3.get_public_access_block(Bucket=self.unique_id)
                bucket_data['PublicAccessBlock'] = public_access
            except Exception:
                bucket_data['PublicAccessBlock'] = {}
            
            return current_status, bucket_data
        except Exception as e:
            error_status = 'not_found' if 'NoSuchBucket' in str(e) else 'error'
            return error_status, str(e)


class CoreAWSACMCertificate(UtilAsset):
    owner = models.ForeignKey(CoreAWSAccount, on_delete=models.CASCADE, related_name='acm_certificates')

    class Meta:
        db_table = "core_aws_acm_certificate"

    def __str__(self):
        return self.name

    @property
    def monitoring_credentials(self):
        metadata = self.metadata if isinstance(self.metadata, dict) else {}
        region = metadata.get('_cloudmoo_region') or self.owner.region
        return {
            'access_key': self.owner.access_key,
            'secret_key': self.owner.secret_key,
            'region': region,
            'resource_region': region,
            'asset_type': self.type or UtilAsset.Type.ACM_CERTIFICATE,
            'metadata': metadata,
        }

    @property
    def provider_url(self):
        region = self.monitoring_credentials['resource_region']
        return f"https://{region}.console.aws.amazon.com/acm/home?region={region}#/certificates/{self.unique_id.split('/')[-1]}"

    def check_status(self):
        try:
            from apps.monitoring.checks.aws import check_aws_acm_certificate_status

            return check_aws_acm_certificate_status(
                self.unique_id,
                self.monitoring_credentials,
            )
        except Exception as e:
            error_status = 'not_found' if 'ResourceNotFoundException' in str(e) else 'error'
            return error_status, str(e)


class CoreAWSSnapshot(UtilAsset):
    owner = models.ForeignKey(CoreAWSAccount, on_delete=models.CASCADE, related_name='snapshots')

    class Meta:
        db_table = "core_aws_snapshot"

    def __str__(self):
        return self.name

    @property
    def monitoring_credentials(self):
        metadata = self.metadata if isinstance(self.metadata, dict) else {}
        region = metadata.get('_cloudmoo_region') or self.owner.region
        provider_id = (
            metadata.get('_cloudmoo_provider_id')
            or metadata.get('_cloudmoo_raw_id')
            or self.unique_id
        )
        return {
            'access_key': self.owner.access_key,
            'secret_key': self.owner.secret_key,
            'region': region,
            'resource_region': region,
            'provider_id': provider_id,
            'resource_name': provider_id,
            'snapshot_kind': metadata.get('_cloudmoo_snapshot_kind'),
            'asset_type': self.type or UtilAsset.Type.SNAPSHOT,
            'metadata': metadata,
        }

    @property
    def provider_url(self):
        region = self.monitoring_credentials['resource_region']
        snapshot_id = self.monitoring_credentials['provider_id']
        return f"https://{region}.console.aws.amazon.com/ec2/home?region={region}#Snapshots:snapshotId={snapshot_id}"

    def check_status(self):
        try:
            from apps.monitoring.checks.aws_backup import (
                check_aws_rds_snapshot_status,
                check_aws_snapshot_status,
            )

            credentials = self.monitoring_credentials
            snapshot_kind = credentials.get('snapshot_kind')
            provider_id = credentials.get('provider_id') or self.unique_id
            check = (
                check_aws_rds_snapshot_status
                if snapshot_kind in {'rds_instance', 'rds_cluster'}
                else check_aws_snapshot_status
            )

            return check(provider_id, credentials)
        except Exception as e:
            error_status = 'not_found' if 'InvalidSnapshot.NotFound' in str(e) else 'error'
            return error_status, str(e)


class CoreAWSElasticIP(UtilAsset):
    owner = models.ForeignKey(CoreAWSAccount, on_delete=models.CASCADE, related_name='elastic_ips')

    class Meta:
        db_table = "core_aws_elastic_ip"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        return f"https://{self.owner.region}.console.aws.amazon.com/ec2/home?region={self.owner.region}#Addresses:"

    def check_status(self):
        try:
            ec2 = self.owner._get_aws_client('ec2')
            # Check if it's a VPC EIP (has AllocationId) or EC2-Classic EIP
            if self.unique_id.startswith('eipalloc-'):
                response = ec2.describe_addresses(AllocationIds=[self.unique_id])
            else:
                response = ec2.describe_addresses(PublicIps=[self.unique_id])
            
            eip_data = response['Addresses'][0]
            # EIPs don't have a traditional "status" - they're either allocated or not
            # We'll use association status: associated, disassociated
            current_status = 'associated' if eip_data.get('InstanceId') or eip_data.get('AssociationId') else 'disassociated'
            return current_status, eip_data
        except Exception as e:
            error_status = 'not_found' if ('InvalidAddress.NotFound' in str(e) or 'InvalidAllocationID.NotFound' in str(e)) else 'error'
            return error_status, str(e)


class CoreAWSLoadBalancer(UtilAsset):
    owner = models.ForeignKey(CoreAWSAccount, on_delete=models.CASCADE, related_name='load_balancers')

    class Meta:
        db_table = "core_aws_load_balancer"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        # Handle both ELBv2 and Classic Load Balancers
        if self.unique_id.startswith('arn:aws:elasticloadbalancing'):
            if 'loadbalancer/app/' in self.unique_id or 'loadbalancer/net/' in self.unique_id or 'loadbalancer/gwy/' in self.unique_id:
                # ELBv2 (ALB, NLB, GWLB)
                return f"https://{self.owner.region}.console.aws.amazon.com/ec2/home?region={self.owner.region}#LoadBalancers:search={self.name}"
            else:
                # Classic Load Balancer
                return f"https://{self.owner.region}.console.aws.amazon.com/ec2/home?region={self.owner.region}#LoadBalancers:search={self.name}"
        return f"https://{self.owner.region}.console.aws.amazon.com/ec2/home?region={self.owner.region}#LoadBalancers:"

    def check_status(self):
        try:
            # Check if it's a Classic Load Balancer or ELBv2
            if self.metadata.get('Type') == 'classic':
                # Classic Load Balancer
                elb = self.owner._get_aws_client('elb')
                response = elb.describe_load_balancers(LoadBalancerNames=[self.name])
                lb_data = response['LoadBalancerDescriptions'][0]
                # Classic LBs don't have a state field, if we can describe it, it's active
                current_status = 'active'
            else:
                # ELBv2 Load Balancer (ALB, NLB, GWLB)
                elbv2 = self.owner._get_aws_client('elbv2')
                response = elbv2.describe_load_balancers(LoadBalancerArns=[self.unique_id])
                lb_data = response['LoadBalancers'][0]
                current_status = lb_data.get('State', {}).get('Code', 'unknown')
            
            return current_status, lb_data
        except Exception as e:
            error_status = 'not_found' if ('LoadBalancerNotFound' in str(e) or 'LoadBalancerNotFound' in str(e)) else 'error'
            return error_status, str(e)


class CoreAWSSecurityGroup(UtilAsset):
    owner = models.ForeignKey(CoreAWSAccount, on_delete=models.CASCADE, related_name='security_groups')

    class Meta:
        db_table = "core_aws_security_group"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        return f"https://{self.owner.region}.console.aws.amazon.com/ec2/home?region={self.owner.region}#SecurityGroups:groupId={self.unique_id}"

    def check_status(self):
        try:
            ec2 = self.owner._get_aws_client('ec2')
            response = ec2.describe_security_groups(GroupIds=[self.unique_id])
            sg_data = response['SecurityGroups'][0]
            # Security groups don't have a traditional status - if we can describe it, it's active
            current_status = 'active'
            return current_status, sg_data
        except Exception as e:
            error_status = 'not_found' if 'InvalidGroupId.NotFound' in str(e) else 'error'
            return error_status, str(e)


class CoreAWSECSService(UtilAsset):
    owner = models.ForeignKey(CoreAWSAccount, on_delete=models.CASCADE, related_name='ecs_services')

    class Meta:
        db_table = "core_aws_ecs_service"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        cluster_name = self.metadata.get('clusterArn', '').split('/')[-1] if self.metadata.get('clusterArn') else 'default'
        return f"https://{self.owner.region}.console.aws.amazon.com/ecs/home?region={self.owner.region}#/clusters/{cluster_name}/services/{self.name}/details"

    def check_status(self):
        try:
            ecs = self.owner._get_aws_client('ecs')
            cluster_arn = self.metadata.get('clusterArn', 'default')
            response = ecs.describe_services(cluster=cluster_arn, services=[self.unique_id])
            service_data = response['services'][0]
            current_status = service_data.get('status', 'unknown')
            return current_status, service_data
        except Exception as e:
            error_status = 'not_found' if 'ServiceNotFound' in str(e) else 'error'
            return error_status, str(e)


class CoreAWSECSTask(UtilAsset):
    owner = models.ForeignKey(CoreAWSAccount, on_delete=models.CASCADE, related_name='ecs_tasks')

    class Meta:
        db_table = "core_aws_ecs_task"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        cluster_name = self.metadata.get('clusterArn', '').split('/')[-1] if self.metadata.get('clusterArn') else 'default'
        return f"https://{self.owner.region}.console.aws.amazon.com/ecs/home?region={self.owner.region}#/clusters/{cluster_name}/tasks/{self.unique_id.split('/')[-1]}/details"

    def check_status(self):
        try:
            ecs = self.owner._get_aws_client('ecs')
            cluster_arn = self.metadata.get('clusterArn', 'default')
            response = ecs.describe_tasks(cluster=cluster_arn, tasks=[self.unique_id])
            task_data = response['tasks'][0]
            current_status = task_data.get('lastStatus', 'unknown')
            return current_status, task_data
        except Exception as e:
            error_status = 'not_found' if 'Could not find task' in str(e) else 'error'
            return error_status, str(e)
