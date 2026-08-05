"""Read-only inventory for Vultr platform and data-plane metadata.

The Vultr account adapter in :mod:`apps.console.cloud.vultr.models` predates
the provider resource framework.  This module is intentionally an inventory
lane for the platform families that have a documented Vultr control-plane
surface.  It does not use S3, Docker, Kubernetes, or inference data-plane
credentials, and it never requests kubeconfig or object contents.

All network access goes through ``VultrClient`` and is limited to the fixed
GET endpoint allowlist below.  A missing collection, an ambiguous response,
or an invalid nested object raises ``CloudInventoryTransientError``; callers
must not reconcile a family from a partial response.
"""

from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any
from urllib.parse import quote, urlsplit

from django.db import models

from apps.console.cloud.models import CloudInventoryTransientError
from apps.console.utils.models import UtilAsset
from apps.monitoring.metadata import redact_sensitive_metadata

from .models import CoreVultrAccount
from .resources_base import (
    CoreVultrResource,
    VultrClient,
    VultrResourceSpec,
    reconcile_collection,
)


VULTR_API_BASE = "https://api.vultr.com/v2"
VULTR_DEFAULT_PAGE_SIZE = 100
VULTR_MAX_PAGES = 100

# These are the only collection roots this module may request.  In
# particular, there is no bucket/object, registry credential, Docker config,
# kubeconfig, or arbitrary URL endpoint here.
VULTR_GET_ENDPOINTS = frozenset(
    {
        "kubernetes/clusters",
        "object-storage",
        "object-storage/clusters",
        "object-storage/tiers",
        "registry",
        "inference",
        "plans",
        "regions",
    }
)

# Detail and nested collection paths are represented as templates in this
# separate allowlist.  Values are quoted path components before matching, so
# an identifier cannot add a path, query string, or fragment.
VULTR_GET_ENDPOINT_TEMPLATES = frozenset(
    {
        "kubernetes/clusters/{id}",
        "kubernetes/clusters/{id}/node-pools",
        "kubernetes/clusters/{id}/node-pools/{id}",
        "object-storage/{id}",
        "object-storage/clusters/{id}",
        "object-storage/tiers/{id}",
        "registry/{id}",
        "registry/{id}/repositories",
        "registry/{id}/repositories/{id}",
        "registry/{id}/repositories/{id}/artifacts",
        "registry/{id}/repositories/{id}/artifacts/{id}",
        "inference/{id}",
        "inference/{id}/health",
        "plans/{id}",
        "regions/{id}",
    }
)


def _constraint(name: str) -> models.UniqueConstraint:
    return models.UniqueConstraint(
        fields=("owner", "unique_id"),
        name=f"vultr_{name}_owner_uid_uniq",
    )


class CoreVultrKubernetesCluster(CoreVultrResource):
    provider_type = "vultr_kubernetes_cluster"
    asset_type = UtilAsset.Type.KUBERNETES_CLUSTER
    api_endpoint = "kubernetes/clusters"

    class Meta:
        db_table = "core_vultr_kubernetes_cluster"
        constraints = [_constraint("kubernetes_cluster")]


class CoreVultrKubernetesNodePool(CoreVultrResource):
    provider_type = "vultr_kubernetes_node_pool"
    asset_type = UtilAsset.Type.KUBERNETES_NODE_POOL
    api_endpoint = "kubernetes/clusters/{cluster_id}/node-pools"

    @property
    def monitoring_credentials(self) -> dict[str, Any]:
        credentials = dict(super().monitoring_credentials)
        metadata = self.metadata if isinstance(self.metadata, dict) else {}
        cluster_id = metadata.get("_cloudmoo_cluster_id") or metadata.get("cluster_id")
        if isinstance(cluster_id, str) and cluster_id.strip():
            credentials["cluster_id"] = cluster_id.strip()
        credentials["pool_id"] = (
            metadata.get("_cloudmoo_pool_id")
            or metadata.get("pool_id")
            or self.unique_id
        )
        return credentials

    class Meta:
        db_table = "core_vultr_kubernetes_node_pool"
        constraints = [_constraint("kubernetes_node_pool")]


class CoreVultrObjectStorage(CoreVultrResource):
    provider_type = "vultr_object_storage"
    asset_type = UtilAsset.Type.OBJECT_STORAGE
    api_endpoint = "object-storage"

    class Meta:
        db_table = "core_vultr_object_storage"
        constraints = [_constraint("object_storage")]


class CoreVultrStorageCluster(CoreVultrResource):
    provider_type = "vultr_storage_cluster"
    asset_type = "vultr_storage_cluster"
    api_endpoint = "object-storage/clusters"

    class Meta:
        db_table = "core_vultr_storage_cluster"
        constraints = [_constraint("storage_cluster")]


class CoreVultrStorageTier(CoreVultrResource):
    provider_type = "vultr_storage_tier"
    asset_type = "vultr_storage_tier"
    api_endpoint = "object-storage/tiers"

    class Meta:
        db_table = "core_vultr_storage_tier"
        constraints = [_constraint("storage_tier")]


