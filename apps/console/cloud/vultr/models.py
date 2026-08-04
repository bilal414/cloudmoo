from django.db import models
from apps.console.cloud.models import (
    CloudInventoryTransientError,
    CloudValidationTransientError,
    CoreCloud,
    require_inventory_list,
    validate_provider_response,
)
from apps.console.utils.models import UtilCloud, UtilAsset
from apps.monitoring.metadata import redact_sensitive_metadata
import requests
from django.utils import timezone


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
        try:
            response = requests.get('https://api.vultr.com/v2/account', headers=headers, timeout=10)
            return validate_provider_response(response, 'Vultr')
        except CloudValidationTransientError:
            raise
        except Exception as error:
            raise CloudValidationTransientError(
                'Vultr validation temporarily unavailable'
            ) from error

    def sync_assets(self):
        self.sync_servers()
        self.sync_databases()
        self.sync_volumes()
        # Additional Vultr families are implemented in read-only service
        # modules.  Legacy server/volume/database tables remain the source of
        # truth for those three historical asset types.
        from apps.console.cloud.vultr.integration import sync_vultr_inventory

        sync_vultr_inventory(self)
        self.last_synced = timezone.now()
        self.save()

    def _make_api_call(self, endpoint, params=None):
        # Keep legacy callers on the same bounded GET-only transport as the
        # expanded Vultr resource families.  The endpoint is validated by the
        # shared client before any request is sent.
        from apps.console.cloud.vultr.resources_base import VultrClient

        return VultrClient(self.access_token).get_json(endpoint, params=params)

    def _paginate_api_call(self, endpoint):
        from apps.console.cloud.vultr.resources_base import list_vultr_collection

        collection_keys = {
            'instances': 'instances',
            'databases': 'databases',
            'blocks': 'blocks',
        }
        collection_key = collection_keys.get(endpoint)
        if collection_key is None:
            raise CloudInventoryTransientError('Vultr inventory endpoint is unsupported')
        return list_vultr_collection(self, endpoint, collection_key)

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
                server.metadata = redact_sensitive_metadata(instance_data)
                server.save()
            except CoreVultrServer.DoesNotExist:
                # Create new server with default ACTIVE monitoring
                server = CoreVultrServer.objects.create(
                    owner=self,
                    unique_id=instance_data['id'],
                    name=instance_data['label'] or instance_data['os'],
                    monitoring=CoreVultrServer.Monitoring.ACTIVE,
                    type=CoreVultrServer.Type.SERVER,
                    metadata=redact_sensitive_metadata(instance_data)
                )
            current_instance_ids.append(instance_data['id'])

        for server in CoreVultrServer.objects.filter(owner=self).exclude(
            unique_id__in=current_instance_ids
        ):
            server.monitoring = CoreVultrServer.Monitoring.NO_LONGER_EXISTS
            server.save()

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
                database.metadata = redact_sensitive_metadata(database_data)
                database.save()
            except CoreVultrDatabase.DoesNotExist:
                # Create new database with default ACTIVE monitoring
                database = CoreVultrDatabase.objects.create(
                    owner=self,
                    unique_id=database_data['id'],
                    name=database_data['label'],
                    monitoring=CoreVultrDatabase.Monitoring.ACTIVE,
                    type=CoreVultrServer.Type.DATABASE,
                    metadata=redact_sensitive_metadata(database_data)
                )
            current_database_ids.append(database_data['id'])

        for database in CoreVultrDatabase.objects.filter(owner=self).exclude(
            unique_id__in=current_database_ids
        ):
            database.monitoring = CoreVultrDatabase.Monitoring.NO_LONGER_EXISTS
            database.save()

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
                volume.metadata = redact_sensitive_metadata(volume_data)
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
                    metadata=redact_sensitive_metadata(volume_data)
                )
            current_volume_ids.append(volume_data['id'])

        for volume in CoreVultrVolume.objects.filter(owner=self).exclude(
            unique_id__in=current_volume_ids
        ):
            volume.monitoring = CoreVultrVolume.Monitoring.NO_LONGER_EXISTS
            volume.save()


class CoreVultrServer(UtilAsset):
    owner = models.ForeignKey(CoreVultrAccount, on_delete=models.CASCADE, related_name='servers')

    class Meta:
        db_table = "core_vultr_server"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.metadata is not None:
            self.metadata = redact_sensitive_metadata(self.metadata)
        return super().save(*args, **kwargs)

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

    def save(self, *args, **kwargs):
        if self.metadata is not None:
            self.metadata = redact_sensitive_metadata(self.metadata)
        return super().save(*args, **kwargs)


class CoreVultrVolume(UtilAsset):
    owner = models.ForeignKey(CoreVultrAccount, on_delete=models.CASCADE, related_name='volumes')

    class Meta:
        db_table = "core_vultr_volume"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.metadata is not None:
            self.metadata = redact_sensitive_metadata(self.metadata)
        return super().save(*args, **kwargs)

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
