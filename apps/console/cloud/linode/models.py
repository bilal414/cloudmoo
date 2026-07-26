from django.db import models
from apps.console.cloud.models import CoreCloud
from apps.console.utils.models import UtilCloud, UtilAsset
import requests
from datetime import datetime


class CoreLinodeAccount(UtilCloud):
    cloud = models.ForeignKey(CoreCloud, on_delete=models.CASCADE, related_name="linode")
    access_token = models.CharField(max_length=255)

    class Meta:
        db_table = "core_linode_account"

    def __str__(self):
        return self.name

    def validate(self):
        try:
            headers = {'Authorization': f'Bearer {self.access_token}'}
            response = requests.get('https://api.linode.com/v4/account', headers=headers, timeout=10)
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
        url = f'https://api.linode.com/v4/{endpoint}'
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        return response.json()

    def _paginate_api_call(self, endpoint):
        all_items = []
        page = 1
        per_page = 100

        while True:
            params = {'page': page, 'page_size': per_page}
            data = self._make_api_call(endpoint, params)

            items = data.get('data', [])
            all_items.extend(items)

            # Check if there are more pages
            if len(items) < per_page:
                break

            page += 1

        return all_items

    def sync_servers(self):
        all_instances = self._paginate_api_call('linode/instances')

        current_instance_ids = []
        for instance_data in all_instances:
            # Get existing server if any
            try:
                server = CoreLinodeServer.objects.get(
                    owner=self,
                    unique_id=str(instance_data['id'])
                )
                # Update server while preserving monitoring status
                server.name = instance_data['label']
                server.type = CoreLinodeServer.Type.SERVER
                server.metadata = instance_data
                server.save()
            except CoreLinodeServer.DoesNotExist:
                # Create new server with default ACTIVE monitoring
                server = CoreLinodeServer.objects.create(
                    owner=self,
                    unique_id=str(instance_data['id']),
                    name=instance_data['label'],
                    monitoring=CoreLinodeServer.Monitoring.ACTIVE,
                    type=CoreLinodeServer.Type.SERVER,
                    metadata=instance_data
                )
            current_instance_ids.append(str(instance_data['id']))

        CoreLinodeServer.objects.filter(owner=self).exclude(unique_id__in=current_instance_ids).update(
            monitoring=CoreLinodeServer.Monitoring.NO_LONGER_EXISTS
        )

    def sync_volumes(self):
        all_volumes = self._paginate_api_call('volumes')

        current_volume_ids = []
        for volume_data in all_volumes:
            try:
                volume = CoreLinodeVolume.objects.get(
                    owner=self,
                    unique_id=str(volume_data['id'])
                )
                volume.name = volume_data['label']
                volume.type = CoreLinodeVolume.Type.VOLUME
                volume.metadata = volume_data
                volume.save()
            except CoreLinodeVolume.DoesNotExist:
                volume = CoreLinodeVolume.objects.create(
                    owner=self,
                    unique_id=str(volume_data['id']),
                    name=volume_data['label'],
                    monitoring=CoreLinodeVolume.Monitoring.ACTIVE,
                    type=CoreLinodeVolume.Type.VOLUME,
                    metadata=volume_data
                )
            current_volume_ids.append(str(volume_data['id']))

        CoreLinodeVolume.objects.filter(owner=self).exclude(unique_id__in=current_volume_ids).update(
            monitoring=CoreLinodeVolume.Monitoring.NO_LONGER_EXISTS
        )


class CoreLinodeServer(UtilAsset):
    owner = models.ForeignKey(CoreLinodeAccount, on_delete=models.CASCADE, related_name='servers')

    class Meta:
        db_table = "core_linode_server"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        return f"https://cloud.linode.com/linodes/{self.unique_id}"

    def check_status(self):
        api_url = f'https://api.linode.com/v4/linode/instances/{self.unique_id}'
        headers = {
            'Authorization': f'Bearer {self.owner.access_token}',
            'Content-Type': 'application/json'
        }
        try:
            response = requests.get(api_url, headers=headers)
            response.raise_for_status()
            data = response.json()
            current_status = data['status']
            return current_status, data
        except requests.exceptions.RequestException as e:
            error_status = 'not_found' if e.response.status_code == 404 else 'invalid_access_token' if e.response.status_code == 401 else 'error'
            return error_status, str(e)



class CoreLinodeVolume(UtilAsset):
    owner = models.ForeignKey(CoreLinodeAccount, on_delete=models.CASCADE, related_name='volumes')

    class Meta:
        db_table = "core_linode_volume"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        return f"https://cloud.linode.com/volumes/{self.unique_id}"


