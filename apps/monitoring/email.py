"""
Status-change notification emails.

Ported from the cloudMooEmailAssetStatusChange Lambda. SES delivery is
replaced with Django's email framework and the DynamoDB email record with the
``AssetStatusEmail`` model.
"""
import logging
import hashlib
from datetime import datetime
from html import escape

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.utils import timezone

from apps.monitoring.metadata import redact_error_message
from apps.monitoring.models import AssetStatusEmail

logger = logging.getLogger(__name__)

STATUS_COLORS = {
    'active': '#34D399',  # green
    'running': '#34D399',
    'online': '#34D399',
    'stopped': '#F87171',  # red
    'offline': '#F87171',
    'error': '#F87171',
    'pending': '#FCD34D',  # yellow
    'unknown': '#9CA3AF',  # gray
}
DEFAULT_STATUS_COLOR = '#9CA3AF'
MAX_DELIVERY_ATTEMPTS = 10


class EmailDeliveryError(Exception):
    """Raised after one or more recipients fail so Celery can retry safely."""


def build_subject(asset):
    """Build the notification subject line for an asset."""
    return f"Status Change Alert for {asset.provider_name} - {asset.name}"


def _unique_recipients(asset):
    """Return normalized, de-duplicated notification recipients."""
    return list(dict.fromkeys(
        str(recipient).strip().lower()
        for recipient in (asset.notification_emails or [])
        if str(recipient).strip()
    ))


def delivery_key_for_event(event_id, recipient):
    """Return the stable idempotency key for one event/recipient pair."""
    return hashlib.sha256(
        f"{event_id}:{recipient.lower()}".encode()
    ).hexdigest()


def ensure_status_change_email_outbox(
    asset,
    previous_status,
    current_status,
    status_timeline,
    metadata_changes=None,
    event_id=None,
):
    """Create pending delivery rows before publishing a notification task.

    This is intentionally safe to call inside the status transition database
    transaction. If the broker publish is lost after commit, the pending rows
    remain available to the periodic recovery task.
    """
    if event_id is None:
        return 0

    email_list = _unique_recipients(asset)
    if not email_list:
        return 0

    subject = build_subject(asset)
    text_body, html_body = create_email_body(
        asset,
        current_status,
        previous_status,
        status_timeline,
        metadata_changes,
    )
    content_type_id = ContentType.objects.get_for_model(asset).id

    for recipient in email_list:
        delivery, created = AssetStatusEmail.objects.get_or_create(
            delivery_key=delivery_key_for_event(event_id, recipient),
            defaults={
                'event_id': event_id,
                'asset_content_type_id': content_type_id,
                'asset_key': asset.key,
                'account_id': asset.owner.cloud.account_id,
                'asset_id': asset.id,
                'provider': asset.provider_code,
                'asset_type': asset.type,
                'recipient': recipient,
                'subject': subject,
                'text_body': text_body,
                'html_body': html_body,
                'status_previous': previous_status or '',
                'status_current': current_status or '',
                'delivery_status': 'pending',
            },
        )
        # Backfill the content type on rows created by an older deployment so
        # the recovery task can handle them when possible.
        if not created and delivery.asset_content_type_id is None:
            AssetStatusEmail.objects.filter(pk=delivery.pk).update(
                asset_content_type_id=content_type_id,
            )

    return len(email_list)


