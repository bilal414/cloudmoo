"""Account-scoped API for the CloudMoo mobile apps (iOS/Android).

Authentication is DRF token auth (``Authorization: Token <key>``) with the
same email+password semantics as the web console.  Every endpoint is scoped
to the requesting member's active account, mirroring the console's
``for_user`` managers.  Provider credentials are never serialized.
"""
import logging
from datetime import timedelta

from django.contrib.auth import authenticate
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.db.models import Count
from rest_framework import status as drf_status
from rest_framework.authtoken.models import Token
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.console.account.models import CoreAccountMembership
from apps.console.asset.registry import get_asset_model
from apps.console.cloud.models import CoreCloud
from apps.console.member.models import CoreMember
from apps.console.utils.models import UtilAsset
from apps.monitoring.health import (
    HEALTH_DOWN,
    HEALTH_HEALTHY,
    HEALTH_WARNING,
    calculate_uptime,
    classify_health,
)
from apps.monitoring.checks.base import NON_ALERTING_STATUSES
from apps.monitoring.metadata import redact_sensitive_metadata
from apps.monitoring.models import (
    AssetMonitoringState,
    AssetStatusEmail,
    AssetStatusLog,
)
from apps.monitoring.tasks import check_asset_status_now, queue_cloud_sync

from .throttling import MobileLoginThrottle
from .serializers import (
    AccountUpdateSerializer,
    AssetUpdateSerializer,
    CloudUpdateSerializer,
    MobileLoginSerializer,
)

logger = logging.getLogger(__name__)

HEALTH_BUCKET_ORDER = (HEALTH_HEALTHY, HEALTH_WARNING, HEALTH_DOWN)

CLOUD_HEALTH_BY_STATUS = {
    CoreCloud.Status.ACTIVE: 'connected',
    CoreCloud.Status.INVALID_AUTH: 'attention',
    CoreCloud.Status.PAUSED: 'disconnected',
    CoreCloud.Status.SUSPENDED: 'disconnected',
    CoreCloud.Status.DELETE: 'disconnected',
}


class MobilePagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = 'page_size'
    max_page_size = 100


def _member_for(user):
    member = getattr(user, 'member', None)
    if member is None or member.active_account_id is None:
        return None
    return member


def _account_for(request):
    member = _member_for(request.user)
    return (member.active_account if member else None), member


def _role_label(member, account):
    membership = CoreAccountMembership.objects.filter(
        account=account, member=member
    ).first()
    return (
        membership.get_role_display() if membership else None
    )


def _installation_payload(request):
    from django.conf import settings

    return {
        'name': request.get_host(),
        'version': getattr(settings, 'CLOUDMOO_VERSION', '1.0.0'),
        'environment': 'Development' if settings.DEBUG else 'Production',
    }


def _user_payload(user, member, account):
    name = user.get_full_name() or user.username
    return {
        'name': name,
        'email': user.email,
        'role': _role_label(member, account) if account else None,
        'account': account.name if account else None,
    }


def _cloud_asset_counts(cloud):
    """Aggregate monitoring-state counts across all of a cloud's relations."""
    counts = {'total': 0, 'active': 0, 'monitored': 0, 'disabled': 0, 'gone': 0}
    try:
        provider_account = cloud.provider_account
    except (AttributeError, NotImplementedError):
        return counts
    for relation, _asset_type in cloud._asset_relations_for_provider():
        manager = getattr(provider_account, relation, None)
        if manager is None:
            continue
        for row in manager.values('monitoring').annotate(n=Count('id')):
            counts['total'] += row['n']
            if row['monitoring'] == UtilAsset.Monitoring.ACTIVE:
                counts['active'] += row['n']
                counts['monitored'] += row['n']
            elif row['monitoring'] == UtilAsset.Monitoring.DISABLED:
                counts['active'] += row['n']
                counts['disabled'] += row['n']
            elif row['monitoring'] == UtilAsset.Monitoring.NO_LONGER_EXISTS:
                counts['gone'] += row['n']
    return counts


def _serialize_cloud(cloud, with_counts=False):
    payload = {
        'uuid': str(cloud.uuid),
        'name': cloud.name,
        'provider': cloud.provider.code.lower(),
        'provider_name': cloud.provider.name,
        'status': cloud.status,
        'health': CLOUD_HEALTH_BY_STATUS.get(cloud.status, 'disconnected'),
        'last_synced': cloud.last_synced,
        'syncing': cloud.sync_in_progress,
    }
    if with_counts:
        payload['asset_counts'] = _cloud_asset_counts(cloud)
    return payload


