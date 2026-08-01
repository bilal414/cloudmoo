import json

from django.views.generic import ListView
from django.core.paginator import Paginator

from apps.console.cloud.aws.models import CoreAWSInstance, CoreAWSVolume, CoreAWSRDSDatabase, CoreAWSLambda, CoreAWSDynamoDB, CoreAWSS3Bucket, CoreAWSACMCertificate, CoreAWSSnapshot, CoreAWSElasticIP, CoreAWSLoadBalancer, CoreAWSSecurityGroup, CoreAWSECSService, CoreAWSECSTask
from apps.console.cloud.digitalocean.models import CoreDigitalOceanServer, CoreDigitalOceanDatabase, \
    CoreDigitalOceanVolume
from apps.console.cloud.hetzner.models import CoreHetznerVolume, CoreHetznerServer
from apps.console.cloud.linode.models import CoreLinodeServer, CoreLinodeVolume
from apps.console.cloud.models import CoreCloudServiceProvider
from apps.console.cloud.upcloud.models import CoreUpCloudServer, CoreUpCloudVolume
from apps.console.cloud.vultr.models import CoreVultrVolume, CoreVultrServer, CoreVultrDatabase
from django.views.decorators.http import require_POST
from django.utils.decorators import method_decorator
from operator import attrgetter
from django.views.generic import DetailView
from django.shortcuts import get_object_or_404
from apps.console.cloud.models import CoreCloud
from apps.console.utils.models import UtilAsset
from apps.monitoring.tasks import check_asset_status_now
from django.http import JsonResponse, Http404


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
        sort_direction = self.request.GET.get('direction', 'desc')

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
                provider_account = cloud.provider_account
                if hasattr(provider_account, 'servers'):
                    assets.extend(provider_account.servers.exclude(
                        monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
                    ))
                if hasattr(provider_account, 'volumes'):
                    assets.extend(provider_account.volumes.exclude(
                        monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
                    ))
                if hasattr(provider_account, 'databases'):
                    assets.extend(provider_account.databases.exclude(
                        monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
                    ))
                # Add AWS-specific assets
                if hasattr(provider_account, 'rds_databases'):
                    assets.extend(provider_account.rds_databases.exclude(
                        monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
                    ))
                if hasattr(provider_account, 'lambda_functions'):
                    assets.extend(provider_account.lambda_functions.exclude(
                        monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
                    ))
                if hasattr(provider_account, 'dynamodb_tables'):
                    assets.extend(provider_account.dynamodb_tables.exclude(
                        monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
                    ))
                if hasattr(provider_account, 's3_buckets'):
                    assets.extend(provider_account.s3_buckets.exclude(
                        monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
                    ))
                if hasattr(provider_account, 'acm_certificates'):
                    assets.extend(provider_account.acm_certificates.exclude(
                        monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
                    ))
                if hasattr(provider_account, 'snapshots'):
                    assets.extend(provider_account.snapshots.exclude(
                        monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
                    ))
                if hasattr(provider_account, 'elastic_ips'):
                    assets.extend(provider_account.elastic_ips.exclude(
                        monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
                    ))
                if hasattr(provider_account, 'load_balancers'):
                    assets.extend(provider_account.load_balancers.exclude(
                        monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
                    ))
                if hasattr(provider_account, 'security_groups'):
                    assets.extend(provider_account.security_groups.exclude(
                        monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
                    ))
                if hasattr(provider_account, 'ecs_services'):
                    assets.extend(provider_account.ecs_services.exclude(
                        monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
                    ))
                if hasattr(provider_account, 'ecs_tasks'):
                    assets.extend(provider_account.ecs_tasks.exclude(
                        monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS
                    ))
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
        assets = self.get_queryset()

        # Get the page size from request or use default
        page_size = int(self.request.GET.get('page_size', self.paginate_by))

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
            'page_size_options': [10, 25, 50],
            'sort_by': self.request.GET.get('sort', 'created'),
            'sort_direction': self.request.GET.get('direction', 'desc'),
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

        asset_classes = {
            'digitalocean': {
                'server': CoreDigitalOceanServer,
                'database': CoreDigitalOceanDatabase,
                'volume': CoreDigitalOceanVolume,
            },
            'vultr': {
                'server': CoreVultrServer,
                'volume': CoreVultrVolume,
                'database': CoreVultrDatabase,
            },
            'hetzner': {
                'server': CoreHetznerServer,
                'volume': CoreHetznerVolume,
            },
            'aws': {
                'server': CoreAWSInstance,
                'volume': CoreAWSVolume,
                'rds_database': CoreAWSRDSDatabase,
                'lambda': CoreAWSLambda,
                'dynamodb': CoreAWSDynamoDB,
                's3_bucket': CoreAWSS3Bucket,
                'acm_certificate': CoreAWSACMCertificate,
                'snapshot': CoreAWSSnapshot,
                'elastic_ip': CoreAWSElasticIP,
                'load_balancer': CoreAWSLoadBalancer,
                'security_group': CoreAWSSecurityGroup,
                'ecs_service': CoreAWSECSService,
                'ecs_task': CoreAWSECSTask,
            },
            'upcloud': {
                'server': CoreUpCloudServer,
                'volume': CoreUpCloudVolume,
            },
            'linode': {
                'server': CoreLinodeServer,
                'volume': CoreLinodeVolume,
            },
        }

        if provider_code not in asset_classes or asset_type not in asset_classes[provider_code]:
            raise Http404("Unsupported asset type or provider")

        asset_class = asset_classes[provider_code][asset_type]
        try:
            return get_object_or_404(asset_class.objects.for_user(self.request.user), id=asset_id)
        except asset_class.DoesNotExist:
            raise Http404("Asset not found")

    def get(self, request, *args, **kwargs):
        if request.GET.get('export') == 'csv':
            return self.export_timeline_csv(request)
        return super().get(request, *args, **kwargs)

    def export_timeline_csv(self, request):
        import csv
        from django.http import HttpResponse
        from datetime import datetime

        asset = self.get_object()
        timeline = asset.get_status_timeline()

        response = HttpResponse(content_type='text/csv')
        response[
            'Content-Disposition'] = f'attachment; filename="status_timeline_{asset.name}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv"'

        writer = csv.writer(response)
        writer.writerow(['Timestamp', 'Status', 'Duration', 'Changes'])

        for entry in timeline:
            # Format the timestamp in the user's timezone if available
            timestamp = entry['timestamp'].strftime("%Y-%m-%d %H:%M:%S %Z")

            # Join multiple metadata changes with semicolons
            changes = '; '.join(entry.get('metadata_changes', [])) or 'No changes'

            writer.writerow([
                timestamp,
                entry['status'],
                entry.get('duration', 'N/A'),
                changes
            ])

        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['cloud'] = self.object.owner.cloud
        context['monitoring_status'] = self.object.monitoring
        context['email_list'] = self.object.notification_emails
        
        # Pagination for status timeline
        timeline_page = self.request.GET.get('timeline_page', 1)
        timeline_page_size = int(self.request.GET.get('timeline_page_size', 10))
        
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
        context['timeline_page_size_options'] = [10, 25, 50, 100, 200]
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
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)}, status=500)

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
        except Exception as e:
            return JsonResponse({
                'success': False,
                'error': f'Failed to update email list: {str(e)}'
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

        except Exception as e:
            return JsonResponse({
                'success': False,
                'error': f'Failed to trigger status check: {str(e)}'
            }, status=500)
