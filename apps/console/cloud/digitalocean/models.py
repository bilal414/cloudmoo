import hashlib
import re
from urllib.parse import quote

import boto3
import requests
from botocore.config import Config
from django.db import models
from django.utils import timezone

from apps.console.cloud.models import (
    CloudInventoryTransientError,
    CloudValidationTransientError,
    CoreCloud,
    require_inventory_list,
    validate_provider_response,
)
from apps.console.utils.models import UtilAsset, UtilCloud
from apps.monitoring.checks.base import _serialize_datetime


DIGITALOCEAN_API_BASE = 'https://api.digitalocean.com/v2'
SPACES_DEFAULT_REGION = 'nyc3'
SPACES_REGION_PATTERN = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
SPACES_CLIENT_CONFIG = Config(
    connect_timeout=5,
    read_timeout=15,
    retries={'mode': 'standard', 'max_attempts': 2},
)


class CoreDigitalOceanAccount(UtilCloud):
    cloud = models.ForeignKey(CoreCloud, on_delete=models.CASCADE, related_name="digitalocean")
    access_token = models.CharField(max_length=255)

    # Spaces uses S3-compatible credentials rather than the DigitalOcean
    # control-plane token. They are optional so existing DigitalOcean
    # connections continue to work without object-storage credentials.
    spaces_access_key = models.CharField(max_length=255, blank=True, default='')
    spaces_secret_key = models.CharField(max_length=255, blank=True, default='')
    spaces_region = models.CharField(max_length=64, blank=True, default=SPACES_DEFAULT_REGION)

    class Meta:
        db_table = "core_digitalocean_account"

    def __str__(self):
        return self.name

    def validate(self):
        try:
            headers = {'Authorization': f'Bearer {self.access_token}'}
            response = requests.get(
                f'{DIGITALOCEAN_API_BASE}/account',
                headers=headers,
                timeout=10,
            )
            return validate_provider_response(response, 'DigitalOcean')
        except CloudValidationTransientError:
            raise
        except Exception as error:
            raise CloudValidationTransientError(
                'DigitalOcean validation temporarily unavailable'
            ) from error

    @property
    def spaces_configured(self):
        return bool(
            self.spaces_access_key
            and self.spaces_secret_key
            and self.spaces_region
        )

    @staticmethod
    def _normalize_spaces_region(region):
        normalized = str(region or SPACES_DEFAULT_REGION).strip().lower()
        if not SPACES_REGION_PATTERN.fullmatch(normalized):
            raise CloudInventoryTransientError(
                'DigitalOcean Spaces returned an invalid region configuration'
            )
        return normalized

    @property
    def spaces_credentials(self):
        return {
            'access_key': self.spaces_access_key,
            'secret_key': self.spaces_secret_key,
            'region': self.spaces_region or SPACES_DEFAULT_REGION,
        }

    def sync_assets(self):
        # Fetch Droplets once because their IDs are also the scope for the
        # account's backup inventory.
        all_droplets = self._paginate_api_call('droplets')
        self.sync_servers(all_droplets)
        self.sync_backups(all_droplets)
        self.sync_databases()
        self.sync_volumes()
        self.sync_snapshots()
        self.sync_reserved_ips()
        self.sync_firewalls()
        self.sync_load_balancers()
        self.sync_apps()
        self.sync_container_registries()
        self.sync_spaces()
        self.sync_kubernetes()
        self.sync_vpcs()
        self.sync_vpc_peerings()
        self.sync_vpc_nat_gateways()
        self.sync_domains()
        self.sync_cdn_endpoints()
        self.sync_certificates()
        self.last_synced = timezone.now()
        self.save()

    def _headers(self):
        return {'Authorization': f'Bearer {self.access_token}'}

    def _make_api_call(self, endpoint):
        url = f'{DIGITALOCEAN_API_BASE}/{endpoint.lstrip("/")}'
        response = requests.get(url, headers=self._headers(), timeout=15)
        response.raise_for_status()
        return response.json()

    def _paginate_api_call(
        self,
        endpoint,
        collection_key=None,
        allow_unpaginated=False,
        allow_null_empty=False,
    ):
        """Read a DigitalOcean collection without ever treating a partial page as empty."""
        collection_key = collection_key or endpoint.rsplit('/', 1)[-1]
        all_items = []
        next_url = f'{DIGITALOCEAN_API_BASE}/{endpoint.lstrip("/")}'
        visited_urls = set()

        while next_url:
            if next_url in visited_urls or len(visited_urls) >= 10000:
                raise CloudInventoryTransientError(
                    'DigitalOcean returned an invalid pagination sequence'
                )
            visited_urls.add(next_url)

            response = requests.get(next_url, headers=self._headers(), timeout=15)
            response.raise_for_status()
            data = response.json()
            collection = data.get(collection_key) if isinstance(data, dict) else None
            if (
                allow_null_empty
                and isinstance(data, dict)
                and collection is None
            ):
                meta = data.get('meta')
                if isinstance(meta, dict) and meta.get('total') == 0:
                    # The live VPC NAT gateway endpoint returns a
                    # present-but-null collection alongside meta total=0 for an
                    # empty account; the pagination tail terminates normally.
                    collection = []
                elif 'meta' not in data and 'links' not in data:
                    # The databases endpoint answers a bare {"databases": null}
                    # with no pagination metadata at all; the confirmed-empty
                    # collection is the complete inventory. Keep rejecting
                    # unexplained nulls for endpoints that do not opt in.
                    return all_items
            if collection is None:
                all_items.extend(require_inventory_list(data, [collection_key], 'DigitalOcean'))
            elif isinstance(collection, list):
                all_items.extend(collection)
            else:
                all_items.extend(require_inventory_list(data, [collection_key], 'DigitalOcean'))

            links = data.get('links')
            pages = links.get('pages') if isinstance(links, dict) else None
            if pages is None:
                # DigitalOcean omits pagination links (returns ``links: {}``)
                # when the collection fits on one page. Only accept that as a
                # terminal page when the API confirms the complete total.
                meta = data.get('meta')
                total = meta.get('total') if isinstance(meta, dict) else None
                if (
                    isinstance(total, int)
                    and not isinstance(total, bool)
                    and total == len(all_items)
                ):
                    next_url = None
                    continue
                if allow_unpaginated and not isinstance(meta, dict):
                    # Some nested DigitalOcean list endpoints, notably DOKS
                    # node pools, are complete collections but omit both
                    # pagination links and the top-level ``meta.total``.
                    # Callers must opt in per endpoint so ordinary paginated
                    # inventories remain fail-closed.
                    next_url = None
                    continue

            if not isinstance(pages, dict):
                raise CloudInventoryTransientError(
                    'DigitalOcean returned an incomplete pagination response'
                )
            next_url = pages.get('next')
            if next_url is not None and not isinstance(next_url, str):
                raise CloudInventoryTransientError(
                    'DigitalOcean returned an invalid pagination URL'
                )

        return all_items

    @staticmethod
    def _item_identifier(item, key='id'):
        if not isinstance(item, dict):
            raise CloudInventoryTransientError(
                'DigitalOcean returned an invalid resource object'
            )
        identifier = item.get(key)
        if identifier is None or identifier == '':
            raise CloudInventoryTransientError(
                'DigitalOcean returned a resource without an identifier'
            )
        return str(identifier)

    @staticmethod
    def _display_name(item, identifier, key='name'):
        name = item.get(key) if isinstance(item, dict) else None
        if not name:
            name = identifier
        return str(name)[:100]

    def _sync_collection(
        self,
        model,
        endpoint,
        asset_type,
        collection_key=None,
        identifier_key='id',
        name_key='name',
        name_getter=None,
        allow_null_empty=False,
    ):
        items = self._paginate_api_call(
            endpoint,
            collection_key=collection_key,
            allow_null_empty=allow_null_empty,
        )
        current_ids = []

        for item in items:
            identifier = self._item_identifier(item, identifier_key)
            name = (
                name_getter(item, identifier)
                if name_getter
                else self._display_name(item, identifier, name_key)
            )
            if not name:
                name = identifier
            name = str(name)[:100]
            metadata = item if isinstance(item, dict) else None

            asset, created = model.objects.get_or_create(
                owner=self,
                unique_id=identifier,
                defaults={
                    'name': name,
                    'monitoring': model.Monitoring.ACTIVE,
                    'type': asset_type,
                    'metadata': metadata,
                },
            )
            if not created:
                asset.name = name
                asset.type = asset_type
                asset.metadata = metadata
                # A resource that was temporarily absent during a previous
                # sync is alive again; preserve explicit DISABLED choices.
                if asset.monitoring == model.Monitoring.NO_LONGER_EXISTS:
                    asset.monitoring = model.Monitoring.ACTIVE
                asset.save()
            current_ids.append(identifier)

        model.objects.filter(owner=self).exclude(unique_id__in=current_ids).update(
            monitoring=model.Monitoring.NO_LONGER_EXISTS
        )

    def sync_servers(self, all_droplets=None):
        all_droplets = all_droplets if all_droplets is not None else self._paginate_api_call('droplets')
        # Keep the established server table and semantics intact while using
        # the same strict inventory validation as the new resources.
        current_ids = []
        for droplet_data in all_droplets:
            identifier = self._item_identifier(droplet_data)
            name = self._display_name(droplet_data, identifier)
            server, created = CoreDigitalOceanServer.objects.get_or_create(
                owner=self,
                unique_id=identifier,
                defaults={
                    'name': name,
                    'monitoring': CoreDigitalOceanServer.Monitoring.ACTIVE,
                    'type': CoreDigitalOceanServer.Type.SERVER,
                    'metadata': droplet_data,
                },
            )
            if not created:
                server.name = name
                server.type = CoreDigitalOceanServer.Type.SERVER
                server.metadata = droplet_data
                if server.monitoring == server.Monitoring.NO_LONGER_EXISTS:
                    server.monitoring = server.Monitoring.ACTIVE
                server.save()
            current_ids.append(identifier)

        CoreDigitalOceanServer.objects.filter(owner=self).exclude(
            unique_id__in=current_ids
        ).update(monitoring=CoreDigitalOceanServer.Monitoring.NO_LONGER_EXISTS)
        return all_droplets

    def sync_backups(self, all_droplets):
        """Inventory automatic Droplet backups through each Droplet's backups endpoint."""
        all_backups = []
        for droplet_data in all_droplets:
            if not isinstance(droplet_data, dict):
                raise CloudInventoryTransientError(
                    'DigitalOcean returned an invalid Droplet object'
                )
            droplet_id = self._item_identifier(droplet_data)
            backup_ids = droplet_data.get('backup_ids')
            if not isinstance(backup_ids, list):
                raise CloudInventoryTransientError(
                    'DigitalOcean returned incomplete Droplet backup metadata'
                )
            # Avoid one request for Droplets that have never produced a
            # backup, while still using the authoritative list endpoint for
            # every Droplet that advertises backups.
            if not droplet_data.get('backup_ids'):
                continue
            for backup in self._paginate_api_call(
                f'droplets/{droplet_id}/backups',
                collection_key='backups',
            ):
                self._item_identifier(backup)
                all_backups.append({**backup, 'droplet_id': droplet_data['id']})

        def backup_name(item, identifier):
            created_at = item.get('created_at', 'unknown')
            return f"Droplet {item.get('droplet_id', 'unknown')} backup {created_at}"

        self._sync_items(
            CoreDigitalOceanBackup,
            all_backups,
            CoreDigitalOceanBackup.Type.BACKUP,
            name_getter=backup_name,
        )

    def _sync_items(self, model, items, asset_type, name_getter=None, identifier_key='id'):
        current_ids = []
        for item in items:
            identifier = self._item_identifier(item, identifier_key)
            name = name_getter(item, identifier) if name_getter else self._display_name(item, identifier)
            asset, created = model.objects.get_or_create(
                owner=self,
                unique_id=identifier,
                defaults={
                    'name': str(name)[:100],
                    'monitoring': model.Monitoring.ACTIVE,
                    'type': asset_type,
                    'metadata': item,
                },
            )
            if not created:
                asset.name = str(name)[:100]
                asset.type = asset_type
                asset.metadata = item
                if asset.monitoring == model.Monitoring.NO_LONGER_EXISTS:
                    asset.monitoring = model.Monitoring.ACTIVE
                asset.save()
            current_ids.append(identifier)
        model.objects.filter(owner=self).exclude(unique_id__in=current_ids).update(
            monitoring=model.Monitoring.NO_LONGER_EXISTS
        )

    def sync_databases(self):
        self._sync_collection(
            CoreDigitalOceanDatabase,
            'databases',
            CoreDigitalOceanDatabase.Type.DATABASE,
            # Accounts without database clusters get a bare null collection.
            allow_null_empty=True,
        )

    def sync_volumes(self):
        self._sync_collection(
            CoreDigitalOceanVolume,
            'volumes',
            CoreDigitalOceanVolume.Type.VOLUME,
        )

    def sync_snapshots(self):
        self._sync_collection(
            CoreDigitalOceanSnapshot,
            'snapshots',
            CoreDigitalOceanSnapshot.Type.SNAPSHOT,
        )

    def sync_reserved_ips(self):
        reserved_ips = []
        for item in self._paginate_api_call('reserved_ips', collection_key='reserved_ips'):
            self._item_identifier(item, 'ip')
            reserved_ips.append({**item, 'ip_version': 4})
        for item in self._paginate_api_call(
            'reserved_ipv6',
            collection_key='reserved_ipv6s',
        ):
            self._item_identifier(item, 'ip')
            reserved_ips.append({**item, 'ip_version': 6})
        self._sync_items(
            CoreDigitalOceanReservedIP,
            reserved_ips,
            CoreDigitalOceanReservedIP.Type.RESERVED_IP,
            name_getter=lambda item, identifier: f"Reserved IP {identifier}",
            identifier_key='ip',
        )

    def sync_firewalls(self):
        self._sync_collection(
            CoreDigitalOceanFirewall,
            'firewalls',
            CoreDigitalOceanFirewall.Type.FIREWALL,
        )

    def sync_load_balancers(self):
        self._sync_collection(
            CoreDigitalOceanLoadBalancer,
            'load_balancers',
            CoreDigitalOceanLoadBalancer.Type.LOAD_BALANCER,
        )

    def sync_apps(self):
        self._sync_collection(
            CoreDigitalOceanApp,
            'apps',
            CoreDigitalOceanApp.Type.APP_PLATFORM,
            # Accounts without App Platform apps omit the collection entirely
            # and answer only {"meta": {"total": 0}}.
            allow_null_empty=True,
            name_getter=lambda item, identifier: (
                item.get('spec', {}).get('name')
                if isinstance(item.get('spec'), dict)
                else None
            ) or item.get('name') or identifier,
        )

    def sync_container_registries(self):
        # DigitalOcean exposes a single registry per account at /v2/registry;
        # the endpoint answers 404 when the account has no registry.
        try:
            data = self._make_api_call('registry')
        except requests.HTTPError as error:
            if error.response is not None and error.response.status_code == 404:
                data = {}
            else:
                raise
        registry = data.get('registry') if isinstance(data, dict) else None
        items = [registry] if isinstance(registry, dict) else []
        self._sync_items(
            CoreDigitalOceanContainerRegistry,
            items,
            CoreDigitalOceanContainerRegistry.Type.CONTAINER_REGISTRY,
            identifier_key='name',
        )

    @staticmethod
    def _stable_identifier(value):
        """Keep provider identifiers inside UtilAsset's 100-character limit."""
        value = str(value)
        if len(value) <= 100:
            return value
        return f"do-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:32]}"

    def sync_kubernetes(self):
        """Inventory DOKS clusters and their authoritative node-pool lists."""
        clusters = self._paginate_api_call(
            'kubernetes/clusters',
            collection_key='kubernetes_clusters',
        )
        self._sync_items(
            CoreDigitalOceanKubernetesCluster,
            clusters,
            CoreDigitalOceanKubernetesCluster.Type.KUBERNETES_CLUSTER,
        )

        node_pools = []
        for cluster in clusters:
            cluster_id = self._item_identifier(cluster)
            for node_pool in self._paginate_api_call(
                f'kubernetes/clusters/{quote(cluster_id, safe="")}/node_pools',
                collection_key='node_pools',
                allow_unpaginated=True,
            ):
                pool_id = self._item_identifier(node_pool)
                node_pools.append({
                    **node_pool,
                    'cluster_id': cluster_id,
                    'pool_id': pool_id,
                })

        self._sync_items(
            CoreDigitalOceanKubernetesNodePool,
            node_pools,
            CoreDigitalOceanKubernetesNodePool.Type.KUBERNETES_NODE_POOL,
            identifier_key='pool_id',
        )

    def sync_vpcs(self):
        self._sync_collection(
            CoreDigitalOceanVPC,
            'vpcs',
            CoreDigitalOceanVPC.Type.VPC,
            collection_key='vpcs',
        )

    def sync_vpc_peerings(self):
        self._sync_collection(
            CoreDigitalOceanVPCPeering,
            'vpc_peerings',
            CoreDigitalOceanVPCPeering.Type.VPC_PEERING,
            collection_key='vpc_peerings',
        )

    def sync_vpc_nat_gateways(self):
        items = self._paginate_api_call(
            'vpc_nat_gateways',
            collection_key='vpc_nat_gateways',
            allow_null_empty=True,
        )
        self._sync_items(
            CoreDigitalOceanVPCNATGateway,
            items,
            CoreDigitalOceanVPCNATGateway.Type.NAT_GATEWAY,
        )

    def sync_domains(self):
        domains = self._paginate_api_call('domains', collection_key='domains')
        enriched_domains = []
        dns_records = []
        for domain in domains:
            domain_name = self._item_identifier(domain, 'name')
            enriched_domain = {
                **domain,
                'domain_name': domain_name,
                'cloudmoo_identifier': self._stable_identifier(domain_name),
            }
            enriched_domains.append(enriched_domain)

            for record in self._paginate_api_call(
                f'domains/{quote(domain_name, safe="")}/records',
                collection_key='domain_records',
            ):
                record_id = self._item_identifier(record)
                dns_records.append({
                    **record,
                    'domain_name': domain_name,
                    'record_id': record_id,
                    'cloudmoo_identifier': self._stable_identifier(
                        f'{domain_name}:{record_id}'
                    ),
                })

        self._sync_items(
            CoreDigitalOceanDomain,
            enriched_domains,
            CoreDigitalOceanDomain.Type.DOMAIN,
            identifier_key='cloudmoo_identifier',
            name_getter=lambda item, _identifier: item['domain_name'],
        )
        self._sync_items(
            CoreDigitalOceanDNSRecord,
            dns_records,
            CoreDigitalOceanDNSRecord.Type.DNS_RECORD,
            identifier_key='cloudmoo_identifier',
            name_getter=lambda item, _identifier: (
                f"{item.get('type', 'DNS')} {item.get('name') or '@'}"
            ),
        )

    def sync_cdn_endpoints(self):
        self._sync_collection(
            CoreDigitalOceanCDNEndpoint,
            'cdn/endpoints',
            CoreDigitalOceanCDNEndpoint.Type.CDN_ENDPOINT,
            collection_key='endpoints',
        )

    def sync_certificates(self):
        self._sync_collection(
            CoreDigitalOceanCertificate,
            'certificates',
            CoreDigitalOceanCertificate.Type.CERTIFICATE,
            collection_key='certificates',
        )

    def _spaces_client(self):
        region = self._normalize_spaces_region(self.spaces_region)
        return boto3.client(
            's3',
            region_name=region,
            endpoint_url=(
                f"https://{region}"
                ".digitaloceanspaces.com"
            ),
            aws_access_key_id=self.spaces_access_key,
            aws_secret_access_key=self.spaces_secret_key,
            config=SPACES_CLIENT_CONFIG,
        )

    def sync_spaces(self):
        # The control-plane token cannot enumerate Space buckets. Do not mark
        # an existing inventory as deleted when the optional S3 credentials
        # are not configured; disable those checks until credentials return.
        if not self.spaces_configured:
            CoreDigitalOceanSpace.objects.filter(owner=self).exclude(
                monitoring=CoreDigitalOceanSpace.Monitoring.NO_LONGER_EXISTS
            ).update(monitoring=CoreDigitalOceanSpace.Monitoring.DISABLED)
            return

        try:
            region = self._normalize_spaces_region(self.spaces_region)
            response = self._spaces_client().list_buckets()
            buckets = response.get('Buckets') if isinstance(response, dict) else None
            if not isinstance(buckets, list):
                raise CloudInventoryTransientError(
                    'DigitalOcean Spaces returned an invalid bucket collection'
                )
            serialized_buckets = []
            for bucket in buckets:
                if not isinstance(bucket, dict) or not bucket.get('Name'):
                    raise CloudInventoryTransientError(
                        'DigitalOcean Spaces returned an invalid bucket object'
                    )
                serialized_buckets.append(_serialize_datetime({
                    **bucket,
                    'region': region,
                }))
            self._sync_items(
                CoreDigitalOceanSpace,
                serialized_buckets,
                CoreDigitalOceanSpace.Type.OBJECT_STORAGE,
                identifier_key='Name',
                name_getter=lambda item, identifier: identifier,
            )
        except CloudInventoryTransientError:
            raise
        except Exception as error:
            raise CloudInventoryTransientError(
                'DigitalOcean Spaces inventory temporarily unavailable'
            ) from error