def _asset_region(asset):
    region = getattr(asset, 'region', None)
    if isinstance(region, str) and region:
        return region
    metadata = asset.metadata if isinstance(asset.metadata, dict) else {}
    for key in ('region', 'Region'):
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict) and isinstance(value.get('name'), str):
            return value['name']
    return ''


def _serialize_asset(asset, state=None):
    monitoring_state = state if state is not None else asset.monitoring_state
    stale = asset.monitoring_stale
    status_value = asset.status
    return {
        'id': asset.pk,
        'uuid': str(asset.uuid),
        'key': asset.key,
        'name': asset.name,
        'identifier': asset.unique_id,
        'type': asset.type,
        'provider': asset.provider_code,
        'provider_name': asset.provider_name,
        'region': _asset_region(asset),
        'status': status_value,
        'health': classify_health(status_value, asset.monitoring, stale),
        'monitoring': asset.monitoring,
        'monitoring_stale': stale,
        'last_checked_at': monitoring_state.last_checked_at if monitoring_state else None,
    }


def _states_for_assets(assets):
    keys = [asset.key for asset in assets]
    return {
        state.asset_key: state
        for state in AssetMonitoringState.objects.filter(asset_key__in=keys)
    }


def _get_scoped_asset(request, provider, asset_type, asset_id):
    asset_class = get_asset_model(provider, asset_type)
    if asset_class is None:
        return None
    return get_object_or_404(
        asset_class.objects.for_user(request.user), id=asset_id
    )


