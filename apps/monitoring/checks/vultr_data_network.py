"""Read-only status checks for Vultr data, network, and edge assets.

The functions in this module use only fixed Vultr v2 GET paths.  They accept
the monitoring contract used by the Celery worker: ``(unique_id,
credentials)`` and return ``(normalized_status, safe_metadata_or_error)``.
Provider response bodies are validated before being returned and sensitive
database, certificate, credential, and signed-URL fields are removed.
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import quote

from apps.console.cloud.models import CloudInventoryTransientError
from apps.monitoring.checks.base import REQUEST_TIMEOUT_SECONDS
from apps.console.cloud.vultr.resources_data_network import normalize_vultr_nested_record
from apps.console.cloud.vultr.resources_base import (
    VULTR_API_BASE as SHARED_VULTR_API_BASE,
    VultrAPIError,
    VultrClient,
)


VULTR_API_BASE = SHARED_VULTR_API_BASE
VULTR_REQUEST_TIMEOUT_SECONDS = REQUEST_TIMEOUT_SECONDS


_AVAILABLE_STATUSES = frozenset(
    {
        "active",
        "available",
        "attached",
        "complete",
        "completed",
        "enabled",
        "healthy",
        "issued",
        "ok",
        "online",
        "operational",
        "ready",
        "running",
        "started",
        "succeeded",
        "success",
        "up",
    }
)
_DEGRADED_STATUSES = frozenset(
    {
        "critical",
        "degraded",
        "error",
        "failed",
        "failure",
        "failing",
        "offline",
        "unhealthy",
        "down",
    }
)
_PENDING_STATUSES = frozenset(
    {
        "activating",
        "building",
        "creating",
        "initializing",
        "migrating",
        "pending",
        "processing",
        "provisioning",
        "queued",
        "rebuilding",
        "starting",
        "stopping",
        "updating",
    }
)
_STOPPED_STATUSES = frozenset({"disabled", "inactive", "off", "stopped", "suspended"})

_ERROR_MESSAGES = {
    "error": "Vultr API request failed",
    "not_found": "Vultr resource was not found",
    "invalid_access_token": "Vultr API authorization failed",
}


VULTR_RESOURCE_ENDPOINTS = {
    "database": {"path": "databases", "response_key": "database", "metadata_key": "database"},
    "load_balancer": {
        "path": "load-balancers",
        "response_key": "load_balancer",
        "metadata_key": "load_balancer",
    },
    "vpc": {"path": "vpc2", "response_key": "vpc", "metadata_key": "vpc"},
    "nat_gateway": {
        "path": "nat-gateways",
        "response_key": "nat_gateway",
        "metadata_key": "nat_gateway",
    },
    "firewall": {
        "path": "firewalls",
        "response_key": "firewall_group",
        "metadata_key": "firewall",
    },
    "reserved_ip": {
        "path": "reserved-ips",
        "response_key": "reserved_ip",
        "metadata_key": "reserved_ip",
    },
    "domain": {"path": "domains", "response_key": "domain", "metadata_key": "domain"},
    "cdn_endpoint": {"path": "cdn", "response_key": "cdn_zone", "metadata_key": "cdn_endpoint"},
    "certificate": {
        "path": "ssl/certificates",
        "response_key": "certificate",
        "metadata_key": "certificate",
    },
}

VULTR_RESOURCE_TYPE_ALIASES = {
    "managed_database": "database",
    "databases": "database",
    "load_balancers": "load_balancer",
    "vpcs": "vpc",
    "vpc2": "vpc",
    "nat_gateways": "nat_gateway",
    "firewall_group": "firewall",
    "firewall_groups": "firewall",
    "firewall_rule": "firewall_rule",
    "firewall_rules": "firewall_rule",
    "reserved_ips": "reserved_ip",
    "zone": "domain",
    "dns_zone": "domain",
    "zones": "domain",
    "domain_record": "dns_record",
    "records": "dns_record",
    "cdn": "cdn_endpoint",
    "cdn_zone": "cdn_endpoint",
    "cdn_pull_zone": "cdn_endpoint",
    "cdn_push_zone": "cdn_endpoint",
    "pull_zone": "cdn_endpoint",
    "push_zone": "cdn_endpoint",
    "tls_certificate": "certificate",
    "ssl_certificate": "certificate",
    "ssl_certificates": "certificate",
}


def _normalize_resource_type(resource_type: Any) -> str:
    value = str(resource_type or "").strip().lower().replace(" ", "_")
    return VULTR_RESOURCE_TYPE_ALIASES.get(value, value)


def _credential_context(credentials: Any) -> tuple[str, dict[str, Any]]:
    if isinstance(credentials, Mapping):
        token = credentials.get("access_token") or credentials.get("api_token") or credentials.get("token")
        options = dict(credentials)
    else:
        token = credentials
        options = {}
    if not isinstance(token, str) or not token.strip():
        raise ValueError("credentials are not configured")
    return token, options


def _safe_error(status: str) -> tuple[str, dict[str, str]]:
    return status, {
        "error": _ERROR_MESSAGES.get(status, _ERROR_MESSAGES["error"]),
        "errorCode": status,
    }


def _request_json(path: str, token: str, params: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    if not isinstance(path, str) or not path or "?" in path or "#" in path:
        raise ValueError("unsupported Vultr endpoint")
    if any(part in {".", ".."} for part in path.split("/")):
        raise ValueError("unsupported Vultr endpoint")
    payload = VultrClient(token).get_json(path.lstrip("/"), params=params)
    if not isinstance(payload, Mapping):
        raise ValueError("Vultr response body is not an object")
    return payload


def _status_for_api_error(error: VultrAPIError) -> str:
    if error.status_code == 404:
        return "not_found"
    if error.status_code in (401, 403):
        return "invalid_access_token"
    return "error"


def _safe_payload(key: str, value: Any) -> dict[str, Any]:
    try:
        normalized = normalize_vultr_nested_record(value)
    except (CloudInventoryTransientError, TypeError, ValueError):
        raise ValueError("Vultr response contains invalid metadata")
    return {key: normalized}


def _provider_status(value: Any, *, default: str = "unknown") -> str:
    if not isinstance(value, str):
        return default
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in _AVAILABLE_STATUSES:
        return "available"
    if normalized in _DEGRADED_STATUSES:
        return "degraded"
    if normalized in _PENDING_STATUSES:
        return "pending"
    if normalized in _STOPPED_STATUSES:
        return "stopped"
    if normalized in {"", "unknown", "unavailable", "undefined"}:
        return "unknown"
    return default


def _status_from_resource(resource_type: str, resource: Mapping[str, Any]) -> str:
    if resource_type in {"firewall", "firewall_rule", "reserved_ip", "domain", "dns_record"}:
        # These APIs expose presence/configuration rather than a lifecycle
        # state.  A validated detail response is therefore available.
        for field in ("status", "state", "health"):
            if field in resource:
                candidate = resource.get(field)
                if isinstance(candidate, Mapping):
                    candidate = candidate.get("status") or candidate.get("state")
                return _provider_status(candidate, default="available")
        return "available"
    for field in ("status", "state", "phase", "health"):
        if field in resource:
            candidate = resource.get(field)
            if isinstance(candidate, Mapping):
                candidate = candidate.get("status") or candidate.get("state") or candidate.get("phase")
            return _provider_status(candidate)
    return "available" if resource_type in {"vpc", "cdn_endpoint"} else "unknown"


def _validate_load_balancer_configuration(resource: Mapping[str, Any]) -> None:
    health_check = resource.get("health_check")
    if health_check is not None and not isinstance(health_check, Mapping):
        raise ValueError("load balancer health check is invalid")
    forwarding_rules = resource.get("forwarding_rules")
    if forwarding_rules is not None and not isinstance(forwarding_rules, list):
        raise ValueError("load balancer forwarding rules are invalid")


def _health_status(value: Any) -> str:
    if isinstance(value, bool):
        return "available" if value else "degraded"
    if isinstance(value, Mapping):
        if isinstance(value.get("healthy"), bool):
            return "available" if value["healthy"] else "degraded"
        value = value.get("status") or value.get("state") or value.get("health")
    if isinstance(value, list):
        if not value:
            return "unknown"
        statuses = [_health_status(item) for item in value]
        if "degraded" in statuses:
            return "degraded"
        if all(status == "available" for status in statuses):
            return "available"
        if "pending" in statuses:
            return "pending"
        return "unknown"
    return _provider_status(value)


def _check_load_balancer_health(unique_id: Any, credentials: Any) -> tuple[str, dict[str, Any]]:
    token, _options = _credential_context(credentials)
    resource_id = quote(str(unique_id), safe="")
    payload = _request_json(f"load-balancers/{resource_id}/health", token)
    health = payload.get("health")
    if health is None:
        health = payload.get("health_checks")
    if health is None:
        raise ValueError("Vultr health response is incomplete")
    status = _health_status(health)
    return status, _safe_payload("health", health)


def check_vultr_load_balancer_status(unique_id: Any, credentials: Any):
    """Check the load balancer detail plus its read-only health endpoint."""
    if unique_id in (None, ""):
        return _safe_error("error")
    try:
        token, options = _credential_context(credentials)
        resource_id = quote(str(unique_id), safe="")
        detail_payload = _request_json(f"load-balancers/{resource_id}", token)
        resource = detail_payload.get("load_balancer")
        if not isinstance(resource, Mapping):
            raise ValueError("Vultr load balancer response is incomplete")
        _validate_load_balancer_configuration(resource)

        health_payload = _request_json(f"load-balancers/{resource_id}/health", token)
        health = health_payload.get("health") or health_payload.get("health_checks")
        if health is None:
            raise ValueError("Vultr load balancer health response is incomplete")
        resource_status = _status_from_resource("load_balancer", resource)
        health_status = _health_status(health)
        if "degraded" in {resource_status, health_status}:
            status = "degraded"
        elif "pending" in {resource_status, health_status}:
            status = "pending"
        elif health_status == "available" or resource_status == "available":
            status = "available"
        else:
            status = "unknown"
        return status, {
            "load_balancer": normalize_vultr_nested_record(resource),
            "health": normalize_vultr_nested_record(health),
            "configuration": {
                "health_check": normalize_vultr_nested_record(resource.get("health_check")),
                "forwarding_rules": normalize_vultr_nested_record(resource.get("forwarding_rules")),
            },
            "_cloudmoo_resource_id": str(unique_id),
            "_cloudmoo_options_present": bool(options),
        }
    except VultrAPIError as error:
        return _safe_error(_status_for_api_error(error))
    except (TypeError, ValueError, KeyError, CloudInventoryTransientError):
        return _safe_error("error")
    except Exception:
        return _safe_error("error")


def check_vultr_load_balancer_health_status(unique_id: Any, credentials: Any):
    if unique_id in (None, ""):
        return _safe_error("error")
    try:
        status, metadata = _check_load_balancer_health(unique_id, credentials)
        return status, metadata
    except VultrAPIError as error:
        return _safe_error(_status_for_api_error(error))
    except (TypeError, ValueError, KeyError, CloudInventoryTransientError):
        return _safe_error("error")
    except Exception:
        return _safe_error("error")


def check_vultr_load_balancer_configuration_status(unique_id: Any, credentials: Any):
    """Validate load-balancer configuration without issuing a write call."""
    if unique_id in (None, ""):
        return _safe_error("error")
    try:
        token, _options = _credential_context(credentials)
        resource_id = quote(str(unique_id), safe="")
        payload = _request_json(f"load-balancers/{resource_id}", token)
        resource = payload.get("load_balancer")
        if not isinstance(resource, Mapping):
            raise ValueError("Vultr load balancer response is incomplete")
        _validate_load_balancer_configuration(resource)
        return "available", {
            "configuration": {
                "health_check": normalize_vultr_nested_record(resource.get("health_check")),
                "forwarding_rules": normalize_vultr_nested_record(resource.get("forwarding_rules")),
            }
        }
    except VultrAPIError as error:
        return _safe_error(_status_for_api_error(error))
    except (TypeError, ValueError, KeyError, CloudInventoryTransientError):
        return _safe_error("error")
    except Exception:
        return _safe_error("error")


def _detail_check(resource_type: str, unique_id: Any, credentials: Any):
    endpoint = VULTR_RESOURCE_ENDPOINTS.get(resource_type)
    if endpoint is None or unique_id in (None, ""):
        return _safe_error("error")
    try:
        token, _options = _credential_context(credentials)
        resource_id = quote(str(unique_id), safe="")
        payload = _request_json(f"{endpoint['path']}/{resource_id}", token)
        resource = payload.get(endpoint["response_key"])
        # Vultr has used both names for firewall-group detail responses across
        # API revisions; both are explicit response keys, never URL input.
        if resource is None and resource_type == "firewall":
            resource = payload.get("firewall")
        if not isinstance(resource, Mapping):
            raise ValueError("Vultr resource response is incomplete")
        status = _status_from_resource(resource_type, resource)
        return status, _safe_payload(endpoint["metadata_key"], resource)
    except VultrAPIError as error:
        return _safe_error(_status_for_api_error(error))
    except (TypeError, ValueError, KeyError, CloudInventoryTransientError):
        return _safe_error("error")
    except Exception:
        return _safe_error("error")


def check_vultr_database_status(unique_id: Any, credentials: Any):
    return _detail_check("database", unique_id, credentials)


def check_vultr_vpc_status(unique_id: Any, credentials: Any):
    return _detail_check("vpc", unique_id, credentials)


def check_vultr_nat_gateway_status(unique_id: Any, credentials: Any):
    return _detail_check("nat_gateway", unique_id, credentials)


def check_vultr_firewall_status(unique_id: Any, credentials: Any):
    return _detail_check("firewall", unique_id, credentials)


def check_vultr_reserved_ip_status(unique_id: Any, credentials: Any):
    return _detail_check("reserved_ip", unique_id, credentials)


def check_vultr_domain_status(unique_id: Any, credentials: Any):
    return _detail_check("domain", unique_id, credentials)


def check_vultr_cdn_endpoint_status(unique_id: Any, credentials: Any):
    return _detail_check("cdn_endpoint", unique_id, credentials)


def check_vultr_certificate_status(unique_id: Any, credentials: Any):
    return _detail_check("certificate", unique_id, credentials)


def _parented_check(resource_type: str, unique_id: Any, credentials: Any):
    if unique_id in (None, ""):
        return _safe_error("error")
    try:
        token, options = _credential_context(credentials)
        parent = options.get("firewall_group_id") if resource_type == "firewall_rule" else options.get("domain")
        if not isinstance(parent, (str, int)) or not str(parent).strip():
            return _safe_error("error")
        parent_id = quote(str(parent), safe="")
        resource_id = quote(str(unique_id), safe="")
        if resource_type == "firewall_rule":
            path = f"firewalls/{parent_id}/rules/{resource_id}"
            response_key = "rule"
            metadata_key = "firewall_rule"
        else:
            path = f"domains/{parent_id}/records/{resource_id}"
            response_key = "record"
            metadata_key = "dns_record"
        payload = _request_json(path, token)
        resource = payload.get(response_key)
        if not isinstance(resource, Mapping):
            raise ValueError("Vultr child resource response is incomplete")
        status = _status_from_resource(resource_type, resource)
        return status, _safe_payload(metadata_key, resource)
    except VultrAPIError as error:
        return _safe_error(_status_for_api_error(error))
    except (TypeError, ValueError, KeyError, CloudInventoryTransientError):
        return _safe_error("error")
    except Exception:
        return _safe_error("error")


def check_vultr_firewall_rule_status(unique_id: Any, credentials: Any):
    return _parented_check("firewall_rule", unique_id, credentials)


def check_vultr_dns_record_status(unique_id: Any, credentials: Any):
    return _parented_check("dns_record", unique_id, credentials)


VULTR_RESOURCE_CHECKS = {
    "database": check_vultr_database_status,
    "load_balancer": check_vultr_load_balancer_status,
    "vpc": check_vultr_vpc_status,
    "nat_gateway": check_vultr_nat_gateway_status,
    "firewall": check_vultr_firewall_status,
    "firewall_rule": check_vultr_firewall_rule_status,
    "reserved_ip": check_vultr_reserved_ip_status,
    "domain": check_vultr_domain_status,
    "dns_record": check_vultr_dns_record_status,
    "cdn_endpoint": check_vultr_cdn_endpoint_status,
    "certificate": check_vultr_certificate_status,
}
VULTR_STATUS_CHECKS = VULTR_RESOURCE_CHECKS
VULTR_CHECK_REGISTRY = VULTR_RESOURCE_CHECKS
CHECK_REGISTRY = VULTR_RESOURCE_CHECKS


def check_vultr_resource_status(resource_type: Any, unique_id: Any, credentials: Any):
    normalized = _normalize_resource_type(resource_type)
    check = VULTR_RESOURCE_CHECKS.get(normalized)
    if not callable(check):
        return _safe_error("error")
    return check(unique_id, credentials)


check_vultr_asset_status = check_vultr_resource_status
check_vultr_managed_database_status = check_vultr_database_status
check_vultr_load_balancer_health_check_status = check_vultr_load_balancer_health_status
check_vultr_load_balancer_config_status = check_vultr_load_balancer_configuration_status
check_vultr_vpc2_status = check_vultr_vpc_status
check_vultr_firewall_group_status = check_vultr_firewall_status
check_vultr_reserved_ip_address_status = check_vultr_reserved_ip_status
check_vultr_zone_status = check_vultr_domain_status
check_vultr_dns_zone_status = check_vultr_domain_status
check_vultr_cdn_status = check_vultr_cdn_endpoint_status
check_vultr_cdn_zone_status = check_vultr_cdn_endpoint_status
check_vultr_cdn_pull_zone_status = check_vultr_cdn_endpoint_status
check_vultr_cdn_push_zone_status = check_vultr_cdn_endpoint_status
check_vultr_tls_certificate_status = check_vultr_certificate_status
check_vultr_ssl_certificate_status = check_vultr_certificate_status


def get_vultr_check_function(resource_type: Any):
    normalized = _normalize_resource_type(resource_type)
    check = VULTR_RESOURCE_CHECKS.get(normalized)
    if not callable(check):
        raise ValueError(f"Unsupported Vultr asset type: {resource_type}")
    return check


__all__ = [
    "CHECK_REGISTRY",
    "VULTR_API_BASE",
    "VULTR_CHECK_REGISTRY",
    "VULTR_REQUEST_TIMEOUT_SECONDS",
    "VULTR_RESOURCE_ENDPOINTS",
    "VULTR_RESOURCE_CHECKS",
    "VULTR_STATUS_CHECKS",
    "VULTR_RESOURCE_TYPE_ALIASES",
    "check_vultr_asset_status",
    "check_vultr_certificate_status",
    "check_vultr_cdn_endpoint_status",
    "check_vultr_cdn_pull_zone_status",
    "check_vultr_cdn_push_zone_status",
    "check_vultr_cdn_status",
    "check_vultr_cdn_zone_status",
    "check_vultr_database_status",
    "check_vultr_dns_record_status",
    "check_vultr_dns_zone_status",
    "check_vultr_domain_status",
    "check_vultr_firewall_group_status",
    "check_vultr_firewall_rule_status",
    "check_vultr_firewall_status",
    "check_vultr_load_balancer_configuration_status",
    "check_vultr_load_balancer_config_status",
    "check_vultr_load_balancer_health_check_status",
    "check_vultr_load_balancer_health_status",
    "check_vultr_load_balancer_status",
    "check_vultr_managed_database_status",
    "check_vultr_nat_gateway_status",
    "check_vultr_resource_status",
    "check_vultr_reserved_ip_address_status",
    "check_vultr_reserved_ip_status",
    "check_vultr_ssl_certificate_status",
    "check_vultr_tls_certificate_status",
    "check_vultr_vpc2_status",
    "check_vultr_vpc_status",
    "check_vultr_zone_status",
    "get_vultr_check_function",
]
