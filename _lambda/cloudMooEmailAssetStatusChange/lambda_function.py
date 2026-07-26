import json
import os
import calendar
from decimal import Decimal
from datetime import datetime, timezone
import boto3
from botocore.exceptions import ClientError
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from boto3.dynamodb.conditions import Key


def lambda_handler(event, context):
    asset_id = event.get('asset_id')
    provider = event.get('provider')
    asset_type = event.get('asset_type')
    current_status = event.get('current_status')
    previous_status = event.get('previous_status')
    status_timeline = event.get('status_timeline')
    asset_key = event.get('asset_key')
    metadata_changes = event.get('metadata_changes', [])

    # Get asset information from DynamoDB
    asset_info = get_asset_info_by_key(asset_key)

    if not asset_info:
        print(f"No asset information found for asset key: {asset_key}")
        return {
            'statusCode': 400,
            'body': json.dumps(f"No asset information found for asset key: {asset_key}")
        }

    email_list = asset_info.get('email_list', [])

    if not email_list:
        print(f"No email addresses found for asset key: {asset_key}")
        return {
            'statusCode': 400,
            'body': json.dumps(f"No email addresses found for asset key: {asset_key}")
        }

    # Configure email parameters
    SENDER = "CloudMoo <support@cloudmoo.com>"
    SUBJECT = f"Status Change Alert for {asset_info['provider_name']} - {asset_info['name']}"

    # Create the email body
    BODY_TEXT, BODY_HTML = create_email_body(
        asset_info,
        asset_info['unique_id'],
        current_status,
        previous_status,
        status_timeline,
        metadata_changes
    )

    # Create an SES client
    ses_client = boto3.client('ses')

    # Send email to each recipient
    for recipient in email_list:
        try:
            # Create a multipart/alternative email message
            msg = MIMEMultipart('alternative')
            msg['Subject'] = SUBJECT
            msg['From'] = SENDER
            msg['To'] = recipient

            # Add plain-text and HTML parts to the message
            part1 = MIMEText(BODY_TEXT, 'plain')
            part2 = MIMEText(BODY_HTML, 'html')
            msg.attach(part1)
            msg.attach(part2)

            # Send the email
            response = ses_client.send_raw_email(
                Source=SENDER,
                Destinations=[recipient],
                RawMessage={'Data': msg.as_string()}
            )

            # Store email record in DynamoDB
            store_email_record(
                asset_id=asset_id,
                provider=provider,
                asset_type=asset_type,
                recipient=recipient,
                text_body=BODY_TEXT,
                html_body=BODY_HTML,
                metadata={
                    'message_id': response['MessageId'],
                    'subject': SUBJECT,
                    'status_change': {
                        'previous': previous_status,
                        'current': current_status
                    }
                },
                asset_key=asset_key
            )

            print(f"Email sent to {recipient}! Message ID: {response['MessageId']}")

        except ClientError as e:
            print(f"Error sending email to {recipient}: {e.response['Error']['Message']}")

    return {
        'statusCode': 200,
        'body': json.dumps("Emails sent successfully to all recipients.")
    }

def get_dynamodb():
    """Get DynamoDB resource"""
    return boto3.resource('dynamodb')

def get_asset_info_by_key(asset_key):
    """
    Get asset information from DynamoDB using asset_key
    """
    try:
        dynamodb = get_dynamodb()
        table = dynamodb.Table('cloudmoo-prod-assets')

        # Query using asset_key
        response = table.query(
            KeyConditionExpression=Key('asset_key').eq(asset_key),
            # Get the latest status by sorting in descending order and limiting to 1
            ScanIndexForward=False,
            Limit=1
        )

        if not response['Items']:
            print(f"No asset found with key: {asset_key}")
            return {}

            # Get the most recent item
        asset = response['Items'][0]

        return {
            'email_list': asset.get('notification_emails', []),
            'provider_url': asset.get('provider_url'),
            'cloudmoo_url': asset.get('cloudmoo_url'),
            'provider_code': asset['cloud']['provider']['code'],
            'provider_name': asset['cloud']['provider']['name'],
            'name': asset['name'],
            'type': asset['type'],
            'asset_id': asset['id'],
            'unique_id': asset['unique_id'],
            'cloud': asset['cloud'],  # Include full cloud info for templates
            'monitoring': asset.get('monitoring'),
            'metadata': asset.get('metadata')
        }

    except Exception as e:
        print(f"Error retrieving asset info from DynamoDB: {str(e)}")
        return {}


