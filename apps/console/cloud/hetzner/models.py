import logging

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


logger = logging.getLogger(__name__)

HETZNER_API_BASE = 'https://api.hetzner.cloud/v1'
HETZNER_PAGE_SIZE = 50
HETZNER_MAX_PAGES = 1000
HETZNER_REQUEST_TIMEOUT = 15


class CoreHetznerAccount(UtilCloud):
    cloud = models.ForeignKey(CoreCloud, on_delete=models.CASCADE, related_name="hetzner")
    access_token = models.CharField(max_length=1024)
    # Object Storage is an S3-compatible product and is intentionally kept
    # separate from the Cloud API token.  These fields are optional so the
    # Cloud API integration remains useful without S3 credentials.
    object_storage_access_key = models.CharField(max_length=255, blank=True, default='')
    object_storage_secret_key = models.CharField(max_length=1024, blank=True, default='')
    object_storage_region = models.CharField(max_length=16, blank=True, default='fsn1')

    class Meta:
        db_table = "core_hetzner_account"

    def __str__(self):
        return self.name

    def validate(self):
        try:
            headers = {'Authorization': f'Bearer {self.access_token}'}
            response = requests.get(
                f'{HETZNER_API_BASE}/servers',
                headers=headers,
                timeout=10,
            )
            return validate_provider_response(response, 'Hetzner')
        except CloudValidationTransientError:
            raise
        except Exception as error:
            raise CloudValidationTransientError(
                'Hetzner validation temporarily unavailable'
            ) from error

    def sync_assets(self):
        """Synchronize all supported read-only Hetzner asset families.

        Existing servers and volumes are kept in their historical models for
        backwards compatibility.  The extended resource adapter owns the
        additional Cloud API families and is imported lazily to avoid the
        circular model import between the account and resource models.
        """
        self.sync_servers()
        self.sync_volumes()
        from . import resources

        resources.sync_hetzner_inventory_assets(self)
        resources.sync_hetzner_object_storage_assets(self)
        self.last_synced = timezone.now()
        self.save()

    def _make_api_call(self, endpoint, params=None):
        headers = {'Authorization': f'Bearer {self.access_token}'}
        url = f'{HETZNER_API_BASE}/{endpoint.lstrip("/")}'
        try:
            response = requests.get(
                url,
                headers=headers,
                params=params,
                timeout=HETZNER_REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as error:
            raise CloudInventoryTransientError(
                'Hetzner inventory temporarily unavailable'
            ) from error
        except (TypeError, ValueError) as error:
            raise CloudInventoryTransientError(
                'Hetzner returned an invalid inventory response'
            ) from error

        if not isinstance(payload, dict):
            raise CloudInventoryTransientError(
                'Hetzner returned an invalid inventory response'
            )
        return payload

    @property
    def object_storage_configured(self):
        return bool(
            self.object_storage_access_key
            and self.object_storage_secret_key
            and self.object_storage_region
        )

    @property
    def object_storage_credentials(self):
        """Return S3 credentials for the provider checker without persistence."""
        return {
            'access_key': self.object_storage_access_key,
            'secret_key': self.object_storage_secret_key,
            'region': self.object_storage_region,
        }

    def _paginate_api_call(self, endpoint, collection_key, params=None, page_size=HETZNER_PAGE_SIZE):
        """Read a complete Cloud API collection or fail closed.

        Hetzner provides ``meta.pagination`` with nullable next/last page
        fields.  We follow the explicit next page when present and only use
        the page-size fallback for old/fixture responses that omit metadata.
        A missing or malformed collection never reconciles local assets away.
        """
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= HETZNER_PAGE_SIZE
        ):
            raise CloudInventoryTransientError(
                'Hetzner inventory page size is invalid'
            )
        base_params = dict(params or {})
        base_params.setdefault('per_page', page_size)
        requested_page_size = base_params.get('per_page')
        if (
            isinstance(requested_page_size, bool)
            or not isinstance(requested_page_size, int)
            or not 1 <= requested_page_size <= HETZNER_PAGE_SIZE
        ):
            raise CloudInventoryTransientError(
                'Hetzner inventory page size is invalid'
            )
        page_size = requested_page_size
        raw_page = base_params.pop('page', 1) or 1
        if isinstance(raw_page, bool) or not isinstance(raw_page, int) or raw_page < 1:
            raise CloudInventoryTransientError(
                'Hetzner inventory page is invalid'
            )
        page = raw_page
        all_items = []
        seen_ids = set()

        for _ in range(HETZNER_MAX_PAGES):
            request_params = {**base_params, 'page': page}
            payload = self._make_api_call(endpoint, request_params)
            items = require_inventory_list(payload, [collection_key], 'Hetzner')
            for item in items:
                if not isinstance(item, dict):
                    raise CloudInventoryTransientError(
                        'Hetzner returned an invalid inventory object'
                    )
                identifier = item.get('id')
                if (
                    isinstance(identifier, bool)
                    or not isinstance(identifier, (str, int))
                    or identifier in (None, '')
                ):
                    raise CloudInventoryTransientError(
                        'Hetzner returned an inventory object without an identifier'
                    )
                identifier = str(identifier).strip()
                if not identifier:
                    raise CloudInventoryTransientError(
                        'Hetzner returned an inventory object without an identifier'
                    )
                if identifier in seen_ids:
                    raise CloudInventoryTransientError(
                        'Hetzner returned a duplicate inventory identifier'
                    )
                seen_ids.add(identifier)
                all_items.append(item)

            has_meta = 'meta' in payload
            meta = payload.get('meta')
            if meta is not None and not isinstance(meta, dict):
                raise CloudInventoryTransientError(
                    'Hetzner returned an invalid pagination response'
                )
            pagination = meta.get('pagination') if isinstance(meta, dict) else None
            if has_meta and (
                not isinstance(meta, dict) or not isinstance(pagination, dict)
            ):
                raise CloudInventoryTransientError(
                    'Hetzner returned an invalid pagination response'
                )
            if isinstance(pagination, dict):
                total_entries = pagination.get('total_entries')
                if total_entries is not None and (
                    isinstance(total_entries, bool)
                    or not isinstance(total_entries, int)
                    or total_entries < len(all_items)
                ):
                    raise CloudInventoryTransientError(
                        'Hetzner returned an invalid pagination total'
                    )
                last_page = pagination.get('last_page')
                if last_page is not None and (
                    isinstance(last_page, bool)
                    or not isinstance(last_page, int)
                    or last_page < page
                ):
                    raise CloudInventoryTransientError(
                        'Hetzner returned an invalid pagination sequence'
                    )
                next_page = pagination.get('next_page')
                if next_page in (None, ''):
                    if (
                        (total_entries is not None and total_entries > len(all_items))
                        or (last_page is not None and page < last_page)
                    ):
                        raise CloudInventoryTransientError(
                            'Hetzner returned an incomplete pagination response'
                        )
                    return all_items
                if (
                    isinstance(next_page, bool)
                    or not isinstance(next_page, int)
                    or next_page <= page
                    or (last_page is not None and next_page > last_page)
                ):
                    raise CloudInventoryTransientError(
                        'Hetzner returned an invalid pagination sequence'
                    )
                page = next_page
                continue

            # Compatibility with old responses that omit pagination metadata.
            if len(items) < page_size:
                return all_items
            page += 1

        raise CloudInventoryTransientError(
            'Hetzner pagination exceeded the safety bound'
        )

    def sync_servers(self):
        all_servers = self._paginate_api_call('servers', 'servers')
        self._validate_legacy_records(all_servers, 'server')

        current_server_ids = []
        for server_data in all_servers:
            try:
                server = CoreHetznerServer.objects.get(
                    owner=self,
                    unique_id=server_data['id']
                )
                # Update server while preserving monitoring status
                server.name = server_data['name'][:100]
                server.type = CoreHetznerServer.Type.SERVER
                server.metadata = redact_sensitive_metadata(server_data)
                server.save()
            except CoreHetznerServer.DoesNotExist:
                # Create new server with default ACTIVE monitoring
                server = CoreHetznerServer.objects.create(
                    owner=self,
                    unique_id=server_data['id'],
                    name=server_data['name'][:100],
                    monitoring=CoreHetznerServer.Monitoring.ACTIVE,
                    type=CoreHetznerServer.Type.SERVER,
                    metadata=redact_sensitive_metadata(server_data)
                )
            current_server_ids.append(server_data['id'])

        CoreHetznerServer.objects.filter(owner=self).exclude(unique_id__in=current_server_ids).update(
            monitoring=CoreHetznerServer.Monitoring.NO_LONGER_EXISTS
        )

    def sync_volumes(self):
        all_volumes = self._paginate_api_call('volumes', 'volumes')
        self._validate_legacy_records(all_volumes, 'volume')

        current_volume_ids = []
        for volume_data in all_volumes:
            try:
                volume = CoreHetznerVolume.objects.get(
                    owner=self,
                    unique_id=volume_data['id']
                )
                # Update volume while preserving monitoring status
                volume.name = volume_data['name'][:100]
                volume.type = CoreHetznerServer.Type.VOLUME
                volume.metadata = redact_sensitive_metadata(volume_data)
                volume.save()
            except CoreHetznerVolume.DoesNotExist:
                # Create new volume with default ACTIVE monitoring
                volume = CoreHetznerVolume.objects.create(
                    owner=self,
                    unique_id=volume_data['id'],
                    name=volume_data['name'][:100],
                    monitoring=CoreHetznerVolume.Monitoring.ACTIVE,
                    type=CoreHetznerServer.Type.VOLUME,
                    metadata=redact_sensitive_metadata(volume_data)
                )
            current_volume_ids.append(volume_data['id'])

        CoreHetznerVolume.objects.filter(owner=self).exclude(unique_id__in=current_volume_ids).update(
            monitoring=CoreHetznerVolume.Monitoring.NO_LONGER_EXISTS
        )

    @staticmethod
    def _validate_legacy_records(records, resource_name):
        """Validate identity/display fields before legacy reconciliation writes."""
        for record in records:
            identifier = record.get('id') if isinstance(record, dict) else None
            name = record.get('name') if isinstance(record, dict) else None
            if (
                isinstance(identifier, bool)
                or not isinstance(identifier, (str, int))
                or identifier in (None, '')
                or not isinstance(name, str)
                or not name.strip()
                or len(str(identifier)) > 100
            ):
                raise CloudInventoryTransientError(
                    f'Hetzner returned an invalid {resource_name} inventory object'
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
        metadata = self.metadata if isinstance(self.metadata, dict) else {}
        return (metadata.get('public_net') or {}).get('ipv4', {}).get('ip')

    @property
    def public_ipv6(self):
        metadata = self.metadata if isinstance(self.metadata, dict) else {}
        return (metadata.get('public_net') or {}).get('ipv6', {}).get('ip')

    @property
    def provider_url(self):
        return f"https://console.hetzner.cloud/servers/{self.unique_id}"


class CoreHetznerVolume(UtilAsset):
    owner = models.ForeignKey(CoreHetznerAccount, on_delete=models.CASCADE, related_name='volumes')

    class Meta:
        db_table = "core_hetzner_volume"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        return f"https://console.hetzner.cloud/volumes/{self.unique_id}"

    # @property
    # def status(self):
    #     return self.metadata['status']
