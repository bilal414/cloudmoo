"""Read-only status checks for AWS database and storage data services."""

from __future__ import annotations

from collections.abc import Mapping
import re

from botocore.exceptions import BotoCoreError, ClientError

from apps.console.cloud.aws.data_services import (
    AWS_DATA_SERVICE_ASSET_TYPES,
    AWS_EFS_FILE_SYSTEM,
    AWS_ELASTICACHE_CLUSTER,
    AWS_ELASTICACHE_REPLICATION_GROUP,
    AWS_ELASTICACHE_SERVERLESS_CACHE,
    AWS_FSX_FILE_SYSTEM,
    AWS_MEMORYDB_CLUSTER,
    AWS_OPENSEARCH_DOMAIN,
    AWS_RDS_CLUSTER,
    ELASTICACHE_SERVERLESS_SUPPORTED,
    _EFS_ALLOWED,
    _ELASTICACHE_CLUSTER_ALLOWED,
    _ELASTICACHE_REPLICATION_ALLOWED,
    _ELASTICACHE_SERVERLESS_ALLOWED,
    _FSX_ALLOWED,
    _MEMORYDB_ALLOWED,
    _OPENSEARCH_ALLOWED,
    _RDS_ALLOWED,
    _bounded_value,
    _opensearch_state,
    _provider_state,
    _redact_structured_sensitive_values,
    _stable_id,
    normalize_data_service_status,
)
from apps.console.cloud.aws.discovery import (
    aws_client,
    aws_error_code,
    iter_pages,
    require_collection,
    serialize_aws,
)
from apps.console.cloud.models import CloudInventoryTransientError
from apps.monitoring.metadata import redact_sensitive_metadata


_REGION_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)+$")
MAX_CHECK_PAGES = 100
MAX_CHECK_ITEMS = 10_000


class _ProviderNotFound(Exception):
    """The provider answered successfully but returned no requested asset."""


class _CredentialAccount:
    """Account-shaped object accepted by the shared AWS client helper."""

    def __init__(self, credentials, region):
        self.access_key = credentials.get("access_key")
        self.secret_key = credentials.get("secret_key")
        self.region = region


def _context(unique_id, credentials):
    if not isinstance(credentials, dict):
        raise ValueError("AWS monitoring credentials are not configured")
    region = credentials.get("resource_region") or credentials.get("region")
    if not isinstance(region, str) or _REGION_PATTERN.fullmatch(region.strip()) is None:
        raise ValueError("AWS monitoring credentials contain an invalid Region")
    region = region.strip()
    if not credentials.get("access_key") or not credentials.get("secret_key"):
        raise ValueError("AWS monitoring credentials are incomplete")

    metadata = credentials.get("metadata") if isinstance(credentials.get("metadata"), dict) else {}
    provider_id = (
        credentials.get("provider_id")
        or metadata.get("_cloudmoo_provider_id")
        or metadata.get("provider_id")
        or metadata.get("_cloudmoo_raw_id")
    )
    if not provider_id and isinstance(unique_id, str):
        parts = unique_id.split(":", 2)
        if len(parts) == 3 and parts[1] == region:
            provider_id = parts[2]
    if not provider_id:
        raise ValueError("AWS monitoring credentials are missing the provider identifier")
    return region, str(provider_id), metadata


def _client(credentials, service, region):
    return aws_client(_CredentialAccount(credentials, region), service, region=region)


def _error_code(error):
    try:
        return str(aws_error_code(getattr(error, "__cause__", None) or error))[:128]
    except Exception:
        return type(error).__name__[:128]


_NOT_FOUND_CODES = {
    "CacheClusterNotFound",
    "CacheClusterNotFoundFault",
    "ClusterNotFoundException",
    "ClusterNotFoundFault",
    "DBClusterNotFoundFault",
    "DomainNotFoundException",
    "FileSystemNotFound",
    "FileSystemNotFoundException",
    "ReplicationGroupNotFound",
    "ReplicationGroupNotFoundFault",
    "ResourceNotFound",
    "ResourceNotFoundException",
    "ResourceNotFoundFault",
    "ServerlessCacheNotFoundFault",
}


