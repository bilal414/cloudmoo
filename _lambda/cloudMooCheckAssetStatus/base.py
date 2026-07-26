import json
import boto3
from datetime import datetime, timezone, timedelta
import calendar
from decimal import Decimal
from boto3.dynamodb.conditions import Key


def get_dynamodb():
    """Get DynamoDB resource"""
    return boto3.resource('dynamodb')


def get_previous_status(asset_key):
    """Get the previous status from DynamoDB using asset_key"""
    try:
        dynamodb = get_dynamodb()
        table = dynamodb.Table('cloudmoo-prod-asset-logs')

        # Query the table for the latest status entry
        response = table.query(
            KeyConditionExpression=Key('asset_key').eq(asset_key),
            ScanIndexForward=False,  # Sort in descending order (newest first)
            Limit=1  # Get only the latest record
        )

        # Check if we got any items
        if response['Items']:
            latest_item = response['Items'][0]
            return {
                'status': latest_item['status'],
                'timestamp': latest_item['timestamp'],
                'timestamp_iso': latest_item['timestamp_iso'],
                'metadata': latest_item['metadata']
            }
        return None

    except Exception as e:
        print(f"Error getting previous status from DynamoDB: {str(e)}")
        return None


# Global variable to cache DynamoDB table
_dynamodb_table = None

def get_cached_dynamodb_table():
    """Get cached DynamoDB table resource"""
    global _dynamodb_table
    if _dynamodb_table is None:
        dynamodb = get_dynamodb()
        _dynamodb_table = dynamodb.Table('cloudmoo-prod-asset-logs')
    return _dynamodb_table

def calculate_status_timeline(asset_key, days=30):
    """Calculate status timeline from DynamoDB data, only including status changes"""
    try:
        table = get_cached_dynamodb_table()

        # Calculate the timestamp for N days ago
        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=days)
        start_timestamp = Decimal(str(calendar.timegm(start_date.utctimetuple())))

        # Initialize variables for pagination
        status_changes = []
        last_evaluated_key = None
        items_processed = 0
        max_items = 10000  # Reasonable limit to prevent runaway queries
        
        previous_status = None

        while True:
            # Prepare query parameters with filter expression to exclude errors at query time
            query_params = {
                'KeyConditionExpression':
                    Key('asset_key').eq(asset_key) &
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

            # Add ExclusiveStartKey if we have a last evaluated key
            if last_evaluated_key:
                query_params['ExclusiveStartKey'] = last_evaluated_key

            # Execute the query
            response = table.query(**query_params)

            # Process items from this page immediately
            if 'Items' in response:
                for item in response['Items']:
                    items_processed += 1
                    current_status = item['status']
                    
                    # Only add if status changed
                    if previous_status is None or current_status != previous_status:
                        status_changes.append({
                            'timestamp': item['timestamp_iso'],
                            'status': current_status
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
                timestamp = datetime.fromisoformat(status_changes[i]['timestamp'].replace('Z', '+00:00'))
                duration = current_time - timestamp
                status_changes[i]['duration'] = format_duration(duration)
            else:
                # Subsequent entries: duration from this timestamp to the previous entry's timestamp
                current_timestamp = datetime.fromisoformat(status_changes[i]['timestamp'].replace('Z', '+00:00'))
                previous_timestamp = datetime.fromisoformat(status_changes[i - 1]['timestamp'].replace('Z', '+00:00'))
                duration = previous_timestamp - current_timestamp
                status_changes[i]['duration'] = format_duration(duration)

        return status_changes

    except Exception as e:
        print(f"Error calculating status timeline from DynamoDB: {str(e)}")
        return []


def store_status(asset_key, timestamp_iso, status, provider, asset_type, error_message=None, metadata=None, metadata_changes=None):
    """Store status in DynamoDB"""
    dynamodb = get_dynamodb()
    table = dynamodb.Table('cloudmoo-prod-asset-logs')

    # Convert ISO timestamp to epoch time as Decimal
    timestamp_dt = datetime.fromisoformat(timestamp_iso.replace('Z', '+00:00'))
    timestamp_epoch = Decimal(str(calendar.timegm(timestamp_dt.utctimetuple())))

    # Convert any float values in metadata to Decimal
    if metadata:
        metadata = json.loads(json.dumps(metadata), parse_float=Decimal)

    # Convert any float values in metadata to Decimal
    if metadata_changes:
        metadata_changes = json.loads(json.dumps(metadata_changes), parse_float=Decimal)

    item = {
        'asset_key': asset_key,
        'timestamp': timestamp_epoch,  # Store as Decimal
        'timestamp_iso': timestamp_iso,
        'status': status,
        'provider': provider,
        'asset_type': asset_type,
    }

    if error_message:
        item['error_message'] = error_message

    if metadata:
        item['metadata'] = metadata

    if metadata_changes:
        item['metadata_changes'] = metadata_changes

    try:
        table.put_item(Item=item)
    except Exception as e:
        print(f"Error storing status in DynamoDB: {str(e)}")
        raise


def format_duration(duration):
    """Format a timedelta duration into a human-readable string"""
    total_seconds = int(duration.total_seconds())
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60

    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if seconds > 0 or not parts:
        parts.append(f"{seconds}s")

    return " ".join(parts)

# In base.py, update trigger_email_notification:

def trigger_email_notification(asset_key, asset_id, provider, asset_type, current_status, previous_status, status_timeline, metadata_changes=None):
    """Trigger email notification using AWS Lambda"""
    import boto3

    # Sanitize status_timeline data
    def sanitize_data(data):
        if isinstance(data, dict):
            return {k: sanitize_data(v) if v is not None else "" for k, v in data.items()}
        elif isinstance(data, list):
            return [sanitize_data(item) if item is not None else "" for item in data]
        return data if data is not None else ""

    # Sanitize the status_timeline
    sanitized_timeline = sanitize_data(status_timeline)

    lambda_client = boto3.client('lambda')
    payload = {
        'asset_key': asset_key or "",
        'asset_id': asset_id or "",
        'provider': provider or "",
        'asset_type': asset_type or "",
        'current_status': current_status or "",
        'previous_status': previous_status or "",
        'status_timeline': sanitized_timeline or [],
        'metadata_changes': metadata_changes or []
    }

    try:
        lambda_client.invoke(
            FunctionName='cloudMooEmailAssetStatusChange',
            InvocationType='Event',
            Payload=json.dumps(payload)
        )
    except Exception as e:
        print(f"Error triggering email notification: {str(e)}")
