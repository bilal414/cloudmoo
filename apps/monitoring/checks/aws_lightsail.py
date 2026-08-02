"""Read-only Amazon Lightsail status and metric checks."""

from datetime import timedelta

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from django.utils import timezone

from apps.monitoring.checks.base import (
    REQUEST_TIMEOUT_SECONDS,
    _serialize_datetime,
    classify_aws_error,
)
from apps.monitoring.metadata import redact_error_message


LIGHTSAIL_CLIENT_CONFIG = Config(
    connect_timeout=5,
    read_timeout=REQUEST_TIMEOUT_SECONDS,
    retries={'mode': 'standard', 'max_attempts': 2},
)

CONTAINER_LOG_WINDOW_MINUTES = 15
MAX_CONTAINER_LOG_PAGES = 3
MAX_CONTAINER_LOG_EVENTS = 100
MAX_CONTAINER_LOG_MESSAGE_LENGTH = 1024


# One low-cost, representative metric is collected per check. The raw API
# response is preserved so the console can display the most recent datapoints.
METRIC_SPECS = {
    'lightsail_instance': ('get_instance_metric_data', 'instanceName', 'CPUUtilization', 'Percent'),
    'lightsail_database': ('get_relational_database_metric_data', 'relationalDatabaseName', 'CPUUtilization', 'Percent'),
    'lightsail_load_balancer': ('get_load_balancer_metric_data', 'loadBalancerName', 'RequestCount', 'Count'),
    'lightsail_bucket': ('get_bucket_metric_data', 'bucketName', 'NumberOfObjects', 'Count'),
    'lightsail_distribution': ('get_distribution_metric_data', 'distributionName', 'Requests', 'Count'),
    'lightsail_container_service': ('get_container_service_metric_data', 'serviceName', 'CPUUtilization', None),
}


DETAIL_SPECS = {
    'lightsail_instance': ('get_instance', 'instance', 'instanceName'),
    'lightsail_disk': ('get_disk', 'disk', 'diskName'),
    'lightsail_instance_snapshot': ('get_instance_snapshot', 'instanceSnapshot', 'instanceSnapshotName'),
    'lightsail_disk_snapshot': ('get_disk_snapshot', 'diskSnapshot', 'diskSnapshotName'),
    'lightsail_static_ip': ('get_static_ip', 'staticIp', 'staticIpName'),
    'lightsail_database': ('get_relational_database', 'relationalDatabase', 'relationalDatabaseName'),
    'lightsail_database_snapshot': (
        'get_relational_database_snapshot',
        'relationalDatabaseSnapshot',
        'relationalDatabaseSnapshotName',
    ),
    'lightsail_load_balancer': ('get_load_balancer', 'loadBalancer', 'loadBalancerName'),
    'lightsail_certificate': ('get_certificates', 'certificates', 'certificateName'),
    'lightsail_bucket': ('get_buckets', 'buckets', 'bucketName'),
    'lightsail_distribution': ('get_distributions', 'distributions', 'distributionName'),
    'lightsail_domain': ('get_domain', 'domain', 'domainName'),
    'lightsail_container_service': ('get_container_services', 'containerServices', 'serviceName'),
    'lightsail_alarm': ('get_alarms', 'alarms', 'alarmName'),
    'lightsail_operation': ('get_operation', 'operation', 'operationId'),
}


LIGHTSAIL_ASSET_TYPES = (
    'lightsail_instance',
    'lightsail_disk',
    'lightsail_instance_snapshot',
    'lightsail_disk_snapshot',
    'lightsail_static_ip',
    'lightsail_database',
    'lightsail_database_snapshot',
    'lightsail_load_balancer',
    'lightsail_certificate',
    'lightsail_bucket',
    'lightsail_distribution',
    'lightsail_domain',
    'lightsail_dns_record',
    'lightsail_container_service',
    'lightsail_container_deployment',
    'lightsail_container_image',
    'lightsail_alarm',
    'lightsail_operation',
    'lightsail_auto_snapshot',
)