def _error_result(error):
    code = _error_code(error)
    normalized = code.lower().replace("_", "")
    if code in _NOT_FOUND_CODES or "notfound" in normalized:
        return "not_found", {"error_code": code}
    return "error", {"error_code": code}


def _bounded_collection(client, operation, collection_key, context, **kwargs):
    values = []
    for page_number, page in enumerate(iter_pages(client, operation, **kwargs), start=1):
        if page_number > MAX_CHECK_PAGES:
            raise CloudInventoryTransientError(
                f"AWS {operation} pagination exceeded the safety bound"
            )
        if not isinstance(page, Mapping):
            raise CloudInventoryTransientError(f"AWS returned an invalid {context} page")
        page_values = require_collection(page, collection_key, context)
        if len(values) + len(page_values) > MAX_CHECK_ITEMS:
            raise CloudInventoryTransientError(f"AWS {context} inventory exceeded the safety bound")
        for value in page_values:
            if not isinstance(value, Mapping):
                raise CloudInventoryTransientError(f"AWS returned an invalid {context} item")
            values.append(value)
    return values


def _resource(response, key, context):
    if not isinstance(response, Mapping):
        raise CloudInventoryTransientError(f"AWS returned an invalid {context} response")
    value = response if key is None else response.get(key)
    if not isinstance(value, Mapping):
        raise CloudInventoryTransientError(f"AWS returned an invalid {context} resource")
    return value


def _find_resource(resources, provider_id, id_keys):
    if not resources:
        raise _ProviderNotFound()
    for resource in resources:
        for key in id_keys:
            if str(resource.get(key, "")) == provider_id:
                return resource
    # Filtered describe calls normally return one resource, even when an older
    # response omits its echoed identifier.  Never guess when several exist.
    if len(resources) == 1:
        return resources[0]
    raise _ProviderNotFound()


def _safe_selected(resource, allowed_keys):
    if not isinstance(resource, Mapping):
        raise CloudInventoryTransientError("AWS returned an invalid resource object")
    selected = {
        key: resource[key]
        for key in allowed_keys
        if key in resource
    }
    serialized = serialize_aws(selected)
    if not isinstance(serialized, dict):
        raise CloudInventoryTransientError("AWS returned an invalid serialized resource object")
    return _bounded_value(serialized)


def _safe_status_metadata(
    resource,
    *,
    asset_type,
    region,
    provider_id,
    allowed_keys,
    provider_state,
):
    raw_id = (
        resource.get("ARN")
        or resource.get("DBClusterArn")
        or resource.get("ServerlessCacheArn")
        or resource.get("FileSystemArn")
        or resource.get("ResourceARN")
        or provider_id
    )
    metadata = {
        "_cloudmoo_region": region,
        "_cloudmoo_provider_id": provider_id,
        "_cloudmoo_raw_id": str(raw_id)[:512],
        "_cloudmoo_asset_id": _stable_id(asset_type, region, provider_id),
        "provider_id": provider_id,
        "raw_id": str(raw_id)[:512],
        "normalized_status": normalize_data_service_status(provider_state),
        "provider_state": provider_state,
    }
    metadata.update(_safe_selected(resource, allowed_keys))
    return _bounded_value(
        _redact_structured_sensitive_values(redact_sensitive_metadata(metadata))
    )


def _result(asset_type, resource, region, provider_id, allowed_keys, provider_state):
    status = normalize_data_service_status(provider_state)
    metadata = _safe_status_metadata(
        resource,
        asset_type=asset_type,
        region=region,
        provider_id=provider_id,
        allowed_keys=allowed_keys,
        provider_state=provider_state,
    )
    return status, {asset_type: metadata}


