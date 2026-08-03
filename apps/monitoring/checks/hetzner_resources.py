"""Read-only monitoring checks for the Hetzner Cloud inventory surface.

The Hetzner inventory adapter is expected to persist the resource identifiers
returned by the ``api.hetzner.cloud/v1`` API.  This module deliberately keeps
the monitoring contract independent from the adapter: every check accepts a
provider identifier and either a token or a credential mapping, performs one
bounded ``GET``, and returns ``(status, metadata_or_error)``.

The canonical health vocabulary is intentionally small and stable:

* ``available`` means the provider reports a usable resource;
* ``degraded`` means the resource exists but reports a failure/unhealthy state;
* ``pending`` means a known lifecycle transition is in progress;
* ``stopped`` means a known stopped/off state;
* ``unknown`` means the provider returned a valid resource without a usable
  status, or an unfamiliar provider status;
* ``error``, ``not_found``, and ``invalid_access_token`` are transport/check
  outcomes normalized with :mod:`apps.monitoring.checks.base`.

The current Cloud API exposes images of type ``snapshot`` rather than a
separate snapshot endpoint, so the ``snapshot`` check is an explicit alias
that keeps the future asset type stable while reading ``/images/{id}``.
Primary IPs, floating IPs, placement groups, SSH keys, and firewalls do not
have a lifecycle status field; their presence is checked and their status is
derived from assignment/blocking fields where those fields are part of the
provider response.  Metrics use a bounded default 15-minute CPU window when
the adapter does not provide one.  These assumptions are documented here so
the eventual dispatch wiring can change without silently changing check
semantics.

No write operation is present in this module.  Provider exceptions and
response bodies are never returned verbatim: error results use generic,
credential-free messages and successful metadata is recursively bounded and
redacted before it is returned to the monitoring engine.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests
import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from apps.monitoring.checks.base import REQUEST_TIMEOUT_SECONDS, classify_http_error
from apps.monitoring.metadata import redact_sensitive_metadata


HETZNER_API_BASE = "https://api.hetzner.cloud/v1"
HETZNER_REQUEST_TIMEOUT_SECONDS = REQUEST_TIMEOUT_SECONDS
METRICS_DEFAULT_TYPE = "cpu"
METRICS_WINDOW_MINUTES = 15
MAX_METADATA_DEPTH = 6
MAX_METADATA_ITEMS = 100
MAX_METADATA_TEXT_LENGTH = 2048
MAX_METRIC_SERIES = 32
MAX_METRIC_POINTS_PER_SERIES = 256


# The values are API path/response-key contracts, not arbitrary URL input.
# Keeping them explicit makes the eventual dispatcher auditable and prevents
# a persisted asset type from selecting an unexpected endpoint.
HETZNER_RESOURCE_ENDPOINTS = {
    "server": {"path": "servers", "response_key": "server", "metadata_key": "server"},
    "volume": {"path": "volumes", "response_key": "volume", "metadata_key": "volume"},
    "primary_ip": {
        "path": "primary_ips",
        "response_key": "primary_ip",
        "metadata_key": "primary_ip",
    },
    "floating_ip": {
        "path": "floating_ips",
        "response_key": "floating_ip",
        "metadata_key": "floating_ip",
    },
    "placement_group": {
        "path": "placement_groups",
        "response_key": "placement_group",
        "metadata_key": "placement_group",
    },
    "ssh_key": {"path": "ssh_keys", "response_key": "ssh_key", "metadata_key": "ssh_key"},
    "image": {"path": "images", "response_key": "image", "metadata_key": "image"},
    # Hetzner represents snapshots as images with image.type == "snapshot".
    "snapshot": {"path": "images", "response_key": "image", "metadata_key": "snapshot"},
    "certificate": {
        "path": "certificates",
        "response_key": "certificate",
        "metadata_key": "certificate",
    },
    "firewall": {"path": "firewalls", "response_key": "firewall", "metadata_key": "firewall"},
    "load_balancer": {
        "path": "load_balancers",
        "response_key": "load_balancer",
        "metadata_key": "load_balancer",
    },
    "network": {"path": "networks", "response_key": "network", "metadata_key": "network"},
    "location": {"path": "locations", "response_key": "location", "metadata_key": "location"},
    "datacenter": {"path": "datacenters", "response_key": "datacenter", "metadata_key": "datacenter"},
    "server_type": {"path": "server_types", "response_key": "server_type", "metadata_key": "server_type"},
    "load_balancer_type": {
        "path": "load_balancer_types",
        "response_key": "load_balancer_type",
        "metadata_key": "load_balancer_type",
    },
    "iso": {"path": "isos", "response_key": "iso", "metadata_key": "iso"},
    "zone": {"path": "zones", "response_key": "zone", "metadata_key": "zone"},
    "action": {"path": "actions", "response_key": "action", "metadata_key": "action"},
}

HETZNER_RESOURCE_TYPE_ALIASES = {
    "servers": "server",
    "volumes": "volume",
    "primary_ips": "primary_ip",
    "primary-ip": "primary_ip",
    "floating_ips": "floating_ip",
    "floating-ip": "floating_ip",
    "placement_groups": "placement_group",
    "placement-groups": "placement_group",
    "ssh_keys": "ssh_key",
    "ssh-keys": "ssh_key",
    "images": "image",
    "snapshots": "snapshot",
    "certificates": "certificate",
    "firewalls": "firewall",
    "load_balancers": "load_balancer",
    "load-balancers": "load_balancer",
    "networks": "network",
    "locations": "location",
    "datacenters": "datacenter",
    "server_types": "server_type",
    "load_balancer_types": "load_balancer_type",
    "isos": "iso",
    "zones": "zone",
    "rrsets": "rrset",
    "rr-set": "rrset",
    "actions": "action",
    "object_storage": "object_storage",
    "object-storage": "object_storage",
    "server_metric": "server_metrics",
    "server-metric": "server_metrics",
    "server_metrics": "server_metrics",
    "server-metrics": "server_metrics",
    "metrics": "server_metrics",
    "load_balancer_metric": "load_balancer_metrics",
    "load-balancer-metric": "load_balancer_metrics",
    "load_balancer_metrics": "load_balancer_metrics",
    "load-balancer-metrics": "load_balancer_metrics",
}

HETZNER_RESOURCE_TYPES = tuple(HETZNER_RESOURCE_ENDPOINTS)
HETZNER_CHECK_TYPES = HETZNER_RESOURCE_TYPES + (
    "rrset",
    "server_metrics",
    "load_balancer_metrics",
    "object_storage",
)


_STATUS_FIELDS = {
    "server": ("status",),
    "volume": ("status",),
    "image": ("status",),
    "snapshot": ("status",),
    "certificate": ("status", "state"),
    "load_balancer": ("status",),
    "action": ("status",),
}

_AVAILABLE_STATUSES = frozenset({
    "active",
    "assigned",
    "attached",
    "available",
    "complete",
    "completed",
    "enabled",
    "healthy",
    "issued",
    "ok",
    "online",
    "operational",
    "ready",
    "registered",
    "reserved",
    "success",
    "valid",
})
_DEGRADED_STATUSES = frozenset({
    "degraded",
    "error",
    "failed",
    "failure",
    "faulted",
    "invalid",
    "maintenance",
    "unhealthy",
    "warning",
})
_PENDING_STATUSES = frozenset({
    "activating",
    "building",
    "creating",
    "deleting",
    "initializing",
    "migrating",
    "pending",
    "processing",
    "queued",
    "rebuilding",
    "starting",
    "stopping",
    "updating",
})
_STOPPED_STATUSES = frozenset({"disabled", "inactive", "off", "stopped"})
_UNKNOWN_STATUSES = frozenset({"", "unknown", "unavailable", "undefined"})

_ERROR_MESSAGES = {
    "error": "Hetzner API request failed",
    "not_found": "Hetzner resource was not found",
    "invalid_access_token": "Hetzner API authorization failed",
}


def _normalize_resource_type(resource_type):
    value = str(resource_type or "").strip().lower().replace(" ", "_")
    return HETZNER_RESOURCE_TYPE_ALIASES.get(value, value)


def _credential_context(credentials):
    """Return ``(token, options)`` without exposing token values."""
    if isinstance(credentials, Mapping):
        token = (
            credentials.get("access_token")
            or credentials.get("api_token")
            or credentials.get("token")
        )
        options = dict(credentials)
    else:
        token = credentials
        options = {}

    if not isinstance(token, str) or not token.strip():
        raise ValueError("credentials are not configured")
    return token, options


def _safe_value(value, depth=0):
    """Bound JSON-like provider data before it reaches persistent metadata."""
    if depth >= MAX_METADATA_DEPTH:
        return "[TRUNCATED]"
    if isinstance(value, Mapping):
        values = {}
        for index, (key, child) in enumerate(value.items()):
            if index >= MAX_METADATA_ITEMS:
                values["_cloudmoo_truncated"] = True
                break
            values[str(key)] = _safe_value(child, depth + 1)
        return redact_sensitive_metadata(values)
    if isinstance(value, (list, tuple)):
        values = [_safe_value(child, depth + 1) for child in value[:MAX_METADATA_ITEMS]]
        if len(value) > MAX_METADATA_ITEMS:
            values.append("[TRUNCATED]")
        return values
    if isinstance(value, str):
        return value[:MAX_METADATA_TEXT_LENGTH]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:MAX_METADATA_TEXT_LENGTH]


def _safe_error(status):
    """Return a bounded error payload with no provider exception text."""
    return status, {"error": _ERROR_MESSAGES.get(status, _ERROR_MESSAGES["error"]), "errorCode": status}


def _request_json(path, token, params=None):
    """Issue one read-only request and validate that it contains JSON."""
    request_kwargs = {
        "headers": {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        "timeout": HETZNER_REQUEST_TIMEOUT_SECONDS,
    }
    if params is not None:
        request_kwargs["params"] = params

    response = requests.get(f"{HETZNER_API_BASE}/{path.lstrip('/')}", **request_kwargs)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, Mapping):
        raise ValueError("response body is not an object")
    return payload


def _normalize_provider_status(value, resource_type):
    if not isinstance(value, str):
        return "unknown"
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    if resource_type == "action" and normalized == "running":
        return "pending"
    if normalized in _AVAILABLE_STATUSES or (
        normalized == "running" and resource_type != "action"
    ):
        return "available"
    if normalized in _DEGRADED_STATUSES:
        return "degraded"
    if normalized in _PENDING_STATUSES:
        return "pending"
    if normalized in _STOPPED_STATUSES:
        return "stopped"
    if normalized in _UNKNOWN_STATUSES:
        return "unknown"
    return "unknown"


def _resource_status(resource_type, resource):
    """Derive a stable status from one validated provider resource."""
    if resource_type in {"primary_ip", "floating_ip"}:
        blocked = resource.get("blocked")
        if blocked is not None and not isinstance(blocked, bool):
            raise ValueError("IP response has an invalid blocked field")
        if blocked is True:
            return "degraded"
        assignee_id = resource.get("assignee_id")
        return "assigned" if assignee_id not in (None, "") else "available"

    if resource_type in {
        "placement_group", "ssh_key", "firewall", "network", "location",
        "datacenter", "server_type", "load_balancer_type", "iso", "zone",
        "rrset",
    }:
        if resource_type == "rrset":
            records = resource.get("records")
            if not isinstance(records, list):
                raise ValueError("RRSet response has an invalid records field")
        return "available"

    if resource_type == "load_balancer":
        targets = resource.get("targets")
        if isinstance(targets, list) and targets:
            health_states = []
            for target in targets:
                if not isinstance(target, Mapping):
                    raise ValueError("load balancer target is invalid")
                health = target.get("health_status")
                if health is None and isinstance(target.get("health"), Mapping):
                    health = target["health"].get("status")
                if isinstance(health, str):
                    health_states.append(health.strip().lower())
            if health_states and any(state in {"unhealthy", "failed", "error"} for state in health_states):
                return "degraded"
            if health_states and all(state == "healthy" for state in health_states):
                return "available"
            if health_states:
                return "unknown"

    fields = _STATUS_FIELDS.get(resource_type, ())
    if not fields:
        return "unknown"
    for field in fields:
        if field in resource:
            return _normalize_provider_status(resource.get(field), resource_type)
    return "unknown"


def _check_resource(resource_type, unique_id, credentials):
    normalized_type = _normalize_resource_type(resource_type)
    endpoint = HETZNER_RESOURCE_ENDPOINTS.get(normalized_type)
    if endpoint is None:
        return _safe_error("error")
    if unique_id in (None, ""):
        return _safe_error("error")

    try:
        token, _options = _credential_context(credentials)
    except ValueError:
        return _safe_error("invalid_access_token")

    try:
        resource_id = quote(str(unique_id), safe="")
        payload = _request_json(f"{endpoint['path']}/{resource_id}", token)
        resource = payload.get(endpoint["response_key"])
        if not isinstance(resource, Mapping):
            raise ValueError("response resource is not an object")
        status = _resource_status(normalized_type, resource)
        return status, {endpoint["metadata_key"]: _safe_value(resource)}
    except requests.exceptions.RequestException as error:
        return _safe_error(classify_http_error(error))
    except (TypeError, ValueError, KeyError):
        return _safe_error("error")
    except Exception:
        # A monitoring worker must fail closed even for an unexpected client
        # implementation error, without returning its provider text.
        return _safe_error("error")


def check_hetzner_resource_status(resource_type, unique_id, credentials):
    """Check one supported Hetzner resource by its canonical asset type."""
    return _check_resource(resource_type, unique_id, credentials)


def check_hetzner_server_status(unique_id, credentials):
    """Check a Hetzner Cloud server's lifecycle state."""
    return _check_resource("server", unique_id, credentials)


