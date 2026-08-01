import boto3
from botocore.config import Config

from apps.monitoring.checks.base import (
    REQUEST_TIMEOUT_SECONDS,
    _serialize_datetime,
    classify_aws_error,
)


# Boto3's defaults can keep a worker occupied for minutes during a provider
# network failure. Monitoring checks need a bounded execution time so one bad
# endpoint cannot consume the entire Celery pool.
AWS_CLIENT_CONFIG = Config(
    connect_timeout=5,
    read_timeout=REQUEST_TIMEOUT_SECONDS,
    retries={'mode': 'standard', 'max_attempts': 2},
)


def check_aws_server_status(unique_id, credentials):
    """Check AWS EC2 instance status"""
    try:
        # Parse credentials
        access_key = credentials['access_key']
        secret_key = credentials['secret_key']
        region = credentials['region']

        # Create EC2 client
        ec2 = boto3.client(
            'ec2',
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=AWS_CLIENT_CONFIG,
        )

        # Get instance details
        response = ec2.describe_instances(InstanceIds=[unique_id])
        instance_data = response['Reservations'][0]['Instances'][0]
        current_status = instance_data['State']['Name']

        return current_status, {
            'instance': _serialize_datetime(instance_data)
        }
    except Exception as e:
        return classify_aws_error(e), str(e)


def check_aws_volume_status(unique_id, credentials):
    """Check AWS EBS volume status"""
    try:
        # Parse credentials
        access_key = credentials['access_key']
        secret_key = credentials['secret_key']
        region = credentials['region']

        # Create EC2 client
        ec2 = boto3.client(
            'ec2',
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=AWS_CLIENT_CONFIG,
        )

        # Get volume details
        response = ec2.describe_volumes(VolumeIds=[unique_id])
        volume_data = response['Volumes'][0]
        current_status = volume_data['State']

        return current_status, {
            'volume': _serialize_datetime(volume_data)
        }
    except Exception as e:
        return classify_aws_error(e), str(e)


def check_aws_rds_database_status(unique_id, credentials):
    """Check AWS RDS database status"""
    try:
        # Parse credentials
        access_key = credentials['access_key']
        secret_key = credentials['secret_key']
        region = credentials['region']

        # Create RDS client
        rds = boto3.client(
            'rds',
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=AWS_CLIENT_CONFIG,
        )

        # Get database details
        response = rds.describe_db_instances(DBInstanceIdentifier=unique_id)
        db_data = response['DBInstances'][0]
        current_status = db_data['DBInstanceStatus']

        return current_status, {
            'database': _serialize_datetime(db_data)
        }
    except Exception as e:
        return classify_aws_error(e), str(e)


def check_aws_lambda_status(unique_id, credentials):
    """Check AWS Lambda function status"""
    try:
        # Parse credentials
        access_key = credentials['access_key']
        secret_key = credentials['secret_key']
        region = credentials['region']

        # Create Lambda client
        lambda_client = boto3.client(
            'lambda',
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=AWS_CLIENT_CONFIG,
        )

        # Get function details
        response = lambda_client.get_function(FunctionName=unique_id)
        function_data = response['Configuration']
        current_status = function_data['State']

        return current_status, {
            'function': _serialize_datetime(function_data)
        }
    except Exception as e:
        return classify_aws_error(e), str(e)


def check_aws_dynamodb_status(unique_id, credentials):
    """Check AWS DynamoDB table status"""
    try:
        # Parse credentials
        access_key = credentials['access_key']
        secret_key = credentials['secret_key']
        region = credentials['region']

        # Create DynamoDB client
        dynamodb = boto3.client(
            'dynamodb',
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=AWS_CLIENT_CONFIG,
        )

        # Get table details
        response = dynamodb.describe_table(TableName=unique_id)
        table_data = response['Table']
        current_status = table_data['TableStatus']

        return current_status, {
            'table': _serialize_datetime(table_data)
        }
    except Exception as e:
        return classify_aws_error(e), str(e)