class LoginView(APIView):
    """Exchange console email+password credentials for an API token."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [MobileLoginThrottle]

    def post(self, request):
        serializer = MobileLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data['email']
        password = serializer.validated_data['password']

        try:
            member = CoreMember.objects.select_related('user').get(
                user__email__iexact=email
            )
        except CoreMember.DoesNotExist:
            member = None

        user = None
        if member is not None:
            user = authenticate(
                request, username=member.user.username, password=password
            )

        if user is None:
            return Response(
                {'error': 'Invalid email or password', 'code': 'invalid_credentials'},
                status=drf_status.HTTP_400_BAD_REQUEST,
            )
        if not member.email_verified:
            return Response(
                {
                    'error': 'Please verify your email address to continue',
                    'code': 'email_not_verified',
                },
                status=drf_status.HTTP_403_FORBIDDEN,
            )
        if user.groups.filter(name='two-factor-app').exists():
            # Never bypass a second factor: token login for 2FA accounts needs
            # a TOTP step that this endpoint does not implement yet.
            return Response(
                {
                    'error': 'This account requires two-factor authentication, '
                             'which the mobile app does not support yet.',
                    'code': 'two_factor_required',
                },
                status=drf_status.HTTP_403_FORBIDDEN,
            )

        token, _created = Token.objects.get_or_create(user=user)
        account = member.active_account
        return Response({
            'token': token.key,
            'user': _user_payload(user, member, account),
            'installation': _installation_payload(request),
        })


class LogoutView(APIView):
    """Revoke the token used for this request."""

    def post(self, request):
        token = getattr(request, 'auth', None)
        if token is not None:
            Token.objects.filter(key=getattr(token, 'key', token)).delete()
        return Response(status=drf_status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    def get(self, request):
        account, member = _account_for(request)
        if member is None:
            return Response(
                {'error': 'This user has no CloudMoo account membership'},
                status=drf_status.HTTP_403_FORBIDDEN,
            )
        return Response({
            'user': _user_payload(request.user, member, account),
            'installation': _installation_payload(request),
        })


class OverviewView(APIView):
    """Dashboard snapshot: counts, health buckets, incidents, last sync."""

    def get(self, request):
        account, member = _account_for(request)
        if member is None:
            return Response(
                {'error': 'This user has no CloudMoo account membership'},
                status=drf_status.HTTP_403_FORBIDDEN,
            )

        clouds = list(
            CoreCloud.objects.for_user(request.user).select_related('provider')
        )

        asset_totals = {'total': 0, 'active': 0, 'monitored': 0, 'disabled': 0, 'gone': 0}
        for cloud in clouds:
            counts = _cloud_asset_counts(cloud)
            for key in asset_totals:
                asset_totals[key] += counts[key]

        # Health buckets from the durable per-asset monitoring state.
        interval_minutes = max(1, account.monitoring_interval)
        stale_after = timezone.now() - timedelta(minutes=max(5, interval_minutes * 3))
        health = {bucket: 0 for bucket in HEALTH_BUCKET_ORDER}
        health['unknown'] = 0
        for state in AssetMonitoringState.objects.filter(account_id=account.id):
            stale = (
                state.last_checked_at is None or state.last_checked_at < stale_after
            )
            bucket = classify_health(
                state.last_status, UtilAsset.Monitoring.ACTIVE, stale
            )
            health[bucket] = health.get(bucket, 0) + 1

        day_ago = timezone.now() - timedelta(hours=24)
        incidents = 0
        for status_value in (
            AssetStatusLog.objects
            .filter(account_id=account.id, timestamp__gte=day_ago)
            .exclude(status__in=NON_ALERTING_STATUSES)
            .values_list('status', flat=True)
        ):
            if classify_health(status_value) == HEALTH_DOWN:
                incidents += 1

        last_sync = None
        for cloud in clouds:
            if cloud.last_synced and (last_sync is None or cloud.last_synced > last_sync):
                last_sync = cloud.last_synced

        observed = sum(health[bucket] for bucket in HEALTH_BUCKET_ORDER)
        uptime_percentage = (
            round(100 * (health[HEALTH_HEALTHY] + health[HEALTH_WARNING]) / observed, 2)
            if observed else None
        )

        return Response({
            'clouds': {
                'total': len(clouds),
                'active': sum(1 for c in clouds if c.status == CoreCloud.Status.ACTIVE),
                'invalid_auth': sum(
                    1 for c in clouds if c.status == CoreCloud.Status.INVALID_AUTH
                ),
                'syncing': sum(1 for c in clouds if c.sync_in_progress),
            },
            'assets': asset_totals,
            'health': health,
            'incidents_last_24h': incidents,
            'uptime_percentage': uptime_percentage,
            'last_sync': last_sync,
            'providers': [_serialize_cloud(cloud, with_counts=True) for cloud in clouds],
        })


class CloudListView(APIView):
    def get(self, request):
        clouds = (
            CoreCloud.objects.for_user(request.user)
            .select_related('provider')
            .order_by('provider__code')
        )
        return Response({
            'count': clouds.count(),
            'results': [_serialize_cloud(cloud, with_counts=True) for cloud in clouds],
        })


class CloudDetailView(APIView):
    def get_cloud(self, request, cloud_uuid):
        return get_object_or_404(
            CoreCloud.objects.for_user(request.user).select_related('provider'),
            uuid=cloud_uuid,
        )

    def get(self, request, cloud_uuid):
        cloud = self.get_cloud(request, cloud_uuid)
        payload = _serialize_cloud(cloud, with_counts=True)

        # Per-type breakdown for the detail sheet.
        counts_by_type = {}
        try:
            provider_account = cloud.provider_account
        except (AttributeError, NotImplementedError):
            provider_account = None
        if provider_account is not None:
            for relation, asset_type in cloud._asset_relations_for_provider():
                manager = getattr(provider_account, relation, None)
                if manager is None:
                    continue
                active = manager.exclude(
                    monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
                ).count()
                if active:
                    counts_by_type[asset_type] = (
                        counts_by_type.get(asset_type, 0) + active
                    )
        payload['asset_counts_by_type'] = counts_by_type

        recent_runs = [
            {
                'uuid': str(run.uuid),
                'status': run.status,
                'started_at': run.started_at,
                'finished_at': run.finished_at,
                'families_total': run.families_total,
                'families_completed': run.families_completed,
                'family_errors': run.family_errors,
            }
            for run in cloud.sync_runs()[:5]
        ]
        payload['recent_sync_runs'] = recent_runs
        return Response(payload)

    def patch(self, request, cloud_uuid):
        cloud = self.get_cloud(request, cloud_uuid)
        serializer = CloudUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if data.get('name'):
            provider_account = cloud.provider_account
            provider_account.name = data['name']
            provider_account.save(update_fields=['name'])

        action = data.get('action')
        if action == 'pause':
            cloud.status = CoreCloud.Status.PAUSED
            cloud.save()
            cloud.delete_all_asset_schedules()
        elif action == 'resume':
            cloud.status = CoreCloud.Status.ACTIVE
            cloud.save()
            cloud.create_all_asset_schedules()

        cloud.refresh_from_db()
        return Response(_serialize_cloud(cloud, with_counts=True))


class CloudSyncView(APIView):
    """Queue a background inventory sync for one cloud."""

    def post(self, request, cloud_uuid):
        cloud = get_object_or_404(
            CoreCloud.objects.for_user(request.user), uuid=cloud_uuid
        )
        result = queue_cloud_sync(cloud)
        payload = {
            'success': result.get('success', False),
            'queued': result.get('queued', False),
            'message': result.get('message', ''),
            'syncing': cloud.sync_in_progress,
        }
        if result.get('run_uuid'):
            payload['run_uuid'] = result['run_uuid']
        return Response(
            payload,
            status=(
                drf_status.HTTP_200_OK if payload['success']
                else drf_status.HTTP_500_INTERNAL_SERVER_ERROR
            ),
        )


class AssetListView(APIView):
    """Cross-cloud asset inventory with search, filters, and pagination."""

    def get(self, request):
        clouds = CoreCloud.objects.for_user(request.user).select_related('provider')

        cloud_filter = request.GET.get('cloud', '').strip()
        if cloud_filter:
            clouds = clouds.filter(uuid=cloud_filter)
        provider_filter = request.GET.get('provider', '').strip()
        if provider_filter:
            clouds = clouds.filter(provider__code=provider_filter)

        assets = []
        for cloud in clouds:
            try:
                assets.extend(asset for asset, _t in cloud.get_active_assets())
            except NotImplementedError:
                continue

        query = request.GET.get('q', '').strip().lower()
        if query:
            assets = [
                asset for asset in assets
                if query in asset.name.lower() or query in asset.unique_id.lower()
            ]
        type_filter = request.GET.get('type', '').strip()
        if type_filter:
            assets = [asset for asset in assets if asset.type == type_filter]
        monitoring_filter = request.GET.get('monitoring', '').strip()
        if monitoring_filter:
            assets = [asset for asset in assets if asset.monitoring == monitoring_filter]

        assets.sort(key=lambda asset: asset.created, reverse=True)

        paginator = MobilePagination()
        page = paginator.paginate_queryset(assets, request)
        UtilAsset.get_bulk_statuses(page)
        states = _states_for_assets(page)
        results = [
            _serialize_asset(asset, states.get(asset.key)) for asset in page
        ]

        health_filter = request.GET.get('health', '').strip()
        if health_filter:
            results = [row for row in results if row['health'] == health_filter]

        return paginator.get_paginated_response(results)


class AssetDetailView(APIView):
    def get(self, request, provider, asset_type, asset_id):
        asset = _get_scoped_asset(request, provider, asset_type, asset_id)
        if asset is None:
            return Response(
                {'error': 'Unsupported asset type or provider'},
                status=drf_status.HTTP_404_NOT_FOUND,
            )

        payload = _serialize_asset(asset)
        payload.update({
            'metadata': redact_sensitive_metadata(asset.metadata) if asset.metadata else {},
            'notes': asset.notes or '',
            'notification_emails': asset.notification_emails,
            'monitoring_supported': asset.monitoring_supported,
            'provider_url': asset.provider_url,
            'uptime_30d': calculate_uptime(asset.key, days=30),
            'cloud': {
                'uuid': str(asset.owner.cloud.uuid),
                'name': asset.owner.cloud.name,
            },
            'created': asset.created,
        })

        timeline_page = request.GET.get('timeline_page', 1)
        try:
            timeline_page = max(1, int(timeline_page))
        except (TypeError, ValueError):
            timeline_page = 1
        timeline = asset.get_status_timeline_paginated(page=timeline_page, page_size=25)
        payload['timeline'] = {
            'items': [
                {
                    'timestamp': entry['timestamp'],
                    'status': entry['status'],
                    'health': classify_health(entry['status']),
                    'duration': entry.get('duration'),
                    'metadata_changes': entry.get('metadata_changes') or [],
                }
                for entry in timeline['items']
            ],
            'has_next': timeline['has_next'],
            'has_previous': timeline['has_previous'],
            'total_pages': timeline['total_pages'],
            'current_page': timeline['current_page'],
        }
        return Response(payload)

    def patch(self, request, provider, asset_type, asset_id):
        asset = _get_scoped_asset(request, provider, asset_type, asset_id)
        if asset is None:
            return Response(
                {'error': 'Unsupported asset type or provider'},
                status=drf_status.HTTP_404_NOT_FOUND,
            )
        serializer = AssetUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if data.get('monitoring'):
            asset.monitoring = data['monitoring']
            # save() syncs the status-check schedule with the monitoring state
            asset.save()
        if 'notification_emails' in data:
            try:
                asset.update_email_config(data['notification_emails'])
            except ValidationError as error:
                return Response(
                    {'error': error.messages[0] if error.messages else 'Invalid email list'},
                    status=drf_status.HTTP_400_BAD_REQUEST,
                )

        payload = _serialize_asset(asset)
        payload['notification_emails'] = asset.notification_emails
        return Response(payload)


class AssetCheckView(APIView):
    """Run an immediate status check (console "Check status now" semantics)."""

    def post(self, request, provider, asset_type, asset_id):
        asset = _get_scoped_asset(request, provider, asset_type, asset_id)
        if asset is None:
            return Response(
                {'error': 'Unsupported asset type or provider'},
                status=drf_status.HTTP_404_NOT_FOUND,
            )
        result = check_asset_status_now(asset)
        if hasattr(asset, '_cached_status'):
            delattr(asset, '_cached_status')
        return Response({
            'success': True,
            'status': result.get('status', 'unknown'),
            'health': classify_health(result.get('status'), asset.monitoring),
            'timestamp': result.get('timestamp'),
            'metadata_changes': result.get('metadata_changes') or [],
            'error': result.get('error'),
        })


class AssetMonitoringView(APIView):
    """Pause or resume monitoring for one asset."""

    def post(self, request, provider, asset_type, asset_id, action):
        asset = _get_scoped_asset(request, provider, asset_type, asset_id)
        if asset is None:
            return Response(
                {'error': 'Unsupported asset type or provider'},
                status=drf_status.HTTP_404_NOT_FOUND,
            )
        asset.monitoring = (
            UtilAsset.Monitoring.ACTIVE if action == 'resume'
            else UtilAsset.Monitoring.DISABLED
        )
        asset.save()
        return Response(_serialize_asset(asset))


class ActivityListView(APIView):
    """Status-change log feed for the account."""

    def get(self, request):
        account, member = _account_for(request)
        if member is None:
            return Response(
                {'error': 'This user has no CloudMoo account membership'},
                status=drf_status.HTTP_403_FORBIDDEN,
            )

        logs = AssetStatusLog.objects.filter(account_id=account.id)
        status_filter = request.GET.get('status', '').strip()
        if status_filter:
            logs = logs.filter(status=status_filter)
        provider_filter = request.GET.get('provider', '').strip()
        if provider_filter:
            logs = logs.filter(provider=provider_filter)
        type_filter = request.GET.get('type', '').strip()
        if type_filter:
            logs = logs.filter(asset_type=type_filter)

        paginator = MobilePagination()
        page = paginator.paginate_queryset(logs, request)
        results = [
            {
                'id': log.id,
                'asset_key': log.asset_key,
                'provider': log.provider,
                'asset_type': log.asset_type,
                'status': log.status,
                'health': classify_health(log.status),
                'timestamp': log.timestamp,
                'error_message': log.error_message,
                'metadata_changes': log.metadata_changes or [],
            }
            for log in page
        ]
        return paginator.get_paginated_response(results)


class NotificationListView(APIView):
    """Status-change alert emails recorded for the account."""

    def get(self, request):
        account, member = _account_for(request)
        if member is None:
            return Response(
                {'error': 'This user has no CloudMoo account membership'},
                status=drf_status.HTTP_403_FORBIDDEN,
            )

        emails = AssetStatusEmail.objects.filter(account_id=account.id)
        paginator = MobilePagination()
        page = paginator.paginate_queryset(emails, request)
        results = [
            {
                'id': email.id,
                'asset_key': email.asset_key,
                'provider': email.provider,
                'asset_type': email.asset_type,
                'recipient': email.recipient,
                'subject': email.subject,
                'status_previous': email.status_previous,
                'status_current': email.status_current,
                'health': classify_health(email.status_current) if email.status_current else None,
                'delivery_status': email.delivery_status,
                'timestamp': email.timestamp,
            }
            for email in page
        ]
        return paginator.get_paginated_response(results)


class AccountView(APIView):
    def get(self, request):
        account, member = _account_for(request)
        if member is None:
            return Response(
                {'error': 'This user has no CloudMoo account membership'},
                status=drf_status.HTTP_403_FORBIDDEN,
            )
        return Response(self._payload(account, member))

    def patch(self, request):
        account, member = _account_for(request)
        if member is None:
            return Response(
                {'error': 'This user has no CloudMoo account membership'},
                status=drf_status.HTTP_403_FORBIDDEN,
            )
        membership = CoreAccountMembership.objects.filter(
            account=account, member=member
        ).first()
        if membership is None or membership.role != CoreAccountMembership.Role.OWNER:
            return Response(
                {'error': 'Only the account owner can update account settings'},
                status=drf_status.HTTP_403_FORBIDDEN,
            )
        serializer = AccountUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        account.name = serializer.validated_data['name']
        account.save(update_fields=['name', 'updated_at'])
        return Response(self._payload(account, member))

    @staticmethod
    def _payload(account, member):
        return {
            'name': account.name,
            'role': _role_label(member, account),
            'monitoring_interval': account.monitoring_interval,
            'log_retention_days': account.log_retention_days,
            'members_count': account.memberships.count(),
            'clouds_count': account.clouds.count(),
        }