class CoreVultrContainerRegistry(CoreVultrResource):
    provider_type = "vultr_container_registry"
    asset_type = UtilAsset.Type.CONTAINER_REGISTRY
    api_endpoint = "registry"

    class Meta:
        db_table = "core_vultr_container_registry"
        constraints = [_constraint("container_registry")]


class CoreVultrRegistryRepository(CoreVultrResource):
    provider_type = "vultr_registry_repository"
    asset_type = "vultr_registry_repository"
    api_endpoint = "registry/{registry_id}/repositories"

    @property
    def monitoring_credentials(self) -> dict[str, Any]:
        credentials = dict(super().monitoring_credentials)
        metadata = self.metadata if isinstance(self.metadata, dict) else {}
        registry_id = metadata.get("_cloudmoo_registry_id") or metadata.get("registry_id")
        if isinstance(registry_id, str) and registry_id.strip():
            credentials["registry_id"] = registry_id.strip()
        credentials["repository_id"] = (
            metadata.get("_cloudmoo_repository_id")
            or metadata.get("repository_id")
            or self.unique_id
        )
        return credentials

    class Meta:
        db_table = "core_vultr_registry_repository"
        constraints = [_constraint("registry_repository")]


class CoreVultrRegistryArtifact(CoreVultrResource):
    provider_type = "vultr_registry_artifact"
    asset_type = "vultr_registry_artifact"
    api_endpoint = "registry/{registry_id}/repositories/{repository_id}/artifacts"

    @property
    def monitoring_credentials(self) -> dict[str, Any]:
        credentials = dict(super().monitoring_credentials)
        metadata = self.metadata if isinstance(self.metadata, dict) else {}
        for key in ("registry_id", "repository_id"):
            value = metadata.get(f"_cloudmoo_{key}") or metadata.get(key)
            if isinstance(value, str) and value.strip():
                credentials[key] = value.strip()
        credentials["artifact_id"] = (
            metadata.get("_cloudmoo_artifact_id")
            or metadata.get("artifact_id")
            or self.unique_id
        )
        return credentials

    class Meta:
        db_table = "core_vultr_registry_artifact"
        constraints = [_constraint("registry_artifact")]


class CoreVultrInferenceEndpoint(CoreVultrResource):
    provider_type = "vultr_inference"
    asset_type = "vultr_inference"
    api_endpoint = "inference"

    class Meta:
        db_table = "core_vultr_inference"
        constraints = [_constraint("inference")]


class CoreVultrPlan(CoreVultrResource):
    provider_type = "vultr_plan"
    asset_type = "vultr_plan"
    api_endpoint = "plans"

    class Meta:
        db_table = "core_vultr_plan"
        constraints = [_constraint("plan")]


class CoreVultrRegion(CoreVultrResource):
    provider_type = "vultr_region"
    asset_type = "vultr_region"
    api_endpoint = "regions"

    class Meta:
        db_table = "core_vultr_region"
        constraints = [_constraint("region")]


# Friendly aliases used by integrations that use the product/API spelling.
CoreVultrCluster = CoreVultrKubernetesCluster
CoreVultrNodePool = CoreVultrKubernetesNodePool
CoreVultrInference = CoreVultrInferenceEndpoint


RESOURCE_MODELS = {
    "kubernetes_cluster": CoreVultrKubernetesCluster,
    "kubernetes_node_pool": CoreVultrKubernetesNodePool,
    "object_storage": CoreVultrObjectStorage,
    "storage_cluster": CoreVultrStorageCluster,
    "storage_tier": CoreVultrStorageTier,
    "container_registry": CoreVultrContainerRegistry,
    "registry_repository": CoreVultrRegistryRepository,
    "registry_artifact": CoreVultrRegistryArtifact,
    "inference": CoreVultrInferenceEndpoint,
    "plan": CoreVultrPlan,
    "region": CoreVultrRegion,
}


