"""Read-only Vultr platform status checks.

Only documented control-plane GET paths are used.  Returned metadata is
projected through the same safe inventory filter as the Vultr resource lane;
credentials, kubeconfig, registry login material, and object contents are
never returned to monitoring persistence.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from apps.console.cloud.vultr.resources_platform import (
    _endpoint_allowed,
    _safe_projection,
)
from apps.console.cloud.vultr.resources_base import (
    VULTR_API_BASE,
    VultrAPIError,
    VultrClient,
    VultrInventoryError,
)


def _credential_token(credentials: Any) -> str:
    if isinstance(credentials, str) and credentials.strip():
        return credentials.strip()
    if isinstance(credentials, dict):
        for key in ("access_token", "api_token", "token"):
            value = credentials.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    raise ValueError("Vultr control-plane credentials are not configured")


def _component(value: Any, label: str) -> str:
    if value is None or isinstance(value, bool):
        raise ValueError(f"Vultr {label} is invalid")
    raw = str(value).strip()
    if not raw or len(raw) > 255 or any(character in raw for character in "/?#"):
        raise ValueError(f"Vultr {label} is invalid")
    return quote(raw, safe="")


def _get(endpoint: str, token: str) -> dict[str, Any]:
    endpoint = _endpoint_allowed(endpoint)
    payload = VultrClient(token).get_json(endpoint)
    if not isinstance(payload, dict):
        raise ValueError("Vultr returned an invalid status response")
    return payload


def _status_for_api_error(error: VultrAPIError) -> str:
    if error.status_code == 404:
        return "not_found"
    if error.status_code in (401, 403):
        return "invalid_access_token"
    return "error"


def _resource(payload: dict[str, Any], keys: tuple[str, ...], family: str) -> dict[str, Any]:
    present = [key for key in keys if key in payload]
    if len(present) != 1 or not isinstance(payload[present[0]], dict):
        raise ValueError(f"Vultr returned an invalid {family} status response")
    return payload[present[0]]


def _status(resource: dict[str, Any], family: str, *, default: str | None = None) -> str:
    health = resource.get("health_status", resource.get("health_state", resource.get("health")))
    if isinstance(health, dict):
        health = health.get("status") or health.get("state") or health.get("health")
    if isinstance(health, bool):
        if not health:
            return "degraded"
        health = "healthy"
    if isinstance(health, str) and health.strip():
        normalized_health = health.strip().lower()
        if normalized_health in {"failed", "failure", "error", "unhealthy", "critical", "down", "offline"}:
            return "degraded"

    lifecycle = resource.get("status", resource.get("state", resource.get("phase")))
    if isinstance(lifecycle, dict):
        lifecycle = lifecycle.get("state") or lifecycle.get("status") or lifecycle.get("phase")
    if isinstance(lifecycle, str) and lifecycle.strip():
        normalized = lifecycle.strip().lower()
        if normalized in {"failed", "failure", "error", "unhealthy", "critical", "down", "offline"}:
            return "degraded"
        return normalized
    if isinstance(health, str) and health.strip():
        return health.strip().lower()
    if default is not None:
        return default
    raise ValueError(f"Vultr returned a {family} without a status")


def _safe_metadata(resource: dict[str, Any], family: str, wrapper: str) -> dict[str, Any]:
    return {wrapper: _safe_projection(resource, family)}


def _check_detail(
    endpoint: str,
    response_keys: tuple[str, ...],
    family: str,
    credentials: Any,
    *,
    default: str | None = None,
) -> tuple[str, Any]:
    try:
        token = _credential_token(credentials)
        resource = _resource(_get(endpoint, token), response_keys, family)
        return _status(resource, family, default=default), _safe_metadata(resource, family, response_keys[0])
    except VultrAPIError as error:
        return _status_for_api_error(error), "Vultr status request failed"
    except (KeyError, TypeError, ValueError, VultrInventoryError):
        return "error", f"Vultr returned an invalid {family} status response"


def check_vultr_kubernetes_cluster_status(unique_id: Any, credentials: Any) -> tuple[str, Any]:
    return _check_detail(
        f"kubernetes/clusters/{_component(unique_id, 'cluster identifier')}",
        ("vke_cluster", "cluster"),
        "kubernetes_cluster",
        credentials,
    )


def check_vultr_kubernetes_node_pool_status(unique_id: Any, credentials: Any) -> tuple[str, Any]:
    try:
        if not isinstance(credentials, dict):
            raise ValueError("Vultr Kubernetes node-pool context is incomplete")
        cluster_id = credentials.get("cluster_id")
        pool_id = credentials.get("pool_id", unique_id)
        endpoint = (
            f"kubernetes/clusters/{_component(cluster_id, 'cluster identifier')}"
            f"/node-pools/{_component(pool_id, 'node-pool identifier')}"
        )
        token = _credential_token(credentials)
        resource = _resource(_get(endpoint, token), ("node_pool", "nodepool"), "kubernetes_node_pool")
        nodes = resource.get("nodes")
        if nodes is not None:
            if not isinstance(nodes, list) or not all(isinstance(node, dict) for node in nodes):
                raise ValueError("Vultr returned an invalid Kubernetes node list")
            states = []
            for node in nodes:
                state = node.get("status", node.get("state", node.get("health")))
                if isinstance(state, dict):
                    state = state.get("state") or state.get("status") or state.get("health")
                if state is not None:
                    if not isinstance(state, str) or not state.strip():
                        raise ValueError("Vultr returned an invalid Kubernetes node state")
                    states.append(state.strip().lower())
            if states and any(state in {"failed", "error", "unhealthy", "degraded"} for state in states):
                current_status = "degraded"
            elif states and all(state in {"running", "ready", "healthy", "active"} for state in states):
                current_status = "running"
            elif states:
                current_status = "provisioning"
            else:
                current_status = _status(resource, "kubernetes_node_pool")
        else:
            current_status = _status(resource, "kubernetes_node_pool")
        return current_status, _safe_metadata(resource, "kubernetes_node_pool", "node_pool")
    except VultrAPIError as error:
        return _status_for_api_error(error), "Vultr status request failed"
    except (KeyError, TypeError, ValueError, VultrInventoryError):
        return "error", "Vultr returned an invalid kubernetes_node_pool status response"


def check_vultr_object_storage_status(unique_id: Any, credentials: Any) -> tuple[str, Any]:
    return _check_detail(
        f"object-storage/{_component(unique_id, 'object-storage identifier')}",
        ("object_storage", "objectstorage"),
        "object_storage",
        credentials,
        default="available",
    )


def check_vultr_storage_cluster_status(unique_id: Any, credentials: Any) -> tuple[str, Any]:
    return _check_detail(
        f"object-storage/clusters/{_component(unique_id, 'storage-cluster identifier')}",
        ("cluster", "object_storage_cluster"),
        "storage_cluster",
        credentials,
        default="available",
    )


def check_vultr_storage_tier_status(unique_id: Any, credentials: Any) -> tuple[str, Any]:
    return _check_detail(
        f"object-storage/tiers/{_component(unique_id, 'storage-tier identifier')}",
        ("tier", "object_storage_tier"),
        "storage_tier",
        credentials,
        default="available",
    )


def check_vultr_container_registry_status(unique_id: Any, credentials: Any) -> tuple[str, Any]:
    return _check_detail(
        f"registry/{_component(unique_id, 'registry identifier')}",
        ("registry",),
        "container_registry",
        credentials,
        default="available",
    )


def _registry_context(credentials: Any, unique_id: Any, kind: str) -> tuple[str, str, str]:
    if not isinstance(credentials, dict):
        raise ValueError(f"Vultr {kind} context is incomplete")
    registry_id = credentials.get("registry_id")
    local_id = credentials.get(f"{kind}_id", unique_id)
    token = _credential_token(credentials)
    return token, _component(registry_id, "registry identifier"), _component(local_id, f"{kind} identifier")


def check_vultr_registry_repository_status(unique_id: Any, credentials: Any) -> tuple[str, Any]:
    try:
        token, registry_id, repository_id = _registry_context(credentials, unique_id, "repository")
        payload = _get(f"registry/{registry_id}/repositories/{repository_id}", token)
        repository = _resource(payload, ("repository",), "registry_repository")
        return _status(repository, "registry_repository", default="available"), _safe_metadata(
            repository, "registry_repository", "repository"
        )
    except VultrAPIError as error:
        return _status_for_api_error(error), "Vultr status request failed"
    except (KeyError, TypeError, ValueError, VultrInventoryError):
        return "error", "Vultr returned an invalid registry_repository status response"


def check_vultr_registry_artifact_status(unique_id: Any, credentials: Any) -> tuple[str, Any]:
    try:
        token, registry_id, repository_id = _registry_context(credentials, unique_id, "artifact")
        # Artifacts may use a separate local ID in the credential context; the
        # scoped asset ID remains the safe fallback when it is unavailable.
        artifact_id = credentials.get("artifact_id", unique_id)
        endpoint = (
            f"registry/{registry_id}/repositories/{repository_id}/artifacts/"
            f"{_component(artifact_id, 'artifact identifier')}"
        )
        payload = _get(endpoint, token)
        artifact = _resource(payload, ("artifact",), "registry_artifact")
        return _status(artifact, "registry_artifact", default="available"), _safe_metadata(
            artifact, "registry_artifact", "artifact"
        )
    except VultrAPIError as error:
        return _status_for_api_error(error), "Vultr status request failed"
    except (KeyError, TypeError, ValueError, VultrInventoryError):
        return "error", "Vultr returned an invalid registry_artifact status response"


def check_vultr_inference_status(unique_id: Any, credentials: Any) -> tuple[str, Any]:
    return _check_detail(
        f"inference/{_component(unique_id, 'inference identifier')}",
        ("inference", "inference_endpoint"),
        "inference",
        credentials,
    )


def check_vultr_inference_health_status(unique_id: Any, credentials: Any) -> tuple[str, Any]:
    return _check_detail(
        f"inference/{_component(unique_id, 'inference identifier')}/health",
        ("health", "inference_health"),
        "inference",
        credentials,
    )


def check_vultr_plan_status(unique_id: Any, credentials: Any) -> tuple[str, Any]:
    return _check_detail(
        f"plans/{_component(unique_id, 'plan identifier')}",
        ("plan",),
        "plan",
        credentials,
        default="available",
    )


def check_vultr_region_status(unique_id: Any, credentials: Any) -> tuple[str, Any]:
    return _check_detail(
        f"regions/{_component(unique_id, 'region identifier')}",
        ("region",),
        "region",
        credentials,
        default="available",
    )


VULTR_RESOURCE_CHECKS = {
    "kubernetes_cluster": check_vultr_kubernetes_cluster_status,
    "kubernetes_node_pool": check_vultr_kubernetes_node_pool_status,
    "object_storage": check_vultr_object_storage_status,
    "storage_cluster": check_vultr_storage_cluster_status,
    "storage_tier": check_vultr_storage_tier_status,
    "container_registry": check_vultr_container_registry_status,
    "registry_repository": check_vultr_registry_repository_status,
    "registry_artifact": check_vultr_registry_artifact_status,
    "inference": check_vultr_inference_status,
    "inference_health": check_vultr_inference_health_status,
    "plan": check_vultr_plan_status,
    "region": check_vultr_region_status,
}

_CHECK_ALIASES = {
    "cluster": "kubernetes_cluster",
    "node_pool": "kubernetes_node_pool",
    "object_storage_cluster": "storage_cluster",
    "object_storage_tier": "storage_tier",
    "registry": "container_registry",
    "registry_repository": "registry_repository",
    "registry_artifact": "registry_artifact",
    "inference_endpoint": "inference",
    "inference_health": "inference_health",
    "vultr_inference": "inference",
}

for _alias, _canonical in tuple(_CHECK_ALIASES.items()):
    VULTR_RESOURCE_CHECKS[_alias] = VULTR_RESOURCE_CHECKS[_canonical]

VULTR_STATUS_CHECKS = VULTR_RESOURCE_CHECKS
VULTR_CHECK_REGISTRY = VULTR_RESOURCE_CHECKS
CHECK_REGISTRY = VULTR_RESOURCE_CHECKS
RESOURCE_CHECKS = VULTR_RESOURCE_CHECKS


def get_vultr_check_function(resource_type: str):
    if not isinstance(resource_type, str):
        return None
    key = resource_type.strip().lower().replace("-", "_").removeprefix("vultr_")
    key = _CHECK_ALIASES.get(key, key)
    return VULTR_RESOURCE_CHECKS.get(key)


get_check_function = get_vultr_check_function


__all__ = [
    "VULTR_RESOURCE_CHECKS",
    "VULTR_STATUS_CHECKS",
    "VULTR_CHECK_REGISTRY",
    "CHECK_REGISTRY",
    "RESOURCE_CHECKS",
    "get_vultr_check_function",
    "get_check_function",
    "check_vultr_kubernetes_cluster_status",
    "check_vultr_kubernetes_node_pool_status",
    "check_vultr_object_storage_status",
    "check_vultr_storage_cluster_status",
    "check_vultr_storage_tier_status",
    "check_vultr_container_registry_status",
    "check_vultr_registry_repository_status",
    "check_vultr_registry_artifact_status",
    "check_vultr_inference_status",
    "check_vultr_inference_health_status",
    "check_vultr_plan_status",
    "check_vultr_region_status",
]
