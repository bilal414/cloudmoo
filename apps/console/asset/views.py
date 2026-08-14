import csv
import json
import logging

from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.http import require_POST
from django.views.generic import DetailView, ListView

from apps.console.cloud.models import CoreCloud, CoreCloudServiceProvider
from apps.console.utils.models import UtilAsset
from apps.monitoring.tasks import check_asset_status_now

from .registry import (
    get_asset_model,
    # Re-exported for tests and callers that verify the registry composition.
    _AWS_PRIORITY0_ASSET_MODELS,
    _AWS_PRIORITY1_ASSET_MODELS,
    _AWS_PRIORITY2_ASSET_MODELS,
)

logger = logging.getLogger(__name__)
PAGE_SIZE_OPTIONS = (10, 25, 50)
TIMELINE_PAGE_SIZE_OPTIONS = (10, 25, 50, 100, 200)
ASSET_SORT_FIELDS = {'created', 'monitoring', 'name', 'type'}


def _validated_page_size(raw_value, options, default):
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return default
    return value if value in options else default


def _safe_csv_cell(value):
    """Prevent spreadsheet software from interpreting exported text as a formula."""
    text = str(value)
    if text.lstrip().startswith(('=', '+', '-', '@')) or text.startswith(('\t', '\r')):
        return f"'{text}"
    return text


class AssetsListView(ListView):
    template_name = 'console/asset/list.html'
    paginate_by = 10
    ordering = '-created'  # Default ordering

    def get_queryset(self):
        user = self.request.user
        clouds = CoreCloud.objects.for_user(user)
        assets = []

        # Get sort parameters
        sort_by = self.request.GET.get('sort', 'created')
        if sort_by not in ASSET_SORT_FIELDS:
            sort_by = 'created'
        sort_direction = self.request.GET.get('direction', 'desc')
        if sort_direction not in {'asc', 'desc'}:
            sort_direction = 'desc'

        # Apply cloud filter
        cloud_filter = self.request.GET.get('cloud', '')
        if cloud_filter:
            clouds = clouds.filter(id=cloud_filter)

        # Apply provider filter
        provider_filter = self.request.GET.get('provider', '')
        if provider_filter:
            clouds = clouds.filter(provider__code=provider_filter)

        for cloud in clouds:
            try:
                assets.extend(asset for asset, _asset_type in cloud.get_active_assets())
            except NotImplementedError:
                continue

        # Apply filters
        search_query = self.request.GET.get('search', '')
        monitoring_filter = self.request.GET.get('monitoring', '')
        type_filter = self.request.GET.get('type', '')

        if search_query:
            search_query = search_query.strip()
            assets = [asset for asset in assets if
                      search_query.lower() in asset.name.lower() or
                      search_query in asset.unique_id]

        if monitoring_filter:
            assets = [asset for asset in assets if asset.monitoring == monitoring_filter]

        if type_filter:
            assets = [asset for asset in assets if asset.type == type_filter]

        # Apply sorting
        reverse = sort_direction == 'desc'
        assets.sort(key=lambda x: getattr(x, sort_by, ''), reverse=reverse)

        return assets

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        assets = self.object_list

        # Get the page size from request or use default
        page_size = _validated_page_size(
            self.request.GET.get('page_size'),
            PAGE_SIZE_OPTIONS,
            self.paginate_by,
        )

        paginator = Paginator(assets, page_size)
        page_number = self.request.GET.get('page')
        page_obj = paginator.get_page(page_number)

        # Optimization: Pre-fetch statuses for assets on current page
        # This prevents N+1 queries when template accesses asset.status
        if page_obj.object_list:
            UtilAsset.get_bulk_statuses(page_obj.object_list)

        # Get current query parameters
        query_params = self.request.GET.copy()
        if 'page' in query_params:
            del query_params['page']

        context.update({
            'page_obj': page_obj,
            'asset_count': len(assets),
            'providers': CoreCloudServiceProvider.objects.filter(
                status=CoreCloudServiceProvider.Status.ACTIVE
            ),
            'asset_types': UtilAsset.Type.choices,
            'monitoring_choices': UtilAsset.Monitoring.choices,
            'clouds': CoreCloud.objects.for_user(self.request.user),
            'page_size': page_size,
            'page_size_options': PAGE_SIZE_OPTIONS,
            'sort_by': (
                self.request.GET.get('sort')
                if self.request.GET.get('sort') in ASSET_SORT_FIELDS
                else 'created'
            ),
            'sort_direction': (
                self.request.GET.get('direction')
                if self.request.GET.get('direction') in {'asc', 'desc'}
                else 'desc'
            ),
            'query_params': query_params.urlencode(),
        })

        return context