RESOURCE_SPECS = {
    "kubernetes_cluster": VultrResourceSpec(
        "kubernetes_cluster",
        "kubernetes/clusters",
        "vke_clusters",
        CoreVultrKubernetesCluster,
        name_fields=("label", "name", "hostname", "domain", "ip"),
    ),
    "kubernetes_node_pool": VultrResourceSpec(
        "kubernetes_node_pool",
        "kubernetes/clusters/{cluster_id}/node-pools",
        "node_pools",
        CoreVultrKubernetesNodePool,
        identifier_fields=("id", "pool_id"),
        name_fields=("label", "name", "hostname", "domain", "ip"),
    ),
    "object_storage": VultrResourceSpec(
        "object_storage",
        "object-storage",
        "object_storages",
        CoreVultrObjectStorage,
        name_fields=("label", "name", "hostname", "domain", "ip"),
    ),
    "storage_cluster": VultrResourceSpec(
        "storage_cluster",
        "object-storage/clusters",
        "clusters",
        CoreVultrStorageCluster,
        identifier_fields=("id", "cluster_id"),
        name_fields=("label", "name", "hostname", "domain", "ip"),
    ),
    "storage_tier": VultrResourceSpec(
        "storage_tier",
        "object-storage/tiers",
        "tiers",
        CoreVultrStorageTier,
        identifier_fields=("id", "tier_id"),
        name_fields=("label", "name", "hostname", "domain", "ip"),
    ),
    "container_registry": VultrResourceSpec(
        "container_registry",
        "registry",
        "registries",
        CoreVultrContainerRegistry,
        name_fields=("label", "name", "hostname", "domain", "ip"),
    ),
    "registry_repository": VultrResourceSpec(
        "registry_repository",
        "registry/{registry_id}/repositories",
        "repositories",
        CoreVultrRegistryRepository,
        identifier_fields=("id", "name", "repository_name"),
        name_fields=("name", "label", "hostname", "domain", "ip"),
    ),
    "registry_artifact": VultrResourceSpec(
        "registry_artifact",
        "registry/{registry_id}/repositories/{repository_id}/artifacts",
        "artifacts",
        CoreVultrRegistryArtifact,
        identifier_fields=("id", "digest", "tag", "name"),
        name_fields=("tag", "name", "digest", "label", "hostname", "domain", "ip"),
    ),
    "inference": VultrResourceSpec(
        "inference",
        "inference",
        "inference_endpoints",
        CoreVultrInferenceEndpoint,
        name_fields=("label", "name", "hostname", "domain", "ip"),
    ),
    "plan": VultrResourceSpec(
        "plan",
        "plans",
        "plans",
        CoreVultrPlan,
        identifier_fields=("id", "plan_id", "slug"),
        name_fields=("label", "name", "slug", "hostname", "domain", "ip"),
    ),
    "region": VultrResourceSpec(
        "region",
        "regions",
        "regions",
        CoreVultrRegion,
        identifier_fields=("id", "region", "slug"),
        name_fields=("label", "name", "city", "slug", "hostname", "domain", "ip"),
    ),
}

# Provider-qualified aliases make discovery by the generic integration layer
# straightforward without duplicating model/spec entries.
VULTR_RESOURCE_MODELS = RESOURCE_MODELS
VULTR_RESOURCE_SPECS = RESOURCE_SPECS

_RESOURCE_ALIASES = {
    "cluster": "kubernetes_cluster",
    "clusters": "kubernetes_cluster",
    "kubernetes": "kubernetes_cluster",
    "node_pool": "kubernetes_node_pool",
    "node_pools": "kubernetes_node_pool",
    "object_storages": "object_storage",
    "object_storage_cluster": "storage_cluster",
    "object_storage_clusters": "storage_cluster",
    "object_storage_tier": "storage_tier",
    "object_storage_tiers": "storage_tier",
    "registry": "container_registry",
    "registries": "container_registry",
    "repositories": "registry_repository",
    "registry_repositories": "registry_repository",
    "artifacts": "registry_artifact",
    "registry_artifacts": "registry_artifact",
    "inference_endpoint": "inference",
    "inference_endpoints": "inference",
    "vultr_inference": "inference",
    "plans": "plan",
    "regions": "region",
}


_COMMON_SAFE_KEYS = {
    "id",
    "uuid",
    "label",
    "name",
    "slug",
    "hostname",
    "domain",
    "ip",
    "endpoint",
    "public_endpoint",
    "url",
    "status",
    "state",
    "phase",
    "health",
    "health_status",
    "health_state",
    "healthy",
    "ready",
    "region",
    "region_id",
    "city",
    "country",
    "date_created",
    "date_updated",
    "created_at",
    "updated_at",
    "version",
    "k8s_version",
    "kubernetes_version",
    "cluster_subnet",
    "service_subnet",
    "plan",
    "plan_id",
    "plan_name",
    "plan_type",
    "tier",
    "tier_id",
    "size",
    "size_gb",
    "size_bytes",
    "cpu",
    "cpus",
    "ram",
    "memory",
    "storage",
    "node_quantity",
    "node_count",
    "count",
    "min_nodes",
    "max_nodes",
    "auto_scale",
    "autoscaler",
    "auto_scaler",
    "monthly_cost",
    "hourly_cost",
    "price",
    "locations",
    "tags",
    "repository_count",
    "artifact_count",
    "pull_count",
    "pulls",
    "total_pulls",
    "last_pulled",
    "last_pushed",
    "latest_pull",
    "latest_push",
    "digest",
    "tag",
    "image",
    "image_name",
    "manifest",
    "architecture",
    "os",
    "model",
    "model_name",
    "port",
    "path",
    "sku",
    "family",
    "type",
    "registry_id",
    "repository_id",
    "pool_id",
    "cluster_id",
    "node_pools",
    "nodes",
}

_FAMILY_SAFE_KEYS = {
    "kubernetes_cluster": _COMMON_SAFE_KEYS - {"nodes"},
    "kubernetes_node_pool": _COMMON_SAFE_KEYS,
    "object_storage": _COMMON_SAFE_KEYS - {"node_pools", "nodes"},
    "storage_cluster": _COMMON_SAFE_KEYS - {"node_pools", "nodes"},
    "storage_tier": _COMMON_SAFE_KEYS - {"node_pools", "nodes"},
    "container_registry": _COMMON_SAFE_KEYS - {"node_pools", "nodes"},
    "registry_repository": _COMMON_SAFE_KEYS - {"node_pools", "nodes"},
    "registry_artifact": _COMMON_SAFE_KEYS - {"node_pools", "nodes"},
    "inference": _COMMON_SAFE_KEYS - {"node_pools", "nodes"},
    "plan": _COMMON_SAFE_KEYS - {"node_pools", "nodes"},
    "region": _COMMON_SAFE_KEYS - {"node_pools", "nodes"},
}