def check_aws_s3_bucket_status(unique_id, credentials):
    """Check AWS S3 bucket status"""
    try:
        # Parse credentials
        access_key = credentials['access_key']
        secret_key = credentials['secret_key']
        region = credentials['region']

        # Create S3 client
        s3 = boto3.client(
            's3',
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=AWS_CLIENT_CONFIG,
        )

        # Check if bucket exists and get configuration
        bucket_data = {
            'Name': unique_id
        }

        # Get bucket location
        try:
            location_response = s3.get_bucket_location(Bucket=unique_id)
            bucket_data['LocationConstraint'] = location_response.get('LocationConstraint', 'us-east-1')
        except Exception:
            bucket_data['LocationConstraint'] = 'unknown'

        # Get versioning configuration
        try:
            versioning_response = s3.get_bucket_versioning(Bucket=unique_id)
            bucket_data['Versioning'] = versioning_response
        except Exception:
            bucket_data['Versioning'] = {}

        # Get encryption configuration
        try:
            encryption_response = s3.get_bucket_encryption(Bucket=unique_id)
            bucket_data['Encryption'] = encryption_response
        except Exception:
            bucket_data['Encryption'] = {}

        # Get public access block
        try:
            public_access_response = s3.get_public_access_block(Bucket=unique_id)
            bucket_data['PublicAccessBlock'] = public_access_response
        except Exception:
            bucket_data['PublicAccessBlock'] = {}

        # S3 buckets don't have complex statuses, if we can access it, it's available
        current_status = 'available'

        return current_status, {
            'bucket': _serialize_datetime(bucket_data)
        }
    except Exception as e:
        return classify_aws_error(e), str(e)


def check_aws_acm_certificate_status(unique_id, credentials):
    """Check AWS ACM certificate status"""
    try:
        # Parse credentials
        access_key = credentials['access_key']
        secret_key = credentials['secret_key']
        region = credentials['region']

        # Create ACM client
        acm = boto3.client(
            'acm',
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=AWS_CLIENT_CONFIG,
        )

        # Get certificate details
        response = acm.describe_certificate(CertificateArn=unique_id)
        cert_data = response['Certificate']
        current_status = cert_data['Status']

        return current_status, {
            'certificate': _serialize_datetime(cert_data)
        }
    except Exception as e:
        return classify_aws_error(e), str(e)


def check_aws_snapshot_status(unique_id, credentials):
    """Check AWS EBS snapshot status"""
    try:
        # Parse credentials
        access_key = credentials['access_key']
        secret_key = credentials['secret_key']
        region = credentials['region']

        # Create EC2 client
        ec2 = boto3.client(
            'ec2',
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=AWS_CLIENT_CONFIG,
        )

        # Get snapshot details
        response = ec2.describe_snapshots(SnapshotIds=[unique_id])
        snapshot_data = response['Snapshots'][0]
        current_status = snapshot_data['State']

        return current_status, {
            'snapshot': _serialize_datetime(snapshot_data)
        }
    except Exception as e:
        return classify_aws_error(e), str(e)


def check_aws_elastic_ip_status(unique_id, credentials):
    """Check AWS Elastic IP status"""
    try:
        # Parse credentials
        access_key = credentials['access_key']
        secret_key = credentials['secret_key']
        region = credentials['region']

        # Create EC2 client
        ec2 = boto3.client(
            'ec2',
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=AWS_CLIENT_CONFIG,
        )

        # Check if it's a VPC EIP (has AllocationId) or EC2-Classic EIP
        if unique_id.startswith('eipalloc-'):
            response = ec2.describe_addresses(AllocationIds=[unique_id])
        else:
            response = ec2.describe_addresses(PublicIps=[unique_id])

        eip_data = response['Addresses'][0]
        # EIPs don't have a traditional "status" - they're either allocated or not
        # We'll use association status: associated, disassociated
        current_status = 'associated' if eip_data.get('InstanceId') or eip_data.get('AssociationId') else 'disassociated'

        return current_status, {
            'elastic_ip': _serialize_datetime(eip_data)
        }
    except Exception as e:
        return classify_aws_error(e), str(e)


def check_aws_load_balancer_status(unique_id, credentials):
    """Check AWS Load Balancer status"""
    try:
        # Parse credentials
        access_key = credentials['access_key']
        secret_key = credentials['secret_key']
        region = credentials['region']

        # Determine if it's a Classic Load Balancer or ELBv2
        if 'loadbalancer/app/' in unique_id or 'loadbalancer/net/' in unique_id or 'loadbalancer/gwy/' in unique_id:
            # ELBv2 Load Balancer (ALB, NLB, GWLB)
            elbv2 = boto3.client(
                'elbv2',
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                region_name=region,
                config=AWS_CLIENT_CONFIG,
            )

            response = elbv2.describe_load_balancers(LoadBalancerArns=[unique_id])
            lb_data = response['LoadBalancers'][0]
            current_status = lb_data.get('State', {}).get('Code', 'unknown')

            # Get additional details
            try:
                listeners_response = elbv2.describe_listeners(LoadBalancerArn=unique_id)
                lb_data['Listeners'] = listeners_response.get('Listeners', [])
            except Exception:
                lb_data['Listeners'] = []

            try:
                target_groups_response = elbv2.describe_target_groups(LoadBalancerArn=unique_id)
                lb_data['TargetGroups'] = target_groups_response.get('TargetGroups', [])
            except Exception:
                lb_data['TargetGroups'] = []
        else:
            # Classic Load Balancer - extract name from pseudo-ARN
            lb_name = unique_id.split('/')[-1]

            elb = boto3.client(
                'elb',
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                region_name=region,
                config=AWS_CLIENT_CONFIG,
            )

            response = elb.describe_load_balancers(LoadBalancerNames=[lb_name])
            lb_data = response['LoadBalancerDescriptions'][0]
            # Classic LBs don't have a state field, if we can describe it, it's active
            current_status = 'active'
            lb_data['Type'] = 'classic'

            # Get instance health
            try:
                health_response = elb.describe_instance_health(LoadBalancerName=lb_name)
                lb_data['InstanceStates'] = health_response.get('InstanceStates', [])
            except Exception:
                lb_data['InstanceStates'] = []

        return current_status, {
            'load_balancer': _serialize_datetime(lb_data)
        }
    except Exception as e:
        return classify_aws_error(e), str(e)