def _error_code(error):
    if isinstance(error, ClientError):
        return (error.response.get('Error') or {}).get('Code', type(error).__name__)
    return type(error).__name__


def _context(credentials):
    if not isinstance(credentials, dict):
        raise ValueError('AWS Lightsail credentials are not configured')
    access_key = credentials.get('access_key')
    secret_key = credentials.get('secret_key')
    region = credentials.get('resource_region') or credentials.get('region')
    resource_name = credentials.get('resource_name')
    asset_type = credentials.get('asset_type')
    metadata = credentials.get('metadata')
    if not access_key or not secret_key or not region or not resource_name or not asset_type:
        raise ValueError('AWS Lightsail credentials are incomplete')
    return access_key, secret_key, region, str(resource_name), str(asset_type), metadata or {}


def _client(access_key, secret_key, region):
    return boto3.client(
        'lightsail',
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=LIGHTSAIL_CLIENT_CONFIG,
    )


def _find_resource(response, key, resource_name=None):
    if key in ('buckets', 'distributions', 'certificates', 'containerServices', 'alarms'):
        resources = response.get(key)
        if not isinstance(resources, list):
            raise ValueError(f'AWS Lightsail returned an invalid {key} collection')
        if not resources:
            raise KeyError('resource not found')
        if resource_name is None:
            return resources[0]
        for resource in resources:
            if not isinstance(resource, dict):
                continue
            for identity_key in (
                'name',
                'certificateName',
                'bucketName',
                'distributionName',
                'serviceName',
                'containerServiceName',
                'alarmName',
            ):
                if resource.get(identity_key) == resource_name:
                    return resource
        raise KeyError('resource not found')
    resource = response.get(key)
    if not isinstance(resource, dict):
        raise ValueError(f'AWS Lightsail returned an invalid {key} resource')
    return resource


def _get_resource(client, asset_type, resource_name, metadata):
    spec = DETAIL_SPECS.get(asset_type)
    if spec is None:
        if asset_type == 'lightsail_dns_record':
            domain_name = metadata.get('_cloudmoo_domain_name')
            if not domain_name:
                raise ValueError('Lightsail DNS record is missing its domain context')
            response = client.get_domain(domainName=domain_name)
            domain = _find_resource(response, 'domain')
            entries = domain.get('domainEntries')
            if not isinstance(entries, list):
                raise ValueError('AWS Lightsail returned an invalid DNS record collection')
            expected_id = metadata.get('id')
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                if expected_id is not None and entry.get('id') == expected_id:
                    return entry
                if all(entry.get(key) == metadata.get(key) for key in ('name', 'type', 'target')):
                    return entry
            raise KeyError('resource not found')

        if asset_type == 'lightsail_container_deployment':
            service_name = metadata.get('_cloudmoo_service_name')
            response = client.get_container_service_deployments(serviceName=service_name)
            resources = response.get('deployments')
            identity = metadata.get('version') or metadata.get('id') or metadata.get('state')
            if not isinstance(resources, list):
                raise ValueError('AWS Lightsail returned an invalid deployment collection')
            for resource in resources:
                if resource.get('version') == identity or resource.get('id') == identity:
                    return resource
            raise KeyError('resource not found')

        if asset_type == 'lightsail_container_image':
            service_name = metadata.get('_cloudmoo_service_name')
            response = client.get_container_images(serviceName=service_name)
            resources = response.get('containerImages')
            identity = metadata.get('image') or metadata.get('imageName') or metadata.get('digest')
            if not isinstance(resources, list):
                raise ValueError('AWS Lightsail returned an invalid image collection')
            for resource in resources:
                if resource.get('image') == identity or resource.get('imageName') == identity or resource.get('digest') == identity:
                    return resource
            raise KeyError('resource not found')

        if asset_type == 'lightsail_auto_snapshot':
            source_name = metadata.get('_cloudmoo_source_name')
            response = client.get_auto_snapshots(resourceName=source_name)
            resources = response.get('autoSnapshots')
            identity = metadata.get('date') or metadata.get('name') or metadata.get('createdAt')
            if not isinstance(resources, list):
                raise ValueError('AWS Lightsail returned an invalid auto-snapshot collection')
            for resource in resources:
                if resource.get('date') == identity or resource.get('name') == identity or resource.get('createdAt') == identity:
                    return resource
            raise KeyError('resource not found')

        raise ValueError(f'Unsupported AWS Lightsail asset type: {asset_type}')

    operation, response_key, parameter = spec
    kwargs = {parameter: resource_name}
    if asset_type in {'lightsail_certificate', 'lightsail_bucket'}:
        response = client.get_certificates(
            certificateName=resource_name,
            includeCertificateDetails=True,
        ) if asset_type == 'lightsail_certificate' else client.get_buckets(
            bucketName=resource_name,
            includeConnectedResources=True,
            includeCors=True,
        )
    else:
        response = getattr(client, operation)(**kwargs)
    return _find_resource(response, response_key, resource_name)