class AssetDetailView(DetailView):
    template_name = 'console/asset/detail.html'
    context_object_name = 'asset'

    def get_object(self):
        provider_code = self.kwargs.get('provider_code')
        asset_type = self.kwargs.get('asset_type')
        asset_id = self.kwargs.get('asset_id')

        asset_class = get_asset_model(provider_code, asset_type)
        if asset_class is None:
            raise Http404("Unsupported asset type or provider")

        try:
            return get_object_or_404(asset_class.objects.for_user(self.request.user), id=asset_id)
        except asset_class.DoesNotExist:
            raise Http404("Asset not found")

    def get(self, request, *args, **kwargs):
        if request.GET.get('export') == 'csv':
            return self.export_timeline_csv(request)
        return super().get(request, *args, **kwargs)

    def export_timeline_csv(self, request):
        asset = self.get_object()
        timeline = asset.get_status_timeline()

        response = HttpResponse(content_type='text/csv; charset=utf-8')
        filename = (
            f'status_timeline_{asset.uuid}_'
            f'{timezone.now().strftime("%Y%m%d_%H%M%S")}.csv'
        )
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        writer.writerow(['Timestamp', 'Status', 'Duration', 'Changes'])

        for entry in timeline:
            # Format the timestamp in the user's timezone if available
            timestamp = entry['timestamp'].strftime("%Y-%m-%d %H:%M:%S %Z")

            # Join multiple metadata changes with semicolons
            changes = '; '.join(entry.get('metadata_changes', [])) or 'No changes'

            writer.writerow([
                _safe_csv_cell(timestamp),
                _safe_csv_cell(entry['status']),
                _safe_csv_cell(entry.get('duration', 'N/A')),
                _safe_csv_cell(changes),
            ])

        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['cloud'] = self.object.owner.cloud
        context['monitoring_status'] = self.object.monitoring
        context['email_list'] = self.object.notification_emails
        
        # Pagination for status timeline
        timeline_page = self.request.GET.get('timeline_page', 1)
        timeline_page_size = _validated_page_size(
            self.request.GET.get('timeline_page_size'),
            TIMELINE_PAGE_SIZE_OPTIONS,
            10,
        )
        
        try:
            timeline_page = int(timeline_page)
        except (ValueError, TypeError):
            timeline_page = 1
        
        # Get paginated timeline data
        timeline_data = self.object.get_status_timeline_paginated(
            page=timeline_page, 
            page_size=timeline_page_size
        )
        
        # Create pagination context
        context['status_timeline'] = timeline_data['items']
        context['timeline_has_next'] = timeline_data['has_next']
        context['timeline_has_previous'] = timeline_data['has_previous']
        context['timeline_page_number'] = timeline_page
        context['timeline_total_pages'] = timeline_data['total_pages']
        context['timeline_page_size'] = timeline_page_size
        context['timeline_page_size_options'] = TIMELINE_PAGE_SIZE_OPTIONS
        context['timeline_page_range'] = range(1, timeline_data['total_pages'] + 1)
        
        return context

    def post(self, request, *args, **kwargs):
        action = kwargs.get('action')
        if action == 'update_monitoring':
            return self.update_monitoring(request, *args, **kwargs)
        elif action == 'update_email_list':
            return self.update_email_list(request, *args, **kwargs)
        elif action == 'check_status':
            return self.check_status(request, *args, **kwargs)
        else:
            return JsonResponse({'error': 'Invalid action'}, status=400)

    @method_decorator(require_POST)
    def update_monitoring(self, request, *args, **kwargs):
        asset = self.get_object()
        new_status = request.POST.get('status')

        if new_status not in [UtilAsset.Monitoring.ACTIVE, UtilAsset.Monitoring.DISABLED]:
            return JsonResponse({'success': False, 'error': 'Invalid status'}, status=400)

        try:
            asset.monitoring = new_status
            # save() syncs the status-check schedule with the monitoring state
            asset.save()

            return JsonResponse({'success': True, 'new_status': new_status})
        except Exception:
            logger.exception("Could not update monitoring for asset %s", asset.pk)
            return JsonResponse({
                'success': False,
                'error': 'Failed to update monitoring status',
            }, status=500)

    @method_decorator(require_POST)
    def update_email_list(self, request, *args, **kwargs):
        asset = self.get_object()

        try:
            data = json.loads(request.body)
            email_list = data.get('email_list', [])

            # Update the notification emails
            asset.update_email_config(email_list)

            return JsonResponse({
                'success': True,
                'email_list': asset.notification_emails
            })
        except json.JSONDecodeError:
            return JsonResponse({
                'success': False,
                'error': 'Invalid JSON data'
            }, status=400)
        except ValidationError as error:
            message = error.messages[0] if error.messages else 'Invalid email list'
            return JsonResponse({
                'success': False,
                'error': message,
            }, status=400)
        except Exception:
            logger.exception("Could not update notification emails for asset %s", asset.pk)
            return JsonResponse({
                'success': False,
                'error': 'Failed to update email list',
            }, status=500)

    @method_decorator(require_POST)
    def check_status(self, request, *args, **kwargs):
        """Trigger an immediate status check for this asset using the monitoring engine"""
        asset = self.get_object()

        try:
            result = check_asset_status_now(asset)

            # Clear any cached status so the next access gets fresh data
            if hasattr(asset, '_cached_status'):
                delattr(asset, '_cached_status')

            return JsonResponse({
                'success': True,
                'message': 'Status check completed successfully',
                'status': result.get('status', 'unknown'),
                'timestamp': result.get('timestamp'),
                'metadata_changes': result.get('metadata_changes') or [],
                'error': result.get('error'),
            })

        except Exception:
            logger.exception("Could not run an immediate status check for asset %s", asset.pk)
            return JsonResponse({
                'success': False,
                'error': 'Failed to trigger status check',
            }, status=500)