def check_aws_security_group_status(unique_id, credentials):
    """Check AWS Security Group status"""
    try:
        # Parse credentials
        access_key = credentials['access_key']
        secret_key = credentials['secret_key']
        region = credentials['region']

        # Create EC2 client
        ec2 = boto3.client(
            'ec2',
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=AWS_CLIENT_CONFIG,
        )

        # Get security group details
        response = ec2.describe_security_groups(GroupIds=[unique_id])
        sg_data = response['SecurityGroups'][0]
        # Security groups don't have a traditional status - if we can describe it, it's active
        current_status = 'active'

        return current_status, {
            'security_group': _serialize_datetime(sg_data)
        }
    except Exception as e:
        return classify_aws_error(e), str(e)


def check_aws_ecs_service_status(unique_id, credentials):
    """Check AWS ECS Service status"""
    try:
        # Parse credentials
        access_key = credentials['access_key']
        secret_key = credentials['secret_key']
        region = credentials['region']

        # Create ECS client
        ecs = boto3.client(
            'ecs',
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=AWS_CLIENT_CONFIG,
        )

        # Extract cluster ARN from the service ARN or use default
        # Service ARN format: arn:aws:ecs:region:account:service/cluster-name/service-name
        cluster_name = unique_id.split('/')[-2] if '/' in unique_id else 'default'

        # Get service details
        response = ecs.describe_services(cluster=cluster_name, services=[unique_id])
        service_data = response['services'][0]
        current_status = service_data.get('status', 'unknown')

        return current_status, {
            'service': _serialize_datetime(service_data)
        }
    except Exception as e:
        return classify_aws_error(e), str(e)


def check_aws_ecs_task_status(unique_id, credentials):
    """Check AWS ECS Task status"""
    try:
        # Parse credentials
        access_key = credentials['access_key']
        secret_key = credentials['secret_key']
        region = credentials['region']

        # Create ECS client
        ecs = boto3.client(
            'ecs',
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=AWS_CLIENT_CONFIG,
        )

        # Extract cluster ARN from the task ARN or use default
        # Task ARN format: arn:aws:ecs:region:account:task/cluster-name/task-id
        cluster_name = unique_id.split('/')[-2] if '/' in unique_id else 'default'

        # First try to get running tasks
        response = ecs.describe_tasks(cluster=cluster_name, tasks=[unique_id])

        # If no running tasks found, try to get stopped tasks as well
        if not response.get('tasks'):
            response = ecs.describe_tasks(cluster=cluster_name, tasks=[unique_id], include=['TAGS'])

        # If still no tasks found, try without specifying cluster (use task ARN directly)
        if not response.get('tasks'):
            try:
                response = ecs.describe_tasks(tasks=[unique_id], include=['TAGS'])
            except Exception:
                pass

        # Check if any tasks were returned
        if not response.get('tasks'):
            # Return stopped status for tasks that may have completed
            return 'stopped', {
                'task': {
                    'taskArn': unique_id,
                    'lastStatus': 'STOPPED',
                    'note': 'Task not found - likely completed and removed from cluster'
                }
            }

        task_data = response['tasks'][0]
        current_status = task_data.get('lastStatus', 'unknown').lower()

        return current_status, {
            'task': _serialize_datetime(task_data)
        }
    except Exception as e:
        # A task that has already completed may disappear from ECS before the
        # next check. Preserve the old behavior and treat that as a stopped
        # task rather than a failed API check.
        if 'Could not find task' in str(e) or 'InvalidParameterValue' in str(e):
            # Task likely completed and was removed
            return 'stopped', {
                'task': {
                    'taskArn': unique_id,
                    'lastStatus': 'STOPPED',
                    'note': 'Task completed and removed from cluster'
                }
            }
        return classify_aws_error(e), str(e)