def deliver_status_email(delivery):
    """Send one outbox row while serializing concurrent attempts."""
    delivery_error = None
    with transaction.atomic():
        delivery = AssetStatusEmail.objects.select_for_update().get(pk=delivery.pk)
        if delivery.delivery_status == 'sent':
            return False
        if delivery.attempt_count >= MAX_DELIVERY_ATTEMPTS:
            delivery.delivery_status = 'dead'
            delivery.last_error = 'Maximum email delivery attempts exceeded'
            delivery.save(update_fields=['delivery_status', 'last_error'])
            logger.error(
                "Giving up on status change email %s for asset %s after %s attempts",
                delivery.pk,
                delivery.asset_key,
                delivery.attempt_count,
            )
            return False

        delivery.attempt_count += 1
        delivery.delivery_status = 'sending'
        delivery.last_attempt_at = timezone.now()
        delivery.last_error = ''
        delivery.save(update_fields=[
            'attempt_count',
            'delivery_status',
            'last_attempt_at',
            'last_error',
        ])

        try:
            message = EmailMultiAlternatives(
                subject=delivery.subject,
                body=delivery.text_body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[delivery.recipient],
            )
            message.attach_alternative(delivery.html_body, "text/html")
            if message.send() != 1:
                raise EmailDeliveryError(
                    f"Email backend reported no delivery for {delivery.recipient}"
                )
        except Exception as error:
            delivery_error = error
            delivery.delivery_status = 'failed'
            delivery.last_error = redact_error_message(error)
            delivery.save(update_fields=['delivery_status', 'last_error'])
        else:
            delivery.delivery_status = 'sent'
            delivery.sent_at = timezone.now()
            delivery.last_error = ''
            delivery.save(update_fields=['delivery_status', 'sent_at', 'last_error'])

    if delivery_error is not None:
        raise delivery_error

    logger.info(
        "Status change email sent to %s for asset %s",
        delivery.recipient,
        delivery.asset_key,
    )
    return True


def send_status_change_emails(
    asset,
    previous_status,
    current_status,
    status_timeline,
    metadata_changes=None,
    event_id=None,
):
    """
    Build and send status-change notification emails to all recipients in
    ``asset.notification_emails``. A failure for one recipient does not abort
    the others. Returns the number of emails successfully sent.
    """
    email_list = _unique_recipients(asset)
    if not email_list:
        logger.info(f"No email addresses found for asset key: {asset.key}")
        return 0

    subject = build_subject(asset)
    text_body, html_body = create_email_body(
        asset,
        current_status,
        previous_status,
        status_timeline,
        metadata_changes
    )

    if event_id is not None:
        ensure_status_change_email_outbox(
            asset,
            previous_status,
            current_status,
            status_timeline,
            metadata_changes,
            event_id=event_id,
        )
        sent = 0
        failed = []
        for recipient in email_list:
            try:
                delivery = AssetStatusEmail.objects.get(
                    delivery_key=delivery_key_for_event(event_id, recipient)
                )
                if deliver_status_email(delivery):
                    sent += 1
            except Exception as error:
                failed.append(recipient)
                logger.exception(
                    "Error sending status change email to %s for asset %s",
                    recipient,
                    asset.key,
                )

        if failed:
            raise EmailDeliveryError(
                f"Failed to deliver status-change email for {asset.key} to: {', '.join(failed)}"
            )
        return sent

    sent = 0
    failed = []
    for recipient in email_list:
        delivery = None
        delivery_key = None
        try:
            message = EmailMultiAlternatives(
                subject=subject,
                body=text_body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[recipient],
            )
            message.attach_alternative(html_body, "text/html")
            if message.send() != 1:
                raise EmailDeliveryError(
                    f"Email backend reported no delivery for {recipient}"
                )
            AssetStatusEmail.objects.create(
                asset_key=asset.key,
                account_id=asset.owner.cloud.account_id,
                asset_id=asset.id,
                asset_content_type_id=ContentType.objects.get_for_model(asset).id,
                provider=asset.provider_code,
                asset_type=asset.type,
                recipient=recipient,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
                status_previous=previous_status or '',
                status_current=current_status or '',
                delivery_status='sent',
                attempt_count=1,
                last_attempt_at=timezone.now(),
                sent_at=timezone.now(),
            )

            sent += 1
            logger.info(f"Status change email sent to {recipient} for asset {asset.key}")
        except Exception as error:
            failed.append(recipient)
            logger.exception(f"Error sending status change email to {recipient} for asset {asset.key}")

    if failed:
        raise EmailDeliveryError(
            f"Failed to deliver status-change email for {asset.key} to: {', '.join(failed)}"
        )

    return sent


