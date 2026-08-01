from datetime import datetime, timezone

from apps.monitoring.models import AssetStatusEmail


class NotificationLog:
    """Wrapper around an AssetStatusEmail row for the notifications console."""

    def __init__(self, email):
        self.asset_key = email.asset_key
        self.timestamp = email.timestamp
        self.asset_id = email.asset_id
        self.provider = email.provider
        self.asset_type = email.asset_type
        self.recipient = email.recipient
        self.text_body = email.text_body
        self.html_body = email.html_body
        self.subject = email.subject or f"Status Change Alert for {email.provider} - {email.asset_type}"
        self.status_previous = email.status_previous
        self.status_current = email.status_current

    @classmethod
    def _apply_filters(cls, queryset, filters):
        """Apply the optional search filters to an AssetStatusEmail queryset."""
        if not filters:
            return queryset

        if filters.get('email'):
            queryset = queryset.filter(recipient__icontains=filters['email'])

        if filters.get('provider'):
            queryset = queryset.filter(provider=filters['provider'])

        if filters.get('asset_type'):
            queryset = queryset.filter(asset_type=filters['asset_type'])

        # Date range filtering
        if filters.get('date_from'):
            date_from = datetime.strptime(filters['date_from'], '%Y-%m-%d')
            date_from = date_from.replace(tzinfo=timezone.utc)
            queryset = queryset.filter(timestamp__gte=date_from)

        if filters.get('date_to'):
            date_to = datetime.strptime(filters['date_to'], '%Y-%m-%d')
            # Set to end of day
            date_to = date_to.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
            queryset = queryset.filter(timestamp__lte=date_to)

        return queryset

    @classmethod
    def get_logs_for_account(cls, account_id, filters=None, limit=50):
        """Get notification logs for a specific account ID with optional filters"""
        try:
            queryset = AssetStatusEmail.objects.filter(account_id=account_id)
            queryset = cls._apply_filters(queryset, filters)
            queryset = queryset.order_by('-timestamp')[:limit]
            return [cls(email) for email in queryset]
        except Exception as e:
            print(f"Error fetching notification logs: {str(e)}")
            return []

    @classmethod
    def get_logs_for_account_paginated(cls, account_id, filters=None, last_evaluated_key=None, limit=50):
        """
        Get notification logs for a specific account ID with pagination and filters.

        Returns a (logs, next_key) tuple; ``next_key`` is an opaque offset
        token that can be passed back as ``last_evaluated_key`` to fetch the
        next page (None when there are no more results).
        """
        try:
            queryset = AssetStatusEmail.objects.filter(account_id=account_id)
            queryset = cls._apply_filters(queryset, filters)
            queryset = queryset.order_by('-timestamp')

            offset = int(last_evaluated_key) if last_evaluated_key else 0
            emails = list(queryset[offset:offset + limit + 1])

            has_more = len(emails) > limit
            logs = [cls(email) for email in emails[:limit]]
            next_key = str(offset + limit) if has_more else None

            return logs, next_key

        except Exception as e:
            print(f"Error fetching notification logs: {str(e)}")
            return [], None

    @property
    def formatted_timestamp(self):
        """Get formatted timestamp for display"""
        if self.timestamp:
            try:
                return self.timestamp.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            except (ValueError, TypeError):
                pass
        return 'Unknown'

    @property
    def status_change(self):
        """Get status change information"""
        if self.status_previous or self.status_current:
            return {
                'previous': self.status_previous,
                'current': self.status_current,
            }
        return None

    def __str__(self):
        return f"NotificationLog({self.asset_key}, {self.formatted_timestamp})"