def check_hetzner_volume_status(unique_id, credentials):
    """Check a Hetzner Cloud volume's lifecycle state."""
    return _check_resource("volume", unique_id, credentials)


def check_hetzner_primary_ip_status(unique_id, credentials):
    """Check a Hetzner primary IP for blocking or assignment."""
    return _check_resource("primary_ip", unique_id, credentials)


def check_hetzner_floating_ip_status(unique_id, credentials):
    """Check a Hetzner floating IP for blocking or assignment."""
    return _check_resource("floating_ip", unique_id, credentials)


def check_hetzner_placement_group_status(unique_id, credentials):
    """Check that a Hetzner placement group remains present."""
    return _check_resource("placement_group", unique_id, credentials)


def check_hetzner_ssh_key_status(unique_id, credentials):
    """Check that a Hetzner SSH key remains present."""
    return _check_resource("ssh_key", unique_id, credentials)


def check_hetzner_image_status(unique_id, credentials):
    """Check a Hetzner image's lifecycle state."""
    return _check_resource("image", unique_id, credentials)


def check_hetzner_snapshot_status(unique_id, credentials):
    """Check a Hetzner snapshot represented by the Images API."""
    return _check_resource("snapshot", unique_id, credentials)


def check_hetzner_certificate_status(unique_id, credentials):
    """Check a Hetzner TLS certificate's issuance state."""
    return _check_resource("certificate", unique_id, credentials)


