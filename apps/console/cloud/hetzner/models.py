from django.db import models
from apps.console.cloud.models import CoreCloud
from apps.console.utils.models import UtilCloud, UtilAsset
import requests
from datetime import datetime


class CoreHetznerAccount(UtilCloud):
    cloud = models.ForeignKey(CoreCloud, on_delete=models.CASCADE, related_name="hetzner")
    access_token = models.CharField(max_length=1024)

    class Meta:
        db_table = "core_hetzner_account"

    def __str__(self):
        return self.name

    def validate(self):
        try:
            headers = {'Authorization': f'Bearer {self.access_token}'}
            response = requests.get('https://api.hetzner.cloud/v1/servers', headers=headers, timeout=10)
            if response.status_code != 200:
                return False
            response.json()
            return True
        except Exception:
            return False

    def sync_assets(self):
        self.sync_servers()
        self.sync_volumes()
        self.last_synced = datetime.now()
        self.save()

    def _make_api_call(self, endpoint, params=None):
        headers = {'Authorization': f'Bearer {self.access_token}'}
        url = f'https://api.hetzner.cloud/v1/{endpoint}'
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        return response.json()

    def sync_servers(self):
        page = 1
        per_page = 50
        all_servers = []

        while True:
            params = {'page': page, 'per_page': per_page}
            data = self._make_api_call('servers', params)
            servers = data['servers']
            all_servers.extend(servers)

            if len(servers) < per_page:
                break
            page += 1

        current_server_ids = []
        for server_data in all_servers:
            try:
                server = CoreHetznerServer.objects.get(
                    owner=self,
                    unique_id=server_data['id']
                )
                # Update server while preserving monitoring status
                server.name = server_data['name']
                server.type = CoreHetznerServer.Type.SERVER
                server.metadata = server_data
                server.save()
            except CoreHetznerServer.DoesNotExist:
                # Create new server with default ACTIVE monitoring
                server = CoreHetznerServer.objects.create(
                    owner=self,
                    unique_id=server_data['id'],
                    name=server_data['name'],
                    monitoring=CoreHetznerServer.Monitoring.ACTIVE,
                    type=CoreHetznerServer.Type.SERVER,
                    metadata=server_data
                )
            current_server_ids.append(server_data['id'])

        CoreHetznerServer.objects.filter(owner=self).exclude(unique_id__in=current_server_ids).update(
            monitoring=CoreHetznerServer.Monitoring.NO_LONGER_EXISTS
        )

    def sync_volumes(self):
        page = 1
        per_page = 50
        all_volumes = []

        while True:
            params = {'page': page, 'per_page': per_page}
            data = self._make_api_call('volumes', params)
            volumes = data['volumes']
            all_volumes.extend(volumes)

            if len(volumes) < per_page:
                break
            page += 1

        current_volume_ids = []
        for volume_data in all_volumes:
            try:
                volume = CoreHetznerVolume.objects.get(
                    owner=self,
                    unique_id=volume_data['id']
                )
                # Update volume while preserving monitoring status
                volume.name = volume_data['name']
                volume.type = CoreHetznerServer.Type.VOLUME
                volume.metadata = volume_data
                volume.save()
            except CoreHetznerVolume.DoesNotExist:
                # Create new volume with default ACTIVE monitoring
                volume = CoreHetznerVolume.objects.create(
                    owner=self,
                    unique_id=volume_data['id'],
                    name=volume_data['name'],
                    monitoring=CoreHetznerVolume.Monitoring.ACTIVE,
                    type=CoreHetznerServer.Type.VOLUME,
                    metadata=volume_data
                )
            current_volume_ids.append(volume_data['id'])

        CoreHetznerVolume.objects.filter(owner=self).exclude(unique_id__in=current_volume_ids).update(
            monitoring=CoreHetznerVolume.Monitoring.NO_LONGER_EXISTS
        )


class CoreHetznerServer(UtilAsset):
    owner = models.ForeignKey(CoreHetznerAccount, on_delete=models.CASCADE, related_name='servers')

    class Meta:
        db_table = "core_hetzner_server"

    def __str__(self):
        return self.name

    # @property
    # def status(self):
    #     return self.metadata['status']

    @property
    def public_ipv4(self):
        return self.metadata['public_net']['ipv4']['ip']

    @property
    def public_ipv6(self):
        return self.metadata['public_net']['ipv6']['ip']

    @property
    def provider_url(self):
        return f"https://console.hetzner.cloud/projects/<project_id>/servers/{self.unique_id}"


class CoreHetznerVolume(UtilAsset):
    owner = models.ForeignKey(CoreHetznerAccount, on_delete=models.CASCADE, related_name='volumes')

    class Meta:
        db_table = "core_hetzner_volume"

    def __str__(self):
        return self.name

    # @property
    # def status(self):
    #     return self.metadata['status']

