from django.db import models
from django.utils import timezone

import oci
from oci.exceptions import ServiceError

from apps.console.cloud.models import (
    CloudInventoryTransientError,
    CloudValidationTransientError,
    CoreCloud,
)
from apps.console.utils.models import UtilAsset, UtilCloud


class CoreOracleAccount(UtilCloud):
    cloud = models.ForeignKey(CoreCloud, on_delete=models.CASCADE, related_name="oracle")
    tenancy_ocid = models.CharField(max_length=255, blank=True, default='')
    user_ocid = models.CharField(max_length=255, blank=True, default='')
    fingerprint = models.CharField(max_length=255, blank=True, default='')
    region = models.CharField(max_length=255, blank=True, default='')
    # RSA private key in PEM format used to sign OCI API requests.
    private_key = models.TextField(blank=True, default='')

    class Meta:
        db_table = "core_oracle_account"

    def __str__(self):
        return self.name

    @property
    def access_token(self):
        return {
            'tenancy_ocid': self.tenancy_ocid,
            'user_ocid': self.user_ocid,
            'fingerprint': self.fingerprint,
            'region': self.region,
            'private_key': self.private_key,
        }

    def _oci_config(self):
        return {
            'tenancy': self.tenancy_ocid,
            'user': self.user_ocid,
            'fingerprint': self.fingerprint,
            'region': self.region,
            'key_content': self.private_key,
        }

    def validate(self):
        try:
            identity_client = oci.identity.IdentityClient(self._oci_config())
            identity_client.get_tenancy(self.tenancy_ocid)
            return True
        except ServiceError as error:
            # Client errors indicate invalid or insufficient credentials. Rate
            # limits and server errors are transient and must not disable
            # monitoring.
            status = getattr(error, 'status', None)
            if status is not None and 400 <= status < 500 and status != 429:
                return False
            raise CloudValidationTransientError(
                'Oracle Cloud validation temporarily unavailable'
            ) from error
        except Exception as error:
            raise CloudValidationTransientError(
                'Oracle Cloud validation temporarily unavailable'
            ) from error

    def sync_assets(self):
        self.sync_instances()
        self.sync_volumes()
        self.last_synced = timezone.now()
        self.save()

    def sync_instances(self):
        compute_client = oci.core.ComputeClient(self._oci_config())
        response = oci.pagination.list_call_get_all_results(
            compute_client.list_instances,
            compartment_id=self.tenancy_ocid,
        )
        instances = response.data
        if not isinstance(instances, list):
            raise CloudInventoryTransientError(
                'Oracle Cloud returned an invalid inventory collection'
            )

        current_instance_ids = []
        for instance in instances:
            instance_data = oci.util.to_dict(instance)
            CoreOracleInstance.objects.update_or_create(
                owner=self,
                unique_id=instance_data['id'],
                defaults={
                    'name': instance_data.get('display_name') or instance_data['id'],
                    'type': CoreOracleInstance.Type.SERVER,
                    'metadata': instance_data
                }
            )
            current_instance_ids.append(instance_data['id'])

        CoreOracleInstance.objects.filter(owner=self).exclude(unique_id__in=current_instance_ids).update(
            monitoring=CoreOracleInstance.Monitoring.NO_LONGER_EXISTS
        )

    def sync_volumes(self):
        blockstorage_client = oci.core.BlockstorageClient(self._oci_config())
        response = oci.pagination.list_call_get_all_results(
            blockstorage_client.list_volumes,
            compartment_id=self.tenancy_ocid,
        )
        volumes = response.data
        if not isinstance(volumes, list):
            raise CloudInventoryTransientError(
                'Oracle Cloud returned an invalid inventory collection'
            )

        current_volume_ids = []
        for volume in volumes:
            volume_data = oci.util.to_dict(volume)
            CoreOracleVolume.objects.update_or_create(
                owner=self,
                unique_id=volume_data['id'],
                defaults={
                    'name': volume_data.get('display_name') or volume_data['id'],
                    'type': CoreOracleVolume.Type.VOLUME,
                    'metadata': volume_data
                }
            )
            current_volume_ids.append(volume_data['id'])

        CoreOracleVolume.objects.filter(owner=self).exclude(unique_id__in=current_volume_ids).update(
            monitoring=CoreOracleVolume.Monitoring.NO_LONGER_EXISTS
        )


class CoreOracleInstance(UtilAsset):
    owner = models.ForeignKey(CoreOracleAccount, on_delete=models.CASCADE, related_name='servers')

    class Meta:
        db_table = "core_oracle_instance"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        return f"https://cloud.oracle.com/compute/instances/{self.unique_id}?region={self.owner.region}"


class CoreOracleVolume(UtilAsset):
    owner = models.ForeignKey(CoreOracleAccount, on_delete=models.CASCADE, related_name='volumes')

    class Meta:
        db_table = "core_oracle_volume"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        return f"https://cloud.oracle.com/block-storage/volumes/{self.unique_id}?region={self.owner.region}"