def check_hetzner_firewall_status(unique_id, credentials):
    """Check that a Hetzner firewall policy remains present."""
    return _check_resource("firewall", unique_id, credentials)


def check_hetzner_load_balancer_status(unique_id, credentials):
    """Check a Hetzner load balancer's lifecycle state."""
    return _check_resource("load_balancer", unique_id, credentials)


def check_hetzner_network_status(unique_id, credentials):
    return _check_resource("network", unique_id, credentials)


def check_hetzner_location_status(unique_id, credentials):
    return _check_resource("location", unique_id, credentials)


def check_hetzner_datacenter_status(unique_id, credentials):
    return _check_resource("datacenter", unique_id, credentials)


def check_hetzner_server_type_status(unique_id, credentials):
    return _check_resource("server_type", unique_id, credentials)


def check_hetzner_load_balancer_type_status(unique_id, credentials):
    return _check_resource("load_balancer_type", unique_id, credentials)


def check_hetzner_iso_status(unique_id, credentials):
    return _check_resource("iso", unique_id, credentials)


def check_hetzner_zone_status(unique_id, credentials):
    return _check_resource("zone", unique_id, credentials)


def check_hetzner_rrset_status(unique_id, credentials):
    """Read one RRSet using the parent zone/name/type context."""
    if unique_id in (None, ""):
        return _safe_error("error")
    try:
        token, options = _credential_context(credentials)
        zone_id = options.get("zone_id") or options.get("zone")
        rr_name = options.get("rr_name") or options.get("name")
        rr_type = options.get("rr_type") or options.get("type")
        if not all(isinstance(value, str) and value.strip() for value in (zone_id, rr_name, rr_type)):
            raise ValueError("RRSet context is incomplete")
        path = (
            f"zones/{quote(zone_id, safe='')}/rrsets/"
            f"{quote(rr_name, safe='')}/{quote(rr_type, safe='')}"
        )
        payload = _request_json(path, token)
        resource = payload.get("rrset")
        if not isinstance(resource, Mapping):
            raise ValueError("RRSet response is not an object")
        return _resource_status("rrset", resource), {"rrset": _safe_value(resource)}
    except requests.exceptions.RequestException as error:
        return _safe_error(classify_http_error(error))
    except (TypeError, ValueError, KeyError):
        return _safe_error("error")
    except Exception:
        return _safe_error("error")