def create_email_body(asset, current_status, previous_status, status_timeline, metadata_changes=None):
    """Create the plain-text and HTML bodies for a status-change email."""
    asset_info = {
        'provider_name': asset.provider_name,
        'name': asset.name,
        'type': asset.type,
        'provider_url': asset.provider_url,
        'cloudmoo_url': asset.cloudmoo_url,
    }
    unique_id = str(asset.unique_id or '')
    asset_type = str(asset_info['type'] or '')
    provider_name = str(asset_info['provider_name'] or '')
    asset_name = str(asset_info['name'] or '')
    provider_url = str(asset_info['provider_url'] or '')
    cloudmoo_url = str(asset_info['cloudmoo_url'] or '')
    current_status_text = str(current_status or '')
    previous_status_text = str(previous_status or '')

    # Format status timeline
    formatted_timeline = format_status_timeline(status_timeline)

    current_status_color = STATUS_COLORS.get(current_status_text.lower(), DEFAULT_STATUS_COLOR)
    previous_status_color = STATUS_COLORS.get(previous_status_text.lower(), DEFAULT_STATUS_COLOR)

    html_asset_type = escape(asset_type.capitalize())
    html_provider_name = escape(provider_name)
    html_asset_name = escape(asset_name)
    html_provider_url = escape(provider_url)
    html_cloudmoo_url = escape(cloudmoo_url)
    html_previous_status = escape(previous_status_text.upper())
    html_current_status = escape(current_status_text.upper())

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
            metadata_changes_html += f'<li>{escape(str(change))}</li>'
        metadata_changes_html += """
                </ul>
            </div>
            """

    BODY_TEXT = f"""
    Status Change Alert for {provider_name} {asset_type} {asset_name} ({unique_id})

    Asset Type: {asset_type.capitalize()}
    Cloud: {provider_name}
    Asset Name: {asset_name}
    Provider URL: {provider_url}
    CloudMoo URL: {cloudmoo_url}

    Previous Status: {previous_status_text}
    Current Status: {current_status_text}

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
                <h2>{html_asset_type} Status Change Alert</h2>
            </div>

            <div class="info-grid">
                <div class="info-item">
                    <div class="label">Asset Name</div>
                    <div class="value">{html_asset_name}</div>
                </div>
                <div class="info-item">
                    <div class="label">Asset Type</div>
                    <div class="value">{html_asset_type}</div>
                </div>
                <div class="info-item">
                    <div class="label">Cloud Provider</div>
                    <div class="value">{html_provider_name}</div>
                </div>
                <div class="info-item">
                    <div class="label">Provider URL</div>
                    <div class="value">{html_provider_url}</div>
                </div>
                <div class="info-item">
                    <div class="label">CloudMoo URL</div>
                    <div class="value">{html_cloudmoo_url}</div>
                </div>
            </div>

            <div style="margin-bottom: 24px;">
                <div style="margin-bottom: 12px;">
                    <div class="label">Previous Status</div>
                    <div class="status-badge" style="background-color: {previous_status_color}">
                        {html_previous_status}
                    </div>
                </div>
                <div>
                    <div class="label">Current Status</div>
                    <div class="status-badge" style="background-color: {current_status_color}">
                        {html_current_status}
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
    """Format status timeline entries into plain-text and HTML tables."""
    status_timeline = status_timeline or []
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
        # Convert timestamp to datetime object (accepts ISO strings and datetimes)
        timestamp = status['timestamp']
        if isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        # Format timestamp to show only up to minutes in UTC
        formatted_time = timestamp.strftime("%Y-%m-%d %H:%M UTC")

        # Get status color, defaulting to gray if status not found
        status_text = str(status.get('status') or '')
        duration_text = str(status.get('duration') or 'N/A')
        status_color = STATUS_COLORS.get(status_text.lower(), DEFAULT_STATUS_COLOR)

        # Add to text timeline
        text_timeline += f"{formatted_time}  {status_text:<10} {duration_text}\n"

        # Add to HTML timeline with modern styling
        html_timeline += f"""
            <tr>
                <td style="white-space: nowrap;">{formatted_time}</td>
                <td>
                    <span class="status-badge" style="background-color: {status_color}">
                        {escape(status_text.upper())}
                    </span>
                </td>
                <td class="duration-cell">
                    {escape(duration_text)}
                </td>
            </tr>
        """

    html_timeline += """
        </tbody>
    </table>
    """

    return {'text': text_timeline, 'html': html_timeline}