def create_email_body(asset_info, unique_id, current_status, previous_status, status_timeline, metadata_changes=None):
    # Format status timeline
    formatted_timeline = format_status_timeline(status_timeline)

    # Get status color for visual indication
    status_colors = {
        'active': '#34D399',  # green
        'running': '#34D399',
        'online': '#34D399',
        'stopped': '#F87171',  # red
        'offline': '#F87171',
        'error': '#F87171',
        'pending': '#FCD34D',  # yellow
        'unknown': '#9CA3AF',  # gray
    }

    current_status_color = status_colors.get(current_status.lower(), '#9CA3AF')
    previous_status_color = status_colors.get(previous_status.lower(), '#9CA3AF')

    # Format metadata changes
    metadata_changes_text = ""
    metadata_changes_html = ""

    if metadata_changes and len(metadata_changes) > 0:
        metadata_changes_text = "\nConfiguration Changes:\n"
        metadata_changes_text += "-" * 50 + "\n"
        for change in metadata_changes:
            metadata_changes_text += f"• {change}\n"

        metadata_changes_html = """
            <div class="metadata-section">
                <h3>Configuration Changes</h3>
                <ul class="changes-list">
            """
        for change in metadata_changes:
            metadata_changes_html += f'<li>{change}</li>'
        metadata_changes_html += """
                </ul>
            </div>
            """

    BODY_TEXT = f"""
    Status Change Alert for {asset_info['provider_name']} {asset_info['type']} {asset_info['name']} ({unique_id})

    Asset Type: {asset_info['type'].capitalize()}
    Cloud: {asset_info['provider_name']}
    Asset Name: {asset_info['name']}
    Provider URL: {asset_info['provider_url']}
    CloudMoo URL: {asset_info['cloudmoo_url']}

    Previous Status: {previous_status}
    Current Status: {current_status}

    Status Timeline:
    {formatted_timeline['text']}
    """

    BODY_HTML = f"""
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen-Sans, Ubuntu, Cantarell, sans-serif;
                line-height: 1.6;
                color: #374151;
                margin: 0;
                padding: 0;
                background-color: #F3F4F6;
            }}
            .container {{
                max-width: 600px;
                margin: 20px auto;
                background-color: #FFFFFF;
                border-radius: 8px;
                box-shadow: 0 1px 3px 0 rgba(0, 0, 0, 0.1);
                padding: 24px;
            }}
            .header {{
                border-bottom: 1px solid #E5E7EB;
                padding-bottom: 16px;
                margin-bottom: 24px;
            }}
            .header h2 {{
                color: #111827;
                margin: 0;
                font-size: 20px;
                font-weight: 600;
            }}
            .info-grid {{
                display: grid;
                grid-template-columns: repeat(2, 1fr);
                gap: 16px;
                margin-bottom: 24px;
            }}
            .info-item {{
                background-color: #F9FAFB;
                padding: 12px;
                border-radius: 6px;
            }}
            .label {{
                font-size: 13px;
                color: #6B7280;
                margin-bottom: 4px;
            }}
            .value {{
                font-size: 15px;
                color: #111827;
                font-weight: 500;
            }}
            .status-badge {{
                display: inline-block;
                padding: 4px 12px;
                border-radius: 9999px;
                font-size: 14px;
                font-weight: 500;
                color: #FFFFFF;
            }}
            .timeline-section {{
                margin-top: 24px;
                background-color: #F9FAFB;
                border-radius: 6px;
                padding: 16px;
            }}
            .timeline-section h3 {{
                margin: 0 0 16px 0;
                font-size: 16px;
                color: #374151;
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
                font-size: 14px;
            }}
            th {{
                background-color: #F3F4F6;
                text-align: left;
                padding: 12px;
                font-weight: 600;
                color: #374151;
            }}
            td {{
                padding: 12px;
                border-top: 1px solid #E5E7EB;
            }}
            .button {{
                display: inline-block;
                padding: 10px 20px;
                background-color: #3B82F6;
                color: #FFFFFF;
                text-decoration: none;
                border-radius: 6px;
                font-weight: 500;
                margin-top: 16px;
            }}
            @media (max-width: 600px) {{
                .container {{
                    margin: 0;
                    border-radius: 0;
                }}
                .info-grid {{
                    grid-template-columns: 1fr;
                }}
            }}
            .metadata-section {{
                margin-top: 24px;
                background-color: #F9FAFB;
                border-radius: 6px;
                padding: 16px;
            }}
            .metadata-section h3 {{
                margin: 0 0 16px 0;
                font-size: 16px;
                color: #374151;
            }}
            .changes-list {{
                margin: 0;
                padding-left: 20px;
            }}
            .changes-list li {{
                margin-bottom: 8px;
                color: #374151;
                font-size: 14px;
            }}
            .changes-list li:last-child {{
                margin-bottom: 0;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h2>{asset_info['type'].capitalize()} Status Change Alert</h2>
            </div>

            <div class="info-grid">
                <div class="info-item">
                    <div class="label">Asset Name</div>
                    <div class="value">{asset_info['name']}</div>
                </div>
                <div class="info-item">
                    <div class="label">Asset Type</div>
                    <div class="value">{asset_info['type'].capitalize()}</div>
                </div>
                <div class="info-item">
                    <div class="label">Cloud Provider</div>
                    <div class="value">{asset_info['provider_name']}</div>
                </div>
                <div class="info-item">
                    <div class="label">Provider URL</div>
                    <div class="value">{asset_info['provider_url']}</div>
                </div>
                <div class="info-item">
                    <div class="label">CloudMoo URL</div>
                    <div class="value">{asset_info['cloudmoo_url']}</div>
                </div>
            </div>

            <div style="margin-bottom: 24px;">
                <div style="margin-bottom: 12px;">
                    <div class="label">Previous Status</div>
                    <div class="status-badge" style="background-color: {previous_status_color}">
                        {previous_status.upper()}
                    </div>
                </div>
                <div>
                    <div class="label">Current Status</div>
                    <div class="status-badge" style="background-color: {current_status_color}">
                        {current_status.upper()}
                    </div>
                </div>
            </div>
            
            {metadata_changes_html if metadata_changes and len(metadata_changes) > 0 else ''}

            <div class="timeline-section">
                <h3>Status Timeline</h3>
                {formatted_timeline['html']}
            </div>
        </div>
    </body>
    </html>
    """

    return BODY_TEXT, BODY_HTML