def _metrics_options(credentials, default_type=METRICS_DEFAULT_TYPE):
    """Validate optional metric query context and create a bounded time window."""
    _token, options = _credential_context(credentials)
    metric_options = options.get("metrics")
    if metric_options is None:
        metric_options = options
    if not isinstance(metric_options, Mapping):
        raise ValueError("metrics context is invalid")

    metric_type = metric_options.get("metric_type", metric_options.get("type", default_type))
    if not isinstance(metric_type, str) or not metric_type.strip() or len(metric_type) > 64:
        raise ValueError("metrics type is invalid")

    start = metric_options.get("start")
    end = metric_options.get("end")
    if start is None and end is None:
        end_time = datetime.now(timezone.utc).replace(microsecond=0)
        start_time = end_time - timedelta(minutes=METRICS_WINDOW_MINUTES)
        start = start_time.isoformat().replace("+00:00", "Z")
        end = end_time.isoformat().replace("+00:00", "Z")
    elif not isinstance(start, str) or not isinstance(end, str) or not start or not end:
        raise ValueError("metrics window is invalid")
    if len(str(start)) > 128 or len(str(end)) > 128:
        raise ValueError("metrics window is invalid")

    try:
        start_dt = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("metrics window is invalid") from error
    if (
        start_dt.tzinfo is None
        or end_dt.tzinfo is None
        or end_dt <= start_dt
        or end_dt - start_dt > timedelta(days=30)
    ):
        raise ValueError("metrics window is invalid")

    return {"type": metric_type.strip(), "start": start, "end": end}