def _check_collection_asset(
    unique_id,
    credentials,
    *,
    asset_type,
    service,
    operation,
    collection_key,
        request,
    id_keys,
    allowed_keys,
    state_keys,
    context,
):
    try:
        region, provider_id, _metadata = _context(unique_id, credentials)
        client = _client(credentials, service, region)
        request_kwargs = request(provider_id) if callable(request) else request
        resources = _bounded_collection(
            client,
            operation,
            collection_key,
            context,
            **request_kwargs,
        )
        resource = _find_resource(resources, provider_id, id_keys)
        state = _provider_state(resource, state_keys)
        return _result(asset_type, resource, region, provider_id, allowed_keys, state)
    except _ProviderNotFound:
        return "not_found", {"error_code": "ResourceNotFound"}
    except (ClientError, BotoCoreError) as error:
        return _error_result(error)
    except CloudInventoryTransientError as error:
        cause = getattr(error, "__cause__", None)
        if isinstance(cause, (ClientError, BotoCoreError)):
            return _error_result(cause)
        return "error", {"error_code": "invalid_response"}
    except (KeyError, TypeError, ValueError):
        return "error", {"error_code": "invalid_response"}
    except Exception as error:
        return _error_result(error)


def check_aws_rds_cluster_status(unique_id, credentials):
    return _check_collection_asset(
        unique_id,
        credentials,
        asset_type=AWS_RDS_CLUSTER,
        service="rds",
        operation="describe_db_clusters",
        collection_key="DBClusters",
        request=lambda provider_id: {"DBClusterIdentifier": provider_id},
        id_keys=("DBClusterIdentifier", "DBClusterArn"),
        allowed_keys=_RDS_ALLOWED,
        state_keys=("Status",),
        context="RDS DB cluster",
    )


def check_aws_elasticache_cluster_status(unique_id, credentials):
    return _check_collection_asset(
        unique_id,
        credentials,
        asset_type=AWS_ELASTICACHE_CLUSTER,
        service="elasticache",
        operation="describe_cache_clusters",
        collection_key="CacheClusters",
        request=lambda provider_id: {"CacheClusterId": provider_id},
        id_keys=("CacheClusterId", "ARN"),
        allowed_keys=_ELASTICACHE_CLUSTER_ALLOWED,
        state_keys=("CacheClusterStatus", "Status"),
        context="ElastiCache cluster",
    )


def check_aws_elasticache_replication_group_status(unique_id, credentials):
    return _check_collection_asset(
        unique_id,
        credentials,
        asset_type=AWS_ELASTICACHE_REPLICATION_GROUP,
        service="elasticache",
        operation="describe_replication_groups",
        collection_key="ReplicationGroups",
        request=lambda provider_id: {"ReplicationGroupId": provider_id},
        id_keys=("ReplicationGroupId", "ARN"),
        allowed_keys=_ELASTICACHE_REPLICATION_ALLOWED,
        state_keys=("Status",),
        context="ElastiCache replication group",
    )


def check_aws_elasticache_serverless_cache_status(unique_id, credentials):
    if not ELASTICACHE_SERVERLESS_SUPPORTED:
        return "error", {"error_code": "unsupported_operation"}
    return _check_collection_asset(
        unique_id,
        credentials,
        asset_type=AWS_ELASTICACHE_SERVERLESS_CACHE,
        service="elasticache",
        operation="describe_serverless_caches",
        collection_key="ServerlessCaches",
        request=lambda provider_id: {"ServerlessCacheName": provider_id},
        id_keys=("ServerlessCacheName", "ServerlessCacheArn"),
        allowed_keys=_ELASTICACHE_SERVERLESS_ALLOWED,
        state_keys=("Status",),
        context="ElastiCache serverless cache",
    )


def check_aws_memorydb_cluster_status(unique_id, credentials):
    return _check_collection_asset(
        unique_id,
        credentials,
        asset_type=AWS_MEMORYDB_CLUSTER,
        service="memorydb",
        operation="describe_clusters",
        collection_key="Clusters",
        request=lambda provider_id: {"ClusterName": provider_id},
        id_keys=("Name", "ARN"),
        allowed_keys=_MEMORYDB_ALLOWED,
        state_keys=("Status",),
        context="MemoryDB cluster",
    )


def check_aws_opensearch_domain_status(unique_id, credentials):
    try:
        region, provider_id, _metadata = _context(unique_id, credentials)
        client = _client(credentials, "opensearch", region)
        response = client.describe_domain(DomainName=provider_id)
        resource = dict(_resource(response, "DomainStatus", "OpenSearch domain"))
        resource.setdefault("DomainName", provider_id)
        state = _opensearch_state(resource)
        return _result(
            AWS_OPENSEARCH_DOMAIN,
            resource,
            region,
            provider_id,
            _OPENSEARCH_ALLOWED,
            state,
        )
    except (ClientError, BotoCoreError) as error:
        return _error_result(error)
    except (CloudInventoryTransientError, KeyError, TypeError, ValueError):
        return "error", {"error_code": "invalid_response"}
    except Exception as error:
        return _error_result(error)


