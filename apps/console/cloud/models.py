import logging
import uuid
from datetime import datetime

from django.db import models
from django.utils.text import slugify
from django_celery_beat.models import PeriodicTask
from model_utils.models import TimeStampedModel

from apps.console.account.models import CoreAccount
from apps.monitoring.models import AssetStatusEmail, AssetStatusLog
from apps.monitoring.schedules import (
    asset_schedule_create,
    asset_schedule_delete,
    cloud_schedule_create,
    cloud_schedule_delete,
)

logger = logging.getLogger(__name__)



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
        super().save(*args, **kwargs)
        if is_new:
            try:
                cloud_schedule_create(self)
            except Exception as e:
                logger.warning(
                    f"Could not create asset sync schedule for cloud {self.pk}: {e}. "
                    f"Run 'python manage.py create_all_cloud_schedules --confirm' to recreate schedules."
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

    def get_all_assets(self):
        """
        Gets all assets (servers, volumes, databases) associated with this cloud.
        Returns a list of tuples containing (asset, asset_type) pairs.
        """
        assets = []
        provider_account = self.provider_account

        # Get servers
        if hasattr(provider_account, 'servers'):
            servers = provider_account.servers.all()
            assets.extend([(server, 'server') for server in servers])

        # Get volumes
        if hasattr(provider_account, 'volumes'):
            volumes = provider_account.volumes.all()
            assets.extend([(volume, 'volume') for volume in volumes])

        # Get databases
        if hasattr(provider_account, 'databases'):
            databases = provider_account.databases.all()
            assets.extend([(database, 'database') for database in databases])
        
        # Get RDS databases (AWS specific)
        if hasattr(provider_account, 'rds_databases'):
            rds_databases = provider_account.rds_databases.all()
            assets.extend([(database, 'rds_database') for database in rds_databases])
        
        # Get Lambda functions (AWS specific)
        if hasattr(provider_account, 'lambda_functions'):
            lambda_functions = provider_account.lambda_functions.all()
            assets.extend([(function, 'lambda') for function in lambda_functions])
        
        # Get DynamoDB tables (AWS specific)
        if hasattr(provider_account, 'dynamodb_tables'):
            dynamodb_tables = provider_account.dynamodb_tables.all()
            assets.extend([(table, 'dynamodb') for table in dynamodb_tables])
        
        # Get S3 buckets (AWS specific)
        if hasattr(provider_account, 's3_buckets'):
            s3_buckets = provider_account.s3_buckets.all()
            assets.extend([(bucket, 's3_bucket') for bucket in s3_buckets])
        
        # Get ACM certificates (AWS specific)
        if hasattr(provider_account, 'acm_certificates'):
            acm_certificates = provider_account.acm_certificates.all()
            assets.extend([(cert, 'acm_certificate') for cert in acm_certificates])
        
        # Get snapshots (AWS specific)
        if hasattr(provider_account, 'snapshots'):
            snapshots = provider_account.snapshots.all()
            assets.extend([(snapshot, 'snapshot') for snapshot in snapshots])
        
        # Get Elastic IPs (AWS specific)
        if hasattr(provider_account, 'elastic_ips'):
            elastic_ips = provider_account.elastic_ips.all()
            assets.extend([(eip, 'elastic_ip') for eip in elastic_ips])
        
        # Get Load Balancers (AWS specific)
        if hasattr(provider_account, 'load_balancers'):
            load_balancers = provider_account.load_balancers.all()
            assets.extend([(lb, 'load_balancer') for lb in load_balancers])
        
        # Get Security Groups (AWS specific)
        if hasattr(provider_account, 'security_groups'):
            security_groups = provider_account.security_groups.all()
            assets.extend([(sg, 'security_group') for sg in security_groups])
        
        # Get ECS Services (AWS specific)
        if hasattr(provider_account, 'ecs_services'):
            ecs_services = provider_account.ecs_services.all()
            assets.extend([(service, 'ecs_service') for service in ecs_services])
        
        # Get ECS Tasks (AWS specific)
        if hasattr(provider_account, 'ecs_tasks'):
            ecs_tasks = provider_account.ecs_tasks.all()
            assets.extend([(task, 'ecs_task') for task in ecs_tasks])

        return assets

    def get_active_assets(self):
        from apps.console.utils.models import UtilAsset

        """
        Gets all active assets (excluding NO_LONGER_EXISTS) associated with this cloud.
        Returns a list of tuples containing (asset, asset_type) pairs.
        """
        assets = []
        provider_account = self.provider_account

        # Get active servers
        if hasattr(provider_account, 'servers'):
            servers = provider_account.servers.exclude(
                monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
            )
            assets.extend([(server, 'server') for server in servers])

        # Get active volumes
        if hasattr(provider_account, 'volumes'):
            volumes = provider_account.volumes.exclude(
                monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
            )
            assets.extend([(volume, 'volume') for volume in volumes])

        # Get active databases
        if hasattr(provider_account, 'databases'):
            databases = provider_account.databases.exclude(
                monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
            )
            assets.extend([(database, 'database') for database in databases])
        
        # Get active RDS databases (AWS specific)
        if hasattr(provider_account, 'rds_databases'):
            rds_databases = provider_account.rds_databases.exclude(
                monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
            )
            assets.extend([(database, 'rds_database') for database in rds_databases])
        
        # Get active Lambda functions (AWS specific)
        if hasattr(provider_account, 'lambda_functions'):
            lambda_functions = provider_account.lambda_functions.exclude(
                monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
            )
            assets.extend([(function, 'lambda') for function in lambda_functions])
        
        # Get active DynamoDB tables (AWS specific)
        if hasattr(provider_account, 'dynamodb_tables'):
            dynamodb_tables = provider_account.dynamodb_tables.exclude(
                monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
            )
            assets.extend([(table, 'dynamodb') for table in dynamodb_tables])
        
        # Get active S3 buckets (AWS specific)
        if hasattr(provider_account, 's3_buckets'):
            s3_buckets = provider_account.s3_buckets.exclude(
                monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
            )
            assets.extend([(bucket, 's3_bucket') for bucket in s3_buckets])
        
        # Get active ACM certificates (AWS specific)
        if hasattr(provider_account, 'acm_certificates'):
            acm_certificates = provider_account.acm_certificates.exclude(
                monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
            )
            assets.extend([(cert, 'acm_certificate') for cert in acm_certificates])
        
        # Get active snapshots (AWS specific)
        if hasattr(provider_account, 'snapshots'):
            snapshots = provider_account.snapshots.exclude(
                monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
            )
            assets.extend([(snapshot, 'snapshot') for snapshot in snapshots])
        
        # Get active Elastic IPs (AWS specific)
        if hasattr(provider_account, 'elastic_ips'):
            elastic_ips = provider_account.elastic_ips.exclude(
                monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
            )
            assets.extend([(eip, 'elastic_ip') for eip in elastic_ips])
        
        # Get active Load Balancers (AWS specific)
        if hasattr(provider_account, 'load_balancers'):
            load_balancers = provider_account.load_balancers.exclude(
                monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
            )
            assets.extend([(lb, 'load_balancer') for lb in load_balancers])
        
        # Get active Security Groups (AWS specific)
        if hasattr(provider_account, 'security_groups'):
            security_groups = provider_account.security_groups.exclude(
                monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
            )
            assets.extend([(sg, 'security_group') for sg in security_groups])
        
        # Get active ECS Services (AWS specific)
        if hasattr(provider_account, 'ecs_services'):
            ecs_services = provider_account.ecs_services.exclude(
                monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
            )
            assets.extend([(service, 'ecs_service') for service in ecs_services])
        
        # Get active ECS Tasks (AWS specific)
        if hasattr(provider_account, 'ecs_tasks'):
            ecs_tasks = provider_account.ecs_tasks.exclude(
                monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
            )
            assets.extend([(task, 'ecs_task') for task in ecs_tasks])

        return assets

    def get_monitored_assets(self):
        from apps.console.utils.models import UtilAsset

        """
        Gets all actively monitored assets associated with this cloud.
        Returns a list of tuples containing (asset, asset_type) pairs.
        """
        assets = []
        provider_account = self.provider_account

        # Get monitored servers
        if hasattr(provider_account, 'servers'):
            servers = provider_account.servers.filter(
                monitoring=UtilAsset.Monitoring.ACTIVE
            )
            assets.extend([(server, 'server') for server in servers])

        # Get monitored volumes
        if hasattr(provider_account, 'volumes'):
            volumes = provider_account.volumes.filter(
                monitoring=UtilAsset.Monitoring.ACTIVE
            )
            assets.extend([(volume, 'volume') for volume in volumes])

        # Get monitored databases
        if hasattr(provider_account, 'databases'):
            databases = provider_account.databases.filter(
                monitoring=UtilAsset.Monitoring.ACTIVE
            )
            assets.extend([(database, 'database') for database in databases])
        
        # Get monitored RDS databases (AWS specific)
        if hasattr(provider_account, 'rds_databases'):
            rds_databases = provider_account.rds_databases.filter(
                monitoring=UtilAsset.Monitoring.ACTIVE
            )
            assets.extend([(database, 'rds_database') for database in rds_databases])
        
        # Get monitored Lambda functions (AWS specific)
        if hasattr(provider_account, 'lambda_functions'):
            lambda_functions = provider_account.lambda_functions.filter(
                monitoring=UtilAsset.Monitoring.ACTIVE
            )
            assets.extend([(function, 'lambda') for function in lambda_functions])
        
        # Get monitored DynamoDB tables (AWS specific)
        if hasattr(provider_account, 'dynamodb_tables'):
            dynamodb_tables = provider_account.dynamodb_tables.filter(
                monitoring=UtilAsset.Monitoring.ACTIVE
            )
            assets.extend([(table, 'dynamodb') for table in dynamodb_tables])
        
        # Get monitored S3 buckets (AWS specific)
        if hasattr(provider_account, 's3_buckets'):
            s3_buckets = provider_account.s3_buckets.filter(
                monitoring=UtilAsset.Monitoring.ACTIVE
            )
            assets.extend([(bucket, 's3_bucket') for bucket in s3_buckets])
        
        # Get monitored ACM certificates (AWS specific)
        if hasattr(provider_account, 'acm_certificates'):
            acm_certificates = provider_account.acm_certificates.filter(
                monitoring=UtilAsset.Monitoring.ACTIVE
            )
            assets.extend([(cert, 'acm_certificate') for cert in acm_certificates])
        
        # Get monitored snapshots (AWS specific)
        if hasattr(provider_account, 'snapshots'):
            snapshots = provider_account.snapshots.filter(
                monitoring=UtilAsset.Monitoring.ACTIVE
            )
            assets.extend([(snapshot, 'snapshot') for snapshot in snapshots])
        
        # Get monitored Elastic IPs (AWS specific)
        if hasattr(provider_account, 'elastic_ips'):
            elastic_ips = provider_account.elastic_ips.filter(
                monitoring=UtilAsset.Monitoring.ACTIVE
            )
            assets.extend([(eip, 'elastic_ip') for eip in elastic_ips])
        
        # Get monitored Load Balancers (AWS specific)
        if hasattr(provider_account, 'load_balancers'):
            load_balancers = provider_account.load_balancers.filter(
                monitoring=UtilAsset.Monitoring.ACTIVE
            )
            assets.extend([(lb, 'load_balancer') for lb in load_balancers])
        
        # Get monitored Security Groups (AWS specific)
        if hasattr(provider_account, 'security_groups'):
            security_groups = provider_account.security_groups.filter(
                monitoring=UtilAsset.Monitoring.ACTIVE
            )
            assets.extend([(sg, 'security_group') for sg in security_groups])
        
        # Get monitored ECS Services (AWS specific)
        if hasattr(provider_account, 'ecs_services'):
            ecs_services = provider_account.ecs_services.filter(
                monitoring=UtilAsset.Monitoring.ACTIVE
            )
            assets.extend([(service, 'ecs_service') for service in ecs_services])
        
        # Get monitored ECS Tasks (AWS specific)
        if hasattr(provider_account, 'ecs_tasks'):
            ecs_tasks = provider_account.ecs_tasks.filter(
                monitoring=UtilAsset.Monitoring.ACTIVE
            )
            assets.extend([(task, 'ecs_task') for task in ecs_tasks])

        return assets

    def delete_all_assets(self):
        """
        Deletes all assets (servers, volumes, databases) associated with this cloud.
        Each asset removes its own schedule and monitoring data on delete.
        """
        try:
            provider_account = self.provider_account

            # Delete servers if they exist
            if hasattr(provider_account, 'servers'):
                servers = provider_account.servers.all()
                for server in servers:
                    try:
                        server.delete()
                    except Exception as e:
                        print(f"Error deleting server {server.name}: {str(e)}")

            # Delete volumes if they exist
            if hasattr(provider_account, 'volumes'):
                volumes = provider_account.volumes.all()
                for volume in volumes:
                    try:
                        volume.delete()
                    except Exception as e:
                        print(f"Error deleting volume {volume.name}: {str(e)}")

            # Delete databases if they exist
            if hasattr(provider_account, 'databases'):
                databases = provider_account.databases.all()
                for database in databases:
                    try:
                        database.delete()
                    except Exception as e:
                        print(f"Error deleting database {database.name}: {str(e)}")
            
            # Delete RDS databases if they exist (AWS specific)
            if hasattr(provider_account, 'rds_databases'):
                rds_databases = provider_account.rds_databases.all()
                for database in rds_databases:
                    try:
                        database.delete()
                    except Exception as e:
                        print(f"Error deleting RDS database {database.name}: {str(e)}")
            
            # Delete Lambda functions if they exist (AWS specific)
            if hasattr(provider_account, 'lambda_functions'):
                lambda_functions = provider_account.lambda_functions.all()
                for function in lambda_functions:
                    try:
                        function.delete()
                    except Exception as e:
                        print(f"Error deleting Lambda function {function.name}: {str(e)}")
            
            # Delete DynamoDB tables if they exist (AWS specific)
            if hasattr(provider_account, 'dynamodb_tables'):
                dynamodb_tables = provider_account.dynamodb_tables.all()
                for table in dynamodb_tables:
                    try:
                        table.delete()
                    except Exception as e:
                        print(f"Error deleting DynamoDB table {table.name}: {str(e)}")
            
            # Delete S3 buckets if they exist (AWS specific)
            if hasattr(provider_account, 's3_buckets'):
                s3_buckets = provider_account.s3_buckets.all()
                for bucket in s3_buckets:
                    try:
                        bucket.delete()
                    except Exception as e:
                        print(f"Error deleting S3 bucket {bucket.name}: {str(e)}")
            
            # Delete ACM certificates if they exist (AWS specific)
            if hasattr(provider_account, 'acm_certificates'):
                acm_certificates = provider_account.acm_certificates.all()
                for cert in acm_certificates:
                    try:
                        cert.delete()
                    except Exception as e:
                        print(f"Error deleting ACM certificate {cert.name}: {str(e)}")
            
            # Delete snapshots if they exist (AWS specific)
            if hasattr(provider_account, 'snapshots'):
                snapshots = provider_account.snapshots.all()
                for snapshot in snapshots:
                    try:
                        snapshot.delete()
                    except Exception as e:
                        print(f"Error deleting snapshot {snapshot.name}: {str(e)}")
            
            # Delete Elastic IPs if they exist (AWS specific)
            if hasattr(provider_account, 'elastic_ips'):
                elastic_ips = provider_account.elastic_ips.all()
                for eip in elastic_ips:
                    try:
                        eip.delete()
                    except Exception as e:
                        print(f"Error deleting Elastic IP {eip.name}: {str(e)}")
            
            # Delete Load Balancers if they exist (AWS specific)
            if hasattr(provider_account, 'load_balancers'):
                load_balancers = provider_account.load_balancers.all()
                for lb in load_balancers:
                    try:
                        lb.delete()
                    except Exception as e:
                        print(f"Error deleting Load Balancer {lb.name}: {str(e)}")
            
            # Delete Security Groups if they exist (AWS specific)
            if hasattr(provider_account, 'security_groups'):
                security_groups = provider_account.security_groups.all()
                for sg in security_groups:
                    try:
                        sg.delete()
                    except Exception as e:
                        print(f"Error deleting Security Group {sg.name}: {str(e)}")
            
            # Delete ECS Services if they exist (AWS specific)
            if hasattr(provider_account, 'ecs_services'):
                ecs_services = provider_account.ecs_services.all()
                for service in ecs_services:
                    try:
                        service.delete()
                    except Exception as e:
                        print(f"Error deleting ECS Service {service.name}: {str(e)}")
            
            # Delete ECS Tasks if they exist (AWS specific)
            if hasattr(provider_account, 'ecs_tasks'):
                ecs_tasks = provider_account.ecs_tasks.all()
                for task in ecs_tasks:
                    try:
                        task.delete()
                    except Exception as e:
                        print(f"Error deleting ECS Task {task.name}: {str(e)}")

        except Exception as e:
            print(f"Error during asset deletion for cloud {self.name}: {str(e)}")
            raise

    def delete_all_asset_schedules(self):
        """
        Deletes the status-check schedules for all assets associated with this cloud
        """
        try:
            # Get all monitored assets
            monitored_assets = self.get_active_assets()

            # Delete schedules for all monitored assets
            for asset, asset_type in monitored_assets:
                asset_schedule_delete(asset)

        except Exception as e:
            print(f"Error deleting asset schedules for cloud {self.name}: {str(e)}")
            raise

    def create_all_asset_schedules(self):
        from apps.console.utils.models import UtilAsset

        """
        Creates default schedules for all active assets that don't have schedules.
        This is useful when reactivating a cloud or fixing authentication issues.
        """
        try:
            # Get all active assets (excluding NO_LONGER_EXISTS)
            active_assets = self.get_active_assets()

            for asset, asset_type in active_assets:
                # Only create schedule if:
                # 1. Asset doesn't already have a schedule
                # 2. Asset monitoring is set to ACTIVE
                if (not PeriodicTask.objects.filter(name=f'asset-{asset.uuid}').exists()
                        and asset.monitoring == UtilAsset.Monitoring.ACTIVE):
                    asset_schedule_create(asset)
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

        except Exception as e:
            print(f"Error deleting monitoring data for cloud {self.name}: {str(e)}")
            pass

    def sync_assets(self):
        try:
            self.provider_account.sync_assets()
            self.last_synced = datetime.now()
            self.save()
        except NotImplementedError:
            raise NotImplementedError("Asset synchronization not implemented for this cloud provider")
