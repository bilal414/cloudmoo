from apps.console.utils.aws import aws_resource
import json
from decimal import Decimal
from datetime import datetime, timezone
from django.conf import settings
from boto3.dynamodb.conditions import Key


class NotificationLog:
    """Model for handling notification logs from DynamoDB"""

    def __init__(self, data):
        self.asset_key = data.get('asset_key')
        self.timestamp = data.get('timestamp')
        self.timestamp_iso = data.get('timestamp_iso')
        self.asset_id = data.get('asset_id')
        self.provider = data.get('provider')
        self.asset_type = data.get('asset_type')
        self.recipient = data.get('recipient')
        self.text_body = data.get('text_body')
        self.html_body = data.get('html_body')
        self.metadata = data.get('metadata', {})

    @classmethod
    def get_dynamodb_table(cls):
        """Get DynamoDB table connection"""
        dynamodb = aws_resource('dynamodb')
        return dynamodb.Table(settings.AWS_DYNAMODB_ASSET_EMAILS_TABLE)

    @classmethod
    def get_logs_for_account(cls, account_id, filters=None, limit=50):
        """Get notification logs for a specific account ID with optional filters"""
        try:
            table = cls.get_dynamodb_table()
            
            # Start with basic account filter
            filter_expression = Key('asset_key').begins_with(f'cm__{account_id}__')
            
            # Add additional filters if provided
            if filters:
                from boto3.dynamodb.conditions import Attr
                
                if filters.get('email'):
                    filter_expression = filter_expression & Attr('recipient').contains(filters['email'])
                
                if filters.get('provider'):
                    filter_expression = filter_expression & Attr('provider').eq(filters['provider'])
                
                if filters.get('asset_type'):
                    filter_expression = filter_expression & Attr('asset_type').eq(filters['asset_type'])
                
                # Date range filtering
                if filters.get('date_from') or filters.get('date_to'):
                    from datetime import datetime, timezone
                    import calendar
                    
                    if filters.get('date_from'):
                        date_from = datetime.strptime(filters['date_from'], '%Y-%m-%d')
                        date_from = date_from.replace(tzinfo=timezone.utc)
                        timestamp_from = calendar.timegm(date_from.timetuple())
                        filter_expression = filter_expression & Attr('timestamp').gte(timestamp_from)
                    
                    if filters.get('date_to'):
                        date_to = datetime.strptime(filters['date_to'], '%Y-%m-%d')
                        # Set to end of day
                        date_to = date_to.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
                        timestamp_to = calendar.timegm(date_to.timetuple())
                        filter_expression = filter_expression & Attr('timestamp').lte(timestamp_to)
            
            # Scan for items
            response = table.scan(
                FilterExpression=filter_expression,
                Limit=limit
            )
            
            logs = []
            for item in response.get('Items', []):
                logs.append(cls(item))
            
            # Sort by timestamp descending (most recent first)
            logs.sort(key=lambda x: x.timestamp, reverse=True)
            
            return logs
            
        except Exception as e:
            print(f"Error fetching notification logs: {str(e)}")
            return []

    @classmethod
    def get_logs_for_account_paginated(cls, account_id, filters=None, last_evaluated_key=None, limit=50):
        """Get notification logs for a specific account ID with pagination and filters"""
        try:
            table = cls.get_dynamodb_table()
            
            # Start with basic account filter
            filter_expression = Key('asset_key').begins_with(f'cm__{account_id}__')
            
            # Add additional filters if provided
            if filters:
                from boto3.dynamodb.conditions import Attr
                
                if filters.get('email'):
                    filter_expression = filter_expression & Attr('recipient').contains(filters['email'])
                
                if filters.get('provider'):
                    filter_expression = filter_expression & Attr('provider').eq(filters['provider'])
                
                if filters.get('asset_type'):
                    filter_expression = filter_expression & Attr('asset_type').eq(filters['asset_type'])
                
                # Date range filtering
                if filters.get('date_from') or filters.get('date_to'):
                    from datetime import datetime, timezone
                    import calendar
                    
                    if filters.get('date_from'):
                        date_from = datetime.strptime(filters['date_from'], '%Y-%m-%d')
                        date_from = date_from.replace(tzinfo=timezone.utc)
                        timestamp_from = calendar.timegm(date_from.timetuple())
                        filter_expression = filter_expression & Attr('timestamp').gte(timestamp_from)
                    
                    if filters.get('date_to'):
                        date_to = datetime.strptime(filters['date_to'], '%Y-%m-%d')
                        # Set to end of day
                        date_to = date_to.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
                        timestamp_to = calendar.timegm(date_to.timetuple())
                        filter_expression = filter_expression & Attr('timestamp').lte(timestamp_to)
            
            scan_kwargs = {
                'FilterExpression': filter_expression,
                'Limit': limit
            }
            
            if last_evaluated_key:
                scan_kwargs['ExclusiveStartKey'] = last_evaluated_key
            
            response = table.scan(**scan_kwargs)
            
            logs = []
            for item in response.get('Items', []):
                logs.append(cls(item))
            
            # Sort by timestamp descending (most recent first)
            logs.sort(key=lambda x: x.timestamp, reverse=True)
            
            return logs, response.get('LastEvaluatedKey')
            
        except Exception as e:
            print(f"Error fetching notification logs: {str(e)}")
            return [], None

    @property
    def formatted_timestamp(self):
        """Get formatted timestamp for display"""
        if self.timestamp_iso:
            try:
                dt = datetime.fromisoformat(self.timestamp_iso.replace('Z', '+00:00'))
                return dt.strftime('%Y-%m-%d %H:%M:%S UTC')
            except:
                pass
        return 'Unknown'

    @property
    def status_change(self):
        """Get status change information from metadata"""
        if self.metadata and 'status_change' in self.metadata:
            return self.metadata['status_change']
        return None

    @property
    def subject(self):
        """Get email subject from metadata"""
        if self.metadata and 'subject' in self.metadata:
            return self.metadata['subject']
        return f"Status Change Alert for {self.provider} - {self.asset_type}"

    def __str__(self):
        return f"NotificationLog({self.asset_key}, {self.formatted_timestamp})"
