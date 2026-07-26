from rest_framework import serializers
from apps.console.cloud.models import CoreCloud


class CloudSyncSerializer(serializers.Serializer):
    uuid = serializers.UUIDField(required=True)

    def validate_uuid(self, value):
        try:
            CoreCloud.objects.get(uuid=value)
            return value
        except CoreCloud.DoesNotExist:
            raise serializers.ValidationError("Cloud not found")