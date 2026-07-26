from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.db import transaction

from apps.console.cloud.models import CoreCloud
from .serializers import CloudSyncSerializer
from .authentication import APIKeyAuthentication


class CloudSyncAPIView(APIView):
    authentication_classes = [APIKeyAuthentication]
    permission_classes = []
    serializer_class = CloudSyncSerializer

    def post(self, request, *args, **kwargs):
        serializer = self.serializer_class(data=request.data)

        if not serializer.is_valid():
            return Response(
                serializer.errors,
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            with transaction.atomic():
                cloud = CoreCloud.objects.select_for_update().get(
                    uuid=serializer.validated_data['uuid']
                )

                # Store original status
                original_status = cloud.status
                current_status = cloud.status

                # Validate cloud credentials
                is_valid = cloud.validate()

                if not is_valid and current_status == CoreCloud.Status.ACTIVE:
                    cloud.delete_all_asset_schedules()

                    # Now update cloud status
                    cloud.status = CoreCloud.Status.INVALID_AUTH
                    cloud.save(update_fields=['status', 'aws_schedule_arn'])

                elif is_valid:
                    if current_status == CoreCloud.Status.INVALID_AUTH:
                        # If previously invalid, now valid
                        cloud.status = CoreCloud.Status.ACTIVE
                        cloud.save(update_fields=['status'])

                        # Create schedules after successful sync
                        cloud.create_all_asset_schedules()

                    if current_status == CoreCloud.Status.ACTIVE:
                        cloud.sync_assets()

                return Response({
                    'success': True,
                    'message': f'Successfully processed cloud {cloud.name}',
                    'status_changed': original_status != cloud.status,
                    'current_status': cloud.status,
                    'last_synced': cloud.last_synced
                })

        except NotImplementedError:
            return Response({
                'error': f'Asset synchronization not implemented for {cloud.provider.name}'
            }, status=status.HTTP_501_NOT_IMPLEMENTED)

        except Exception as e:
            return Response({
                'error': f'Error processing cloud: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