class CoreDigitalOceanServer(UtilAsset):
    owner = models.ForeignKey(CoreDigitalOceanAccount, on_delete=models.CASCADE, related_name='servers')

    class Meta:
        db_table = "core_digitalocean_server"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        return f"https://cloud.digitalocean.com/droplets/{self.unique_id}"

    def check_status(self):
        api_url = f'{DIGITALOCEAN_API_BASE}/droplets/{self.unique_id}'
        try:
            response = requests.get(
                api_url,
                headers=self.owner._headers(),
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()
            current_status = data['droplet']['status']
            return current_status, data
        except requests.exceptions.RequestException as error:
            status_code = getattr(getattr(error, 'response', None), 'status_code', None)
            error_status = (
                'not_found' if status_code == 404
                else 'invalid_access_token' if status_code in (401, 403)
                else 'error'
            )
            return error_status, str(error)


class CoreDigitalOceanDatabase(UtilAsset):
    owner = models.ForeignKey(CoreDigitalOceanAccount, on_delete=models.CASCADE, related_name='databases')

    class Meta:
        db_table = "core_digitalocean_database"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        return f"https://cloud.digitalocean.com/databases/{self.unique_id}"


class CoreDigitalOceanVolume(UtilAsset):
    owner = models.ForeignKey(CoreDigitalOceanAccount, on_delete=models.CASCADE, related_name='volumes')

    class Meta:
        db_table = "core_digitalocean_volume"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        return f"https://cloud.digitalocean.com/volumes/{self.unique_id}"


class CoreDigitalOceanSnapshot(UtilAsset):
    owner = models.ForeignKey(CoreDigitalOceanAccount, on_delete=models.CASCADE, related_name='snapshots')

    class Meta:
        db_table = "core_digitalocean_snapshot"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        return f"https://cloud.digitalocean.com/snapshots/{self.unique_id}"


class CoreDigitalOceanBackup(UtilAsset):
    owner = models.ForeignKey(CoreDigitalOceanAccount, on_delete=models.CASCADE, related_name='backups')

    class Meta:
        db_table = "core_digitalocean_backup"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        return f"https://cloud.digitalocean.com/images/{self.unique_id}"


class CoreDigitalOceanReservedIP(UtilAsset):
    owner = models.ForeignKey(CoreDigitalOceanAccount, on_delete=models.CASCADE, related_name='reserved_ips')

    class Meta:
        db_table = "core_digitalocean_reserved_ip"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        return "https://cloud.digitalocean.com/networking/reserved-ips"


class CoreDigitalOceanFirewall(UtilAsset):
    owner = models.ForeignKey(CoreDigitalOceanAccount, on_delete=models.CASCADE, related_name='firewalls')

    class Meta:
        db_table = "core_digitalocean_firewall"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        return f"https://cloud.digitalocean.com/networking/firewalls/{self.unique_id}"


class CoreDigitalOceanLoadBalancer(UtilAsset):
    owner = models.ForeignKey(CoreDigitalOceanAccount, on_delete=models.CASCADE, related_name='load_balancers')

    class Meta:
        db_table = "core_digitalocean_load_balancer"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        return f"https://cloud.digitalocean.com/networking/load-balancers/{self.unique_id}"


class CoreDigitalOceanApp(UtilAsset):
    owner = models.ForeignKey(CoreDigitalOceanAccount, on_delete=models.CASCADE, related_name='apps')

    class Meta:
        db_table = "core_digitalocean_app"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        return f"https://cloud.digitalocean.com/apps/{self.unique_id}"


class CoreDigitalOceanSpace(UtilAsset):
    owner = models.ForeignKey(CoreDigitalOceanAccount, on_delete=models.CASCADE, related_name='spaces')

    class Meta:
        db_table = "core_digitalocean_space"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        return f"https://cloud.digitalocean.com/spaces/{self.unique_id}"

    @property
    def monitoring_credentials(self):
        return self.owner.spaces_credentials


class CoreDigitalOceanContainerRegistry(UtilAsset):
    owner = models.ForeignKey(CoreDigitalOceanAccount, on_delete=models.CASCADE, related_name='container_registries')

    class Meta:
        db_table = "core_digitalocean_container_registry"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        return f"https://cloud.digitalocean.com/registry/{self.unique_id}"


class CoreDigitalOceanKubernetesCluster(UtilAsset):
    owner = models.ForeignKey(
        CoreDigitalOceanAccount,
        on_delete=models.CASCADE,
        related_name='kubernetes_clusters',
    )

    class Meta:
        db_table = "core_digitalocean_kubernetes_cluster"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        return f"https://cloud.digitalocean.com/kubernetes/clusters/{self.unique_id}"


class CoreDigitalOceanKubernetesNodePool(UtilAsset):
    owner = models.ForeignKey(
        CoreDigitalOceanAccount,
        on_delete=models.CASCADE,
        related_name='kubernetes_node_pools',
    )

    class Meta:
        db_table = "core_digitalocean_kubernetes_node_pool"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        cluster_id = (self.metadata or {}).get('cluster_id', self.unique_id)
        return f"https://cloud.digitalocean.com/kubernetes/clusters/{cluster_id}"

    @property
    def monitoring_credentials(self):
        return {
            'access_token': self.owner.access_token,
            'cluster_id': (self.metadata or {}).get('cluster_id'),
            'pool_id': (self.metadata or {}).get('pool_id', self.unique_id),
        }


class CoreDigitalOceanVPC(UtilAsset):
    owner = models.ForeignKey(CoreDigitalOceanAccount, on_delete=models.CASCADE, related_name='vpcs')

    class Meta:
        db_table = "core_digitalocean_vpc"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        return f"https://cloud.digitalocean.com/networking/vpc/{self.unique_id}"


class CoreDigitalOceanVPCPeering(UtilAsset):
    owner = models.ForeignKey(CoreDigitalOceanAccount, on_delete=models.CASCADE, related_name='vpc_peerings')

    class Meta:
        db_table = "core_digitalocean_vpc_peering"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        return f"https://cloud.digitalocean.com/networking/vpc/{self.unique_id}"


class CoreDigitalOceanVPCNATGateway(UtilAsset):
    owner = models.ForeignKey(
        CoreDigitalOceanAccount,
        on_delete=models.CASCADE,
        related_name='vpc_nat_gateways',
    )

    class Meta:
        db_table = "core_digitalocean_vpc_nat_gateway"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        return f"https://cloud.digitalocean.com/networking/vpc/{self.unique_id}"


class CoreDigitalOceanDomain(UtilAsset):
    owner = models.ForeignKey(CoreDigitalOceanAccount, on_delete=models.CASCADE, related_name='domains')

    class Meta:
        db_table = "core_digitalocean_domain"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        domain_name = (self.metadata or {}).get('domain_name', self.unique_id)
        return f"https://cloud.digitalocean.com/networking/domains/{domain_name}"

    @property
    def monitoring_credentials(self):
        return {
            'access_token': self.owner.access_token,
            'domain_name': (self.metadata or {}).get('domain_name', self.unique_id),
        }


class CoreDigitalOceanDNSRecord(UtilAsset):
    owner = models.ForeignKey(
        CoreDigitalOceanAccount,
        on_delete=models.CASCADE,
        related_name='dns_records',
    )

    class Meta:
        db_table = "core_digitalocean_dns_record"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        domain_name = (self.metadata or {}).get('domain_name', '')
        return f"https://cloud.digitalocean.com/networking/domains/{domain_name}"

    @property
    def monitoring_credentials(self):
        metadata = self.metadata or {}
        return {
            'access_token': self.owner.access_token,
            'domain_name': metadata.get('domain_name'),
            'record_id': metadata.get('record_id', self.unique_id),
        }


class CoreDigitalOceanCDNEndpoint(UtilAsset):
    owner = models.ForeignKey(
        CoreDigitalOceanAccount,
        on_delete=models.CASCADE,
        related_name='cdn_endpoints',
    )

    class Meta:
        db_table = "core_digitalocean_cdn_endpoint"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        return f"https://cloud.digitalocean.com/spaces/cdn/{self.unique_id}"


class CoreDigitalOceanCertificate(UtilAsset):
    owner = models.ForeignKey(
        CoreDigitalOceanAccount,
        on_delete=models.CASCADE,
        related_name='certificates',
    )

    class Meta:
        db_table = "core_digitalocean_certificate"

    def __str__(self):
        return self.name

    @property
    def provider_url(self):
        return "https://cloud.digitalocean.com/account/api/certificates"
