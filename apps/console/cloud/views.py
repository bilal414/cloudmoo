from .models import CoreCloudServiceProvider
from django.views import View
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib import messages
from .models import CoreCloud
from .forms import CloudEditForm
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.core.paginator import Paginator
from django.views.generic import ListView, DetailView
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.utils.decorators import method_decorator
from django.db import models

from ..utils.models import UtilAsset
from apps.monitoring.schedules import cloud_schedule_update


class CloudConnect(ListView):
    model = CoreCloudServiceProvider
    template_name = 'console/cloud/connect.html'
    context_object_name = 'cloud_providers'

    def get_queryset(self):
        return CoreCloudServiceProvider.objects.order_by(
            'status',  # '-status' puts 'active' before 'disabled'
            'position'
        )

class CloudListView(ListView):
    model = CoreCloud
    template_name = 'console/cloud/list.html'
    context_object_name = 'clouds'

    def get_queryset(self):
        return CoreCloud.objects.for_user(self.request.user).order_by('provider__code')


class CloudEditView(LoginRequiredMixin, View):
    template_name = 'console/cloud/edit.html'
    form_class = CloudEditForm

    def get(self, request, cloud_id):
        cloud = get_object_or_404(CoreCloud.objects.for_user(self.request.user), id=cloud_id)
        form = self.form_class(instance=cloud)
        return render(request, self.template_name, {'form': form, 'cloud': cloud})

    def post(self, request, cloud_id):
        cloud = get_object_or_404(CoreCloud.objects.for_user(self.request.user), id=cloud_id)

        # Check if this is a delete request
        if request.POST.get('action') == 'delete':
            return self.delete(request, cloud)

        form = self.form_class(request.POST, instance=cloud)
        if form.is_valid():
            form.save()

            # Sync the asset-sync schedule with the cloud status (creates it
            # when missing; disables it unless the cloud is ACTIVE)
            cloud_schedule_update(cloud)

            if cloud.status == CoreCloud.Status.ACTIVE:
                cloud.create_all_asset_schedules()
            elif cloud.status == CoreCloud.Status.PAUSED:
                cloud.delete_all_asset_schedules()

            messages.success(request, 'Cloud settings updated successfully!')
            return redirect('console:cloud:detail', cloud_id=cloud.id)
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field.capitalize()}: {error}")
        return render(request, self.template_name, {'form': form, 'cloud': cloud})

    @method_decorator(require_POST)
    def delete(self, request, cloud):
        try:
            cloud_name = cloud.name
            cloud.delete()
            messages.success(request, f'Cloud "{cloud_name}" has been deleted successfully.')
            return JsonResponse({'success': True, 'redirect_url': reverse('console:cloud:list')})
        except Exception as e:
            return JsonResponse({
                'success': False,
                'error': f'Failed to delete cloud: {str(e)}'
            }, status=500)

class CloudSyncView(LoginRequiredMixin, View):
    def post(self, request, cloud_id):
        cloud = get_object_or_404(CoreCloud.objects.for_user(self.request.user), id=cloud_id)
        try:
            cloud.sync_assets()
            messages.success(request, f"Assets for {cloud.name} synced successfully!")
        except NotImplementedError:
            messages.error(request, f"Asset synchronization not implemented for {cloud.provider.name}")
        except Exception as e:
            messages.error(request, f"Error syncing assets: {str(e)}")
        return HttpResponseRedirect(reverse('console:cloud:list'))


