from django.core.exceptions import ValidationError
from django.db import models
from apps.console.cloud.models import CoreCloud
from apps.console.utils.models import UtilCloud, UtilAsset
import requests
from datetime import datetime


class CoreVultrAccount(UtilCloud):
    cloud = models.ForeignKey(CoreCloud, on_delete=models.CASCADE, related_name="vultr")
    access_token = models.CharField(max_length=255)

    class Meta:
        db_table = "core_vultr_account"

    def __str__(self):
        return self.name

    def validate(self):
        headers = {
            'Authorization': f'Bearer {self.access_token}',
            'Content-Type': 'application/json'
        }
        response = requests.get('https://api.vultr.com/v2/account', headers=headers, timeout=10)
        if response.status_code == 200:
            return True
        else:
            error_message = response.json().get('error', 'Unknown error occurred')
            raise ValidationError(f"Vultr API validation failed: {error_message}")

    def sync_assets(self):
        self.sync_servers()
        # self.sync_databases()
        self.sync_volumes()
        self.last_synced = datetime.now()
        self.save()

    def _make_api_call(self, endpoint, params=None):
        headers = {
            'Authorization': f'Bearer {self.access_token}',
            'Content-Type': 'application/json'
        }
        url = f'https://api.vultr.com/v2/{endpoint}'
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        return response.json()

    def _paginate_api_call(self, endpoint):
        all_items = []
        cursor = None
        while True:
            params = {'per_page': 100}
            if cursor:
                params['cursor'] = cursor
            data = self._make_api_call(endpoint, params)
            all_items.extend(data.get(endpoint, []))
            cursor = data.get('meta', {}).get('links', {}).get('next')
            if not cursor:
                break
        return all_items

    def sync_servers(self):
        all_instances = self._paginate_api_call('instances')

        current_instance_ids = []
        for instance_data in all_instances:
            try:
                server = CoreVultrServer.objects.get(
                    owner=self,
                    unique_id=instance_data['id']
                )
                # Update server while preserving monitoring status
                server.name = instance_data['label'] or instance_data['os']
                server.type = CoreVultrServer.Type.SERVER
                server.metadata = instance_data
                server.save()
            except CoreVultrServer.DoesNotExist:
                # Create new server with default ACTIVE monitoring
                server = CoreVultrServer.objects.create(
                    owner=self,
                    unique_id=instance_data['id'],
                    name=instance_data['label'] or instance_data['os'],
                    monitoring=CoreVultrServer.Monitoring.ACTIVE,
                    type=CoreVultrServer.Type.SERVER,
                    metadata=instance_data
                )
            current_instance_ids.append(instance_data['id'])

        CoreVultrServer.objects.filter(owner=self).exclude(unique_id__in=current_instance_ids).update(
            monitoring=CoreVultrServer.Monitoring.NO_LONGER_EXISTS
        )

    def sync_databases(self):
        all_databases = self._paginate_api_call('databases')

        current_database_ids = []
        for database_data in all_databases:
            try:
                database = CoreVultrDatabase.objects.get(
                    owner=self,
                    unique_id=database_data['id']
                )
                # Update database while preserving monitoring status
                database.name = database_data['label']
                database.type = CoreVultrServer.Type.DATABASE
                database.metadata = database_data
                database.save()
            except CoreVultrDatabase.DoesNotExist:
                # Create new database with default ACTIVE monitoring
                database = CoreVultrDatabase.objects.create(
                    owner=self,
                    unique_id=database_data['id'],
                    name=database_data['label'],
                    monitoring=CoreVultrDatabase.Monitoring.ACTIVE,
                    type=CoreVultrServer.Type.DATABASE,
                    metadata=database_data
                )
            current_database_ids.append(database_data['id'])

        CoreVultrDatabase.objects.filter(owner=self).exclude(unique_id__in=current_database_ids).update(
            monitoring=CoreVultrDatabase.Monitoring.NO_LONGER_EXISTS
        )

    def sync_volumes(self):
        all_volumes = self._paginate_api_call('blocks')

        current_volume_ids = []
        for volume_data in all_volumes:
            try:
                volume = CoreVultrVolume.objects.get(
                    owner=self,
                    unique_id=volume_data['id']
                )
                # Update volume while preserving monitoring status
                volume.name = f"Block Storage {volume_data['size_gb']} GB" if not volume_data['label'] else volume_data[
                    'label']
                volume.type = CoreVultrServer.Type.VOLUME
                volume.metadata = volume_data
                volume.save()
            except CoreVultrVolume.DoesNotExist:
                # Create new volume with default ACTIVE monitoring
                volume = CoreVultrVolume.objects.create(
                    owner=self,
                    unique_id=volume_data['id'],
                    name=f"Block Storage {volume_data['size_gb']} GB" if not volume_data['label'] else volume_data[
                        'label'],
                    monitoring=CoreVultrVolume.Monitoring.ACTIVE,
                    type=CoreVultrServer.Type.VOLUME,
                    metadata=volume_data
                )
            current_volume_ids.append(volume_data['id'])

        CoreVultrVolume.objects.filter(owner=self).exclude(unique_id__in=current_volume_ids).update(
            monitoring=CoreVultrVolume.Monitoring.NO_LONGER_EXISTS
        )


class CoreVultrServer(UtilAsset):
    owner = models.ForeignKey(CoreVultrAccount, on_delete=models.CASCADE, related_name='servers')

    class Meta:
        db_table = "core_vultr_server"

    def __str__(self):
        return self.name

    @property
    def name_alt(self):
        if self.metadata:
            if self.metadata['label'] == "":
                return self.metadata['main_ip']
            else:
                return self.metadata['label']

    @property
    def provider_url(self):
        return f"https://my.vultr.com/instances/instance-id/{self.unique_id}/"

    # @property
    # def status(self):
    #     if self.metadata:
    #         return self.metadata['power_status']


class CoreVultrDatabase(UtilAsset):
    owner = models.ForeignKey(CoreVultrAccount, on_delete=models.CASCADE, related_name='databases')

    class Meta:
        db_table = "core_vultr_database"

    def __str__(self):
        return self.name


class CoreVultrVolume(UtilAsset):
    owner = models.ForeignKey(CoreVultrAccount, on_delete=models.CASCADE, related_name='volumes')

    class Meta:
        db_table = "core_vultr_volume"

    def __str__(self):
        return self.name

    @property
    def name_alt(self):
        if self.metadata:
            if self.metadata['label'] == "":
                return self.metadata['id']
            else:
                return self.metadata['label']
    # @property
    # def status(self):
    #     if self.metadata:
    #         return self.metadata['status']
