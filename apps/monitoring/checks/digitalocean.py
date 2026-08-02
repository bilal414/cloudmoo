import re
from urllib.parse import quote

import boto3
import requests
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from apps.monitoring.checks.base import REQUEST_TIMEOUT_SECONDS, classify_http_error


DIGITALOCEAN_API_BASE = 'https://api.digitalocean.com/v2'
SPACES_CLIENT_CONFIG = Config(
    connect_timeout=5,
    read_timeout=REQUEST_TIMEOUT_SECONDS,
    retries={'mode': 'standard', 'max_attempts': 2},
)
SPACES_REGION_PATTERN = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')


def _get_resource(endpoint, response_key, access_token):
    response = requests.get(
        f'{DIGITALOCEAN_API_BASE}/{endpoint.lstrip("/")}',
        headers={'Authorization': f'Bearer {access_token}'},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    data = response.json()
    resource = data.get(response_key) if isinstance(data, dict) else None
    if not isinstance(resource, dict):
        raise ValueError('DigitalOcean returned an invalid resource response')
    return resource


def _check_resource(endpoint, response_key, metadata_key, access_token, status_getter):
    try:
        resource = _get_resource(endpoint, response_key, access_token)
        current_status = status_getter(resource)
        if not isinstance(current_status, str) or not current_status:
            raise ValueError('DigitalOcean returned a resource without a status')
        return current_status, {metadata_key: resource}
    except requests.exceptions.RequestException as error:
        return classify_http_error(error), str(error)
    except (KeyError, TypeError, ValueError) as error:
        return 'error', str(error)


def _context_access_token(credentials):
    """Extract a control-plane token from an asset-specific credential context."""
    if not isinstance(credentials, dict) or not credentials.get('access_token'):
        raise ValueError('DigitalOcean control-plane credentials are not configured')
    return credentials['access_token']


def _check_context_resource(
    endpoint,
    response_key,
    metadata_key,
    credentials,
    status_getter,
):
    try:
        access_token = _context_access_token(credentials)
        resource = _get_resource(endpoint, response_key, access_token)
        current_status = status_getter(resource)
        if not isinstance(current_status, str) or not current_status:
            raise ValueError('DigitalOcean returned a resource without a status')
        return current_status, {metadata_key: resource}
    except requests.exceptions.RequestException as error:
        return classify_http_error(error), str(error)
    except (KeyError, TypeError, ValueError) as error:
        return 'error', str(error)


def check_digitalocean_server_status(unique_id, access_token):
    """Check the lifecycle status of a DigitalOcean Droplet."""
    return _check_resource(
        f'droplets/{quote(str(unique_id), safe="")}',
        'droplet',
        'droplet',
        access_token,
        lambda resource: resource['status'],
    )


def check_digitalocean_volume_status(unique_id, access_token):
    """Check whether a DigitalOcean Block Storage volume is attached."""
    try:
        resource = _get_resource(
            f'volumes/{quote(str(unique_id), safe="")}',
            'volume',
            access_token,
        )
        droplet_ids = resource.get('droplet_ids')
        if not isinstance(droplet_ids, list):
            raise ValueError('DigitalOcean returned an invalid volume attachment list')
        current_status = 'attached' if droplet_ids else 'detached'
        return current_status, {'volume': resource}
    except requests.exceptions.RequestException as error:
        return classify_http_error(error), str(error)
    except (KeyError, TypeError, ValueError) as error:
        return 'error', str(error)


def check_digitalocean_database_status(unique_id, access_token):
    """Check a managed database cluster's lifecycle status."""
    return _check_resource(
        f'databases/{quote(str(unique_id), safe="")}',
        'database',
        'database',
        access_token,
        lambda resource: resource['status'],
    )


def check_digitalocean_load_balancer_status(unique_id, access_token):
    """Check a load balancer's provisioning status."""
    return _check_resource(
        f'load_balancers/{quote(str(unique_id), safe="")}',
        'load_balancer',
        'load_balancer',
        access_token,
        lambda resource: resource['status'],
    )


def check_digitalocean_snapshot_status(unique_id, access_token):
    """Confirm that a saved Droplet or volume snapshot is available."""
    def snapshot_status(resource):
        # The snapshots API represents availability by returning regions for
        # the saved image; it does not expose a separate lifecycle status.
        if not isinstance(resource.get('regions'), list) or not resource['regions']:
            raise ValueError('DigitalOcean returned an invalid snapshot region list')
        return 'available'

    return _check_resource(
        f'snapshots/{quote(str(unique_id), safe="")}',
        'snapshot',
        'snapshot',
        access_token,
        snapshot_status,
    )


def check_digitalocean_backup_status(unique_id, access_token):
    """Check the image status of an automatic Droplet backup."""
    return _check_resource(
        f'images/{quote(str(unique_id), safe="")}',
        'image',
        'backup',
        access_token,
        lambda resource: resource['status'],
    )


def check_digitalocean_reserved_ip_status(unique_id, access_token):
    """Check whether a Reserved IPv4 or IPv6 address is assigned."""
    is_ipv6 = ':' in str(unique_id)
    endpoint = 'reserved_ipv6' if is_ipv6 else 'reserved_ips'
    response_key = 'reserved_ipv6' if is_ipv6 else 'reserved_ip'

    def reserved_ip_status(resource):
        droplet = resource.get('droplet')
        if droplet is not None and not isinstance(droplet, dict):
            raise ValueError('DigitalOcean returned an invalid Reserved IP attachment')
        return 'assigned' if droplet else 'reserved'

    return _check_resource(
        f'{endpoint}/{quote(str(unique_id), safe="")}',
        response_key,
        'reserved_ip',
        access_token,
        reserved_ip_status,
    )


def check_digitalocean_firewall_status(unique_id, access_token):
    """Check a DigitalOcean firewall policy application status."""
    return _check_resource(
        f'firewalls/{quote(str(unique_id), safe="")}',
        'firewall',
        'firewall',
        access_token,
        lambda resource: resource['status'],
    )


def check_digitalocean_app_platform_status(unique_id, access_token):
    """Check App Platform deployment phase and surface in-progress failures."""
    def app_status(resource):
        in_progress = resource.get('in_progress_deployment')
        if isinstance(in_progress, dict) and in_progress.get('phase'):
            return in_progress['phase']

        active = resource.get('active_deployment')
        if isinstance(active, dict) and active.get('phase'):
            return active['phase']

        state = resource.get('state')
        if isinstance(state, str) and state:
            return state
        raise ValueError('DigitalOcean returned an App without a deployment status')

    return _check_resource(
        f'apps/{quote(str(unique_id), safe="")}',
        'app',
        'app',
        access_token,
        app_status,
    )


def check_digitalocean_container_registry_status(unique_id, access_token):
    """Check that a DigitalOcean Container Registry is accessible."""
    return _check_resource(
        f'registries/{quote(str(unique_id), safe="")}',
        'registry',
        'registry',
        access_token,
        lambda _resource: 'available',
    )


def check_digitalocean_kubernetes_cluster_status(unique_id, access_token):
    """Check the lifecycle state of a DigitalOcean Kubernetes cluster."""
    return _check_resource(
        f'kubernetes/clusters/{quote(str(unique_id), safe="")}',
        'kubernetes_cluster',
        'kubernetes_cluster',
        access_token,
        lambda resource: resource['status']['state'],
    )


def check_digitalocean_kubernetes_node_pool_status(unique_id, credentials):
    """Check the node states in one DigitalOcean Kubernetes node pool."""
    if not isinstance(credentials, dict):
        return 'invalid_access_token', 'DigitalOcean Kubernetes credentials are not configured'

    cluster_id = credentials.get('cluster_id')
    pool_id = credentials.get('pool_id', unique_id)
    if not cluster_id or not pool_id:
        return 'error', 'DigitalOcean Kubernetes node-pool context is incomplete'

    def node_pool_status(resource):
        nodes = resource.get('nodes')
        if not isinstance(nodes, list):
            raise ValueError('DigitalOcean returned an invalid node-pool node list')
        if not nodes:
            if resource.get('count') == 0:
                return 'empty'
            raise ValueError('DigitalOcean returned a node pool without nodes')

        states = []
        for node in nodes:
            if not isinstance(node, dict):
                raise ValueError('DigitalOcean returned an invalid node object')
            state = (node.get('status') or {}).get('state')
            if not isinstance(state, str) or not state:
                raise ValueError('DigitalOcean returned a node without a state')
            states.append(state)

        if any(state in {'draining', 'deleting'} for state in states):
            return 'degraded'
        if any(state != 'running' for state in states):
            return 'provisioning'
        return 'running'

    return _check_context_resource(
        f'kubernetes/clusters/{quote(str(cluster_id), safe="")}/node_pools/{quote(str(pool_id), safe="")}',
        'node_pool',
        'kubernetes_node_pool',
        credentials,
        node_pool_status,
    )


def check_digitalocean_vpc_status(unique_id, access_token):
    """Check that a DigitalOcean VPC still has a valid network definition."""
    def vpc_status(resource):
        if not isinstance(resource.get('ip_range'), str) or not resource['ip_range']:
            raise ValueError('DigitalOcean returned a VPC without an IP range')
        region = resource.get('region')
        if isinstance(region, dict):
            region_slug = region.get('slug')
        elif isinstance(region, str):
            # The live VPC API returns the region as a slug, while some
            # fixtures/API surfaces represent it as ``{"slug": ...}``.
            region_slug = region
        else:
            region_slug = None
        if not region_slug:
            raise ValueError('DigitalOcean returned a VPC without a region')
        return 'available'

    return _check_resource(
        f'vpcs/{quote(str(unique_id), safe="")}',
        'vpc',
        'vpc',
        access_token,
        vpc_status,
    )


def check_digitalocean_vpc_peering_status(unique_id, access_token):
    """Check the lifecycle state of a VPC peering."""
    return _check_resource(
        f'vpc_peerings/{quote(str(unique_id), safe="")}',
        'vpc_peering',
        'vpc_peering',
        access_token,
        lambda resource: resource['status'],
    )


def check_digitalocean_nat_gateway_status(unique_id, access_token):
    """Check the lifecycle state of a VPC NAT gateway."""
    return _check_resource(
        f'vpc_nat_gateways/{quote(str(unique_id), safe="")}',
        'vpc_nat_gateway',
        'nat_gateway',
        access_token,
        lambda resource: resource['state'],
    )


def check_digitalocean_domain_status(unique_id, credentials):
    """Check that a DNS domain is still present in the DigitalOcean account."""
    if not isinstance(credentials, dict):
        return 'invalid_access_token', 'DigitalOcean DNS credentials are not configured'
    domain_name = credentials.get('domain_name', unique_id)
    if not domain_name:
        return 'error', 'DigitalOcean domain context is incomplete'

    return _check_context_resource(
        f'domains/{quote(str(domain_name), safe="")}',
        'domain',
        'domain',
        credentials,
        lambda resource: 'available' if resource.get('name') else None,
    )


def check_digitalocean_dns_record_status(unique_id, credentials):
    """Check that a DNS record remains present under its authoritative domain."""
    if not isinstance(credentials, dict):
        return 'invalid_access_token', 'DigitalOcean DNS credentials are not configured'
    domain_name = credentials.get('domain_name')
    record_id = credentials.get('record_id', unique_id)
    if not domain_name or not record_id:
        return 'error', 'DigitalOcean DNS record context is incomplete'

    def dns_record_status(resource):
        if not resource.get('id') or not resource.get('type'):
            raise ValueError('DigitalOcean returned an invalid DNS record')
        return 'present'

    return _check_context_resource(
        f'domains/{quote(str(domain_name), safe="")}/records/{quote(str(record_id), safe="")}',
        'domain_record',
        'domain_record',
        credentials,
        dns_record_status,
    )


def check_digitalocean_cdn_endpoint_status(unique_id, access_token):
    """Check that a DigitalOcean CDN endpoint has a configured origin."""
    def endpoint_status(resource):
        if not isinstance(resource.get('origin'), str) or not resource['origin']:
            raise ValueError('DigitalOcean returned a CDN endpoint without an origin')
        return 'available'

    return _check_resource(
        f'cdn/endpoints/{quote(str(unique_id), safe="")}',
        'endpoint',
        'endpoint',
        access_token,
        endpoint_status,
    )


def check_digitalocean_certificate_status(unique_id, access_token):
    """Check the verification state of a DigitalOcean TLS certificate."""
    return _check_resource(
        f'certificates/{quote(str(unique_id), safe="")}',
        'certificate',
        'certificate',
        access_token,
        lambda resource: resource['state'],
    )


def _classify_spaces_error(error):
    if isinstance(error, ClientError):
        details = error.response.get('Error', {})
        code = str(details.get('Code', ''))
        if code in {'404', 'NoSuchBucket', 'NotFound'}:
            return 'not_found'
        if code in {
            '401',
            '403',
            'AccessDenied',
            'InvalidAccessKeyId',
            'SignatureDoesNotMatch',
        }:
            return 'invalid_access_token'
    if isinstance(error, BotoCoreError):
        return 'error'
    return 'error'


def check_digitalocean_object_storage_status(unique_id, credentials):
    """Check a DigitalOcean Space bucket with the S3-compatible API."""
    if not isinstance(credentials, dict):
        return 'invalid_access_token', 'DigitalOcean Spaces credentials are not configured'

    access_key = credentials.get('access_key')
    secret_key = credentials.get('secret_key')
    region = str(credentials.get('region') or '').strip().lower()
    if not access_key or not secret_key or not region:
        return 'invalid_access_token', 'DigitalOcean Spaces credentials are not configured'
    if not SPACES_REGION_PATTERN.fullmatch(region):
        return 'error', 'DigitalOcean Spaces region is invalid'

    try:
        client = boto3.client(
            's3',
            region_name=region,
            endpoint_url=f'https://{region}.digitaloceanspaces.com',
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=SPACES_CLIENT_CONFIG,
        )
        client.head_bucket(Bucket=str(unique_id))
        return 'available', {
            'bucket': {
                'name': str(unique_id),
                'region': region,
            }
        }
    except (ClientError, BotoCoreError, ValueError) as error:
        return _classify_spaces_error(error), str(error)
