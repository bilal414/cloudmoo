import importlib
import logging

from urllib.parse import quote

from apps.console.cloud.vultr.resources_base import (
    VultrAPIError,
    VultrInventoryError,
    VultrReadOnlyClient,
)
from apps.monitoring.metadata import redact_sensitive_metadata


logger = logging.getLogger(__name__)

_VULTR_FAMILY_CHECK_MODULES = (
    "apps.monitoring.checks.vultr_compute",
    "apps.monitoring.checks.vultr_data_network",
    "apps.monitoring.checks.vultr_platform",
    "apps.monitoring.checks.vultr_account_operations",
)


def _access_token(credentials):
    if isinstance(credentials, dict):
        return (
            credentials.get("access_token")
            or credentials.get("api_token")
            or credentials.get("token")
        )
    return credentials


def _safe_request(endpoint, credentials):
    token = _access_token(credentials)
    if not token:
        return None, ("invalid_access_token", {"error": "Vultr credentials are not configured"})
    try:
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise VultrInventoryError("Vultr endpoint is invalid")
        return VultrReadOnlyClient(str(token).strip()).get_json(endpoint), None
    except VultrAPIError as error:
        if error.status_code == 404:
            status = "not_found"
        elif error.status_code in (401, 403):
            status = "invalid_access_token"
        else:
            status = "error"
        return None, (status, {"error": "Vultr request failed"})
    except (VultrInventoryError, TypeError, ValueError):
        return None, ("error", {"error": "Vultr returned an invalid response"})


def check_vultr_server_status(unique_id, access_token):
    """Check Vultr server status"""
    data, error = _safe_request(f"instances/{quote(str(unique_id), safe='')}", access_token)
    if error:
        return error
    instance = data.get("instance")
    if not isinstance(instance, dict):
        return "error", {"error": "Vultr returned an invalid instance response"}
    status = instance.get("power_status") or instance.get("server_status") or instance.get("status")
    if not status:
        return "error", {"error": "Vultr instance status is unavailable"}
    return status, redact_sensitive_metadata({"instance": instance})


def check_vultr_volume_status(unique_id, access_token):
    """Check Vultr volume status"""
    data, error = _safe_request(f"blocks/{quote(str(unique_id), safe='')}", access_token)
    if error:
        return error
    block = data.get("block")
    if not isinstance(block, dict):
        return "error", {"error": "Vultr returned an invalid block response"}
    status = block.get("status") or block.get("state")
    if not status:
        return "error", {"error": "Vultr block status is unavailable"}
    return status, redact_sensitive_metadata({"block": block})


def _family_check_registry(module):
    for registry_name in (
        "RESOURCE_CHECKS",
        "VULTR_RESOURCE_CHECKS",
        "CHECK_REGISTRY",
        "VULTR_CHECKS",
    ):
        registry = getattr(module, registry_name, None)
        if isinstance(registry, dict):
            return registry
    return {}


def get_vultr_check_function(resource_type):
    """Resolve a read-only Vultr status check across service families."""
    normalized = str(resource_type or "").strip().lower()
    legacy = {
        "server": check_vultr_server_status,
        "volume": check_vultr_volume_status,
    }
    if normalized in legacy:
        return legacy[normalized]

    for module_name in _VULTR_FAMILY_CHECK_MODULES:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            logger.warning("Vultr check module is unavailable: %s", module_name)
            continue
        resolver = getattr(module, "get_vultr_check_function", None)
        if callable(resolver):
            try:
                check = resolver(normalized)
            except (KeyError, ValueError):
                check = None
            if callable(check):
                return check
        check = _family_check_registry(module).get(normalized)
        if callable(check):
            return check
        generated = getattr(module, f"check_vultr_{normalized}_status", None)
        if callable(generated):
            return generated
    return None


VULTR_RESOURCE_CHECKS = {
    "server": check_vultr_server_status,
    "volume": check_vultr_volume_status,
}
