"""Read-only Vultr compute and storage-adjacent monitoring checks."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from apps.console.cloud.vultr.resources_base import (
    VultrAPIError,
    VultrInventoryError,
    VultrReadOnlyClient,
    redact_vultr_metadata,
)
from apps.console.cloud.vultr.resources_compute import RESOURCE_ALIASES, RESOURCE_SPECS


_AVAILABLE = frozenset({"active", "available", "healthy", "ok", "online", "ready", "running", "completed", "complete", "success"})
_DEGRADED = frozenset({"degraded", "error", "failed", "failure", "faulted", "unhealthy", "warning"})
_PENDING = frozenset({"building", "creating", "initializing", "migrating", "pending", "processing", "queued", "rebuilding", "starting", "stopping", "updating"})
_STOPPED = frozenset({"disabled", "inactive", "off", "powered_off", "stopped"})


def _safe_error(status: str) -> tuple[str, dict[str, str]]:
    messages = {
        "error": "Vultr API request failed",
        "not_found": "Vultr resource was not found",
        "invalid_access_token": "Vultr API authorization failed",
        "unsupported": "Vultr resource type is unsupported",
    }
    return status, {"error": messages.get(status, messages["error"]), "errorCode": status}


def _credential_context(credentials: Any) -> tuple[str | None, dict[str, Any]]:
    if isinstance(credentials, Mapping):
        token = credentials.get("access_token") or credentials.get("api_token") or credentials.get("token")
        return token if isinstance(token, str) and token.strip() else None, dict(credentials)
    return credentials if isinstance(credentials, str) and credentials.strip() else None, {}


def _canonical_type(resource_type: Any) -> str:
    value = str(resource_type or "").strip().lower().replace("-", "_").replace(" ", "_")
    return RESOURCE_ALIASES.get(value, value)


def _normalize_status(resource_type: str, resource: Mapping[str, Any]) -> str:
    for field in ("power_status", "status", "state", "server_status", "health"):
        value = resource.get(field)
        if not isinstance(value, str):
            continue
        normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
        if normalized in _AVAILABLE:
            return "available"
        if normalized in _DEGRADED:
            return "degraded"
        if normalized in _PENDING:
            return "pending"
        if normalized in _STOPPED:
            return "stopped"
        return "unknown"
    # A successfully fetched immutable catalog/backup object is available
    # even when Vultr does not expose a lifecycle field for it.
    if resource_type in {"block_snapshot", "backup", "compute_plan"}:
        return "available"
    return "unknown"


def _check_resource(resource_type: str, unique_id: Any, credentials: Any) -> tuple[str, dict[str, Any]]:
    canonical = _canonical_type(resource_type)
    spec = RESOURCE_SPECS.get(canonical)
    if spec is None:
        return _safe_error("unsupported")
    if not spec.supported or not spec.endpoint:
        return _safe_error("unsupported")
    if unique_id in (None, ""):
        return _safe_error("error")
    token, _options = _credential_context(credentials)
    if token is None:
        return _safe_error("invalid_access_token")

    try:
        client = VultrReadOnlyClient(token)
        path = f"{spec.endpoint}/{quote(str(unique_id), safe='')}"
        payload = client.get_json(path)
        resource = payload.get(spec.response_key or canonical)
        # The block snapshot detail endpoint has historically returned the
        # snapshot object at the top level; accept that documented shape only
        # when it is plainly an object with the requested identifier.
        if resource is None and canonical == "block_snapshot":
            resource = payload if payload.get("id") is not None else None
        if not isinstance(resource, Mapping):
            raise VultrInventoryError("Vultr returned an invalid resource response")
        safe_resource = redact_vultr_metadata(dict(resource))
        return _normalize_status(canonical, resource), {spec.metadata_key or canonical: safe_resource}
    except VultrAPIError as error:
        if error.status_code == 404:
            return _safe_error("not_found")
        if error.status_code in (401, 403):
            return _safe_error("invalid_access_token")
        return _safe_error("error")
    except (VultrInventoryError, TypeError, ValueError, KeyError):
        return _safe_error("error")
    except Exception:
        return _safe_error("error")


def check_vultr_compute_resource_status(resource_type: str, unique_id: Any, credentials: Any):
    return _check_resource(resource_type, unique_id, credentials)


def check_vultr_bare_metal_status(unique_id: Any, credentials: Any):
    return _check_resource("bare_metal", unique_id, credentials)


def check_vultr_block_snapshot_status(unique_id: Any, credentials: Any):
    return _check_resource("block_snapshot", unique_id, credentials)


def check_vultr_instance_backup_status(unique_id: Any, credentials: Any):
    return _check_resource("backup", unique_id, credentials)


def check_vultr_backup_status(unique_id: Any, credentials: Any):
    return check_vultr_instance_backup_status(unique_id, credentials)


def check_vultr_compute_plan_status(unique_id: Any, credentials: Any):
    return _check_resource("compute_plan", unique_id, credentials)


def _check_bandwidth(unique_id: Any, credentials: Any, resource_kind: str) -> tuple[str, dict[str, Any]]:
    if unique_id in (None, ""):
        return _safe_error("error")
    token, options = _credential_context(credentials)
    if token is None:
        return _safe_error("invalid_access_token")
    kind = _canonical_type(options.get("resource_kind") or resource_kind)
    endpoint_root = {"instance": "instances", "bare_metal": "bare-metals"}.get(kind)
    if endpoint_root is None:
        return _safe_error("unsupported")
    resource_id = options.get("resource_id") or unique_id
    try:
        path = f"{endpoint_root}/{quote(str(resource_id), safe='')}/bandwidth"
        payload = VultrReadOnlyClient(token).get_json(path)
        metrics = payload.get("bandwidth")
        if not isinstance(metrics, Mapping):
            raise VultrInventoryError("Vultr returned an invalid bandwidth response")
        safe_metrics = redact_vultr_metadata(dict(metrics))
        return ("available" if safe_metrics else "unknown"), {"metrics": safe_metrics}
    except VultrAPIError as error:
        if error.status_code == 404:
            return _safe_error("not_found")
        if error.status_code in (401, 403):
            return _safe_error("invalid_access_token")
        return _safe_error("error")
    except (VultrInventoryError, TypeError, ValueError, KeyError):
        return _safe_error("error")
    except Exception:
        return _safe_error("error")


def check_vultr_bandwidth_metric_status(unique_id: Any, credentials: Any):
    return _check_bandwidth(unique_id, credentials, "instance")


def check_vultr_instance_bandwidth_status(unique_id: Any, credentials: Any):
    return _check_bandwidth(unique_id, credentials, "instance")


def check_vultr_bare_metal_bandwidth_status(unique_id: Any, credentials: Any):
    return _check_bandwidth(unique_id, credentials, "bare_metal")


def check_vultr_vfs_status(unique_id: Any, credentials: Any):
    return _safe_error("unsupported")


def check_vultr_storage_gateway_status(unique_id: Any, credentials: Any):
    return _safe_error("unsupported")


VULTR_COMPUTE_CHECKS = {
    "bare_metal": check_vultr_bare_metal_status,
    "block_snapshot": check_vultr_block_snapshot_status,
    "backup": check_vultr_instance_backup_status,
    "instance_backup": check_vultr_instance_backup_status,
    "compute_plan": check_vultr_compute_plan_status,
    "bandwidth_metric": check_vultr_bandwidth_metric_status,
    "vfs": check_vultr_vfs_status,
    "storage_gateway": check_vultr_storage_gateway_status,
}
for _alias, _canonical in RESOURCE_ALIASES.items():
    if _canonical in VULTR_COMPUTE_CHECKS:
        VULTR_COMPUTE_CHECKS[_alias] = VULTR_COMPUTE_CHECKS[_canonical]
VULTR_STATUS_CHECKS = VULTR_COMPUTE_CHECKS
VULTR_CHECK_REGISTRY = VULTR_COMPUTE_CHECKS
CHECK_REGISTRY = VULTR_COMPUTE_CHECKS


def get_vultr_compute_check_function(resource_type: Any):
    normalized = _canonical_type(resource_type)
    check = VULTR_COMPUTE_CHECKS.get(normalized)
    if not callable(check):
        raise ValueError("Unsupported Vultr asset type")
    return check


get_vultr_check_function = get_vultr_compute_check_function


__all__ = [
    "CHECK_REGISTRY",
    "VULTR_CHECK_REGISTRY",
    "VULTR_COMPUTE_CHECKS",
    "VULTR_STATUS_CHECKS",
    "check_vultr_bare_metal_bandwidth_status",
    "check_vultr_bare_metal_status",
    "check_vultr_bandwidth_metric_status",
    "check_vultr_block_snapshot_status",
    "check_vultr_backup_status",
    "check_vultr_compute_plan_status",
    "check_vultr_compute_resource_status",
    "check_vultr_instance_backup_status",
    "check_vultr_instance_bandwidth_status",
    "check_vultr_storage_gateway_status",
    "check_vultr_vfs_status",
    "get_vultr_check_function",
    "get_vultr_compute_check_function",
]