def format_status_timeline(status_timeline):
    # Status color mapping
    status_colors = {
        'active': '#34D399',  # green
        'running': '#34D399',
        'online': '#34D399',
        'stopped': '#F87171',  # red
        'offline': '#F87171',
        'error': '#F87171',
        'pending': '#FCD34D',  # yellow
        'unknown': '#9CA3AF',  # gray
    }

    # Plain text version
    text_timeline = "Timestamp (UTC)      Status     Duration\n"
    text_timeline += "-" * 50 + "\n"

    # Modern HTML version
    html_timeline = """
    <style>
        .timeline-table {
            width: 100%;
            border-collapse: separate;
            border-spacing: 0;
            margin-top: 8px;
            font-size: 14px;
        }
        .timeline-table th {
            background-color: #F3F4F6;
            padding: 12px 16px;
            text-align: left;
            font-weight: 600;
            color: #374151;
            border-bottom: 2px solid #E5E7EB;
        }
        .timeline-table td {
            padding: 12px 16px;
            border-bottom: 1px solid #E5E7EB;
            color: #374151;
        }
        .timeline-table tr:last-child td {
            border-bottom: none;
        }
        .timeline-table tr:hover td {
            background-color: #F9FAFB;
        }
        .status-badge {
            display: inline-block;
            padding: 4px 12px;
            border-radius: 9999px;
            font-size: 13px;
            font-weight: 500;
            color: #FFFFFF;
        }
        .duration-cell {
            color: #6B7280;
            font-size: 13px;
        }
        @media (max-width: 600px) {
            .timeline-table {
                font-size: 13px;
            }
            .timeline-table th,
            .timeline-table td {
                padding: 8px 12px;
            }
            .status-badge {
                padding: 3px 8px;
                font-size: 12px;
            }
        }
    </style>
    <table class="timeline-table">
        <thead>
            <tr>
                <th>Timestamp (UTC)</th>
                <th>Status</th>
                <th>Duration</th>
            </tr>
        </thead>
        <tbody>
    """

    for status in status_timeline:
        # Convert timestamp to datetime object
        timestamp = datetime.fromisoformat(status['timestamp'].replace('Z', '+00:00'))
        # Format timestamp to show only up to minutes in UTC
        formatted_time = timestamp.strftime("%Y-%m-%d %H:%M UTC")

        # Get status color, defaulting to gray if status not found
        status_color = status_colors.get(status['status'].lower(), '#9CA3AF')

        # Add to text timeline
        text_timeline += f"{formatted_time}  {status['status']:<10} {status['duration'] if status['duration'] else 'N/A'}\n"

        # Add to HTML timeline with modern styling
        html_timeline += f"""
            <tr>
                <td style="white-space: nowrap;">{formatted_time}</td>
                <td>
                    <span class="status-badge" style="background-color: {status_color}">
                        {status['status'].upper()}
                    </span>
                </td>
                <td class="duration-cell">
                    {status['duration'] if status['duration'] else 'N/A'}
                </td>
            </tr>
        """

    html_timeline += """
        </tbody>
    </table>
    """

    return {'text': text_timeline, 'html': html_timeline}