def _status(resource, asset_type):
    if asset_type == 'lightsail_static_ip':
        return 'attached' if resource.get('attachedTo') else 'available'
    if asset_type in {'lightsail_domain', 'lightsail_dns_record'}:
        return 'available'
    if asset_type == 'lightsail_certificate':
        certificate_detail = resource.get('certificateDetail')
        if isinstance(certificate_detail, dict):
            return certificate_detail.get('status') or 'available'
    if asset_type == 'lightsail_distribution' and resource.get('isEnabled') is False:
        return 'disabled'

    state = resource.get('state')
    if isinstance(state, dict):
        state = state.get('name') or state.get('code')
    current = resource.get('status') or state or resource.get('stateCode')
    if current is None and asset_type == 'lightsail_operation':
        current = resource.get('status') or resource.get('operationDetails')
    if current is None:
        return 'available'
    return str(current)


def _metric(client, asset_type, resource_name):
    spec = METRIC_SPECS.get(asset_type)
    if spec is None:
        return None
    operation, name_parameter, metric_name, unit = spec
    end_time = timezone.now()
    kwargs = {
        name_parameter: resource_name,
        'metricName': metric_name,
        'period': 300,
        'startTime': end_time - timedelta(minutes=15),
        'endTime': end_time,
        'statistics': ['Average'],
    }
    if unit is not None:
        kwargs['unit'] = unit
    try:
        response = getattr(client, operation)(**kwargs)
        return {
            'metricName': metric_name,
            'metricData': _serialize_datetime(response.get('metricData', [])),
        }
    except Exception as error:
        return {'metricName': metric_name, 'errorCode': _error_code(error)}


def _container_logs(client, service_name, resource):
    """Read a bounded, redacted log window for the current deployment.

    Lightsail exposes logs per service/container rather than as an inventory
    resource.  Keep this supplement deliberately bounded so a noisy service
    cannot make a monitoring check unbounded or persist an entire log stream.
    """
    end_time = timezone.now()
    start_time = end_time - timedelta(minutes=CONTAINER_LOG_WINDOW_MINUTES)
    deployment = resource.get('currentDeployment') if isinstance(resource, dict) else None
    containers = deployment.get('containers') if isinstance(deployment, dict) else None
    if not isinstance(containers, dict):
        return {
            'windowStart': start_time.isoformat(),
            'windowEnd': end_time.isoformat(),
            'containers': {},
        }

    container_results = {}
    for container_name in sorted(str(name) for name in containers)[:20]:
        events = []
        page_token = None
        truncated = False
        try:
            for page_number in range(MAX_CONTAINER_LOG_PAGES):
                request = {
                    'serviceName': service_name,
                    'containerName': container_name,
                    'startTime': start_time,
                    'endTime': end_time,
                }
                if page_token:
                    request['pageToken'] = page_token
                response = client.get_container_log(**request)
                page_events = response.get('logEvents') if isinstance(response, dict) else None
                if not isinstance(page_events, list):
                    raise ValueError('AWS Lightsail returned an invalid container log response')

                for event in page_events:
                    if len(events) >= MAX_CONTAINER_LOG_EVENTS:
                        truncated = True
                        break
                    if not isinstance(event, dict):
                        continue
                    safe_event = {}
                    if event.get('createdAt') is not None:
                        safe_event['createdAt'] = _serialize_datetime(event['createdAt'])
                    if event.get('message') is not None:
                        safe_event['message'] = redact_error_message(event['message'])[
                            :MAX_CONTAINER_LOG_MESSAGE_LENGTH
                        ]
                    events.append(safe_event)

                if truncated:
                    break
                page_token = response.get('nextPageToken')
                if not page_token:
                    break
                if page_number == MAX_CONTAINER_LOG_PAGES - 1:
                    truncated = True

            container_results[container_name] = {
                'events': events,
                'eventCount': len(events),
                'truncated': truncated,
            }
        except (ClientError, BotoCoreError) as error:
            container_results[container_name] = {'errorCode': _error_code(error)}
        except Exception as error:
            container_results[container_name] = {'errorCode': type(error).__name__}

    return {
        'windowStart': start_time.isoformat(),
        'windowEnd': end_time.isoformat(),
        'containers': container_results,
    }


