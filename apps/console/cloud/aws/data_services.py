"""Read-only inventory for AWS database and storage data services.

The integration lane owns the global asset registry and migrations.  This
module owns only the regional data-service models, bounded provider
collection, safe metadata reduction, and the adapter contract that lane can
register later.

No provider response is reconciled until the complete family/Region
collection has been staged.  A malformed page, an unsupported response, or a
child describe failure therefore leaves existing local rows untouched.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import logging
import re
from urllib.parse import quote

from django.db import models

from apps.console.cloud.aws.discovery import (
    aws_client,
    aws_error_code,
    get_enabled_regions,
    iter_pages,
    require_collection,
    serialize_aws,
)
from apps.console.cloud.aws.models import CoreAWSAccount
from apps.console.cloud.models import CloudInventoryTransientError
from apps.console.utils.models import UtilAsset
from apps.monitoring.metadata import redact_sensitive_metadata


logger = logging.getLogger(__name__)


AWS_RDS_CLUSTER = "aws_rds_cluster"
AWS_ELASTICACHE_CLUSTER = "aws_elasticache_cluster"
AWS_ELASTICACHE_REPLICATION_GROUP = "aws_elasticache_replication_group"
AWS_ELASTICACHE_SERVERLESS_CACHE = "aws_elasticache_serverless_cache"
AWS_MEMORYDB_CLUSTER = "aws_memorydb_cluster"
AWS_OPENSEARCH_DOMAIN = "aws_opensearch_domain"
AWS_EFS_FILE_SYSTEM = "aws_efs_file_system"
AWS_FSX_FILE_SYSTEM = "aws_fsx_file_system"


NORMALIZED_DATA_SERVICE_STATUSES = (
    "available",
    "active",
    "degraded",
    "pending",
    "failed",
    "not_found",
    "error",
)

MAX_COLLECTION_PAGES = 100
MAX_COLLECTION_ITEMS = 10_000
MAX_CHILD_ITEMS = 10_000
MAX_MOUNT_TARGETS_PER_FILE_SYSTEM = 1_000
MAX_METADATA_ITEMS = 80
MAX_METADATA_LIST_ITEMS = 100
MAX_METADATA_DEPTH = 5
MAX_METADATA_STRING = 2_048
MAX_PROVIDER_ID = 512
MAX_REGION_LENGTH = 32

_REGION_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)+$")
_SAFE_OPERATION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_SENSITIVE_LABEL_PARTS = (
    "password",
    "secret",
    "token",
    "accesskey",
    "privatekey",
    "credential",
    "authorization",
    "apikey",
)


def _service_operations(service):
    """Return the installed botocore operation names without network I/O."""
    try:
        from botocore.session import get_session

        return frozenset(get_session().get_service_model(service).operation_names)
    except Exception:
        return frozenset()


_ELASTICACHE_OPERATIONS = _service_operations("elasticache")
ELASTICACHE_SERVERLESS_SUPPORTED = "DescribeServerlessCaches" in _ELASTICACHE_OPERATIONS
EFS_MOUNT_TARGETS_SUPPORTED = "DescribeMountTargets" in _service_operations("efs")


class _InventoryIncomplete(CloudInventoryTransientError):
    """A provider response crossed a safety boundary or was incomplete."""


def _owner_region_constraint(name):
    return models.UniqueConstraint(
        fields=("owner", "region", "unique_id"),
        name=f"aws_data_{name}_owner_region_uid_uniq",
    )


class CoreAWSDataServiceAsset(UtilAsset):
    """Common regional fields and monitoring context for data services."""

    owner = models.ForeignKey(
        CoreAWSAccount,
        on_delete=models.CASCADE,
        related_name="%(class)s_assets",
    )
    region = models.CharField(max_length=MAX_REGION_LENGTH, db_index=True)
    asset_type = None

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self.type and self.asset_type:
            self.type = self.asset_type
        if self.metadata is not None:
            self.metadata = redact_sensitive_metadata(self.metadata)
        return super().save(*args, **kwargs)

    @property
    def monitoring_credentials(self):
        metadata = self.metadata if isinstance(self.metadata, dict) else {}
        provider_id = (
            metadata.get("_cloudmoo_provider_id")
            or metadata.get("provider_id")
            or metadata.get("_cloudmoo_raw_id")
            or self.unique_id
        )
        return {
            # These values are runtime monitoring inputs only.  They are not
            # copied into ``metadata`` by this adapter.
            "access_key": self.owner.access_key,
            "secret_key": self.owner.secret_key,
            "region": self.region,
            "resource_region": self.region,
            "provider_id": provider_id,
            "resource_name": provider_id,
            "asset_type": self.type or self.asset_type,
            "provider_type": self.asset_type,
            "metadata": metadata,
        }

    @property
    def provider_url(self):
        region = _safe_region(self.region or getattr(self.owner, "region", ""))
        metadata = self.metadata if isinstance(self.metadata, dict) else {}
        provider_id = (
            metadata.get("_cloudmoo_provider_id")
            or metadata.get("provider_id")
            or self.unique_id
        )
        service_path = _SERVICE_CONSOLE_PATHS.get(self.type or self.asset_type, "rds")
        safe_region = quote(region, safe="")
        safe_id = quote(str(provider_id)[:MAX_PROVIDER_ID], safe="-_.:/")
        return (
            f"https://{safe_region}.console.aws.amazon.com/{service_path}/home"
            f"?region={safe_region}#resource/{safe_id}"
        )

    def check_status(self):
        from apps.monitoring.checks.aws_data_services import (
            AWS_DATA_SERVICE_STATUS_CHECKS,
        )

        check = AWS_DATA_SERVICE_STATUS_CHECKS.get(self.type or self.asset_type)
        if check is None:
            return "error", {"error_code": "unsupported_asset_type"}
        return check(self.unique_id, self.monitoring_credentials)

    def __str__(self):
        return self.name


class CoreAWSRDSCluster(CoreAWSDataServiceAsset):
    asset_type = AWS_RDS_CLUSTER

    class Meta:
        db_table = "core_aws_rds_cluster"
        constraints = [_owner_region_constraint("rds_cluster")]


class CoreAWSElasticacheCluster(CoreAWSDataServiceAsset):
    asset_type = AWS_ELASTICACHE_CLUSTER

    class Meta:
        db_table = "core_aws_elasticache_cluster"
        constraints = [_owner_region_constraint("elasticache_cluster")]


class CoreAWSElasticacheReplicationGroup(CoreAWSDataServiceAsset):
    asset_type = AWS_ELASTICACHE_REPLICATION_GROUP

    class Meta:
        db_table = "core_aws_elasticache_replication_group"
        constraints = [_owner_region_constraint("elasticache_replication")]


class CoreAWSElasticacheServerlessCache(CoreAWSDataServiceAsset):
    asset_type = AWS_ELASTICACHE_SERVERLESS_CACHE

    class Meta:
        db_table = "core_aws_elasticache_serverless_cache"
        constraints = [_owner_region_constraint("elasticache_serverless")]


class CoreAWSMemoryDBCluster(CoreAWSDataServiceAsset):
    asset_type = AWS_MEMORYDB_CLUSTER

    class Meta:
        db_table = "core_aws_memorydb_cluster"
        constraints = [_owner_region_constraint("memorydb_cluster")]


class CoreAWSOpenSearchDomain(CoreAWSDataServiceAsset):
    asset_type = AWS_OPENSEARCH_DOMAIN

    class Meta:
        db_table = "core_aws_opensearch_domain"
        constraints = [_owner_region_constraint("opensearch_domain")]


class CoreAWSEFSFileSystem(CoreAWSDataServiceAsset):
    asset_type = AWS_EFS_FILE_SYSTEM

    class Meta:
        db_table = "core_aws_efs_file_system"
        constraints = [_owner_region_constraint("efs_file_system")]


class CoreAWSFSxFileSystem(CoreAWSDataServiceAsset):
    asset_type = AWS_FSX_FILE_SYSTEM

    class Meta:
        db_table = "core_aws_fsx_file_system"
        constraints = [_owner_region_constraint("fsx_file_system")]


# Spellings used by callers that mirror the AWS service brand or acronym.
CoreAWSElastiCacheCluster = CoreAWSElasticacheCluster
CoreAWSElastiCacheReplicationGroup = CoreAWSElasticacheReplicationGroup
CoreAWSElastiCacheServerlessCache = CoreAWSElasticacheServerlessCache


_SERVICE_CONSOLE_PATHS = {
    AWS_RDS_CLUSTER: "rds",
    AWS_ELASTICACHE_CLUSTER: "elasticache",
    AWS_ELASTICACHE_REPLICATION_GROUP: "elasticache",
    AWS_ELASTICACHE_SERVERLESS_CACHE: "elasticache",
    AWS_MEMORYDB_CLUSTER: "memorydb",
    AWS_OPENSEARCH_DOMAIN: "aos",
    AWS_EFS_FILE_SYSTEM: "efs",
    AWS_FSX_FILE_SYSTEM: "fsx",
}


AWS_DATA_SERVICE_ASSET_TYPES = (
    AWS_RDS_CLUSTER,
    AWS_ELASTICACHE_CLUSTER,
    AWS_ELASTICACHE_REPLICATION_GROUP,
    *((AWS_ELASTICACHE_SERVERLESS_CACHE,) if ELASTICACHE_SERVERLESS_SUPPORTED else ()),
    AWS_MEMORYDB_CLUSTER,
    AWS_OPENSEARCH_DOMAIN,
    AWS_EFS_FILE_SYSTEM,
    AWS_FSX_FILE_SYSTEM,
)

AWS_DATA_SERVICE_ASSET_MODELS = {
    AWS_RDS_CLUSTER: CoreAWSRDSCluster,
    AWS_ELASTICACHE_CLUSTER: CoreAWSElasticacheCluster,
    AWS_ELASTICACHE_REPLICATION_GROUP: CoreAWSElasticacheReplicationGroup,
    **(
        {AWS_ELASTICACHE_SERVERLESS_CACHE: CoreAWSElasticacheServerlessCache}
        if ELASTICACHE_SERVERLESS_SUPPORTED
        else {}
    ),
    AWS_MEMORYDB_CLUSTER: CoreAWSMemoryDBCluster,
    AWS_OPENSEARCH_DOMAIN: CoreAWSOpenSearchDomain,
    AWS_EFS_FILE_SYSTEM: CoreAWSEFSFileSystem,
    AWS_FSX_FILE_SYSTEM: CoreAWSFSxFileSystem,
}


_RDS_ALLOWED = (
    "DBClusterIdentifier",
    "DBClusterArn",
    "Status",
    "Engine",
    "EngineMode",
    "EngineVersion",
    "DatabaseName",
    "Endpoint",
    "ReaderEndpoint",
    "CustomEndpoints",
    "Port",
    "PreferredBackupWindow",
    "PreferredMaintenanceWindow",
    "BackupRetentionPeriod",
    "AllocatedStorage",
    "StorageType",
    "StorageEncrypted",
    "KmsKeyId",
    "DbClusterResourceId",
    "DBSubnetGroup",
    "VpcSecurityGroups",
    "AvailabilityZones",
    "MultiAZ",
    "DeletionProtection",
    "CopyTagsToSnapshot",
    "CrossAccountClone",
    "HttpEndpointEnabled",
    "ServerlessV2ScalingConfiguration",
    "ScalingConfigurationInfo",
    "DBClusterMembers",
    "AssociatedRoles",
    "CertificateDetails",
    "TagList",
    "CreatedAt",
)

_ELASTICACHE_CLUSTER_ALLOWED = (
    "CacheClusterId",
    "ARN",
    "CacheClusterStatus",
    "Engine",
    "EngineVersion",
    "CacheNodeType",
    "NumCacheNodes",
    "PreferredAvailabilityZone",
    "PreferredAvailabilityZones",
    "PreferredOutpostArn",
    "CacheNodes",
    "CacheSecurityGroups",
    "CacheParameterGroup",
    "CacheSubnetGroupName",
    "VpcId",
    "AutoMinorVersionUpgrade",
    "SnapshotRetentionLimit",
    "SnapshotWindow",
    "PreferredMaintenanceWindow",
    "NotificationConfiguration",
    "TransitEncryptionEnabled",
    "AtRestEncryptionEnabled",
    "NetworkType",
    "IpDiscovery",
    "LogDeliveryConfigurations",
    "DataTieringEnabled",
    "CreatedAt",
)

_ELASTICACHE_REPLICATION_ALLOWED = (
    "ReplicationGroupId",
    "ARN",
    "Status",
    "Description",
    "Engine",
    "EngineVersion",
    "CacheNodeType",
    "NumCacheClusters",
    "NumNodeGroups",
    "CacheClusterIds",
    "MemberClusters",
    "NodeGroups",
    "PreferredCacheClusterAZs",
    "PreferredMaintenanceWindow",
    "SnapshottingClusterId",
    "AutomaticFailover",
    "MultiAZ",
    "CacheParameterGroup",
    "CacheSecurityGroups",
    "CacheSubnetGroup",
    "VpcId",
    "TransitEncryptionEnabled",
    "AtRestEncryptionEnabled",
    "AuthTokenEnabled",
    "ClusterMode",
    "GlobalReplicationGroupId",
    "GlobalReplicationGroupInfo",
    "LogDeliveryConfigurations",
    "DataTieringEnabled",
    "NetworkType",
    "IpDiscovery",
    "CreatedAt",
)

_ELASTICACHE_SERVERLESS_ALLOWED = (
    "ServerlessCacheName",
    "ServerlessCacheArn",
    "Description",
    "Status",
    "Engine",
    "MajorEngineVersion",
    "FullEngineVersion",
    "Endpoint",
    "ReaderEndpoint",
    "SubnetIds",
    "SecurityGroupIds",
    "VpcId",
    "DailySnapshotTime",
    "SnapshotRetentionLimit",
    "TransitEncryptionEnabled",
    "NetworkType",
    "IpDiscovery",
    "KmsKeyId",
    "CacheUsageLimits",
    "UserGroupId",
    "CreatedTime",
    "LastModifiedTime",
)

_MEMORYDB_ALLOWED = (
    "Name",
    "ARN",
    "Status",
    "NodeType",
    "Engine",
    "EngineVersion",
    "NumberOfShards",
    "NumReplicasPerShard",
    "AvailabilityMode",
    "ClusterEndpoint",
    "TLSEnabled",
    "ACLName",
    "SubnetGroupName",
    "SecurityGroupIds",
    "ParameterGroupName",
    "MaintenanceWindow",
    "SnapshotRetentionLimit",
    "SnapshotWindow",
    "DataTiering",
    "Shards",
    "MultiRegionClusterName",
    "MultiRegionClusterArn",
    "CreatedAt",
)

_OPENSEARCH_ALLOWED = (
    "DomainId",
    "DomainName",
    "ARN",
    "EngineVersion",
    "Created",
    "Deleted",
    "Processing",
    "UpgradeProcessing",
    "ClusterConfig",
    "EBSOptions",
    "VPCOptions",
    "EncryptionAtRestOptions",
    "NodeToNodeEncryptionOptions",
    "DomainEndpointOptions",
    "IPAddressType",
    "CognitoOptions",
    "LogPublishingOptions",
    "AutoTuneOptions",
    "OffPeakWindowOptions",
    "SoftwareUpdateOptions",
    "AdvancedOptions",
    "InstanceType",
    "InstanceCount",
    "DedicatedMasterType",
    "DedicatedMasterCount",
    "WarmType",
    "WarmCount",
    "ZoneAwarenessEnabled",
    "ZoneAwarenessConfig",
    "Tags",
)

_EFS_ALLOWED = (
    "FileSystemId",
    "FileSystemArn",
    "CreationTime",
    "LifeCycleState",
    "Name",
    "NumberOfMountTargets",
    "OwnerId",
    "CreationToken",
    "PerformanceMode",
    "ThroughputMode",
    "ProvisionedThroughputInMibps",
    "Encrypted",
    "KmsKeyId",
    "SizeInBytes",
    "AvailabilityZoneName",
    "AvailabilityZoneId",
    "SubnetId",
    "VpcId",
    "FileSystemProtection",
    "Tags",
)

_FSX_ALLOWED = (
    "FileSystemId",
    "FileSystemType",
    "FileSystemTypeVersion",
    "Lifecycle",
    "StorageCapacity",
    "StorageType",
    "CreationTime",
    "StorageVirtualMachineIds",
    "SubnetIds",
    "NetworkInterfaceIds",
    "VpcId",
    "DNSName",
    "ResourceARN",
    "KmsKeyId",
    "LustreConfiguration",
    "OntapConfiguration",
    "OpenZFSConfiguration",
    "WindowsConfiguration",
    "FileSystemEndpoints",
    "AdministrativeActions",
    "Tags",
)


def normalize_data_service_status(value):
    """Normalize provider lifecycle values to the monitoring vocabulary."""
    if value is None or isinstance(value, bool):
        return "error"
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")
    if not normalized:
        return "error"

    if normalized in {
        "available",
        "ready",
        "healthy",
        "online",
        "succeeded",
        "success",
        "complete",
        "completed",
        "green",
    }:
        return "available"
    if normalized in {
        "active",
        "running",
        "enabled",
        "in_use",
        "inuse",
        "operational",
    }:
        return "active"
    if normalized in {
        "degraded",
        "impaired",
        "warning",
        "yellow",
        "partial",
        "failing_over",
    }:
        return "degraded"
    if normalized in {
        "pending",
        "creating",
        "starting",
        "stopping",
        "deleting",
        "modifying",
        "updating",
        "processing",
        "in_progress",
        "rebooting",
        "snapshotting",
        "maintenance",
        "upgrading",
        "configuring",
    }:
        return "pending"
    if normalized in {
        "failed",
        "failure",
        "error",
        "errored",
        "misconfigured",
        "inaccessible",
        "cancelled",
        "canceled",
        "aborted",
        "red",
    }:
        return "failed"
    if normalized in {
        "not_found",
        "notfound",
        "deleted",
        "removed",
        "does_not_exist",
    }:
        return "not_found"
    return "error"


def _safe_region(region):
    if not isinstance(region, str):
        raise CloudInventoryTransientError("AWS returned an invalid Region")
    region = region.strip()
    if (
        not region
        or len(region) > MAX_REGION_LENGTH
        or _REGION_PATTERN.fullmatch(region) is None
    ):
        raise CloudInventoryTransientError("AWS returned an invalid Region")
    return region


def _normalize_regions(regions):
    if not isinstance(regions, (list, tuple, set, frozenset)):
        raise CloudInventoryTransientError("AWS returned an invalid enabled Region collection")
    normalized = {_safe_region(region) for region in regions}
    return sorted(normalized)


def _bounded_value(value, depth=0):
    if depth > MAX_METADATA_DEPTH:
        return "<truncated>"
    if isinstance(value, Mapping):
        result = {}
        for index, (key, child) in enumerate(value.items()):
            if index >= MAX_METADATA_ITEMS:
                result["_cloudmoo_truncated"] = True
                break
            result[str(key)[:128]] = _bounded_value(child, depth + 1)
        return result
    if isinstance(value, list):
        result = [_bounded_value(item, depth + 1) for item in value[:MAX_METADATA_LIST_ITEMS]]
        if len(value) > MAX_METADATA_LIST_ITEMS:
            result.append("<truncated>")
        return result
    if isinstance(value, tuple):
        return _bounded_value(list(value), depth)
    if isinstance(value, str):
        return value[:MAX_METADATA_STRING]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:MAX_METADATA_STRING]


def _redact_structured_sensitive_values(value):
    """Redact values paired with sensitive tag/field labels."""
    if isinstance(value, Mapping):
        labels = []
        for key in ("Key", "Name", "Field"):
            label = value.get(key)
            if isinstance(label, str):
                normalized = "".join(character for character in label.lower() if character.isalnum())
                labels.append(normalized)
        sensitive_label = any(
            any(part in label for part in _SENSITIVE_LABEL_PARTS)
            for label in labels
        )
        result = {}
        for key, child in value.items():
            normalized_key = "".join(character for character in str(key).lower() if character.isalnum())
            if sensitive_label and normalized_key in {"value", "values"}:
                result[key] = "[REDACTED]"
            else:
                result[key] = _redact_structured_sensitive_values(child)
        return result
    if isinstance(value, list):
        return [_redact_structured_sensitive_values(item) for item in value]
    return value


def _provider_id(resource, keys, context):
    if not isinstance(resource, Mapping):
        raise _InventoryIncomplete(f"AWS returned an invalid {context} object")
    for key in keys:
        value = resource.get(key)
        if value is None:
            continue
        value = str(value).strip()
        if value:
            if len(value) > MAX_PROVIDER_ID:
                raise _InventoryIncomplete(f"AWS returned an oversized {context} identifier")
            return value
    raise _InventoryIncomplete(f"AWS returned a {context} without an identifier")


def _first_value(resource, keys):
    for key in keys:
        value = resource.get(key) if isinstance(resource, Mapping) else None
        if value is None:
            continue
        value = str(value).strip()
        if value:
            return value[:MAX_PROVIDER_ID]
    return None


def _provider_state(resource, keys):
    if not isinstance(resource, Mapping):
        return None
    for key in keys:
        if key not in resource:
            continue
        value = resource[key]
        if isinstance(value, bool):
            if key in {"Processing", "UpgradeProcessing"}:
                return "processing" if value else "active"
            if key == "Deleted":
                return "deleted" if value else "active"
            continue
        if value is not None and str(value).strip():
            return str(value).strip()[:128]
    return None


def _safe_selected(resource, allowed_keys):
    if not isinstance(resource, Mapping):
        raise _InventoryIncomplete("AWS returned an invalid resource object")
    selected = {
        key: resource[key]
        for key in allowed_keys
        if key in resource
    }
    serialized = serialize_aws(selected)
    if not isinstance(serialized, dict):
        raise _InventoryIncomplete("AWS returned an invalid serialized resource object")
    return _bounded_value(serialized)


def _safe_nested(value, allowed_keys):
    if not isinstance(value, Mapping):
        return None
    return _safe_selected(value, allowed_keys)


def _safe_mount_targets(value):
    if value is None:
        return []
    if not isinstance(value, list):
        raise _InventoryIncomplete("AWS returned an invalid EFS mount-target collection")
    if len(value) > MAX_MOUNT_TARGETS_PER_FILE_SYSTEM:
        raise _InventoryIncomplete("AWS EFS mount-target inventory exceeded the safety bound")
    allowed = (
        "MountTargetId",
        "FileSystemId",
        "SubnetId",
        "LifeCycleState",
        "IpAddress",
        "NetworkInterfaceId",
        "AvailabilityZoneName",
        "AvailabilityZoneId",
        "OwnerId",
    )
    result = []
    for target in value:
        result.append(_safe_selected(target, allowed))
    return result


def _safe_metadata(
    resource,
    *,
    region,
    provider_id,
    raw_id,
    normalized_status,
    provider_state,
    allowed_keys,
    extra=None,
):
    metadata = {
        "_cloudmoo_region": region,
        "_cloudmoo_provider_id": provider_id,
        "_cloudmoo_raw_id": raw_id or provider_id,
        "_cloudmoo_asset_id": _stable_id("data", region, provider_id),
        "provider_id": provider_id,
        "raw_id": raw_id or provider_id,
        "normalized_status": normalized_status,
    }
    if provider_state is not None:
        metadata["provider_state"] = provider_state
    metadata.update(_safe_selected(resource, allowed_keys))
    if extra:
        metadata.update(_bounded_value(extra))
    return _bounded_value(
        _redact_structured_sensitive_values(redact_sensitive_metadata(metadata))
    )


def _stable_id(asset_type, region, *identifiers):
    values = [str(value).strip() for value in (asset_type, region, *identifiers) if value is not None]
    raw = ":".join(value for value in values if value)
    if len(raw) <= 100:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:56]
    return f"{asset_type}:{region}:sha256:{digest}"[:100]


def _record(
    resource,
    region,
    asset_type,
    *,
    id_keys,
    raw_id_keys=None,
    name_keys=None,
    state_keys=("Status", "State", "Lifecycle", "LifeCycleState"),
    allowed_keys=(),
    extra=None,
    context="AWS data-service resource",
):
    if not isinstance(resource, Mapping):
        raise _InventoryIncomplete(f"AWS returned an invalid {context} object")
    provider_id = _provider_id(resource, id_keys, context)
    raw_id = _first_value(resource, raw_id_keys or id_keys) or provider_id
    name = _first_value(resource, name_keys or id_keys) or provider_id
    provider_state = _provider_state(resource, state_keys)
    normalized_status = normalize_data_service_status(provider_state)
    return {
        "unique_id": _stable_id(asset_type, region, provider_id),
        "name": name[:100],
        "provider_id": provider_id,
        "raw_id": raw_id,
        "status": normalized_status,
        "metadata": _safe_metadata(
            resource,
            region=region,
            provider_id=provider_id,
            raw_id=raw_id,
            normalized_status=normalized_status,
            provider_state=provider_state,
            allowed_keys=allowed_keys,
            extra=extra,
        ),
    }


def _bounded_pages(client, operation, **kwargs):
    if not isinstance(operation, str) or _SAFE_OPERATION_RE.fullmatch(operation) is None:
        raise ValueError("AWS operation is invalid")
    for page_number, page in enumerate(iter_pages(client, operation, **kwargs), start=1):
        if page_number > MAX_COLLECTION_PAGES:
            raise _InventoryIncomplete(f"AWS {operation} pagination exceeded the safety bound")
        if not isinstance(page, Mapping):
            raise _InventoryIncomplete(f"AWS returned an invalid {operation} page")
        yield page


def _collection_items(client, operation, collection_key, context, **kwargs):
    items = []
    for page in _bounded_pages(client, operation, **kwargs):
        page_items = require_collection(page, collection_key, context)
        if len(items) + len(page_items) > MAX_COLLECTION_ITEMS:
            raise _InventoryIncomplete(f"AWS {context} inventory exceeded the safety bound")
        for item in page_items:
            if not isinstance(item, Mapping):
                raise _InventoryIncomplete(f"AWS returned an invalid {context} item")
            items.append(item)
    return items


def _describe_resource(client, operation, response_key, context, **kwargs):
    if not isinstance(operation, str) or _SAFE_OPERATION_RE.fullmatch(operation) is None:
        raise ValueError("AWS operation is invalid")
    describe = getattr(client, operation)
    response = describe(**kwargs)
    if not isinstance(response, Mapping):
        raise _InventoryIncomplete(f"AWS returned an invalid {context} response")
    if response_key is None:
        resource = response
    else:
        resource = response.get(response_key)
    if not isinstance(resource, Mapping):
        raise _InventoryIncomplete(f"AWS returned an invalid {context} resource")
    return resource


def _collect_rds(client, region):
    resources = _collection_items(
        client,
        "describe_db_clusters",
        "DBClusters",
        "RDS DB clusters",
    )
    return [
        _record(
            resource,
            region,
            AWS_RDS_CLUSTER,
            id_keys=("DBClusterIdentifier", "DBClusterArn"),
            raw_id_keys=("DBClusterArn", "DBClusterIdentifier"),
            name_keys=("DBClusterIdentifier", "DBClusterArn"),
            state_keys=("Status",),
            allowed_keys=_RDS_ALLOWED,
            context="RDS DB cluster",
        )
        for resource in resources
    ]


def _collect_elasticache_clusters(client, region):
    resources = _collection_items(
        client,
        "describe_cache_clusters",
        "CacheClusters",
        "ElastiCache clusters",
    )
    return [
        _record(
            resource,
            region,
            AWS_ELASTICACHE_CLUSTER,
            id_keys=("CacheClusterId", "ARN"),
            raw_id_keys=("ARN", "CacheClusterId"),
            name_keys=("CacheClusterId",),
            state_keys=("CacheClusterStatus", "Status"),
            allowed_keys=_ELASTICACHE_CLUSTER_ALLOWED,
            context="ElastiCache cluster",
        )
        for resource in resources
    ]


def _collect_elasticache_replication_groups(client, region):
    resources = _collection_items(
        client,
        "describe_replication_groups",
        "ReplicationGroups",
        "ElastiCache replication groups",
    )
    return [
        _record(
            resource,
            region,
            AWS_ELASTICACHE_REPLICATION_GROUP,
            id_keys=("ReplicationGroupId", "ARN"),
            raw_id_keys=("ARN", "ReplicationGroupId"),
            name_keys=("ReplicationGroupId", "Description"),
            state_keys=("Status",),
            allowed_keys=_ELASTICACHE_REPLICATION_ALLOWED,
            context="ElastiCache replication group",
        )
        for resource in resources
    ]


def _collect_elasticache_serverless(client, region):
    if not ELASTICACHE_SERVERLESS_SUPPORTED:
        return []
    resources = _collection_items(
        client,
        "describe_serverless_caches",
        "ServerlessCaches",
        "ElastiCache serverless caches",
    )
    return [
        _record(
            resource,
            region,
            AWS_ELASTICACHE_SERVERLESS_CACHE,
            id_keys=("ServerlessCacheName", "ServerlessCacheArn"),
            raw_id_keys=("ServerlessCacheArn", "ServerlessCacheName"),
            name_keys=("ServerlessCacheName",),
            state_keys=("Status",),
            allowed_keys=_ELASTICACHE_SERVERLESS_ALLOWED,
            context="ElastiCache serverless cache",
        )
        for resource in resources
    ]


def _collect_memorydb(client, region):
    resources = _collection_items(
        client,
        "describe_clusters",
        "Clusters",
        "MemoryDB clusters",
    )
    return [
        _record(
            resource,
            region,
            AWS_MEMORYDB_CLUSTER,
            id_keys=("Name", "ARN"),
            raw_id_keys=("ARN", "Name"),
            name_keys=("Name",),
            state_keys=("Status",),
            allowed_keys=_MEMORYDB_ALLOWED,
            context="MemoryDB cluster",
        )
        for resource in resources
    ]


def _opensearch_state(resource):
    if resource.get("Deleted") is True:
        return "deleted"
    if resource.get("Processing") is True or resource.get("UpgradeProcessing") is True:
        return "processing"
    state = _provider_state(resource, ("Status", "State"))
    return state or "active"


def _collect_opensearch(client, region):
    summaries = _collection_items(
        client,
        "list_domain_names",
        "DomainNames",
        "OpenSearch domain names",
    )
    if len(summaries) > MAX_CHILD_ITEMS:
        raise _InventoryIncomplete("AWS OpenSearch domain inventory exceeded the safety bound")

    records = []
    for summary in summaries:
        domain_name = _provider_id(summary, ("DomainName",), "OpenSearch domain")
        resource = dict(
            _describe_resource(
                client,
                "describe_domain",
                "DomainStatus",
                "OpenSearch domain",
                DomainName=domain_name,
            )
        )
        # DomainStatus normally echoes DomainName.  The list request is the
        # authoritative identity when an older response omits that echo.
        resource.setdefault("DomainName", domain_name)
        records.append(
            _record(
                resource,
                region,
                AWS_OPENSEARCH_DOMAIN,
                id_keys=("DomainName", "DomainId", "ARN"),
                raw_id_keys=("ARN", "DomainId", "DomainName"),
                name_keys=("DomainName",),
                state_keys=("Status", "State", "Processing", "UpgradeProcessing", "Deleted"),
                allowed_keys=_OPENSEARCH_ALLOWED,
                context="OpenSearch domain",
            )
        )
    return records


def _client_supports(client, operation):
    try:
        return callable(getattr(client, operation))
    except AttributeError:
        return False


def _collect_efs_mount_targets(client, provider_id, resource):
    if "MountTargets" in resource:
        return _safe_mount_targets(resource.get("MountTargets"))
    if not EFS_MOUNT_TARGETS_SUPPORTED or not _client_supports(client, "describe_mount_targets"):
        return []
    targets = _collection_items(
        client,
        "describe_mount_targets",
        "MountTargets",
        "EFS mount targets",
        FileSystemId=provider_id,
    )
    return _safe_mount_targets(targets)


def _collect_efs(client, region):
    resources = _collection_items(
        client,
        "describe_file_systems",
        "FileSystems",
        "EFS file systems",
    )
    records = []
    for resource in resources:
        provider_id = _provider_id(resource, ("FileSystemId", "FileSystemArn"), "EFS file system")
        mount_targets = _collect_efs_mount_targets(client, provider_id, resource)
        records.append(
            _record(
                resource,
                region,
                AWS_EFS_FILE_SYSTEM,
                id_keys=("FileSystemId", "FileSystemArn"),
                raw_id_keys=("FileSystemArn", "FileSystemId"),
                name_keys=("Name", "FileSystemId"),
                state_keys=("LifeCycleState", "Status"),
                allowed_keys=_EFS_ALLOWED,
                extra={"mount_targets": mount_targets},
                context="EFS file system",
            )
        )
    return records


def _collect_fsx(client, region):
    resources = _collection_items(
        client,
        "describe_file_systems",
        "FileSystems",
        "FSx file systems",
    )
    return [
        _record(
            resource,
            region,
            AWS_FSX_FILE_SYSTEM,
            id_keys=("FileSystemId", "ResourceARN"),
            raw_id_keys=("ResourceARN", "FileSystemId"),
            name_keys=("FileSystemId", "DNSName"),
            state_keys=("Lifecycle", "LifeCycleState", "Status"),
            allowed_keys=_FSX_ALLOWED,
            context="FSx file system",
        )
        for resource in resources
    ]


def _upsert_asset(model, account, region, asset_type, record):
    asset, created = model.objects.get_or_create(
        owner=account,
        region=region,
        unique_id=record["unique_id"],
        defaults={
            "name": record["name"][:100],
            "type": asset_type,
            "metadata": record["metadata"],
            "monitoring": UtilAsset.Monitoring.ACTIVE,
        },
    )
    asset.name = record["name"][:100]
    asset.region = region
    asset.type = asset_type
    asset.metadata = record["metadata"]
    if not created and asset.monitoring == UtilAsset.Monitoring.NO_LONGER_EXISTS:
        asset.monitoring = UtilAsset.Monitoring.ACTIVE
    asset.save()
    return asset


def _reconcile(model, account, region, asset_type, records):
    if not isinstance(records, list):
        raise _InventoryIncomplete(f"AWS returned an invalid {asset_type} collection")

    current_ids = []
    seen_ids = set()
    for record in records:
        if not isinstance(record, Mapping) or not record.get("unique_id"):
            raise _InventoryIncomplete(f"AWS returned an invalid {asset_type} record")
        unique_id = str(record["unique_id"])
        if unique_id in seen_ids:
            raise _InventoryIncomplete(f"AWS returned a duplicate {asset_type} identifier")
        seen_ids.add(unique_id)
        _upsert_asset(model, account, region, asset_type, record)
        current_ids.append(unique_id)

    model.objects.filter(owner=account, region=region).exclude(
        unique_id__in=current_ids
    ).update(monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS)
    return len(current_ids)


def _error_code(error):
    try:
        return str(aws_error_code(error))[:128]
    except Exception:
        return type(error).__name__[:128]


def _family_error(region, asset_type, error):
    return {
        "region": region,
        "asset_type": asset_type,
        "error_code": _error_code(error),
    }


_SERVERLESS_FAMILY_SPEC = (
    (
        AWS_ELASTICACHE_SERVERLESS_CACHE,
        CoreAWSElasticacheServerlessCache,
        "elasticache",
        _collect_elasticache_serverless,
    ),
) if ELASTICACHE_SERVERLESS_SUPPORTED else ()


_FAMILY_SPECS = (
    (AWS_RDS_CLUSTER, CoreAWSRDSCluster, "rds", _collect_rds),
    (AWS_ELASTICACHE_CLUSTER, CoreAWSElasticacheCluster, "elasticache", _collect_elasticache_clusters),
    (
        AWS_ELASTICACHE_REPLICATION_GROUP,
        CoreAWSElasticacheReplicationGroup,
        "elasticache",
        _collect_elasticache_replication_groups,
    ),
    *_SERVERLESS_FAMILY_SPEC,
    (AWS_MEMORYDB_CLUSTER, CoreAWSMemoryDBCluster, "memorydb", _collect_memorydb),
    (AWS_OPENSEARCH_DOMAIN, CoreAWSOpenSearchDomain, "opensearch", _collect_opensearch),
    (AWS_EFS_FILE_SYSTEM, CoreAWSEFSFileSystem, "efs", _collect_efs),
    (AWS_FSX_FILE_SYSTEM, CoreAWSFSxFileSystem, "fsx", _collect_fsx),
)


def sync_aws_data_service_assets(account):
    """Synchronize all supported AWS data-service families read-only."""
    counts = {asset_type: 0 for asset_type in AWS_DATA_SERVICE_ASSET_TYPES}
    families = {
        asset_type: {"complete": False, "reconciled": False, "count": None}
        for asset_type in AWS_DATA_SERVICE_ASSET_TYPES
    }
    errors = []
    failed_families = set()

    try:
        regions = _normalize_regions(get_enabled_regions(account))
    except Exception as error:
        error_code = _error_code(error)
        for asset_type in AWS_DATA_SERVICE_ASSET_TYPES:
            errors.append({"asset_type": asset_type, "error_code": error_code})
        return {
            "regions": [],
            "counts": {asset_type: None for asset_type in AWS_DATA_SERVICE_ASSET_TYPES},
            "synced": {asset_type: None for asset_type in AWS_DATA_SERVICE_ASSET_TYPES},
            "families": families,
            "errors": errors,
        }

    for region in regions:
        clients = {}
        client_failures = {}
        for asset_type, model, service, collector in _FAMILY_SPECS:
            if service in client_failures:
                errors.append(_family_error(region, asset_type, client_failures[service]))
                counts[asset_type] = None
                failed_families.add(asset_type)
                families[asset_type] = {
                    "complete": False,
                    "reconciled": False,
                    "count": None,
                }
                continue
            try:
                client = clients.get(service)
                if client is None:
                    client = aws_client(account, service, region=region)
                    clients[service] = client
                records = collector(client, region)
                count = _reconcile(model, account, region, asset_type, records)
                if asset_type not in failed_families:
                    counts[asset_type] += count
                    families[asset_type] = {
                        "complete": True,
                        "reconciled": True,
                        "count": counts[asset_type],
                    }
            except Exception as error:
                if service not in clients and service not in client_failures:
                    # A client-construction failure applies to every family
                    # using the same regional service client.
                    client_failures[service] = error
                errors.append(_family_error(region, asset_type, error))
                counts[asset_type] = None
                failed_families.add(asset_type)
                families[asset_type] = {
                    "complete": False,
                    "reconciled": False,
                    "count": None,
                }
                logger.warning(
                    "AWS data-service collection failed for %s/%s: %s",
                    region,
                    asset_type,
                    _error_code(error),
                )

    synced = dict(counts)
    return {
        "regions": regions,
        "counts": counts,
        "synced": synced,
        "families": families,
        "errors": errors,
    }


# Explicit names make the integration boundary discoverable without changing
# the global asset registry in this lane.
AWS_DATA_SERVICE_MODELS = AWS_DATA_SERVICE_ASSET_MODELS
AWS_DATA_SERVICE_TYPES = AWS_DATA_SERVICE_ASSET_TYPES


__all__ = [
    "AWS_DATA_SERVICE_ASSET_MODELS",
    "AWS_DATA_SERVICE_ASSET_TYPES",
    "AWS_DATA_SERVICE_MODELS",
    "AWS_DATA_SERVICE_TYPES",
    "AWS_EFS_FILE_SYSTEM",
    "AWS_ELASTICACHE_CLUSTER",
    "AWS_ELASTICACHE_REPLICATION_GROUP",
    "AWS_ELASTICACHE_SERVERLESS_CACHE",
    "AWS_FSX_FILE_SYSTEM",
    "AWS_MEMORYDB_CLUSTER",
    "AWS_OPENSEARCH_DOMAIN",
    "AWS_RDS_CLUSTER",
    "CoreAWSDataServiceAsset",
    "CoreAWSEFSFileSystem",
    "CoreAWSElastiCacheCluster",
    "CoreAWSElastiCacheReplicationGroup",
    "CoreAWSElastiCacheServerlessCache",
    "CoreAWSElasticacheCluster",
    "CoreAWSElasticacheReplicationGroup",
    "CoreAWSElasticacheServerlessCache",
    "CoreAWSFSxFileSystem",
    "CoreAWSMemoryDBCluster",
    "CoreAWSOpenSearchDomain",
    "CoreAWSRDSCluster",
    "ELASTICACHE_SERVERLESS_SUPPORTED",
    "EFS_MOUNT_TARGETS_SUPPORTED",
    "NORMALIZED_DATA_SERVICE_STATUSES",
    "normalize_data_service_status",
    "sync_aws_data_service_assets",
]
