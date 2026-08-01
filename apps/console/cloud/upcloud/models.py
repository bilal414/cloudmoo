from django.db import models
from apps.console.cloud.models import (
    CloudValidationTransientError,
    CoreCloud,
    require_inventory_list,
    validate_provider_response,
)
from apps.console.utils.models import UtilCloud, UtilAsset
import requests
from django.utils import timezone

class CoreUpCloudAccount(UtilCloud):
    cloud = models.ForeignKey(CoreCloud, on_delete=models.CASCADE, related_name="upcloud")
    username = models.CharField(max_length=255)
    password = models.CharField(max_length=255)

    class Meta:
        db_table = "core_upcloud_account"

    def __str__(self):
        return self.name

    @property
    def access_token(self):
        return {'username': self.username, 'password': self.password}

    def validate(self):
        try:
            headers = {
                'Authorization': f'Basic {self._get_auth_token()}',
                'Content-Type': 'application/json'
            }
            response = requests.get('https://api.upcloud.com/1.3/account', headers=headers, timeout=10)
            return validate_provider_response(response, 'UpCloud')
        except CloudValidationTransientError:
            raise
        except Exception as error:
            raise CloudValidationTransientError(
                'UpCloud validation temporarily unavailable'
            ) from error

    def _get_auth_token(self):
        import base64
        return base64.b64encode(f"{self.username}:{self.password}".encode()).decode()

    def sync_assets(self):
        self.sync_servers()
        self.sync_volumes()
        self.last_synced = timezone.now()
        self.save()

    def _make_api_call(self, endpoint):
        headers = {
            'Authorization': f'Basic {self._get_auth_token()}',
            'Content-Type': 'application/json'
        }
        url = f'https://api.upcloud.com/1.3/{endpoint}'
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        return response.json()

    def sync_servers(self):
        data = self._make_api_call('server')
        servers = require_inventory_list(data, ['servers', 'server'], 'UpCloud')

        current_server_ids = []
        for server_data in servers:
            server, created = CoreUpCloudServer.objects.update_or_create(
                owner=self,
                unique_id=server_data['uuid'],
                defaults={
                    'name': server_data['title'],
                    'type': CoreUpCloudServer.Type.SERVER,
                    'metadata': server_data
                }
            )
            current_server_ids.append(server_data['uuid'])

        CoreUpCloudServer.objects.filter(owner=self).exclude(unique_id__in=current_server_ids).update(
            monitoring=CoreUpCloudServer.Monitoring.NO_LONGER_EXISTS
        )

    def sync_volumes(self):
        data = self._make_api_call('storage/normal')
        volumes = require_inventory_list(data, ['storages', 'storage'], 'UpCloud')

        current_volume_ids = []
        for volume_data in volumes:
            volume, created = CoreUpCloudVolume.objects.update_or_create(
                owner=self,
                unique_id=volume_data['uuid'],
                defaults={
                    'name': volume_data['title'],
                    'type': CoreUpCloudVolume.Type.VOLUME,
                    'metadata': volume_data
                }
            )
            current_volume_ids.append(volume_data['uuid'])

        CoreUpCloudVolume.objects.filter(owner=self).exclude(unique_id__in=current_volume_ids).update(
            monitoring=CoreUpCloudVolume.Monitoring.NO_LONGER_EXISTS
        )

class CoreUpCloudServer(UtilAsset):
    owner = models.ForeignKey(CoreUpCloudAccount, on_delete=models.CASCADE, related_name='servers')

    class Meta:
        db_table = "core_upcloud_server"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        return f"https://hub.upcloud.com/servers/{self.unique_id}"


class CoreUpCloudVolume(UtilAsset):
    owner = models.ForeignKey(CoreUpCloudAccount, on_delete=models.CASCADE, related_name='volumes')

    class Meta:
        db_table = "core_upcloud_volume"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        return f"https://hub.upcloud.com/storage/{self.unique_id}"