def check_aws_efs_file_system_status(unique_id, credentials):
    return _check_collection_asset(
        unique_id,
        credentials,
        asset_type=AWS_EFS_FILE_SYSTEM,
        service="efs",
        operation="describe_file_systems",
        collection_key="FileSystems",
        request=lambda provider_id: {"FileSystemId": provider_id},
        id_keys=("FileSystemId", "FileSystemArn"),
        allowed_keys=_EFS_ALLOWED,
        state_keys=("LifeCycleState", "Status"),
        context="EFS file system",
    )


def check_aws_fsx_file_system_status(unique_id, credentials):
    return _check_collection_asset(
        unique_id,
        credentials,
        asset_type=AWS_FSX_FILE_SYSTEM,
        service="fsx",
        operation="describe_file_systems",
        collection_key="FileSystems",
        request=lambda provider_id: {"FileSystemIds": [provider_id]},
        id_keys=("FileSystemId", "ResourceARN"),
        allowed_keys=_FSX_ALLOWED,
        state_keys=("Lifecycle", "LifeCycleState", "Status"),
        context="FSx file system",
    )


AWS_DATA_SERVICE_STATUS_CHECKS = {
    AWS_RDS_CLUSTER: check_aws_rds_cluster_status,
    AWS_ELASTICACHE_CLUSTER: check_aws_elasticache_cluster_status,
    AWS_ELASTICACHE_REPLICATION_GROUP: check_aws_elasticache_replication_group_status,
    **(
        {AWS_ELASTICACHE_SERVERLESS_CACHE: check_aws_elasticache_serverless_cache_status}
        if ELASTICACHE_SERVERLESS_SUPPORTED
        else {}
    ),
    AWS_MEMORYDB_CLUSTER: check_aws_memorydb_cluster_status,
    AWS_OPENSEARCH_DOMAIN: check_aws_opensearch_domain_status,
    AWS_EFS_FILE_SYSTEM: check_aws_efs_file_system_status,
    AWS_FSX_FILE_SYSTEM: check_aws_fsx_file_system_status,
}

# These aliases are the integration-lane contract.  The global AWS check
# dispatcher can discover this module without importing implementation names.
AWS_DATA_SERVICE_CHECKS = AWS_DATA_SERVICE_STATUS_CHECKS
AWS_DATA_SERVICE_CHECK_FUNCTIONS = AWS_DATA_SERVICE_STATUS_CHECKS
AWS_DATA_SERVICE_ASSET_TYPE_CHECKS = AWS_DATA_SERVICE_STATUS_CHECKS
AWS_DATA_SERVICE_CHECK_MAP = AWS_DATA_SERVICE_STATUS_CHECKS


def _compatibility_aliases():
    aliases = {}
    for asset_type, check in AWS_DATA_SERVICE_STATUS_CHECKS.items():
        aliases[f"check_aws_{asset_type[4:]}_status"] = check
        aliases[f"check_aws_{asset_type}_status"] = check
    return aliases


globals().update(_compatibility_aliases())


__all__ = [
    "AWS_DATA_SERVICE_ASSET_TYPE_CHECKS",
    "AWS_DATA_SERVICE_ASSET_TYPES",
    "AWS_DATA_SERVICE_CHECKS",
    "AWS_DATA_SERVICE_CHECK_FUNCTIONS",
    "AWS_DATA_SERVICE_CHECK_MAP",
    "AWS_DATA_SERVICE_STATUS_CHECKS",
    "check_aws_efs_file_system_status",
    "check_aws_elasticache_cluster_status",
    "check_aws_elasticache_replication_group_status",
    "check_aws_elasticache_serverless_cache_status",
    "check_aws_fsx_file_system_status",
    "check_aws_memorydb_cluster_status",
    "check_aws_opensearch_domain_status",
    "check_aws_rds_cluster_status",
]
