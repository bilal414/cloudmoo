"""Request-body serializers for the mobile API.

Responses are built as plain dicts in the views because assets are
heterogeneous provider models without a shared concrete serializer target.
"""
from rest_framework import serializers

from apps.console.utils.models import MAX_NOTIFICATION_EMAILS, UtilAsset


class MobileLoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(trim_whitespace=False)


class CloudUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255, required=False)
    action = serializers.ChoiceField(choices=('pause', 'resume'), required=False)

    def validate(self, data):
        if not data:
            raise serializers.ValidationError("Provide 'name' and/or 'action'.")
        return data


class AssetUpdateSerializer(serializers.Serializer):
    monitoring = serializers.ChoiceField(
        choices=(UtilAsset.Monitoring.ACTIVE, UtilAsset.Monitoring.DISABLED),
        required=False,
    )
    notification_emails = serializers.ListField(
        child=serializers.EmailField(),
        max_length=MAX_NOTIFICATION_EMAILS,
        required=False,
    )

    def validate(self, data):
        if not data:
            raise serializers.ValidationError(
                "Provide 'monitoring' and/or 'notification_emails'."
            )
        return data


class AccountUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255)
