"""Compute-adjacent Vultr inventory models and explicit resource contracts.

The legacy server and volume tables remain owned by ``vultr.models``.  This
module only models additional, read-only families.  VFS and storage-gateway
records are represented for future integration but intentionally have no
endpoint until their account-level v2 API shapes are confirmed.
"""

from __future__ import annotations

from django.db import models

from apps.console.cloud.vultr.resources_base import (
    CoreVultrResource,
    VultrResourceSpec,
    register_vultr_resource_specs,
)
from apps.console.utils.models import UtilAsset


class CoreVultrBareMetal(CoreVultrResource):
    provider_type = "vultr_bare_metal"
    asset_type = "vultr_bare_metal"
    api_endpoint = "bare-metals"

    class Meta:
        db_table = "core_vultr_bare_metal"


class CoreVultrBlockSnapshot(CoreVultrResource):
    provider_type = "vultr_block_snapshot"
    asset_type = "vultr_block_snapshot"
    api_endpoint = "blocks/snapshots"

    class Meta:
        db_table = "core_vultr_block_snapshot"


class CoreVultrInstanceBackup(CoreVultrResource):
    provider_type = "vultr_instance_backup"
    asset_type = UtilAsset.Type.BACKUP
    api_endpoint = "backups"

    class Meta:
        db_table = "core_vultr_instance_backup"


class CoreVultrBandwidthMetric(CoreVultrResource):
    provider_type = "vultr_bandwidth_metric"
    asset_type = "vultr_bandwidth_metric"
    api_endpoint = None

    class Meta:
        db_table = "core_vultr_bandwidth_metric"


class CoreVultrVFS(CoreVultrResource):
    provider_type = "vultr_vfs"
    asset_type = "vultr_vfs"
    api_endpoint = None

    class Meta:
        db_table = "core_vultr_vfs"


class CoreVultrStorageGateway(CoreVultrResource):
    provider_type = "vultr_storage_gateway"
    asset_type = "vultr_storage_gateway"
    api_endpoint = None

    class Meta:
        db_table = "core_vultr_storage_gateway"


class CoreVultrComputePlan(CoreVultrResource):
    provider_type = "vultr_compute_plan"
    asset_type = "vultr_compute_plan"
    api_endpoint = "plans"

    class Meta:
        db_table = "core_vultr_compute_plan"


# Clear aliases make the registry convenient for integration code without
# registering duplicate Django models.
CoreVultrBareMetalServer = CoreVultrBareMetal
CoreVultrBlockStorageSnapshot = CoreVultrBlockSnapshot
CoreVultrBackup = CoreVultrInstanceBackup
CoreVultrVultrFileSystem = CoreVultrVFS
CoreVultrPlan = CoreVultrComputePlan


RESOURCE_MODELS = {
    "bare_metal": CoreVultrBareMetal,
    "vultr_bare_metal": CoreVultrBareMetal,
    "block_snapshot": CoreVultrBlockSnapshot,
    "vultr_block_snapshot": CoreVultrBlockSnapshot,
    "backup": CoreVultrInstanceBackup,
    "instance_backup": CoreVultrInstanceBackup,
    "bandwidth_metric": CoreVultrBandwidthMetric,
    "vultr_bandwidth_metric": CoreVultrBandwidthMetric,
    "vfs": CoreVultrVFS,
    "vultr_vfs": CoreVultrVFS,
    "storage_gateway": CoreVultrStorageGateway,
    "vultr_storage_gateway": CoreVultrStorageGateway,
    "compute_plan": CoreVultrComputePlan,
    "vultr_compute_plan": CoreVultrComputePlan,
}