def _validated_metrics(metrics):
    """Validate the stable subset of the Hetzner metrics response."""
    if not isinstance(metrics, Mapping):
        raise ValueError("metrics response is not an object")

    time_series = metrics.get("time_series")
    if not isinstance(time_series, Mapping):
        raise ValueError("metrics time series is invalid")

    series_count = 0
    point_count = 0
    for name, series in time_series.items():
        if series_count >= MAX_METRIC_SERIES:
            break
        if not isinstance(name, str) or not isinstance(series, Mapping):
            raise ValueError("metrics series is invalid")
        values = series.get("values")
        if not isinstance(values, list):
            raise ValueError("metrics values are invalid")
        for point in values[:MAX_METRIC_POINTS_PER_SERIES]:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ValueError("metrics point is invalid")
            if point[0] in (None, "") or point[1] is None:
                raise ValueError("metrics point is invalid")
            point_count += 1
        series_count += 1

    safe_metrics = _safe_value(metrics)
    if not time_series:
        return "unknown", safe_metrics
    if point_count == 0:
        return "unknown", safe_metrics
    return "available", safe_metrics


def check_hetzner_server_metrics_status(unique_id, credentials):
    """Check that recent CPU metrics for a Hetzner server are readable."""
    if unique_id in (None, ""):
        return _safe_error("error")
    try:
        token, _options = _credential_context(credentials)
        params = _metrics_options(credentials, "cpu")
        resource_id = quote(str(unique_id), safe="")
        payload = _request_json(f"servers/{resource_id}/metrics", token, params=params)
        metrics = payload.get("metrics")
        status, safe_metrics = _validated_metrics(metrics)
        return status, {"metrics": safe_metrics}
    except requests.exceptions.RequestException as error:
        return _safe_error(classify_http_error(error))
    except (TypeError, ValueError, KeyError):
        return _safe_error("error")
    except Exception:
        return _safe_error("error")


