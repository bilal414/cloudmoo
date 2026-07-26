from django.db import models
from apps.console.cloud.models import CoreCloud
import requests
from datetime import datetime

from apps.console.utils.models import UtilAsset, UtilCloud


class CoreDigitalOceanAccount(UtilCloud):
    cloud = models.ForeignKey(CoreCloud, on_delete=models.CASCADE, related_name="digitalocean")
    access_token = models.CharField(max_length=255)

    class Meta:
        db_table = "core_digitalocean_account"

    def __str__(self):
        return self.name

    def validate(self):
        try:
            headers = {'Authorization': f'Bearer {self.access_token}'}
            response = requests.get('https://api.digitalocean.com/v2/account', headers=headers, timeout=10)
            if response.status_code != 200:
                return False
            response.json()
            return True
        except Exception:
            return False

    def sync_assets(self):
        self.sync_servers()
        # self.sync_databases()
        self.sync_volumes()
        self.last_synced = datetime.now()
        self.save()

    def _make_api_call(self, endpoint):
        headers = {'Authorization': f'Bearer {self.access_token}'}
        url = f'https://api.digitalocean.com/v2/{endpoint}'
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        return response.json()

    def _paginate_api_call(self, endpoint):
        all_items = []
        next_url = f'https://api.digitalocean.com/v2/{endpoint}'

        while next_url:
            headers = {'Authorization': f'Bearer {self.access_token}'}
            response = requests.get(next_url, headers=headers)
            response.raise_for_status()
            data = response.json()
            all_items.extend(data.get(endpoint, []))
            next_url = data.get('links', {}).get('pages', {}).get('next')

        return all_items

    def sync_servers(self):
        all_droplets = self._paginate_api_call('droplets')

        current_droplet_ids = []
        for droplet_data in all_droplets:
            # Get existing server if any
            try:
                server = CoreDigitalOceanServer.objects.get(
                    owner=self,
                    unique_id=str(droplet_data['id'])
                )
                # Update server while preserving monitoring status
                server.name = droplet_data['name']
                server.type = CoreDigitalOceanServer.Type.SERVER
                server.metadata = droplet_data
                server.save()
            except CoreDigitalOceanServer.DoesNotExist:
                # Create new server with default ACTIVE monitoring
                server = CoreDigitalOceanServer.objects.create(
                    owner=self,
                    unique_id=str(droplet_data['id']),
                    name=droplet_data['name'],
                    monitoring=CoreDigitalOceanServer.Monitoring.ACTIVE,
                    type=CoreDigitalOceanServer.Type.SERVER,
                    metadata=droplet_data
                )
            current_droplet_ids.append(str(droplet_data['id']))

        CoreDigitalOceanServer.objects.filter(owner=self).exclude(unique_id__in=current_droplet_ids).update(
            monitoring=CoreDigitalOceanServer.Monitoring.NO_LONGER_EXISTS
        )

    def sync_databases(self):
        all_databases = self._paginate_api_call('databases')

        current_database_ids = []
        for database_data in all_databases:
            try:
                database = CoreDigitalOceanDatabase.objects.get(
                    owner=self,
                    unique_id=database_data['id']
                )
                database.name = database_data['name']
                database.type = CoreDigitalOceanServer.Type.DATABASE
                database.metadata = database_data
                database.save()
            except CoreDigitalOceanDatabase.DoesNotExist:
                database = CoreDigitalOceanDatabase.objects.create(
                    owner=self,
                    unique_id=database_data['id'],
                    name=database_data['name'],
                    monitoring=CoreDigitalOceanDatabase.Monitoring.ACTIVE,
                    type=CoreDigitalOceanServer.Type.DATABASE,
                    metadata=database_data
                )
            current_database_ids.append(database_data['id'])

        CoreDigitalOceanDatabase.objects.filter(owner=self).exclude(unique_id__in=current_database_ids).update(
            monitoring=CoreDigitalOceanDatabase.Monitoring.NO_LONGER_EXISTS
        )

    def sync_volumes(self):
        all_volumes = self._paginate_api_call('volumes')

        current_volume_ids = []
        for volume_data in all_volumes:
            try:
                volume = CoreDigitalOceanVolume.objects.get(
                    owner=self,
                    unique_id=volume_data['id']
                )
                volume.name = volume_data['name']
                volume.type = CoreDigitalOceanServer.Type.VOLUME
                volume.metadata = volume_data
                volume.save()
            except CoreDigitalOceanVolume.DoesNotExist:
                volume = CoreDigitalOceanVolume.objects.create(
                    owner=self,
                    unique_id=volume_data['id'],
                    name=volume_data['name'],
                    monitoring=CoreDigitalOceanVolume.Monitoring.ACTIVE,
                    type=CoreDigitalOceanServer.Type.VOLUME,
                    metadata=volume_data
                )
            current_volume_ids.append(volume_data['id'])

        CoreDigitalOceanVolume.objects.filter(owner=self).exclude(unique_id__in=current_volume_ids).update(
            monitoring=CoreDigitalOceanVolume.Monitoring.NO_LONGER_EXISTS
        )

class CoreDigitalOceanServer(UtilAsset):
    owner = models.ForeignKey(CoreDigitalOceanAccount, on_delete=models.CASCADE, related_name='servers')

    class Meta:
        db_table = "core_digitalocean_server"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        return f"https://cloud.digitalocean.com/droplets/{self.unique_id}"

    # @property
    # def status(self):
    #     try:
    #         headers = {'Authorization': f'Bearer {self.owner.access_token}'}
    #         url = f'https://api.digitalocean.com/v2/droplets/{self.unique_id}'
    #         response = requests.get(url, headers=headers)
    #         response.raise_for_status()
    #         data = response.json()
    #         return data['droplet']['status']
    #     except requests.RequestException as e:
    #         error_status = 'not_found' if e.response.status_code == 404 else 'invalid_access_token' if e.response.status_code == 401 else 'error'
    #         return error_status
    #     except KeyError:
    #         print(f"Unexpected response format for server {self.name}")
    #         return "unknown"

    def check_status(self):
        api_url = f'https://api.digitalocean.com/v2/droplets/{self.unique_id}'
        headers = {
            'Authorization': f'Bearer {self.owner.access_token}',
            'Content-Type': 'application/json'
        }
        try:
            response = requests.get(api_url, headers=headers)
            response.raise_for_status()
            data = response.json()
            current_status = data['droplet']['status']
            return current_status, data
        except requests.exceptions.RequestException as e:
            error_status = 'not_found' if e.response.status_code == 404 else 'invalid_access_token' if e.response.status_code == 401 else 'error'
            return error_status, str(e)


class CoreDigitalOceanDatabase(UtilAsset):
    owner = models.ForeignKey(CoreDigitalOceanAccount, on_delete=models.CASCADE, related_name='databases')

    class Meta:
        db_table = "core_digitalocean_database"

    def __str__(self):
        return self.name


class CoreDigitalOceanVolume(UtilAsset):
    owner = models.ForeignKey(CoreDigitalOceanAccount, on_delete=models.CASCADE, related_name='volumes')

    class Meta:
        db_table = "core_digitalocean_volume"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        return f"https://cloud.digitalocean.com/volumes/{self.unique_id}"

