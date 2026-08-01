from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from apps.console.cloud.models import CoreCloud
from apps.monitoring.tasks import run_cloud_sync
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

        cloud = CoreCloud.objects.get(uuid=serializer.validated_data['uuid'])
        result = run_cloud_sync(cloud)

        if result.get('not_implemented'):
            return Response({
                'error': result['message']
            }, status=status.HTTP_501_NOT_IMPLEMENTED)

        if not result.get('success'):
            return Response({
                'error': result['message']
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response({
            'success': True,
            'message': result['message'],
            'status_changed': result['status_changed'],
            'current_status': result['current_status'],
            'last_synced': result['last_synced'],
        })