def check_hetzner_load_balancer_metrics_status(unique_id, credentials):
    """Check that recent Load Balancer metrics are readable."""
    if unique_id in (None, ""):
        return _safe_error("error")
    try:
        token, _options = _credential_context(credentials)
        params = _metrics_options(credentials, "open_connections")
        resource_id = quote(str(unique_id), safe="")
        payload = _request_json(
            f"load_balancers/{resource_id}/metrics",
            token,
            params=params,
        )
        metrics = payload.get("metrics")
        status, safe_metrics = _validated_metrics(metrics)
        return status, {"metrics": safe_metrics}
    except requests.exceptions.RequestException as error:
        return _safe_error(classify_http_error(error))
    except (TypeError, ValueError, KeyError):
        return _safe_error("error")
    except Exception:
        return _safe_error("error")


def check_hetzner_action_status(unique_id, credentials):
    """Check a Hetzner action without polling or mutating provider state."""
    return _check_resource("action", unique_id, credentials)


def check_hetzner_object_storage_status(unique_id, credentials):
    """Perform a metadata-only S3 ``HeadBucket`` check for a Hetzner bucket."""
    if unique_id in (None, "") or not isinstance(credentials, Mapping):
        return _safe_error("invalid_access_token")
    access_key = credentials.get("access_key")
    secret_key = credentials.get("secret_key")
    region = str(credentials.get("region") or "").strip().lower()
    bucket = credentials.get("bucket") or unique_id
    if (
        not isinstance(access_key, str)
        or not access_key
        or not isinstance(secret_key, str)
        or not secret_key
        or region not in {"fsn1", "nbg1", "hel1"}
        or not isinstance(bucket, str)
        or not bucket
        or len(bucket) > 255
    ):
        return _safe_error("invalid_access_token")
    try:
        client = boto3.client(
            "s3",
            region_name="us-east-1",
            endpoint_url=f"https://{region}.your-objectstorage.com",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(
                connect_timeout=5,
                read_timeout=15,
                retries={"mode": "standard", "max_attempts": 2},
                signature_version="s3v4",
            ),
        )
        client.head_bucket(Bucket=bucket)
        return "available", {"bucket": {"name": bucket, "region": region}}
    except ClientError as error:
        code = str((error.response.get("Error") or {}).get("Code", ""))
        if code in {"403", "AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"}:
            return _safe_error("invalid_access_token")
        if code in {"404", "NoSuchBucket", "NotFound"}:
            return _safe_error("not_found")
        return _safe_error("error")
    except (BotoCoreError, OSError):
        return _safe_error("error")
    except Exception:
        return _safe_error("error")