_SENSITIVE_KEY_PARTS = (
    "password",
    "secret",
    "token",
    "accesskey",
    "privatekey",
    "credential",
    "authorization",
    "apikey",
    "kubeconfig",
    "dockerconfig",
)
_DROP = object()
_SECRET_VALUE_PATTERN = re.compile(
    r"(?i)(?:bearer\s+|(?:password|secret|token|access[_-]?key|private[_-]?key|credential|authorization|api[_-]?key)\s*[:=])"
)


def _normalized_key(value: Any) -> str:
    return "".join(character for character in str(value).lower() if character.isalnum())


def _is_sensitive_key(value: Any) -> bool:
    normalized = _normalized_key(value)
    return (
        normalized in {"variables", "environment", "config", "kube", "kubeconfig"}
        or any(part in normalized for part in _SENSITIVE_KEY_PARTS)
    )


def _safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if _SECRET_VALUE_PATTERN.search(value):
            return _DROP
        if len(value) > 4096:
            return value[:4096]
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        allowed_keys = {_normalized_key(key) for key in _COMMON_SAFE_KEYS}
        for key, child in value.items():
            if _is_sensitive_key(key) or _normalized_key(key) not in allowed_keys:
                continue
            safe_child = _safe_value(child)
            if safe_child is not _DROP:
                result[str(key)] = safe_child
        return result
    if isinstance(value, list):
        if len(value) > 100:
            value = value[:100]
        result = []
        for child in value:
            safe_child = _safe_value(child)
            if safe_child is not _DROP:
                result.append(safe_child)
        return result
    return _DROP


def _safe_projection(item: Mapping[str, Any], family: str) -> dict[str, Any]:
    allowed = _FAMILY_SAFE_KEYS[family]
    allowed_keys = {_normalized_key(key) for key in allowed}
    result: dict[str, Any] = {}
    for key, value in item.items():
        normalized = _normalized_key(key)
        if normalized in {"kubeconfig", "kubeconfigdata", "credentials", "registrycredentials"}:
            continue
        if _is_sensitive_key(key) or normalized not in allowed_keys:
            continue
        safe_value = _safe_value(value)
        if safe_value is _DROP:
            continue
        if normalized in {"endpoint", "publicendpoint", "url"} and isinstance(safe_value, str):
            parsed = urlsplit(safe_value)
            if parsed.query or parsed.fragment or parsed.username or parsed.password:
                continue
        result[str(key)] = safe_value
    projected = redact_sensitive_metadata(result)
    if not isinstance(projected, dict):  # pragma: no cover - defensive
        raise CloudInventoryTransientError("Vultr returned invalid safe metadata")
    return projected


def _identifier(item: Mapping[str, Any], fields: Sequence[str], family: str) -> str:
    for field in fields:
        value = item.get(field)
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, (str, int)) and str(value).strip():
            identifier = str(value).strip()
            if len(identifier) > 255:
                raise CloudInventoryTransientError(
                    f"Vultr returned an oversized {family} identifier"
                )
            return identifier
    raise CloudInventoryTransientError(f"Vultr returned a {family} without an identifier")


def _display_name(item: Mapping[str, Any], identifier: str, fields: Sequence[str]) -> str:
    for field in fields:
        value = item.get(field)
        if value is not None and not isinstance(value, bool) and str(value).strip():
            return str(value).strip()[:100]
    return identifier[:100]


def _scoped_identifier(*parts: Any) -> str:
    values = [str(part).strip() for part in parts]
    if any(not value or "/" in value or "?" in value or "#" in value for value in values):
        raise CloudInventoryTransientError("Vultr returned an invalid nested resource identifier")
    result = ":".join(values)
    if len(result) <= 100:
        return result
    digest = hashlib.sha256(result.encode("utf-8")).hexdigest()[:32]
    return f"vultr-{digest}"


