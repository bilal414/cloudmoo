"""Read-only Vultr account-operation and provider-health checks.

Vultr API checks use the shared account-operation resource helpers and report
control-plane state.  The public ``status.json`` page is intentionally a
separate unauthenticated check: it describes Vultr's external service health,
not the state of a customer's account or resources.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Callable

import requests

from apps.console.cloud.vultr.resources_account_operations import (
    VULTR_ACCOUNT_LOG,
    VULTR_ACCOUNT_OPERATIONS_ASSET_TYPES,
    VULTR_ACCOUNT_PROFILE,
    VULTR_ACCOUNT_PLAN,
    VULTR_ACCOUNT_LIMITS,
    VULTR_API_KEY_METADATA,
    VULTR_BGP_SESSION,
    VULTR_BANDWIDTH_METRIC,
    VULTR_BILLING_TRANSACTION,
    VULTR_DOCUMENTED_GET_ENDPOINTS,
    VULTR_IAM_USER,
    VULTR_OPERATION,
    VULTR_STATUS_INCIDENT,
    VULTR_SUPPORT_STATUS,
    VULTR_STATUS_JSON_URL,
    VultrCredentialsUnavailable,
    VultrUnsupportedEndpoint,
    fetch_vultr_account_operation,
    get_vultr_action_status,
    get_vultr_api_key_metadata,
    get_vultr_support_status,
    list_vultr_account_logs,
    list_vultr_bgp_sessions,
    list_vultr_billing_transactions,
    list_vultr_iam_users,
    vultr_client_from_credentials,
    vultr_error_code,
)
from apps.monitoring.checks.base import REQUEST_TIMEOUT_SECONDS, classify_http_error
from apps.monitoring.metadata import redact_sensitive_metadata


CONTROL_PLANE = "vultr_api"
EXTERNAL_HEALTH = "vultr_status_json"
MAX_STATUS_COMPONENTS = 100
MAX_STATUS_INCIDENTS = 50
MAX_STATUS_MAINTENANCES = 50


def _safe_error_payload(asset_type: str, code: str, *, control_plane: str = CONTROL_PLANE) -> dict[str, Any]:
    return {
        asset_type: {
            "status": "error",
            "errorCode": str(code)[:128],
            "controlPlane": control_plane,
        }
    }


def _credentials_token_present(credentials: Any) -> bool:
    if isinstance(credentials, str):
        return bool(credentials.strip())
    if isinstance(credentials, Mapping):
        return any(
            isinstance(credentials.get(key), str) and credentials.get(key).strip()
            for key in ("access_token", "api_token", "token")
        )
    return bool(
        getattr(credentials, "access_token", None)
        or getattr(credentials, "api_token", None)
    )


def _normalized_state(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in {
        "active", "available", "ok", "healthy", "operational", "running",
        "established", "up", "complete", "completed", "success", "succeeded",
        "enabled",
    }:
        return "available"
    if normalized in {
        "degraded", "warning", "partial_outage", "major_outage", "minor_outage",
        "down", "failed", "failure", "error", "unhealthy", "critical",
    }:
        return "degraded"
    if normalized in {
        "pending", "queued", "processing", "in_progress", "creating", "updating",
        "under_maintenance", "maintenance",
    }:
        return "pending"
    if normalized in {"stopped", "disabled", "inactive"}:
        return "stopped"
    if normalized in {"resolved", "closed", "completed_ok"}:
        return "available"
    return "unknown"


def _record_state(record: Mapping[str, Any]) -> str:
    for key in ("status", "state", "health", "operation_status", "account_status"):
        if key in record and record[key] not in (None, ""):
            return _normalized_state(record[key])
    return "available"


def _status_from_result(result: Mapping[str, Any], asset_type: str) -> tuple[str, dict[str, Any]]:
    collection_status = result.get("status")
    records = result.get("records")
    if not isinstance(records, list):
        records = []
    safe_result: dict[str, Any] = {
        "controlPlane": CONTROL_PLANE,
        "collectionStatus": collection_status,
        "partial": bool(result.get("partial")),
        "records": redact_sensitive_metadata(records),
    }
    if result.get("reason"):
        safe_result["reason"] = str(result["reason"])[:256]
    if result.get("errorCode"):
        safe_result["errorCode"] = str(result["errorCode"])[:128]

    if collection_status == "unsupported":
        return "unsupported", {asset_type: safe_result}
    if collection_status == "partial":
        return "partial", {asset_type: safe_result}
    if collection_status != "complete":
        return "error", {asset_type: safe_result}
    states = [_record_state(record) for record in records if isinstance(record, Mapping)]
    if "degraded" in states:
        status = "degraded"
    elif "pending" in states:
        status = "pending"
    elif "stopped" in states:
        status = "stopped"
    elif states and all(state == "unknown" for state in states):
        status = "unknown"
    else:
        status = "available"
    if len(records) == 1 and isinstance(records[0], Mapping):
        safe_result.update(records[0])
        safe_result["records"] = redact_sensitive_metadata(records)
    return status, {asset_type: safe_result}


def _check_collection(
    asset_type: str,
    resource_key: str,
    credentials: Any,
    *,
    list_helper: Callable[..., Mapping[str, Any]] | None = None,
) -> tuple[str, dict[str, Any]]:
    try:
        client = vultr_client_from_credentials(credentials)
        result = (
            list_helper(client) if list_helper else fetch_vultr_account_operation(client, resource_key)
        )
        return _status_from_result(result, asset_type)
    except VultrCredentialsUnavailable:
        return "credentials_unavailable", _safe_error_payload(
            asset_type,
            "credentials_unavailable",
        )
    except Exception as error:
        return _error_status_payload(asset_type, error)


def _error_status_payload(asset_type: str, error: BaseException) -> tuple[str, dict[str, Any]]:
    code = vultr_error_code(error)
    if isinstance(error, VultrUnsupportedEndpoint) or code == "unsupported_endpoint":
        status = "unsupported"
    elif code == "invalid_access_token":
        status = "invalid_access_token"
    elif code == "not_found":
        status = "not_found"
    elif code == "credentials_unavailable":
        status = "credentials_unavailable"
    else:
        status = "error"
    return status, _safe_error_payload(asset_type, code)


def check_vultr_account_profile_status(unique_id: str, credentials: Any):
    return _check_collection(VULTR_ACCOUNT_PROFILE, "account_profile", credentials)


def check_vultr_account_status(unique_id: str, credentials: Any):
    return check_vultr_account_profile_status(unique_id, credentials)


def check_vultr_account_plan_status(unique_id: str, credentials: Any):
    return _check_collection(VULTR_ACCOUNT_PLAN, "account_plan", credentials)


def check_vultr_account_limits_status(unique_id: str, credentials: Any):
    return _check_collection(VULTR_ACCOUNT_LIMITS, "account_limits", credentials)


def check_vultr_account_bandwidth_status(unique_id: str, credentials: Any):
    return _check_collection(VULTR_BANDWIDTH_METRIC, "account_bandwidth", credentials)


def check_vultr_billing_transaction_status(unique_id: str, credentials: Any):
    return _check_collection(
        VULTR_BILLING_TRANSACTION,
        "billing_transactions",
        credentials,
        list_helper=list_vultr_billing_transactions,
    )


def check_vultr_account_log_status(unique_id: str, credentials: Any):
    return _check_collection(
        VULTR_ACCOUNT_LOG,
        "account_logs",
        credentials,
        list_helper=list_vultr_account_logs,
    )


def check_vultr_iam_user_status(unique_id: str, credentials: Any):
    return _check_collection(
        VULTR_IAM_USER,
        "iam_users",
        credentials,
        list_helper=list_vultr_iam_users,
    )


def check_vultr_bgp_session_status(unique_id: str, credentials: Any):
    return _check_collection(
        VULTR_BGP_SESSION,
        "bgp_sessions",
        credentials,
        list_helper=list_vultr_bgp_sessions,
    )


def check_vultr_api_key_metadata_status(unique_id: str, credentials: Any):
    if _credentials_token_present(credentials):
        return "unsupported", {VULTR_API_KEY_METADATA: get_vultr_api_key_metadata()}
    return "credentials_unavailable", _safe_error_payload(
        VULTR_API_KEY_METADATA,
        "credentials_unavailable",
    )


def check_vultr_support_status_status(unique_id: str, credentials: Any):
    if _credentials_token_present(credentials):
        return "unsupported", {VULTR_SUPPORT_STATUS: get_vultr_support_status()}
    return "credentials_unavailable", _safe_error_payload(
        VULTR_SUPPORT_STATUS,
        "credentials_unavailable",
    )


def _action_context(unique_id: Any, credentials: Any) -> tuple[Any, Any, str]:
    metadata = credentials if isinstance(credentials, Mapping) else {}
    action_id = metadata.get("action_id") or metadata.get("operation_id") or unique_id
    resource_id = metadata.get("resource_id") or metadata.get("instance_id")
    resource_type = metadata.get("resource_type") or metadata.get("parent_type") or "instance"
    if resource_id in (None, ""):
        raise VultrUnsupportedEndpoint("Vultr action status requires a parent resource")
    return resource_id, action_id, str(resource_type)


def check_vultr_action_status(unique_id: str, credentials: Any):
    asset_type = VULTR_ACTION = "action"
    try:
        client = vultr_client_from_credentials(credentials)
        resource_id, action_id, resource_type = _action_context(unique_id, credentials)
        result = get_vultr_action_status(
            client,
            resource_id,
            action_id,
            resource_type=resource_type,
        )
        return _status_from_result(result, asset_type)
    except Exception as error:
        return _error_status_payload(asset_type, error)


def _safe_status_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return None
    if isinstance(value, Mapping):
        result = {}
        for key, child in list(value.items())[:100]:
            key_text = str(key).lower()
            if key_text in {"url", "shortlink", "href", "uri", "incident_updates"}:
                continue
            safe_child = _safe_status_value(child, depth=depth + 1)
            if safe_child is not None:
                result[str(key)[:128]] = safe_child
        return result
    if isinstance(value, list):
        return [
            safe_child
            for child in value[:MAX_STATUS_INCIDENTS]
            if (safe_child := _safe_status_value(child, depth=depth + 1)) is not None
        ]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value if not isinstance(value, str) else value[:512]
    return str(value)[:512]


def _status_page_state(payload: Mapping[str, Any]) -> str:
    page = payload.get("page")
    if isinstance(page, Mapping):
        page_state = _normalized_state(page.get("status"))
        if page_state in {"degraded", "pending"}:
            return page_state
    component_states = []
    for component in payload.get("components", [])[:MAX_STATUS_COMPONENTS]:
        if isinstance(component, Mapping):
            component_states.append(_normalized_state(component.get("status")))
    incident_states = []
    for incident in payload.get("incidents", [])[:MAX_STATUS_INCIDENTS]:
        if isinstance(incident, Mapping):
            incident_status = str(incident.get("status") or "").lower()
            if incident_status not in {"resolved", "closed", "completed"}:
                incident_states.append(_normalized_state(incident.get("impact") or "degraded"))
    maintenance_states = []
    for maintenance in payload.get("scheduled_maintenances", [])[:MAX_STATUS_MAINTENANCES]:
        if isinstance(maintenance, Mapping):
            status = str(maintenance.get("status") or "").lower()
            if status not in {"completed", "resolved", "past"}:
                maintenance_states.append("pending")
    if "degraded" in component_states or incident_states:
        return "degraded"
    if maintenance_states or "pending" in component_states:
        return "pending"
    if component_states and all(state == "unknown" for state in component_states):
        return "unknown"
    return "available"


def check_vultr_status_incident_status(unique_id: str | None = None, credentials: Any = None):
    """Poll Vultr's public status page without an Authorization header."""

    try:
        response = requests.get(
            VULTR_STATUS_JSON_URL,
            headers={"Accept": "application/json"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise ValueError("invalid status payload")
        safe_payload = _safe_status_value(payload)
        status = _status_page_state(payload)
        result = {
            "controlPlane": EXTERNAL_HEALTH,
            "externalServiceHealth": True,
            "source": "status.json",
            "status": status,
            "page": safe_payload.get("page", {}) if isinstance(safe_payload, Mapping) else {},
            "components": safe_payload.get("components", []) if isinstance(safe_payload, Mapping) else [],
            "incidents": safe_payload.get("incidents", []) if isinstance(safe_payload, Mapping) else [],
            "scheduledMaintenances": safe_payload.get("scheduled_maintenances", []) if isinstance(safe_payload, Mapping) else [],
        }
        return status, {VULTR_STATUS_INCIDENT: redact_sensitive_metadata(result)}
    except requests.exceptions.RequestException as error:
        response = getattr(error, "response", None)
        status_code = getattr(response, "status_code", None)
        error_code = "external_health_unavailable"
        if status_code == 404:
            error_code = "not_found"
        return "error", _safe_error_payload(
            VULTR_STATUS_INCIDENT,
            error_code,
            control_plane=EXTERNAL_HEALTH,
        )
    except Exception:
        return "error", _safe_error_payload(
            VULTR_STATUS_INCIDENT,
            "external_health_unavailable",
            control_plane=EXTERNAL_HEALTH,
        )


# Friendly aliases used by the monitoring resolver and tests.
check_vultr_provider_status_status = check_vultr_status_incident_status
check_vultr_status_json = check_vultr_status_incident_status


VULTR_ACCOUNT_OPERATIONS_CHECKS: dict[str, Callable[..., tuple[str, dict[str, Any]]]] = {
    VULTR_ACCOUNT_PROFILE: check_vultr_account_profile_status,
    VULTR_ACCOUNT_PLAN: check_vultr_account_plan_status,
    VULTR_ACCOUNT_LIMITS: check_vultr_account_limits_status,
    VULTR_BANDWIDTH_METRIC: check_vultr_account_bandwidth_status,
    VULTR_BILLING_TRANSACTION: check_vultr_billing_transaction_status,
    VULTR_ACCOUNT_LOG: check_vultr_account_log_status,
    VULTR_IAM_USER: check_vultr_iam_user_status,
    VULTR_BGP_SESSION: check_vultr_bgp_session_status,
    VULTR_API_KEY_METADATA: check_vultr_api_key_metadata_status,
    VULTR_SUPPORT_STATUS: check_vultr_support_status_status,
    VULTR_OPERATION: check_vultr_action_status,
    "action": check_vultr_action_status,
    VULTR_STATUS_INCIDENT: check_vultr_status_incident_status,
}
VULTR_ACCOUNT_OPERATIONS_STATUS_CHECKS = VULTR_ACCOUNT_OPERATIONS_CHECKS
VULTR_ACCOUNT_OPERATIONS_CHECK_REGISTRY = VULTR_ACCOUNT_OPERATIONS_CHECKS


def get_vultr_account_operation_check(asset_type: str) -> Callable[..., tuple[str, dict[str, Any]]]:
    normalized = str(asset_type or "").strip().lower()
    if normalized.startswith("vultr_") and normalized not in VULTR_ACCOUNT_OPERATIONS_CHECKS:
        normalized = normalized[6:]
    check = VULTR_ACCOUNT_OPERATIONS_CHECKS.get(normalized) or VULTR_ACCOUNT_OPERATIONS_CHECKS.get(asset_type)
    if not callable(check):
        raise ValueError(f"Unsupported Vultr account-operation asset type: {asset_type}")
    return check


def get_vultr_check_function(asset_type: str) -> Callable[..., tuple[str, dict[str, Any]]]:
    return get_vultr_account_operation_check(asset_type)


def check_vultr_account_operations_status(asset_type: str, unique_id: str, credentials: Any):
    return get_vultr_account_operation_check(asset_type)(unique_id, credentials)


__all__ = [
    "VULTR_ACCOUNT_OPERATIONS_CHECKS",
    "VULTR_ACCOUNT_OPERATIONS_CHECK_REGISTRY",
    "VULTR_ACCOUNT_OPERATIONS_STATUS_CHECKS",
    "check_vultr_account_operations_status",
    "check_vultr_account_profile_status",
    "check_vultr_account_status",
    "check_vultr_account_plan_status",
    "check_vultr_account_limits_status",
    "check_vultr_account_bandwidth_status",
    "check_vultr_account_log_status",
    "check_vultr_api_key_metadata_status",
    "check_vultr_bgp_session_status",
    "check_vultr_billing_transaction_status",
    "check_vultr_action_status",
    "check_vultr_status_incident_status",
    "check_vultr_status_json",
    "get_vultr_account_operation_check",
    "get_vultr_check_function",
]