def _supplement(client, asset_type, resource_name, resource):
    metadata = {}
    if asset_type == 'lightsail_instance':
        try:
            response = client.get_instance_port_states(instanceName=resource_name)
            metadata['portStates'] = _serialize_datetime(response.get('portStates', []))
        except Exception as error:
            metadata['portStatesError'] = _error_code(error)
        try:
            response = client.get_auto_snapshots(resourceName=resource_name)
            metadata['autoSnapshots'] = _serialize_datetime(response.get('autoSnapshots', []))
        except Exception as error:
            metadata['autoSnapshotsError'] = _error_code(error)
    elif asset_type == 'lightsail_disk':
        try:
            response = client.get_auto_snapshots(resourceName=resource_name)
            metadata['autoSnapshots'] = _serialize_datetime(response.get('autoSnapshots', []))
        except Exception as error:
            metadata['autoSnapshotsError'] = _error_code(error)
    elif asset_type == 'lightsail_load_balancer':
        try:
            response = client.get_load_balancer_tls_certificates(loadBalancerName=resource_name)
            metadata['tlsCertificates'] = _serialize_datetime(response.get('tlsCertificates', []))
        except Exception as error:
            metadata['tlsCertificatesError'] = _error_code(error)
    elif asset_type == 'lightsail_container_service':
        metadata['containerLogs'] = _container_logs(client, resource_name, resource)

    metric = _metric(client, asset_type, resource_name)
    if metric is not None:
        metadata['metric'] = metric
    return metadata


def check_lightsail_resource_status(asset_type, unique_id, credentials):
    """Check one Lightsail resource using only read-only API operations."""
    try:
        access_key, secret_key, region, resource_name, context_asset_type, context_metadata = _context(credentials)
        if context_asset_type != asset_type:
            raise ValueError('AWS Lightsail asset type context does not match the requested check')
        client = _client(access_key, secret_key, region)
        resource = _get_resource(client, asset_type, resource_name, context_metadata)
        supplement = _supplement(client, asset_type, resource_name, resource)
        current_status = _status(resource, asset_type)
        if not current_status:
            raise ValueError('AWS Lightsail returned a resource without a status')
        payload = {asset_type: _serialize_datetime(resource)}
        if supplement:
            payload['lightsailDetails'] = supplement
        return current_status, payload
    except (ClientError, BotoCoreError) as error:
        return classify_aws_error(error), redact_error_message(error)
    except (KeyError, TypeError, ValueError) as error:
        return 'error', redact_error_message(error)
    except Exception as error:
        return 'error', redact_error_message(error)


def _make_check(asset_type):
    def check(unique_id, credentials):
        return check_lightsail_resource_status(asset_type, unique_id, credentials)

    check.__name__ = f'check_aws_{asset_type}_status'
    return check


for _asset_type in LIGHTSAIL_ASSET_TYPES:
    globals()[f'check_aws_{_asset_type}_status'] = _make_check(_asset_type)