def _record(
    item: Mapping[str, Any],
    spec: VultrResourceSpec,
    family: str,
    *,
    identifier: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise CloudInventoryTransientError(f"Vultr returned an invalid {family} object")
    unique_id = identifier or _identifier(item, spec.identifier_fields, family)
    metadata = _safe_projection(item, family)
    if extra:
        for key, value in extra.items():
            if _is_sensitive_key(key):
                continue
            if not isinstance(value, (str, int, float, bool)) and value is not None:
                raise CloudInventoryTransientError(f"Vultr returned invalid {family} context")
            metadata[key] = value
    metadata.update(
        {
            "_cloudmoo_provider_type": spec.model.provider_type,
            "_cloudmoo_asset_type": spec.model.asset_type,
            "_cloudmoo_endpoint": spec.endpoint,
            "_cloudmoo_raw_id": unique_id,
        }
    )
    return {
        "unique_id": unique_id,
        "name": _display_name(item, unique_id, spec.name_fields),
        "metadata": metadata,
        "raw": copy.deepcopy(metadata),
    }


def _endpoint_component(value: Any, label: str) -> str:
    if value is None or isinstance(value, bool):
        raise CloudInventoryTransientError(f"Vultr {label} is invalid")
    raw = str(value).strip()
    if not raw or len(raw) > 255 or any(character in raw for character in "/?#"):
        raise CloudInventoryTransientError(f"Vultr {label} is invalid")
    return quote(raw, safe="")


def _endpoint_allowed(endpoint: str) -> str:
    if not isinstance(endpoint, str):
        raise CloudInventoryTransientError("Vultr inventory endpoint is invalid")
    normalized = endpoint.strip().strip("/")
    if (
        not normalized
        or "?" in normalized
        or "#" in normalized
        or any(part in {".", ".."} for part in normalized.split("/"))
    ):
        raise CloudInventoryTransientError("Vultr inventory endpoint is invalid")
    if normalized in VULTR_GET_ENDPOINTS:
        return normalized

    parts = normalized.split("/")
    for template in VULTR_GET_ENDPOINT_TEMPLATES:
        template_parts = template.split("/")
        if len(parts) != len(template_parts):
            continue
        if all(
            expected == actual or expected == "{id}"
            for expected, actual in zip(template_parts, parts)
        ):
            return normalized
    raise CloudInventoryTransientError("Vultr inventory endpoint is not allowlisted")


def _payload_from_response(response: Any) -> dict[str, Any]:
    if isinstance(response, Mapping):
        return dict(response)
    raise CloudInventoryTransientError("Vultr returned an invalid inventory response")


def _client_get(client: Any, endpoint: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    endpoint = _endpoint_allowed(endpoint)
    getter = getattr(client, "get", None)
    try:
        if callable(getter):
            try:
                response = getter(endpoint, params=dict(params or {}))
            except TypeError:
                response = getter(endpoint, dict(params or {}))
        else:
            get_json = getattr(client, "get_json", None)
            if callable(get_json):
                response = get_json(endpoint, params=dict(params or {}))
            else:
                request = getattr(client, "request", None)
                if not callable(request):
                    raise CloudInventoryTransientError("Vultr GET client is unavailable")
                response = request("GET", endpoint, params=dict(params or {}))

        # The base client normally returns decoded JSON.  Supporting a
        # response-like test double keeps the GET-only boundary explicit.
        if hasattr(response, "raise_for_status"):
            response.raise_for_status()
        if hasattr(response, "json") and callable(response.json):
            response = response.json()
        return _payload_from_response(response)
    except CloudInventoryTransientError:
        raise
    except Exception as error:
        # Do not include provider/request exception text: it may contain an
        # Authorization header, a signed URL, or another secret.
        raise CloudInventoryTransientError("Vultr inventory request failed") from error


def _collection_keys(spec: VultrResourceSpec) -> tuple[str, ...]:
    aliases = {
        "storage_cluster": ("object_storage_clusters", "storage_clusters"),
        "storage_tier": ("object_storage_tiers", "storage_tiers"),
        "inference": ("inferences",),
    }
    return (spec.collection_key, *aliases.get(spec.key, ()))


def _extract_collection(
    payload: Mapping[str, Any],
    spec: VultrResourceSpec,
    family: str,
) -> list[dict[str, Any]]:
    present = [key for key in _collection_keys(spec) if key in payload]
    if len(present) != 1 or not isinstance(payload[present[0]], list):
        raise CloudInventoryTransientError(
            f"Vultr returned an invalid {family} collection"
        )
    items = payload[present[0]]
    if not all(isinstance(item, dict) for item in items):
        raise CloudInventoryTransientError(f"Vultr returned an invalid {family} object")
    return items


def _next_cursor(
    payload: Mapping[str, Any],
    *,
    item_count: int,
    fetched_count: int,
) -> str | None:
    meta = payload.get("meta")
    if meta is None:
        # Small responses from older catalog endpoints can omit pagination.
        # A full page without pagination metadata is not safe to treat as a
        # complete inventory.
        if item_count >= VULTR_DEFAULT_PAGE_SIZE:
            raise CloudInventoryTransientError("Vultr returned incomplete pagination metadata")
        return None
    if not isinstance(meta, dict):
        raise CloudInventoryTransientError("Vultr returned invalid pagination metadata")

    links = meta.get("links")
    if links is not None:
        if not isinstance(links, dict):
            raise CloudInventoryTransientError("Vultr returned invalid pagination links")
        cursor = links.get("next")
        if cursor in (None, ""):
            total = meta.get("total")
            if total is not None and (
                isinstance(total, bool)
                or not isinstance(total, int)
                or total != fetched_count
            ):
                raise CloudInventoryTransientError("Vultr returned incomplete pagination metadata")
            return None
        if not isinstance(cursor, str) or not cursor or len(cursor) > 255:
            raise CloudInventoryTransientError("Vultr returned an invalid pagination cursor")
        if any(character in cursor for character in "/?#"):
            raise CloudInventoryTransientError("Vultr returned an invalid pagination cursor")
        return cursor

    total = meta.get("total")
    if total is None:
        if item_count >= VULTR_DEFAULT_PAGE_SIZE:
            raise CloudInventoryTransientError("Vultr returned incomplete pagination metadata")
        return None
    if isinstance(total, bool) or not isinstance(total, int) or total < fetched_count:
        raise CloudInventoryTransientError("Vultr returned invalid pagination totals")
    if total != fetched_count:
        raise CloudInventoryTransientError("Vultr returned incomplete pagination metadata")
    return None


def _list_collection(
    client: Any,
    spec: VultrResourceSpec,
    endpoint: str,
    *,
    family: str,
) -> list[dict[str, Any]]:
    endpoint = _endpoint_allowed(endpoint)
    records: list[dict[str, Any]] = []
    cursor: str | None = None
    visited: set[str | None] = set()

    for _page in range(VULTR_MAX_PAGES):
        if cursor in visited:
            raise CloudInventoryTransientError("Vultr returned an invalid pagination sequence")
        visited.add(cursor)
        params: dict[str, Any] = {"per_page": VULTR_DEFAULT_PAGE_SIZE}
        if cursor:
            params["cursor"] = cursor
        payload = _client_get(client, endpoint, params)
        items = _extract_collection(payload, spec, family)
        records.extend(items)
        next_cursor = _next_cursor(
            payload,
            item_count=len(items),
            fetched_count=len(records),
        )
        if next_cursor is None:
            return records
        cursor = next_cursor

    raise CloudInventoryTransientError("Vultr inventory pagination bound exceeded")


def _normalise_cluster(item: Mapping[str, Any], spec: VultrResourceSpec) -> dict[str, Any]:
    node_pools = item.get("node_pools")
    if node_pools is not None and not isinstance(node_pools, list):
        raise CloudInventoryTransientError("Vultr returned an invalid Kubernetes node-pool summary")
    extra: dict[str, Any] = {}
    if isinstance(node_pools, list):
        if not all(isinstance(pool, dict) for pool in node_pools):
            raise CloudInventoryTransientError("Vultr returned an invalid Kubernetes node-pool summary")
        extra["_cloudmoo_node_pool_count"] = len(node_pools)
    elif isinstance(item.get("node_pool_count"), int) and not isinstance(item.get("node_pool_count"), bool):
        extra["_cloudmoo_node_pool_count"] = item["node_pool_count"]
    return _record(item, spec, "kubernetes_cluster", extra=extra)


def _node_states(item: Mapping[str, Any], family: str) -> list[str]:
    nodes = item.get("nodes")
    if nodes is None:
        return []
    if not isinstance(nodes, list) or not all(isinstance(node, dict) for node in nodes):
        raise CloudInventoryTransientError(f"Vultr returned an invalid {family} node list")
    states: list[str] = []
    for node in nodes:
        state = node.get("status") or node.get("state") or node.get("health")
        if isinstance(state, dict):
            state = state.get("state") or state.get("status") or state.get("health")
        if state is not None:
            if not isinstance(state, str) or not state.strip():
                raise CloudInventoryTransientError(f"Vultr returned an invalid {family} node state")
            states.append(state.strip().lower())
    return states


def _normalise_node_pool(
    item: Mapping[str, Any],
    spec: VultrResourceSpec,
    cluster_id: str,
) -> dict[str, Any]:
    local_id = _identifier(item, spec.identifier_fields, "kubernetes_node_pool")
    states = _node_states(item, "Kubernetes node-pool")
    extra: dict[str, Any] = {
        "_cloudmoo_cluster_id": cluster_id,
        "_cloudmoo_pool_id": local_id,
        "cluster_id": cluster_id,
        "pool_id": local_id,
    }
    if states:
        extra["_cloudmoo_node_states"] = states
    return _record(
        item,
        spec,
        "kubernetes_node_pool",
        identifier=_scoped_identifier(cluster_id, local_id),
        extra=extra,
    )


def _normalise_registry(item: Mapping[str, Any], spec: VultrResourceSpec) -> dict[str, Any]:
    repositories = item.get("repositories")
    if repositories is not None and not isinstance(repositories, list):
        raise CloudInventoryTransientError("Vultr returned an invalid registry summary")
    extra: dict[str, Any] = {}
    if isinstance(repositories, list):
        if not all(isinstance(repository, dict) for repository in repositories):
            raise CloudInventoryTransientError("Vultr returned an invalid registry summary")
        extra["_cloudmoo_repository_count"] = len(repositories)
    return _record(item, spec, "container_registry", extra=extra)


def _normalise_repository(
    item: Mapping[str, Any],
    spec: VultrResourceSpec,
    registry_id: str,
) -> dict[str, Any]:
    local_id = _identifier(item, spec.identifier_fields, "registry_repository")
    return _record(
        item,
        spec,
        "registry_repository",
        identifier=_scoped_identifier(registry_id, local_id),
        extra={
            "_cloudmoo_registry_id": registry_id,
            "_cloudmoo_repository_id": local_id,
            "registry_id": registry_id,
            "repository_id": local_id,
        },
    )


def _normalise_artifact(
    item: Mapping[str, Any],
    spec: VultrResourceSpec,
    registry_id: str,
    repository_id: str,
) -> dict[str, Any]:
    local_id = _identifier(item, spec.identifier_fields, "registry_artifact")
    return _record(
        item,
        spec,
        "registry_artifact",
        identifier=_scoped_identifier(registry_id, repository_id, local_id),
        extra={
            "_cloudmoo_registry_id": registry_id,
            "_cloudmoo_repository_id": repository_id,
            "_cloudmoo_artifact_id": local_id,
            "registry_id": registry_id,
            "repository_id": repository_id,
        },
    )


def _resolve_spec(resource: str | VultrResourceSpec) -> VultrResourceSpec:
    if isinstance(resource, VultrResourceSpec):
        return resource
    if not isinstance(resource, str):
        raise CloudInventoryTransientError("Vultr resource type is invalid")
    key = resource.strip().lower().replace("-", "_")
    key = key.removeprefix("vultr_")
    key = _RESOURCE_ALIASES.get(key, key)
    spec = RESOURCE_SPECS.get(key)
    if spec is None:
        # Deliberately make unsupported App Platform requests explicit. Vultr
        # has no verified product/API family for that concept.
        raise CloudInventoryTransientError("Vultr resource type is unsupported")
    return spec


def _client_for_account(account: Any) -> VultrClient:
    token = getattr(account, "access_token", None)
    if not isinstance(token, str) or not token.strip():
        raise CloudInventoryTransientError("Vultr inventory credentials are unavailable")
    return VultrClient(token)


def _selected_specs(resources: Iterable[str | VultrResourceSpec] | None) -> list[VultrResourceSpec]:
    selected = (
        list(RESOURCE_SPECS.values())
        if resources is None
        else [_resolve_spec(resource) for resource in resources]
    )
    keys = [spec.key for spec in selected]
    if len(keys) != len(set(keys)):
        raise CloudInventoryTransientError("Vultr inventory contains duplicate resource families")
    return selected


def _assert_unique(records: list[dict[str, Any]], family: str) -> list[dict[str, Any]]:
    seen: set[str] = set()
    for record in records:
        identifier = record.get("unique_id")
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise CloudInventoryTransientError(f"Vultr returned duplicate {family} identifiers")
        seen.add(identifier)
    return records


def _collect_selected(
    client: Any,
    selected: Sequence[VultrResourceSpec],
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    by_key = {spec.key: spec for spec in selected}

    cluster_specs = [
        spec for spec in selected if spec.key in {"kubernetes_cluster", "kubernetes_node_pool"}
    ]
    if cluster_specs:
        cluster_spec = RESOURCE_SPECS["kubernetes_cluster"]
        raw_clusters = _list_collection(
            client,
            cluster_spec,
            cluster_spec.endpoint,
            family="kubernetes_cluster",
        )
        if "kubernetes_cluster" in by_key:
            result["kubernetes_cluster"] = _assert_unique(
                [_normalise_cluster(item, cluster_spec) for item in raw_clusters],
                "kubernetes_cluster",
            )
        if "kubernetes_node_pool" in by_key:
            pool_spec = RESOURCE_SPECS["kubernetes_node_pool"]
            pools: list[dict[str, Any]] = []
            for cluster in raw_clusters:
                cluster_id = _identifier(cluster, cluster_spec.identifier_fields, "kubernetes_cluster")
                endpoint = f"kubernetes/clusters/{_endpoint_component(cluster_id, 'cluster identifier')}/node-pools"
                for item in _list_collection(
                    client,
                    pool_spec,
                    endpoint,
                    family="kubernetes_node_pool",
                ):
                    pools.append(_normalise_node_pool(item, pool_spec, cluster_id))
            result["kubernetes_node_pool"] = _assert_unique(pools, "kubernetes_node_pool")

    registry_specs = [
        spec
        for spec in selected
        if spec.key in {"container_registry", "registry_repository", "registry_artifact"}
    ]
    if registry_specs:
        registry_spec = RESOURCE_SPECS["container_registry"]
        raw_registries = _list_collection(
            client,
            registry_spec,
            registry_spec.endpoint,
            family="container_registry",
        )
        if "container_registry" in by_key:
            result["container_registry"] = _assert_unique(
                [_normalise_registry(item, registry_spec) for item in raw_registries],
                "container_registry",
            )

        repository_spec = RESOURCE_SPECS["registry_repository"]
        artifact_spec = RESOURCE_SPECS["registry_artifact"]
        repositories: list[dict[str, Any]] = []
        artifacts: list[dict[str, Any]] = []
        if "registry_repository" in by_key or "registry_artifact" in by_key:
            for registry in raw_registries:
                registry_id = _identifier(registry, registry_spec.identifier_fields, "container_registry")
                registry_endpoint = (
                    f"registry/{_endpoint_component(registry_id, 'registry identifier')}/repositories"
                )
                raw_repositories = _list_collection(
                    client,
                    repository_spec,
                    registry_endpoint,
                    family="registry_repository",
                )
                if "registry_repository" in by_key:
                    repositories.extend(
                        _normalise_repository(item, repository_spec, registry_id)
                        for item in raw_repositories
                    )
                if "registry_artifact" in by_key:
                    for repository in raw_repositories:
                        repository_id = _identifier(
                            repository,
                            repository_spec.identifier_fields,
                            "registry_repository",
                        )
                        repository_endpoint = (
                            f"registry/{_endpoint_component(registry_id, 'registry identifier')}"
                            f"/repositories/{_endpoint_component(repository_id, 'repository identifier')}"
                            "/artifacts"
                        )
                        for item in _list_collection(
                            client,
                            artifact_spec,
                            repository_endpoint,
                            family="registry_artifact",
                        ):
                            artifacts.append(
                                _normalise_artifact(
                                    item,
                                    artifact_spec,
                                    registry_id,
                                    repository_id,
                                )
                            )
        if "registry_repository" in by_key:
            result["registry_repository"] = _assert_unique(repositories, "registry_repository")
        if "registry_artifact" in by_key:
            result["registry_artifact"] = _assert_unique(artifacts, "registry_artifact")

    handled = {
        "kubernetes_cluster",
        "kubernetes_node_pool",
        "container_registry",
        "registry_repository",
        "registry_artifact",
    }
    for spec in selected:
        if spec.key in handled:
            continue
        raw_items = _list_collection(client, spec, spec.endpoint, family=spec.key)
        records = [_record(item, spec, spec.key) for item in raw_items]
        result[spec.key] = _assert_unique(records, spec.key)

    return {spec.key: result.get(spec.key, []) for spec in selected}


def collect_vultr_resource_records(
    account: Any,
    resource: str | VultrResourceSpec,
    *,
    client: Any | None = None,
) -> list[dict[str, Any]]:
    """Fetch and normalize one complete Vultr resource family.

    Nested families fetch their parent collections first.  This is still
    metadata-only: the implementation never calls a bucket/object, registry
    credential, kubeconfig, or inference data-plane endpoint.
    """

    selected = [_resolve_spec(resource)]
    client = client or _client_for_account(account)
    return _collect_selected(client, selected)[selected[0].key]


# Friendly names for parent adapters and tests that use a generic collect API.
collect_vultr_collection = collect_vultr_resource_records
list_vultr_resource_records = collect_vultr_resource_records


def collect_vultr_inventory(
    account: Any,
    resources: Iterable[str | VultrResourceSpec] | None = None,
    *,
    client: Any | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Fetch all selected families before any reconciliation can occur."""

    selected = _selected_specs(resources)
    client = client or _client_for_account(account)
    return _collect_selected(client, selected)


def sync_vultr_resource(
    account: CoreVultrAccount,
    resource: str | VultrResourceSpec,
    *,
    records: list[dict[str, Any]] | None = None,
    client: Any | None = None,
) -> Any:
    """Reconcile one already-complete, redacted family through the base API."""

    spec = _resolve_spec(resource)
    client = client or _client_for_account(account)
    if records is None:
        records = collect_vultr_resource_records(account, spec, client=client)
    return reconcile_collection(account, spec, records, client)


def sync_vultr_resources(
    account: CoreVultrAccount,
    resources: Iterable[str | VultrResourceSpec] | None = None,
    *,
    client: Any | None = None,
) -> dict[str, Any]:
    """Collect all selected families, then reconcile them one by one."""

    selected = _selected_specs(resources)
    client = client or _client_for_account(account)
    inventory = _collect_selected(client, selected)
    return {
        spec.key: reconcile_collection(account, spec, inventory[spec.key], client)
        for spec in selected
    }


sync_vultr_platform = sync_vultr_resources


__all__ = [
    "VULTR_API_BASE",
    "VULTR_DEFAULT_PAGE_SIZE",
    "VULTR_MAX_PAGES",
    "VULTR_GET_ENDPOINTS",
    "VULTR_GET_ENDPOINT_TEMPLATES",
    "VultrResourceSpec",
    "CoreVultrKubernetesCluster",
    "CoreVultrKubernetesNodePool",
    "CoreVultrObjectStorage",
    "CoreVultrStorageCluster",
    "CoreVultrStorageTier",
    "CoreVultrContainerRegistry",
    "CoreVultrRegistryRepository",
    "CoreVultrRegistryArtifact",
    "CoreVultrInferenceEndpoint",
    "CoreVultrPlan",
    "CoreVultrRegion",
    "CoreVultrCluster",
    "CoreVultrNodePool",
    "CoreVultrInference",
    "RESOURCE_MODELS",
    "RESOURCE_SPECS",
    "VULTR_RESOURCE_MODELS",
    "VULTR_RESOURCE_SPECS",
    "collect_vultr_resource_records",
    "collect_vultr_collection",
    "list_vultr_resource_records",
    "collect_vultr_inventory",
    "sync_vultr_resource",
    "sync_vultr_resources",
    "sync_vultr_platform",
]