RESOURCE_SPECS = {
    "bare_metal": VultrResourceSpec(
        key="bare_metal",
        endpoint="bare-metals",
        collection_key="bare_metals",
        model=CoreVultrBareMetal,
        asset_type="vultr_bare_metal",
        provider_type="vultr_bare_metal",
        name_fields=("label", "hostname", "id"),
        response_key="bare_metal",
        metadata_key="bare_metal",
    ),
    "block_snapshot": VultrResourceSpec(
        key="block_snapshot",
        endpoint="blocks/snapshots",
        collection_key="snapshots",
        model=CoreVultrBlockSnapshot,
        asset_type="vultr_block_snapshot",
        provider_type="vultr_block_snapshot",
        name_fields=("description", "date_created", "id"),
        response_key="snapshot",
        metadata_key="snapshot",
    ),
    "backup": VultrResourceSpec(
        key="backup",
        endpoint="backups",
        collection_key="backups",
        model=CoreVultrInstanceBackup,
        asset_type=UtilAsset.Type.BACKUP,
        provider_type="vultr_instance_backup",
        name_fields=("description", "date_created", "id"),
        response_key="backup",
        metadata_key="backup",
    ),
    "compute_plan": VultrResourceSpec(
        key="compute_plan",
        endpoint="plans",
        collection_key="plans",
        model=CoreVultrComputePlan,
        asset_type="vultr_compute_plan",
        provider_type="vultr_compute_plan",
        name_fields=("name", "description", "id"),
        response_key="plan",
        metadata_key="plan",
        monitoring_default=UtilAsset.Monitoring.DISABLED,
    ),
    # These declarations are intentionally unsupported.  An endpoint is not
    # guessed, so generic collection/reconciliation helpers fail closed.
    "bandwidth_metric": VultrResourceSpec(
        key="bandwidth_metric",
        endpoint=None,
        collection_key=None,
        model=CoreVultrBandwidthMetric,
        asset_type="vultr_bandwidth_metric",
        provider_type="vultr_bandwidth_metric",
        supported=False,
    ),
    "vfs": VultrResourceSpec(
        key="vfs",
        endpoint=None,
        collection_key=None,
        model=CoreVultrVFS,
        asset_type="vultr_vfs",
        provider_type="vultr_vfs",
        supported=False,
    ),
    "storage_gateway": VultrResourceSpec(
        key="storage_gateway",
        endpoint=None,
        collection_key=None,
        model=CoreVultrStorageGateway,
        asset_type="vultr_storage_gateway",
        provider_type="vultr_storage_gateway",
        supported=False,
    ),
}

RESOURCE_ALIASES = {
    "bare_metals": "bare_metal",
    "vultr_bare_metal": "bare_metal",
    "vultr_bare_metal_server": "bare_metal",
    "block_snapshots": "block_snapshot",
    "vultr_block_snapshot": "block_snapshot",
    "block_storage_snapshot": "block_snapshot",
    "instance_backups": "backup",
    "vultr_backup": "backup",
    "vultr_instance_backup": "backup",
    "vultr_bandwidth_metric": "bandwidth_metric",
    "vultr_vfs": "vfs",
    "vultr_storage_gateway": "storage_gateway",
    "plans": "compute_plan",
    "vultr_compute_plan": "compute_plan",
}

SUPPORTED_RESOURCE_KEYS = tuple(
    key for key, spec in RESOURCE_SPECS.items() if spec.supported
)
UNSUPPORTED_RESOURCE_KEYS = tuple(
    key for key, spec in RESOURCE_SPECS.items() if not spec.supported
)

register_vultr_resource_specs(RESOURCE_SPECS)


__all__ = [
    "CoreVultrBareMetal",
    "CoreVultrBareMetalServer",
    "CoreVultrBackup",
    "CoreVultrBandwidthMetric",
    "CoreVultrBlockSnapshot",
    "CoreVultrBlockStorageSnapshot",
    "CoreVultrComputePlan",
    "CoreVultrInstanceBackup",
    "CoreVultrPlan",
    "CoreVultrStorageGateway",
    "CoreVultrVFS",
    "CoreVultrVultrFileSystem",
    "RESOURCE_ALIASES",
    "RESOURCE_MODELS",
    "RESOURCE_SPECS",
    "SUPPORTED_RESOURCE_KEYS",
    "UNSUPPORTED_RESOURCE_KEYS",
]