class CloudDetailView(LoginRequiredMixin, DetailView):
    model = CoreCloud
    template_name = 'console/cloud/detail.html'
    context_object_name = 'cloud'
    pk_url_kwarg = 'cloud_id'
    paginate_by = 10

    def get_queryset(self):
        return CoreCloud.objects.for_user(self.request.user)

    def get_aws_asset_categories(self):
        """Define AWS asset categories for better organization"""
        return {
            'compute': {
                'name': 'Compute',
                'icon': 'cpu',
                'assets': ['servers', 'lambda_functions', 'ecs_services', 'ecs_tasks']
            },
            'storage': {
                'name': 'Storage',
                'icon': 'database',
                'assets': ['volumes', 's3_buckets', 'snapshots']
            },
            'database': {
                'name': 'Database',
                'icon': 'table-cells',
                'assets': ['databases', 'rds_databases', 'dynamodb_tables']
            },
            'networking': {
                'name': 'Networking',
                'icon': 'globe-alt',
                'assets': ['load_balancers', 'elastic_ips', 'security_groups']
            },
            'security': {
                'name': 'Security',
                'icon': 'shield-check',
                'assets': ['acm_certificates']
            }
        }

    def get_detailed_asset_counts(self, cloud):
        """Get detailed counts for all AWS asset types"""
        provider_account = cloud.provider_account
        counts = {}

        # AWS-specific asset types
        aws_asset_types = [
            'servers', 'volumes', 'databases', 'rds_databases', 'lambda_functions',
            'dynamodb_tables', 's3_buckets', 'acm_certificates', 'snapshots',
            'elastic_ips', 'load_balancers', 'security_groups', 'ecs_services', 'ecs_tasks'
        ]

        for asset_type in aws_asset_types:
            if hasattr(provider_account, asset_type):
                asset_manager = getattr(provider_account, asset_type)
                counts[asset_type] = {
                    'total': asset_manager.count(),
                    'active': asset_manager.exclude(
                        monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
                    ).count(),
                    'monitored': asset_manager.filter(
                        monitoring=UtilAsset.Monitoring.ACTIVE
                    ).count(),
                    'not_monitored': asset_manager.filter(
                        monitoring=UtilAsset.Monitoring.DISABLED
                    ).count(),
                    'no_longer_exists': asset_manager.filter(
                        monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
                    ).count()
                }
            else:
                counts[asset_type] = {
                    'total': 0, 'active': 0, 'monitored': 0, 
                    'not_monitored': 0, 'no_longer_exists': 0
                }

        return counts

    def get_monitoring_summary(self, cloud):
        """Get monitoring summary statistics"""
        asset_counts = self.get_detailed_asset_counts(cloud)
        
        total_assets = sum(counts['active'] for counts in asset_counts.values())
        monitored_assets = sum(counts['monitored'] for counts in asset_counts.values())
        not_monitored_assets = sum(counts['not_monitored'] for counts in asset_counts.values())
        
        monitoring_percentage = (monitored_assets / total_assets * 100) if total_assets > 0 else 0
        
        return {
            'total_assets': total_assets,
            'monitored_assets': monitored_assets,
            'not_monitored_assets': not_monitored_assets,
            'monitoring_percentage': round(monitoring_percentage, 1)
        }

    def get_cost_insights(self, cloud):
        """Get cost-related insights (placeholder for future implementation)"""
        # This could be expanded to include actual AWS cost data
        return {
            'estimated_monthly_cost': 0,  # Placeholder
            'cost_trend': 'stable',  # Placeholder
            'cost_alerts': 0  # Placeholder
        }

    def get_health_status(self, cloud):
        """Get overall health status of the cloud"""
        try:
            # Check if we can validate the cloud connection
            is_healthy = cloud.validate()
            last_sync_ago = None
            
            if cloud.last_synced:
                from django.utils import timezone
                last_sync_ago = timezone.now() - cloud.last_synced
                sync_status = 'recent' if last_sync_ago.days < 1 else 'stale'
            else:
                sync_status = 'never'
            
            return {
                'status': 'healthy' if is_healthy else 'unhealthy',
                'last_sync_ago': last_sync_ago,
                'sync_status': sync_status,
                'connection_valid': is_healthy
            }
        except Exception:
            return {
                'status': 'unknown',
                'last_sync_ago': None,
                'sync_status': 'error',
                'connection_valid': False
            }

    def get_assets(self, cloud):
        provider_account = cloud.provider_account
        assets = []

        # Get all AWS asset types
        aws_asset_types = [
            'servers', 'volumes', 'databases', 'rds_databases', 'lambda_functions',
            'dynamodb_tables', 's3_buckets', 'acm_certificates', 'snapshots',
            'elastic_ips', 'load_balancers', 'security_groups', 'ecs_services', 'ecs_tasks'
        ]

        for asset_type in aws_asset_types:
            if hasattr(provider_account, asset_type):
                assets.extend(getattr(provider_account, asset_type).all())

        # Apply search filter
        search_query = self.request.GET.get('search', '').strip()
        if search_query:
            assets = [asset for asset in assets if
                      search_query.lower() in asset.name.lower() or
                      search_query in asset.unique_id]

        # Apply type filter
        type_filter = self.request.GET.get('type', '')
        if type_filter:
            assets = [asset for asset in assets if asset.type == type_filter]

        # Apply monitoring filter
        monitoring_filter = self.request.GET.get('monitoring', '')
        if monitoring_filter:
            assets = [asset for asset in assets if asset.monitoring == monitoring_filter]

        # Get sort parameters
        sort_by = self.request.GET.get('sort', 'created')
        sort_direction = self.request.GET.get('direction', 'desc')

        # Apply sorting
        reverse = sort_direction == 'desc'
        assets.sort(key=lambda x: getattr(x, sort_by, ''), reverse=reverse)

        return assets

    def get_asset_counts(self, cloud):
        """Legacy method for backward compatibility"""
        detailed_counts = self.get_detailed_asset_counts(cloud)
        return {
            'servers': detailed_counts['servers']['active'],
            'volumes': detailed_counts['volumes']['active'],
            'databases': detailed_counts['databases']['active'] + detailed_counts['rds_databases']['active'],
        }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        cloud = self.object
        provider_account = cloud.provider_account

        assets = self.get_assets(cloud)

        # Get the page size from request or use default
        page_size = int(self.request.GET.get('page_size', self.paginate_by))

        # Pagination
        paginator = Paginator(assets, page_size)
        page_number = self.request.GET.get('page', 1)
        page_obj = paginator.get_page(page_number)

        # Optimization: Pre-fetch statuses for assets on current page
        # This prevents N+1 queries when template accesses asset.status
        if page_obj.object_list:
            from apps.console.utils.models import UtilAsset
            UtilAsset.get_bulk_statuses(page_obj.object_list)

        # Get current query parameters
        query_params = self.request.GET.copy()
        if 'page' in query_params:
            del query_params['page']

        # Enhanced context for AWS optimization
        context.update({
            'assets': page_obj,
            'page_obj': page_obj,
            'asset_counts': self.get_asset_counts(cloud),  # Legacy compatibility
            'detailed_asset_counts': self.get_detailed_asset_counts(cloud),
            'aws_asset_categories': self.get_aws_asset_categories(),
            'monitoring_summary': self.get_monitoring_summary(cloud),
            'cost_insights': self.get_cost_insights(cloud),
            'health_status': self.get_health_status(cloud),
            'monitoring_choices': UtilAsset.Monitoring.choices,
            'asset_types': UtilAsset.Type.choices,
            'page_size': page_size,
            'page_size_options': [10, 25, 50],
            'sort_by': self.request.GET.get('sort', 'created'),
            'sort_direction': self.request.GET.get('direction', 'desc'),
            'query_params': query_params.urlencode(),
            'is_aws': cloud.provider.code.lower() == 'aws',
        })
        return context