# Explicit aliases make the eventual dispatcher tolerant of the common asset
# spelling variants while keeping one implementation per endpoint.
check_hetzner_server_metric_status = check_hetzner_server_metrics_status
check_hetzner_metrics_status = check_hetzner_server_metrics_status
check_hetzner_asset_status = check_hetzner_resource_status


HETZNER_RESOURCE_CHECKS = {
    "server": check_hetzner_server_status,
    "volume": check_hetzner_volume_status,
    "primary_ip": check_hetzner_primary_ip_status,
    "floating_ip": check_hetzner_floating_ip_status,
    "placement_group": check_hetzner_placement_group_status,
    "ssh_key": check_hetzner_ssh_key_status,
    "image": check_hetzner_image_status,
    "snapshot": check_hetzner_snapshot_status,
    "certificate": check_hetzner_certificate_status,
    "firewall": check_hetzner_firewall_status,
    "load_balancer": check_hetzner_load_balancer_status,
    "network": check_hetzner_network_status,
    "location": check_hetzner_location_status,
    "datacenter": check_hetzner_datacenter_status,
    "server_type": check_hetzner_server_type_status,
    "load_balancer_type": check_hetzner_load_balancer_type_status,
    "iso": check_hetzner_iso_status,
    "zone": check_hetzner_zone_status,
    "rrset": check_hetzner_rrset_status,
    "action": check_hetzner_action_status,
    "server_metrics": check_hetzner_server_metrics_status,
    "load_balancer_metrics": check_hetzner_load_balancer_metrics_status,
    "object_storage": check_hetzner_object_storage_status,
}
HETZNER_STATUS_CHECKS = HETZNER_RESOURCE_CHECKS
HETZNER_CHECK_REGISTRY = HETZNER_RESOURCE_CHECKS
CHECK_REGISTRY = HETZNER_RESOURCE_CHECKS


def get_hetzner_check_function(resource_type):
    """Resolve a canonical or aliased asset type to a check function."""
    normalized_type = _normalize_resource_type(resource_type)
    check = HETZNER_RESOURCE_CHECKS.get(normalized_type)
    if not callable(check):
        raise ValueError(f"Unsupported Hetzner asset type: {resource_type}")
    return check


__all__ = [
    "CHECK_REGISTRY",
    "HETZNER_API_BASE",
    "HETZNER_CHECK_REGISTRY",
    "HETZNER_CHECK_TYPES",
    "HETZNER_REQUEST_TIMEOUT_SECONDS",
    "HETZNER_RESOURCE_CHECKS",
    "HETZNER_RESOURCE_ENDPOINTS",
    "HETZNER_RESOURCE_TYPES",
    "HETZNER_RESOURCE_TYPE_ALIASES",
    "HETZNER_STATUS_CHECKS",
    "METRICS_DEFAULT_TYPE",
    "METRICS_WINDOW_MINUTES",
    "check_hetzner_action_status",
    "check_hetzner_object_storage_status",
    "check_hetzner_asset_status",
    "check_hetzner_certificate_status",
    "check_hetzner_firewall_status",
    "check_hetzner_floating_ip_status",
    "check_hetzner_image_status",
    "check_hetzner_load_balancer_status",
    "check_hetzner_load_balancer_metrics_status",
    "check_hetzner_network_status",
    "check_hetzner_location_status",
    "check_hetzner_datacenter_status",
    "check_hetzner_server_type_status",
    "check_hetzner_load_balancer_type_status",
    "check_hetzner_iso_status",
    "check_hetzner_zone_status",
    "check_hetzner_rrset_status",
    "check_hetzner_metrics_status",
    "check_hetzner_placement_group_status",
    "check_hetzner_primary_ip_status",
    "check_hetzner_resource_status",
    "check_hetzner_server_metric_status",
    "check_hetzner_server_metrics_status",
    "check_hetzner_server_status",
    "check_hetzner_snapshot_status",
    "check_hetzner_ssh_key_status",
    "check_hetzner_volume_status",
    "get_hetzner_check_function",
]
