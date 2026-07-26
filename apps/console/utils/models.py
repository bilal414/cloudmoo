import json
import calendar
import logging
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from apps.console.utils.aws import aws_client, aws_resource, monitoring_engine_configured
from boto3.dynamodb.conditions import Key
from django.db import models
from django.utils.text import slugify
from model_utils.models import TimeStampedModel
from django.conf import settings
from dateutil.parser import parse
import uuid
from apps.console.account.models import CoreAccountMembership

logger = logging.getLogger(__name__)


class UtilCloud(TimeStampedModel):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        DISABLED = "disabled", "Disabled"

    name = models.CharField(max_length=255)
    status = models.CharField(max_length=64, choices=Status.choices, default=Status.ACTIVE)
    last_synced = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(null=True)
    notes = models.TextField(null=True, blank=True)

    class Meta:
        abstract = True


class AssetQuerySet(models.QuerySet):
    def for_user(self, user):
        if user.is_superuser:
            return self
        return self.filter(owner__cloud__account=user.member.active_account)


class AssetManager(models.Manager):
    def get_queryset(self):
        return AssetQuerySet(self.model, using=self._db)

    def for_user(self, user):
        return self.get_queryset().for_user(user)


class UtilAsset(TimeStampedModel):
    objects = AssetManager()

    class Monitoring(models.TextChoices):
        ACTIVE = "active", "Active"
        DISABLED = "disabled", "Disabled"
        NO_LONGER_EXISTS = "no_longer_exists", "No Longer Exists"

    class Type(models.TextChoices):
        SERVER = "server", "Server"
        VOLUME = "volume", "Volume"
        DATABASE = "database", "Database"
        RDS_DATABASE = "rds_database", "RDS Database"
        LAMBDA = "lambda", "Lambda Function"
        DYNAMODB = "dynamodb", "DynamoDB Table"
        S3_BUCKET = "s3_bucket", "S3 Bucket"
        ACM_CERTIFICATE = "acm_certificate", "ACM Certificate"
        SNAPSHOT = "snapshot", "Snapshot"
        ELASTIC_IP = "elastic_ip", "Elastic IP"
        LOAD_BALANCER = "load_balancer", "Load Balancer"
        SECURITY_GROUP = "security_group", "Security Group"
        ECS_SERVICE = "ecs_service", "ECS Service"
        ECS_TASK = "ecs_task", "ECS Task"

    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    unique_id = models.CharField(max_length=100)
    name = models.CharField(max_length=100)
    metadata = models.JSONField(null=True)
    notes = models.TextField(null=True, blank=True)
    monitoring = models.CharField(max_length=64, choices=Monitoring.choices, default=Monitoring.ACTIVE)
    type = models.CharField(max_length=64, choices=Type.choices, null=True)
    notification_emails = models.JSONField(default=list,
                                           help_text="List of email addresses to notify for asset status changes")
    aws_schedule_arn = models.TextField(null=True, blank=True)

    class Meta:
        abstract = True

    @property
    def key(self):
        import hashlib
        
        # Create a base key without unique_id first
        base_key = f"cm__{self.owner.cloud.account.id}__{self.provider_code}__"
        
        # If the full key is too long (over 64 chars), hash the unique_id
        full_key = f"{base_key}{self.unique_id}"
        if len(slugify(full_key)) > 64:
            # Hash the unique_id to keep it short but unique
            unique_id_hash = hashlib.md5(self.unique_id.encode()).hexdigest()[:16]
            final_key = f"{base_key}{unique_id_hash}"
        else:
            final_key = full_key
            
        return slugify(final_key)

    @property
    def owner_email(self):
        return self.owner.cloud.account.memberships.get(role=CoreAccountMembership.Role.OWNER).member.user.email

    @property
    def provider_code(self):
        return self.owner.cloud.provider.code.lower()

    @property
    def provider_name(self):
        return self.owner.cloud.provider.name

    @property
    def provider_url(self):
        return None

    @property
    def cloudmoo_url(self):
        """
        Returns the CloudMoo web URL for the asset detail page.
        Example: https://cloudmoo.com/console/assets/digitalocean/server/35/
        """
        return f"{settings.APP_URL}/console/assets/{self.provider_code}/{self.type.lower()}/{self.id}/"

    @property
    def status(self):
        """
        Gets the latest status from DynamoDB cloudmoo-prod-asset-logs table.
        Returns "unknown" if no status is found or if there's an error.
        
        For better performance when loading multiple assets, use get_bulk_statuses() class method.
        """
        # Check if status is already cached on this instance
        if hasattr(self, '_cached_status'):
            return self._cached_status
            
        try:
            if self.monitoring == self.Monitoring.ACTIVE:
                # Initialize DynamoDB client
                dynamodb = aws_resource('dynamodb')
                table = dynamodb.Table(settings.AWS_DYNAMODB_ASSET_LOGS_TABLE)

                # Query the most recent log entry for this asset
                response = table.query(
                    KeyConditionExpression=Key('asset_key').eq(self.key),
                    ScanIndexForward=False,  # Get newest items first
                    Limit=1  # We only need the most recent entry
                )

                # Check if we got any items
                if response['Items']:
                    latest_status = response['Items'][0]['status']
                    # Filter out error statuses
                    if latest_status not in ['error', 'invalid_access_token']:
                        self._cached_status = latest_status
                        return latest_status

                self._cached_status = "unknown"
                return "unknown"
            else:
                self._cached_status = "unknown"  
                return "unknown"

        except Exception as e:
            print(f"Error fetching status for asset {self.name} from DynamoDB: {str(e)}")
            self._cached_status = "unknown"
            return "unknown"

    @classmethod
    def get_bulk_statuses(cls, assets):
        """
        Efficiently fetches statuses for multiple assets using DynamoDB batch operations.
        
        Args:
            assets: QuerySet or list of UtilAsset instances
            
        Returns:
            dict: {asset.key: status} mapping
        """
        try:
            # Initialize DynamoDB client
            dynamodb = aws_resource('dynamodb')
            table = dynamodb.Table(settings.AWS_DYNAMODB_ASSET_LOGS_TABLE)
            
            # Prepare asset keys for active monitoring assets only
            active_assets = [asset for asset in assets if asset.monitoring == cls.Monitoring.ACTIVE]
            if not active_assets:
                return {}
            
            # DynamoDB batch_get_item can only handle 100 items at once
            status_map = {}
            batch_size = 100
            
            for i in range(0, len(active_assets), batch_size):
                batch_assets = active_assets[i:i + batch_size]
                
                # Use batch queries for better performance
                # Since we need the latest item per key, we'll use individual queries but with threading
                import concurrent.futures
                
                def get_asset_status(asset):
                    try:
                        response = table.query(
                            KeyConditionExpression=Key('asset_key').eq(asset.key),
                            ScanIndexForward=False,
                            Limit=1
                        )
                        
                        if response['Items']:
                            latest_status = response['Items'][0]['status']
                            if latest_status not in ['error', 'invalid_access_token']:
                                return asset.key, latest_status
                        return asset.key, "unknown"
                    except Exception:
                        return asset.key, "unknown"
                
                # Execute queries in parallel for better performance
                with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                    results = list(executor.map(get_asset_status, batch_assets))
                    
                for asset_key, status in results:
                    status_map[asset_key] = status
            
            # Cache statuses on asset instances
            for asset in active_assets:
                if asset.key in status_map:
                    asset._cached_status = status_map[asset.key]
            
            return status_map
            
        except Exception as e:
            print(f"Error fetching bulk statuses from DynamoDB: {str(e)}")
            return {}

    def save(self, *args, **kwargs):
        """
        Override save method to sync with DynamoDB after saving to database
        """
        is_new = self._state.adding

        if not is_new:
            # Get the original object from the database
            original = self.__class__.objects.get(pk=self.pk)

            # Check if monitoring status has changed to NO_LONGER_EXISTS
            if self.monitoring == self.Monitoring.NO_LONGER_EXISTS and original.monitoring != self.Monitoring.NO_LONGER_EXISTS:
                self.aws_schedule_delete()

        super().save(*args, **kwargs)

        if is_new and self.type in [self.Type.SERVER, self.Type.VOLUME, self.Type.DATABASE, self.Type.RDS_DATABASE, self.Type.LAMBDA, self.Type.DYNAMODB, self.Type.S3_BUCKET, self.Type.ACM_CERTIFICATE, self.Type.SNAPSHOT, self.Type.ELASTIC_IP, self.Type.LOAD_BALANCER, self.Type.SECURITY_GROUP, self.Type.ECS_SERVICE, self.Type.ECS_TASK]:
            if monitoring_engine_configured():
                try:
                    self.aws_schedule_create()
                except Exception as e:
                    logger.warning(
                        f"Could not create EventBridge schedule for asset {self.key}: {e}. "
                        f"Schedules can be recreated with 'python manage.py create_all_cloud_schedules --confirm'."
                    )
            # When it's new asset then add owner email to notification_emails
            if self.owner_email not in self.notification_emails:
                self.notification_emails.append(self.owner_email)
                self.save()
                return

        # Sync to DynamoDB after saving (no-op unless monitoring engine is configured)
        self.sync_to_dynamodb()

    def delete(self, *args, **kwargs):
        """
        Override delete method to remove entry from DynamoDB before deleting
        """
        try:
            # Initialize DynamoDB client
            dynamodb = aws_resource('dynamodb')
            table = dynamodb.Table(settings.AWS_DYNAMODB_ASSETS_TABLE)

            # Delete from DynamoDB
            table.delete_item(
                Key={
                    'asset_key': self.key
                }
            )
        except Exception as e:
            print(f"Error deleting asset {self.name} from DynamoDB: {str(e)}")

        # Call aws_schedule_delete before deleting the model item
        self.aws_schedule_delete()
        super().delete(*args, **kwargs)

    def _get_aws_scheduler_client(self):
        return aws_client("scheduler")

    def _get_schedule_settings(self):
        unique_id = self.unique_id
        access_token = self.owner.access_token

        lambda_arn = settings.AWS_LAMBDA_ASSET_STATUS

        return {
            "RoleArn": settings.AWS_SCHEDULER_ROLE,
            "Arn": lambda_arn,
            "RetryPolicy": {"MaximumEventAgeInSeconds": 24 * 3600, "MaximumRetryAttempts": 100},
            "Input": json.dumps(
                {"asset_id": self.id, "unique_id": unique_id, "access_token": access_token,
                 "provider": self.provider_code.lower(),
                 "asset_type": self.type.lower(), "asset_key": self.key, "uuid": f"{self.uuid}"}
            ),
        }

    def aws_schedule_create(self):
        aws_scheduler = self._get_aws_scheduler_client()
        schedule_settings = self._get_schedule_settings()

        aws_schedule = {
            "Name": str(self.uuid),
            "State": "ENABLED",
            "ScheduleExpression": 'rate(1 minutes)',
            "ScheduleExpressionTimezone": 'UTC',
            "Target": schedule_settings,
            "FlexibleTimeWindow": {"Mode": "OFF"},
        }

        aws_response = aws_scheduler.create_schedule(**aws_schedule)
        self.aws_schedule_arn = aws_response.get("ScheduleArn")
        self.save()

    def aws_schedule_update(self):
        if not self.aws_schedule_arn:
            return
            
        # Extract schedule name from ARN
        schedule_name = self.aws_schedule_arn.split('/')[-1]
        
        aws_scheduler = self._get_aws_scheduler_client()
        schedule_settings = self._get_schedule_settings()

        aws_schedule = {
            "Name": schedule_name,
            "State": "ENABLED" if self.monitoring == self.Monitoring.ACTIVE else "DISABLED",
            "ScheduleExpression": 'rate(1 minutes)',
            "ScheduleExpressionTimezone": 'UTC',
            "Target": schedule_settings,
            "FlexibleTimeWindow": {"Mode": "OFF"},
        }

        aws_scheduler.get_schedule(Name=schedule_name)
        aws_response = aws_scheduler.update_schedule(**aws_schedule)
        self.aws_schedule_arn = aws_response.get("ScheduleArn")
        self.save()

    def aws_schedule_delete(self):
        if self.aws_schedule_arn:
            # Extract schedule name from ARN
            schedule_name = self.aws_schedule_arn.split('/')[-1]
            
            aws_scheduler = self._get_aws_scheduler_client()
            try:
                aws_scheduler.delete_schedule(Name=schedule_name)
            except aws_scheduler.exceptions.ResourceNotFoundException as e:
                print(e.__str__())

    def get_email_config(self):
        """
        Get the list of notification email addresses for this asset
        """
        return self.notification_emails

    def update_email_config(self, email_list):
        """
        Update the notification email list for this asset
        """
        # Remove duplicates
        self.notification_emails = list(set(email_list))
        self.save()
        return True

    def sync_to_dynamodb(self):
        """
        Syncs asset information to the DynamoDB assets table.
        This includes asset details and related cloud information.
        No-op when the AWS monitoring engine is not configured.
        """
        if not monitoring_engine_configured():
            return

        try:
            # Initialize DynamoDB client
            dynamodb = aws_resource('dynamodb')
            table = dynamodb.Table(settings.AWS_DYNAMODB_ASSETS_TABLE)

            # Prepare cloud information
            cloud = self.owner.cloud
            cloud_info = {
                'id': str(cloud.id),
                'name': cloud.name,
                'status': cloud.status,
                'provider': {
                    'code': cloud.provider.code,
                    'name': cloud.provider.name
                },
                'account': {
                    'id': str(cloud.account.id),
                    'name': cloud.account.name,
                    'status': str(cloud.account.status)
                }
            }

            # Convert metadata to string if it's not None
            metadata = json.loads(json.dumps(self.metadata)) if self.metadata else None

            # Prepare asset information
            asset_data = {
                'asset_key': self.key,  # Partition key
                'monitoring': self.monitoring,  # Sort key
                'id': str(self.id),
                'name': self.name,
                'unique_id': self.unique_id,
                'type': self.type,
                'provider_url': self.provider_url,
                'cloudmoo_url': self.cloudmoo_url,
                'notification_emails': list(self.notification_emails),  # Ensure it's a list
                'notes': self.notes,
                'created': self.created.isoformat(),
                'modified': self.modified.isoformat(),
                'cloud': cloud_info,
                'owner': {
                    'id': str(self.owner.id),
                    'name': self.owner.name,
                    'status': self.owner.status,
                }
            }

            # Remove None values as DynamoDB doesn't support them
            asset_data = {k: v for k, v in asset_data.items() if v is not None}

            # Convert any float values to Decimal
            def convert_floats(obj):
                if isinstance(obj, float):
                    return Decimal(str(obj))
                elif isinstance(obj, dict):
                    return {k: convert_floats(v) for k, v in obj.items()}
                elif isinstance(obj, list):
                    return [convert_floats(v) for v in obj]
                return obj

            asset_data = convert_floats(asset_data)

            # Update DynamoDB
            table.put_item(Item=asset_data)

        except Exception as e:
            # Status data is best-effort: never block a local save on DynamoDB.
            logger.warning(f"Error syncing asset {self.name} to DynamoDB: {str(e)}")

    def get_status_timeline_off2(self, days=30):
        """
        Gets the status timeline for the asset from DynamoDB.
        Returns status changes with durations over the specified number of days.
        """
        try:
            # Initialize DynamoDB client
            dynamodb = aws_resource('dynamodb')
            table = dynamodb.Table(settings.AWS_DYNAMODB_ASSET_LOGS_TABLE)

            # Calculate time range
            end_date = datetime.now(timezone.utc)
            start_date = end_date - timedelta(days=days)
            start_timestamp = Decimal(str(calendar.timegm(start_date.utctimetuple())))

            # Initialize variables for pagination
            all_items = []
            last_evaluated_key = None

            while True:
                # Prepare query parameters
                query_params = {
                    'KeyConditionExpression':
                        Key('asset_key').eq(self.key) &
                        Key('timestamp').gte(start_timestamp),
                    'ScanIndexForward': False  # Get newest items first
                }

                if last_evaluated_key:
                    query_params['ExclusiveStartKey'] = last_evaluated_key

                # Execute the query
                response = table.query(**query_params)

                # Add items from this page
                if 'Items' in response:
                    all_items.extend(response['Items'])

                # Get the last evaluated key for pagination
                last_evaluated_key = response.get('LastEvaluatedKey')

                # If no more data to fetch, break the loop
                if not last_evaluated_key:
                    break

            # Filter out error statuses and create status_data
            status_data = [
                {
                    'timestamp': item['timestamp_iso'],
                    'status': item['status']
                }
                for item in all_items
                if item['status'] not in ['error', 'invalid_access_token']
            ]

            # Sort by timestamp (should already be sorted due to ScanIndexForward=False)
            status_data = sorted(status_data, key=lambda x: x['timestamp'], reverse=True)

            # When creating status_changes list, parse the timestamp string and preserve timezone
            status_changes = []
            for i, item in enumerate(status_data):
                if i == 0 or item['status'] != status_data[i - 1]['status']:
                    parsed_timestamp = parse(item['timestamp'])
                    status_changes.append({
                        'timestamp': parsed_timestamp,  # This will preserve the timezone
                        'timezone': parsed_timestamp.tzinfo.tzname(
                            parsed_timestamp) if parsed_timestamp.tzinfo else 'UTC',
                        'status': item['status']
                    })

            # Calculate durations
            for i in range(len(status_changes)):
                if i < len(status_changes) - 1:
                    current_time = status_changes[i]['timestamp']
                    next_time = status_changes[i + 1]['timestamp']
                    duration = current_time - next_time
                    status_changes[i]['duration'] = self.format_duration(duration)
                else:
                    status_changes[i]['duration'] = None

            return status_changes

        except Exception as e:
            print(f"Error fetching status timeline from DynamoDB: {str(e)}")
            return []

    def get_status_timeline_off(self, days=30):
        """
        Gets the status timeline for the asset over the specified number of days.
        Uses a database function to efficiently calculate status changes and durations.
        """
        from django.db import connection
        from datetime import datetime, timedelta
        from dateutil.tz import tzutc

        end_date = datetime.now(tzutc())
        start_date = end_date - timedelta(days=days)

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT status_value, event_timestamp, time_duration 
                FROM get_asset_timeline(%s, %s, %s, %s)
                """,
                [
                    self._meta.db_table,
                    self.id,
                    start_date,
                    end_date
                ]
            )

            status_changes = []
            for row in cursor.fetchall():
                status, timestamp, duration = row
                status_changes.append({
                    'status': status,
                    'timestamp': timestamp,
                    'duration': self.format_duration(duration) if duration else None
                })

            return status_changes

    @classmethod
    def _get_dynamodb_table(cls):
        """Get cached DynamoDB table resource"""
        if not hasattr(cls, '_dynamodb_table'):
            dynamodb = aws_resource('dynamodb')
            cls._dynamodb_table = dynamodb.Table(settings.AWS_DYNAMODB_ASSET_LOGS_TABLE)
        return cls._dynamodb_table

    def get_status_timeline(self, days=30):
        """
        Gets the status timeline for the asset from DynamoDB.
        Returns status changes with durations and metadata changes over the specified number of days.
        """
        try:
            table = self._get_dynamodb_table()

            # Calculate time range
            end_date = datetime.now(timezone.utc)
            start_date = end_date - timedelta(days=days)
            start_timestamp = Decimal(str(calendar.timegm(start_date.utctimetuple())))

            # Initialize variables for pagination
            status_changes = []
            last_evaluated_key = None
            items_processed = 0
            max_items = 10000  # Reasonable limit to prevent runaway queries
            
            previous_status = None
            previous_item = None

            while True:
                # Prepare query parameters with filter expression to exclude errors at query time
                query_params = {
                    'KeyConditionExpression':
                        Key('asset_key').eq(self.key) &
                        Key('timestamp').gte(start_timestamp),
                    'FilterExpression': 'NOT #status IN (:error1, :error2)',
                    'ExpressionAttributeNames': {'#status': 'status'},
                    'ExpressionAttributeValues': {
                        ':error1': 'error',
                        ':error2': 'invalid_access_token'
                    },
                    'ScanIndexForward': False,  # Get newest items first
                    'Limit': 1000  # Process in smaller batches
                }

                if last_evaluated_key:
                    query_params['ExclusiveStartKey'] = last_evaluated_key

                # Execute the query
                response = table.query(**query_params)

                # Process items from this page immediately
                if 'Items' in response:
                    for item in response['Items']:
                        items_processed += 1
                        current_status = item['status']
                        has_metadata_changes = bool(item.get('metadata_changes'))
                        
                        # Only add if status changed or has metadata changes
                        if (previous_status is None or 
                            current_status != previous_status or 
                            has_metadata_changes):
                            
                            # Parse timestamp once
                            parsed_timestamp = parse(item['timestamp_iso'])
                            status_changes.append({
                                'timestamp': parsed_timestamp,
                                'timezone': parsed_timestamp.tzinfo.tzname(
                                    parsed_timestamp) if parsed_timestamp.tzinfo else 'UTC',
                                'status': current_status,
                                'metadata_changes': item.get('metadata_changes', [])
                            })
                            
                        previous_status = current_status
                        
                        # Safety limit
                        if items_processed >= max_items:
                            break

                # Get the last evaluated key for pagination
                last_evaluated_key = response.get('LastEvaluatedKey')

                # Break if no more data, hit limit, or no pagination key
                if not last_evaluated_key or items_processed >= max_items:
                    break

            # Calculate durations (data is already in correct order)
            current_time = end_date  # Use the same timestamp for consistency
            for i in range(len(status_changes)):
                if i == 0:
                    # First entry: duration from its timestamp to now
                    duration = current_time - status_changes[i]['timestamp']
                    status_changes[i]['duration'] = self.format_duration(duration)
                else:
                    # Subsequent entries: duration from this timestamp to the previous entry's timestamp
                    duration = status_changes[i - 1]['timestamp'] - status_changes[i]['timestamp']
                    status_changes[i]['duration'] = self.format_duration(duration)

            return status_changes

        except Exception as e:
            print(f"Error fetching status timeline from DynamoDB: {str(e)}")
            return []

    def get_status_timeline_paginated(self, page=1, page_size=10, days=30):
        """
        Gets a paginated status timeline for the asset from DynamoDB.
        Returns status changes with pagination metadata over the specified number of days.
        """
        try:
            table = self._get_dynamodb_table()
            
            # Calculate time range
            end_date = datetime.now(timezone.utc)
            start_date = end_date - timedelta(days=days)
            start_timestamp = Decimal(str(calendar.timegm(start_date.utctimetuple())))
            
            # Calculate pagination parameters
            offset = (page - 1) * page_size
            
            # Initialize variables for pagination
            status_changes = []
            last_evaluated_key = None
            items_processed = 0
            items_skipped = 0
            max_items = 10000  # Reasonable limit to prevent runaway queries
            
            previous_status = None
            
            while True:
                # Prepare query parameters with filter expression to exclude errors at query time
                query_params = {
                    'KeyConditionExpression':
                        Key('asset_key').eq(self.key) &
                        Key('timestamp').gte(start_timestamp),
                    'FilterExpression': 'NOT #status IN (:error1, :error2)',
                    'ExpressionAttributeNames': {'#status': 'status'},
                    'ExpressionAttributeValues': {
                        ':error1': 'error',
                        ':error2': 'invalid_access_token'
                    },
                    'ScanIndexForward': False,  # Get newest items first
                    'Limit': 1000  # Process in smaller batches
                }
                
                if last_evaluated_key:
                    query_params['ExclusiveStartKey'] = last_evaluated_key
                
                # Execute the query
                response = table.query(**query_params)
                
                # Process items from this page immediately
                if 'Items' in response:
                    for item in response['Items']:
                        items_processed += 1
                        current_status = item['status']
                        has_metadata_changes = bool(item.get('metadata_changes'))
                        
                        # Only add if status changed or has metadata changes
                        if (previous_status is None or 
                            current_status != previous_status or 
                            has_metadata_changes):
                            
                            # Skip items until we reach the offset
                            if items_skipped < offset:
                                items_skipped += 1
                                previous_status = current_status
                                continue
                            
                            # Stop if we have enough items for this page
                            if len(status_changes) >= page_size:
                                break
                            
                            # Parse timestamp once
                            parsed_timestamp = parse(item['timestamp_iso'])
                            status_changes.append({
                                'timestamp': parsed_timestamp,
                                'timezone': parsed_timestamp.tzinfo.tzname(
                                    parsed_timestamp) if parsed_timestamp.tzinfo else 'UTC',
                                'status': current_status,
                                'metadata_changes': item.get('metadata_changes', [])
                            })
                            
                        previous_status = current_status
                        
                        # Safety limit
                        if items_processed >= max_items:
                            break
                
                # Get the last evaluated key for pagination
                last_evaluated_key = response.get('LastEvaluatedKey')
                
                # Break if no more data, hit limit, page full, or no pagination key
                if (not last_evaluated_key or 
                    items_processed >= max_items or 
                    len(status_changes) >= page_size):
                    break
            
            # Calculate durations (data is already in correct order)
            current_time = end_date
            for i in range(len(status_changes)):
                if i == 0:
                    # First entry: duration from its timestamp to now
                    duration = current_time - status_changes[i]['timestamp']
                    status_changes[i]['duration'] = self.format_duration(duration)
                else:
                    # Subsequent entries: duration from this timestamp to the previous entry's timestamp
                    duration = status_changes[i - 1]['timestamp'] - status_changes[i]['timestamp']
                    status_changes[i]['duration'] = self.format_duration(duration)
            
            # Calculate pagination info
            has_next = (last_evaluated_key is not None and 
                       len(status_changes) == page_size and 
                       items_processed < max_items)
            has_previous = page > 1
            
            # Calculate total pages more accurately
            if has_next:
                # If we have a next page, we know there's at least current page + 1
                total_pages = page + 1
            else:
                # No more pages, current page is the last
                total_pages = page if len(status_changes) > 0 else 1
            
            return {
                'items': status_changes,
                'has_next': has_next,
                'has_previous': has_previous,
                'total_pages': total_pages,
                'current_page': page,
                'page_size': page_size
            }
            
        except Exception as e:
            print(f"Error fetching paginated status timeline from DynamoDB: {str(e)}")
            return {
                'items': [],
                'has_next': False,
                'has_previous': False,
                'total_pages': 1,
                'current_page': page,
                'page_size': page_size
            }

    @staticmethod
    def format_duration(duration):
        """
        Formats a duration into a human-readable string.
        """
        days, remainder = divmod(duration.total_seconds(), 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{int(days)}d {int(hours)}h {int(minutes)}m {int(seconds)}s"