def store_email_record(asset_id, provider, asset_type, recipient, text_body, html_body, metadata, asset_key):
    """
    Store email record in DynamoDB cloudmoo-prod-asset-emails table
    """
    try:
        dynamodb = get_dynamodb()
        table = dynamodb.Table('cloudmoo-prod-asset-emails')

        # Get current timestamp in epoch format
        current_timestamp = Decimal(str(calendar.timegm(datetime.now(timezone.utc).timetuple())))
        timestamp_iso = datetime.now(timezone.utc).isoformat()

        # Prepare email record
        email_record = {
            'asset_key': asset_key,  # Partition key
            'timestamp': current_timestamp,  # Sort key
            'timestamp_iso': timestamp_iso,  # Human-readable timestamp
            'asset_id': str(asset_id),
            'provider': provider,
            'asset_type': asset_type,
            'recipient': recipient,
            'text_body': text_body,
            'html_body': html_body,
            'metadata': json.loads(json.dumps(metadata))  # Ensure proper JSON serialization
        }

        # Remove any None values
        email_record = {k: v for k, v in email_record.items() if v is not None}

        # Convert any float values to Decimal
        def convert_floats(obj):
            if isinstance(obj, float):
                return Decimal(str(obj))
            elif isinstance(obj, dict):
                return {k: convert_floats(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [convert_floats(v) for v in obj]
            return obj

        email_record = convert_floats(email_record)

        # Store in DynamoDB
        table.put_item(Item=email_record)

        print(f"Email record stored successfully for asset_key: {asset_key}")
        return True

    except Exception as e:
        print(f"Error storing email record in DynamoDB: {str(e)}")
        return False
