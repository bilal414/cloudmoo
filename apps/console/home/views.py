from django.views.generic import TemplateView
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Sum, Case, When, IntegerField
from django.db.models import Count, Q

from apps.console.cloud.aws.models import CoreAWSInstance, CoreAWSVolume, CoreAWSRDSDatabase, CoreAWSLambda, CoreAWSDynamoDB, CoreAWSS3Bucket, CoreAWSACMCertificate, CoreAWSSnapshot, CoreAWSElasticIP, CoreAWSLoadBalancer, CoreAWSSecurityGroup, CoreAWSECSService, CoreAWSECSTask
from apps.console.cloud.linode.models import CoreLinodeVolume, CoreLinodeServer
from apps.console.cloud.models import CoreCloud
from apps.console.cloud.digitalocean.models import CoreDigitalOceanServer, CoreDigitalOceanVolume, \
    CoreDigitalOceanDatabase
from apps.console.cloud.hetzner.models import CoreHetznerServer, CoreHetznerVolume
from apps.console.cloud.upcloud.models import CoreUpCloudServer, CoreUpCloudVolume
from apps.console.cloud.vultr.models import CoreVultrServer, CoreVultrVolume, CoreVultrDatabase
from apps.console.utils.models import UtilAsset


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
        cloud_asset_counts = {cloud.id: {'servers': 0, 'volumes': 0, 'databases': 0, 'lambda_functions': 0, 'dynamodb_tables': 0, 's3_buckets': 0, 'acm_certificates': 0, 'snapshots': 0, 'elastic_ips': 0, 'load_balancers': 0, 'security_groups': 0, 'ecs_services': 0, 'ecs_tasks': 0} for cloud in clouds}

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

        # Calculate totals
        total_servers = sum(cloud['servers'] for cloud in cloud_asset_counts.values())
        total_volumes = sum(cloud['volumes'] for cloud in cloud_asset_counts.values())
        total_databases = sum(cloud['databases'] for cloud in cloud_asset_counts.values())
        total_lambda_functions = sum(cloud['lambda_functions'] for cloud in cloud_asset_counts.values())
        total_dynamodb_tables = sum(cloud['dynamodb_tables'] for cloud in cloud_asset_counts.values())
        total_s3_buckets = sum(cloud['s3_buckets'] for cloud in cloud_asset_counts.values())
        total_acm_certificates = sum(cloud['acm_certificates'] for cloud in cloud_asset_counts.values())
        total_snapshots = sum(cloud['snapshots'] for cloud in cloud_asset_counts.values())
        total_elastic_ips = sum(cloud['elastic_ips'] for cloud in cloud_asset_counts.values())
        total_load_balancers = sum(cloud['load_balancers'] for cloud in cloud_asset_counts.values())
        total_security_groups = sum(cloud['security_groups'] for cloud in cloud_asset_counts.values())
        total_ecs_services = sum(cloud['ecs_services'] for cloud in cloud_asset_counts.values())
        total_ecs_tasks = sum(cloud['ecs_tasks'] for cloud in cloud_asset_counts.values())
        total_assets = total_servers + total_volumes + total_databases + total_lambda_functions + total_dynamodb_tables + total_s3_buckets + total_acm_certificates + total_snapshots + total_elastic_ips + total_load_balancers + total_security_groups + total_ecs_services + total_ecs_tasks

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
            cloud.elastic_ip_count = cloud_asset_counts[cloud.id]['elastic_ips']
            cloud.load_balancer_count = cloud_asset_counts[cloud.id]['load_balancers']
            cloud.security_group_count = cloud_asset_counts[cloud.id]['security_groups']
            cloud.ecs_service_count = cloud_asset_counts[cloud.id]['ecs_services']
            cloud.ecs_task_count = cloud_asset_counts[cloud.id]['ecs_tasks']
            cloud.total_assets = cloud.server_count + cloud.volume_count + cloud.database_count + cloud.lambda_function_count + cloud.dynamodb_table_count + cloud.s3_bucket_count + cloud.acm_certificate_count + cloud.snapshot_count + cloud.elastic_ip_count + cloud.load_balancer_count + cloud.security_group_count + cloud.ecs_service_count + cloud.ecs_task_count

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
            'elastic_ips': {'count': total_elastic_ips, 'percentage': round((total_elastic_ips / total_assets * 100) if total_assets > 0 else 0, 1)},
            'load_balancers': {'count': total_load_balancers, 'percentage': round((total_load_balancers / total_assets * 100) if total_assets > 0 else 0, 1)},
            'security_groups': {'count': total_security_groups, 'percentage': round((total_security_groups / total_assets * 100) if total_assets > 0 else 0, 1)},
            'ecs_services': {'count': total_ecs_services, 'percentage': round((total_ecs_services / total_assets * 100) if total_assets > 0 else 0, 1)},
            'ecs_tasks': {'count': total_ecs_tasks, 'percentage': round((total_ecs_tasks / total_assets * 100) if total_assets > 0 else 0, 1)},
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
            'total_elastic_ips': total_elastic_ips,
            'total_load_balancers': total_load_balancers,
            'total_security_groups': total_security_groups,
            'total_ecs_services': total_ecs_services,
            'total_ecs_tasks': total_ecs_tasks,
            'total_assets': total_assets,
            'asset_breakdown': asset_breakdown,
            'show_welcome': False
        })

        return context