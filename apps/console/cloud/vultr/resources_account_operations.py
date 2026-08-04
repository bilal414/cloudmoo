"""Read-only Vultr account, governance, and operations helpers.

This module deliberately sits on top of :mod:`resources_base`.  The base
module owns transport concerns (including the bearer token and the GET-only
request boundary); this module owns the smaller, account-scoped surface and
the provider-payload allowlists.  There are no Django models here on purpose:
the Vultr integration worker that owns the shared registries can attach these
specifications to the appropriate asset models without changing this lane.

The provider has several account-like surfaces whose availability differs by
API version, account permission, or product.  A missing optional endpoint is
therefore represented as ``unsupported``.  It is never converted into an
empty inventory, because doing so could mark locally known assets as gone.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlsplit

from apps.console.cloud.models import CloudInventoryTransientError
from apps.console.cloud.vultr import resources_base as _resources_base
from apps.monitoring.metadata import redact_error_message, redact_sensitive_metadata


# The base module is the only transport implementation.  Re-exporting these
# names makes the dependency visible to callers while keeping this module
# compatible with the base worker's public contract.
VultrClient = _resources_base.VultrClient
VultrResourceSpec = _resources_base.VultrResourceSpec


VULTR_API_BASE = "https://api.vultr.com/v2"
VULTR_STATUS_JSON_URL = "https://status.vultr.com/status.json"
VULTR_DEFAULT_PER_PAGE = 100
VULTR_MAX_PER_PAGE = 100
VULTR_MAX_PAGES = 20
VULTR_MAX_ITEMS = VULTR_MAX_PER_PAGE * VULTR_MAX_PAGES
VULTR_MAX_CURSOR_LENGTH = 512
VULTR_MAX_STRING_LENGTH = 512
VULTR_MAX_NESTED_KEYS = 64
VULTR_MAX_NESTED_ITEMS = 100

# Compatibility names used by provider-neutral inventory code.
DEFAULT_PER_PAGE = VULTR_DEFAULT_PER_PAGE
MAX_PER_PAGE = VULTR_MAX_PER_PAGE
MAX_PAGES_PER_COLLECTION = VULTR_MAX_PAGES
MAX_ITEMS_PER_COLLECTION = VULTR_MAX_ITEMS


# Provider-neutral values are used where the shared asset vocabulary already
# has the right meaning.  Account-only families remain explicitly Vultr
# qualified so they do not silently acquire a cross-provider meaning.
VULTR_ACCOUNT_PROFILE = "vultr_account_profile"
VULTR_ACCOUNT_PLAN = "vultr_account_plan"
VULTR_ACCOUNT_LIMITS = "vultr_account_limits"
VULTR_BILLING_TRANSACTION = "vultr_billing_transaction"
VULTR_ACCOUNT_LOG = "vultr_account_log"
VULTR_IAM_USER = "vultr_iam_user"
VULTR_API_KEY_METADATA = "vultr_api_key_metadata"
VULTR_BGP_SESSION = "vultr_bgp_session"
VULTR_BANDWIDTH_METRIC = "vultr_bandwidth_metric"
VULTR_OPERATION = "vultr_operation"
VULTR_STATUS_INCIDENT = "vultr_status_incident"
VULTR_SUPPORT_STATUS = "vultr_support_status"
VULTR_ACTION = "action"


VULTR_ACCOUNT_OPERATIONS_ASSET_TYPES = frozenset(
    {
        VULTR_ACCOUNT_PROFILE,
        VULTR_ACCOUNT_PLAN,
        VULTR_ACCOUNT_LIMITS,
        VULTR_BILLING_TRANSACTION,
        VULTR_ACCOUNT_LOG,
        VULTR_IAM_USER,
        VULTR_API_KEY_METADATA,
        VULTR_BGP_SESSION,
        VULTR_BANDWIDTH_METRIC,
        VULTR_OPERATION,
        VULTR_STATUS_INCIDENT,
        VULTR_SUPPORT_STATUS,
        VULTR_ACTION,
    }
)


class VultrAccountOperationsError(CloudInventoryTransientError):
    """Base class for credential-free, bounded account-operation failures."""


class VultrCredentialsUnavailable(VultrAccountOperationsError):
    """The caller did not provide an ephemeral API token."""


class VultrUnsupportedEndpoint(VultrAccountOperationsError):
    """A requested account surface has no supported documented GET endpoint."""


class VultrPartialCollection(VultrAccountOperationsError):
    """A collection stopped at a safety boundary after returning some records."""


class VultrInvalidResponse(VultrAccountOperationsError):
    """The provider response cannot safely be interpreted as inventory."""


class VultrResourceSpecCompatibilityError(VultrAccountOperationsError):
    """The shared base spec contract could not represent this read-only lane."""


# These are the only account-operation paths this module can ask the base
# client to read.  A caller cannot turn a collection helper into an arbitrary
# URL fetch by passing an endpoint string through a public function.
VULTR_DOCUMENTED_GET_ENDPOINTS = {
    "account": "account",
    "account_plan": "account/plan",
    "account_limits": "account/limits",
    "account_bandwidth": "account/bandwidth",
    "account_transactions": "account/transactions",
    "account_log": "account/log",
    "account_users": "account/users",
    "bgp_sessions": "bgp",
}


def _spec(
    key: str,
    endpoint: str,
    collection_key: str,
    asset_type: str,
    *,
    identifier_fields: Sequence[str] = ("id",),
    name_fields: Sequence[str] = ("name", "label", "id"),
    monitoring_default: str | None = None,
) -> Any:
    """Construct a base spec without coupling to implementation details.

    The base worker's public contract intentionally allows the implementation
    to evolve.  The common keyword names below are the stable contract.  A
    small signature filter keeps this module usable with a dataclass-style
    base as well as the equivalent immutable class used by the integration
    worker; it does not provide a local replacement for the base class.
    """

    values: dict[str, Any] = {
        "key": key,
        "endpoint": endpoint,
        "collection_key": collection_key,
        # Account-operation surfaces are response helpers rather than Django
        # inventory models. The shared spec still requires a model slot, so
        # retain the canonical asset type there and let integration skip
        # non-model specs safely.
        "model": asset_type,
        "asset_type": asset_type,
        "identifier_fields": tuple(identifier_fields),
        "name_fields": tuple(name_fields),
    }
    if monitoring_default is not None:
        values["monitoring_default"] = monitoring_default

    try:
        signature = inspect.signature(VultrResourceSpec)
        parameters = signature.parameters
    except (TypeError, ValueError):
        parameters = {}

    accepts_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    if not accepts_kwargs and parameters:
        values = {
            name: value
            for name, value in values.items()
            if name in parameters
        }

    try:
        return VultrResourceSpec(**values)
    except TypeError as error:
        # A positional fallback supports a base implementation that exposes
        # the same public fields through a hand-written constructor.  It still
        # uses the imported base class and never creates a substitute spec.
        try:
            return VultrResourceSpec(
                key,
                endpoint,
                collection_key,
                asset_type,
                identifier_fields=tuple(identifier_fields),
                name_fields=tuple(name_fields),
            )
        except TypeError as positional_error:
            raise VultrResourceSpecCompatibilityError(
                "Vultr resource base does not expose the account-operation spec contract"
            ) from positional_error


VULTR_ACCOUNT_OPERATION_SPECS = {
    "account_profile": _spec(
        "account_profile",
        VULTR_DOCUMENTED_GET_ENDPOINTS["account"],
        "account",
        VULTR_ACCOUNT_PROFILE,
        identifier_fields=("id", "account_id"),
        name_fields=("name", "account_name", "id"),
    ),
    "account_plan": _spec(
        "account_plan",
        VULTR_DOCUMENTED_GET_ENDPOINTS["account_plan"],
        "plan",
        VULTR_ACCOUNT_PLAN,
        identifier_fields=("id", "code", "name"),
        name_fields=("name", "code", "id"),
    ),
    "account_limits": _spec(
        "account_limits",
        VULTR_DOCUMENTED_GET_ENDPOINTS["account_limits"],
        "limits",
        VULTR_ACCOUNT_LIMITS,
        identifier_fields=("id", "account_id"),
        name_fields=("name", "id"),
    ),
    "account_bandwidth": _spec(
        "account_bandwidth",
        VULTR_DOCUMENTED_GET_ENDPOINTS["account_bandwidth"],
        "bandwidth",
        VULTR_BANDWIDTH_METRIC,
        identifier_fields=("id", "date", "timestamp"),
        name_fields=("metric", "date", "id"),
    ),
    "billing_transactions": _spec(
        "billing_transactions",
        VULTR_DOCUMENTED_GET_ENDPOINTS["account_transactions"],
        "transactions",
        VULTR_BILLING_TRANSACTION,
        identifier_fields=("id", "transaction_id"),
        name_fields=("type", "description", "id"),
    ),
    "account_logs": _spec(
        "account_logs",
        VULTR_DOCUMENTED_GET_ENDPOINTS["account_log"],
        "logs",
        VULTR_ACCOUNT_LOG,
        identifier_fields=("id", "log_id"),
        name_fields=("action", "event", "type", "id"),
    ),
    "iam_users": _spec(
        "iam_users",
        VULTR_DOCUMENTED_GET_ENDPOINTS["account_users"],
        "users",
        VULTR_IAM_USER,
        identifier_fields=("id", "user_id"),
        name_fields=("name", "username", "role", "id"),
    ),
    "bgp_sessions": _spec(
        "bgp_sessions",
        VULTR_DOCUMENTED_GET_ENDPOINTS["bgp_sessions"],
        "bgp_sessions",
        VULTR_BGP_SESSION,
        identifier_fields=("id", "session_id"),
        name_fields=("name", "state", "status", "id"),
    ),
}

# Public aliases used by integrations that group resource specs by provider.
VULTR_ACCOUNT_OPERATIONS_RESOURCE_SPECS = VULTR_ACCOUNT_OPERATION_SPECS
RESOURCE_SPECS = VULTR_ACCOUNT_OPERATION_SPECS


VULTR_UNSUPPORTED_ACCOUNT_SURFACES = {
    VULTR_API_KEY_METADATA: {
        "status": "unsupported",
        "reason": "no documented safe GET endpoint exposes API-key metadata",
        "endpoint": None,
    },
    VULTR_SUPPORT_STATUS: {
        "status": "unsupported",
        "reason": "support-ticket data is private and no safe documented GET surface is configured",
        "endpoint": None,
    },
    "account_cost_detail": {
        "status": "unsupported",
        "reason": "detailed cost data is not collected without a documented safe GET response",
        "endpoint": None,
    },
    "account_action_listing": {
        "status": "unsupported",
        "reason": "unscoped action listing is not used; action status requires a known parent resource",
        "endpoint": None,
    },
}


# Fields are deliberately narrow.  These lists are not a promise that every
# Vultr API version returns every field; unknown fields are dropped.
_SAFE_FIELDS: dict[str, tuple[str, ...]] = {
    VULTR_ACCOUNT_PROFILE: (
        "id",
        "account_id",
        "name",
        "status",
        "plan",
        "plan_code",
        "created_at",
        "updated_at",
        "balance",
        "pending_charges",
        "currency",
    ),
    VULTR_ACCOUNT_PLAN: (
        "id",
        "code",
        "name",
        "type",
        "status",
        "monthly_cost",
        "currency",
        "included_bandwidth",
        "included_storage",
        "included_cpu",
        "max_instances",
        "max_vpcs",
        "max_vpcs_per_region",
        "max_firewalls",
        "max_rules_per_firewall",
        "max_load_balancers",
        "max_block_storage",
        "max_block_storage_size",
        "max_snapshots",
        "max_ssh_keys",
        "max_users",
    ),
    VULTR_ACCOUNT_LIMITS: (
        "id",
        "account_id",
        "max_instances",
        "max_vpcs",
        "max_vpcs_per_region",
        "max_firewalls",
        "max_rules_per_firewall",
        "max_load_balancers",
        "max_block_storage",
        "max_block_storage_size",
        "max_snapshots",
        "max_ssh_keys",
        "max_users",
        "current_instances",
        "current_vpcs",
        "current_firewalls",
        "current_load_balancers",
        "current_block_storage",
        "current_snapshots",
        "current_ssh_keys",
        "current_users",
    ),
    VULTR_BILLING_TRANSACTION: (
        "id",
        "transaction_id",
        "type",
        "status",
        "description",
        "amount",
        "currency",
        "balance",
        "date",
        "created_at",
        "updated_at",
        "invoice_id",
    ),
    VULTR_ACCOUNT_LOG: (
        "id",
        "log_id",
        "date",
        "timestamp",
        "created_at",
        "action",
        "event",
        "type",
        "resource_type",
        "resource_id",
        "status",
        "success",
        "actor_type",
        "actor_id",
        "description",
    ),
    VULTR_IAM_USER: (
        "id",
        "user_id",
        "name",
        "username",
        "role",
        "roles",
        "permissions",
        "status",
        "enabled",
        "created_at",
        "updated_at",
        "last_login",
    ),
    VULTR_BGP_SESSION: (
        "id",
        "session_id",
        "name",
        "status",
        "state",
        "local_asn",
        "remote_asn",
        "asn",
        "region",
        "resource_id",
        "created_at",
        "updated_at",
    ),
    VULTR_BANDWIDTH_METRIC: (
        "id",
        "metric",
        "unit",
        "value",
        "values",
        "date",
        "start",
        "end",
        "timestamp",
        "bandwidth",
        "in",
        "out",
        "received",
        "sent",
        "total",
        "instances",
        "regions",
    ),
    VULTR_OPERATION: (
        "id",
        "action_id",
        "operation_id",
        "status",
        "state",
        "progress",
        "type",
        "action",
        "resource_id",
        "created_at",
        "updated_at",
        "completed_at",
        "error_code",
    ),
}


_DROP_FIELD_PARTS = (
    "api_key",
    "apikey",
    "access_token",
    "authorization",
    "bearer",
    "credential",
    "cookie",
    "header",
    "password",
    "private_key",
    "secret",
    "signed_url",
    "signature",
    "token",
)
_DROP_FIELD_NAMES = frozenset(
    {
        "body",
        "headers",
        "request",
        "request_headers",
        "response",
        "response_headers",
        "raw",
        "query",
        "query_string",
        "url",
        "uri",
        "href",
        "shortlink",
        "email",
        "phone",
        "address",
        "customer",
        "payment",
        "card",
    }
)
_URL_PATTERN = re.compile(r"(?i)https?://[^\s<>\"']+")
_QUERY_SECRET_PATTERN = re.compile(
    r"(?i)([?&](?:api[_-]?key|access[_-]?token|authorization|credential|password|secret|signature|sig|token|x-api-key|x-amz-[^=]+)=)[^&#\s]+"
)
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[^\s,;]+")


def _base_redact(value: Any) -> Any:
    """Use the base redactor when it exposes one, then the shared redactor."""

    result = value
    for name in (
        "redact_vultr_payload",
        "redact_payload",
        "redact_sensitive_metadata",
        "redact",
    ):
        redactor = getattr(_resources_base, name, None)
        if callable(redactor):
            try:
                result = redactor(result)
            except Exception:
                # The local allowlist below remains the final boundary.  Do
                # not expose a redactor exception or its provider payload.
                result = value
            break
    return redact_sensitive_metadata(result)


def _safe_text(value: Any) -> str:
    text = redact_error_message(value)
    text = _BEARER_PATTERN.sub("Bearer [REDACTED]", text)
    text = _QUERY_SECRET_PATTERN.sub(r"\1[REDACTED]", text)
    # A signed URL is not useful account metadata.  Remove the whole URL so
    # a future signing parameter cannot be persisted by accident.
    text = _URL_PATTERN.sub("[URL_REDACTED]", text)
    return text[:VULTR_MAX_STRING_LENGTH]


def _field_is_dropped(field: Any) -> bool:
    normalized = str(field).strip().lower().replace("-", "_")
    if normalized in _DROP_FIELD_NAMES:
        return True
    return any(part in normalized for part in _DROP_FIELD_PARTS)


def _safe_value(value: Any, *, field: str = "", depth: int = 0) -> Any:
    if depth > 5 or _field_is_dropped(field):
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return _safe_text(value)
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (child_key, child_value) in enumerate(value.items()):
            if index >= VULTR_MAX_NESTED_KEYS or _field_is_dropped(child_key):
                continue
            safe_child = _safe_value(
                child_value,
                field=str(child_key),
                depth=depth + 1,
            )
            if safe_child is not None:
                result[str(child_key)[:VULTR_MAX_STRING_LENGTH]] = safe_child
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        result = []
        for child in list(value)[:VULTR_MAX_NESTED_ITEMS]:
            safe_child = _safe_value(child, field=field, depth=depth + 1)
            if safe_child is not None:
                result.append(safe_child)
        return result
    # Provider SDK objects must not cross the persistence boundary.
    return _safe_text(value)


def _safe_record(asset_type: str, value: Mapping[str, Any]) -> dict[str, Any]:
    allowed = _SAFE_FIELDS.get(asset_type, ())
    selected: dict[str, Any] = {}
    for field in allowed:
        if field not in value or _field_is_dropped(field):
            continue
        safe_value = _safe_value(value[field], field=field)
        if safe_value is not None:
            selected[field] = safe_value
    # Re-run the shared/base redaction after allowlisting because a provider
    # may put credential-like text inside an otherwise safe description.
    redacted = _base_redact(selected)
    if not isinstance(redacted, Mapping):
        return {}
    return dict(_safe_value(redacted) or {})


def safe_vultr_record(asset_type: str, value: Mapping[str, Any]) -> dict[str, Any]:
    """Public, non-persisting payload sanitizer for provider records."""

    if not isinstance(value, Mapping):
        raise VultrInvalidResponse("Vultr returned an invalid account-operation record")
    return _safe_record(asset_type, value)


def _require_token(credentials: Any) -> str:
    if isinstance(credentials, str):
        token = credentials
    elif isinstance(credentials, Mapping):
        token = (
            credentials.get("access_token")
            or credentials.get("api_token")
            or credentials.get("token")
        )
    else:
        token = getattr(credentials, "access_token", None) or getattr(
            credentials, "api_token", None
        )
    if not isinstance(token, str) or not token.strip():
        raise VultrCredentialsUnavailable("Vultr account-operation credentials are unavailable")
    if len(token) > 4096:
        raise VultrCredentialsUnavailable("Vultr account-operation credentials are invalid")
    return token.strip()


def vultr_client_from_credentials(credentials: Any) -> Any:
    """Create an ephemeral base client without returning the token."""

    return VultrClient(_require_token(credentials))


def _endpoint_path(endpoint: str) -> str:
    if not isinstance(endpoint, str):
        raise VultrInvalidResponse("Vultr account-operation endpoint is invalid")
    normalized = endpoint.strip().strip("/")
    if (
        not normalized
        or "?" in normalized
        or "#" in normalized
        or "//" in normalized
        or any(part in {".", ".."} for part in normalized.split("/"))
    ):
        raise VultrInvalidResponse("Vultr account-operation endpoint is invalid")
    return normalized


def _spec_key(spec_or_key: Any) -> str:
    if isinstance(spec_or_key, str):
        key = spec_or_key
    else:
        key = getattr(spec_or_key, "key", None)
    if not isinstance(key, str) or key not in VULTR_ACCOUNT_OPERATION_SPECS:
        raise VultrUnsupportedEndpoint("Vultr account-operation surface is unsupported")
    return key


def _spec_value(spec_or_key: Any) -> Any:
    return VULTR_ACCOUNT_OPERATION_SPECS[_spec_key(spec_or_key)]


def _response_json(response: Any) -> Mapping[str, Any]:
    if isinstance(response, Mapping):
        return response
    json_method = getattr(response, "json", None)
    if callable(json_method):
        try:
            payload = json_method()
        except Exception as error:
            raise VultrInvalidResponse("Vultr returned invalid JSON") from error
        if isinstance(payload, Mapping):
            return payload
    raise VultrInvalidResponse("Vultr returned an invalid account-operation response")


def _get(client: Any, endpoint: str, params: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    """Call only the base client's GET method with a fixed endpoint path."""

    endpoint = _endpoint_path(endpoint)
    if endpoint not in set(VULTR_DOCUMENTED_GET_ENDPOINTS.values()) and not (
        endpoint.startswith("instances/")
        or endpoint.startswith("blocks/")
        or endpoint.startswith("databases/")
        or endpoint.startswith("bare-metals/")
        or endpoint.startswith("load-balancers/")
        or endpoint.startswith("kubernetes/clusters/")
    ):
        raise VultrUnsupportedEndpoint("Vultr GET endpoint is outside the allowlist")

    getter = getattr(client, "get", None)
    if not callable(getter):
        raise VultrInvalidResponse("Vultr base client does not expose GET")
    request_params = dict(params or {})
    try:
        response = getter(endpoint, params=request_params)
    except TypeError as first_error:
        # Some small test doubles implement the same public contract with a
        # positional params argument.  Do not fall back to another verb.
        try:
            response = getter(endpoint, request_params)
        except TypeError:
            raise first_error
    return _response_json(response)


def _status_code(error: BaseException) -> int | None:
    response = getattr(error, "response", None)
    for candidate in (
        getattr(response, "status_code", None),
        getattr(error, "status_code", None),
        getattr(error, "code", None),
    ):
        if isinstance(candidate, int):
            return candidate
        if isinstance(candidate, str) and candidate.isdigit():
            return int(candidate)
    return None


def vultr_error_code(error: BaseException) -> str:
    """Return a stable error category without interpolating provider text."""

    if isinstance(error, VultrCredentialsUnavailable):
        return "credentials_unavailable"
    if isinstance(error, VultrUnsupportedEndpoint):
        return "unsupported_endpoint"
    if isinstance(error, VultrPartialCollection):
        return "pagination_bound"
    status_code = _status_code(error)
    if status_code in (401, 403):
        return "invalid_access_token"
    if status_code == 404:
        return "not_found"
    if status_code == 429:
        return "rate_limited"
    if status_code is not None and status_code >= 500:
        return "provider_unavailable"
    return "provider_error"


def _unsupported_result(surface: str, *, asset_type: str | None = None) -> dict[str, Any]:
    details = VULTR_UNSUPPORTED_ACCOUNT_SURFACES.get(surface, {})
    result: dict[str, Any] = {
        "status": "unsupported",
        "partial": False,
        "asset_type": asset_type or surface,
        "records": [],
        "items": [],
        "reason": str(details.get("reason") or "no supported GET endpoint"),
    }
    endpoint = details.get("endpoint")
    if endpoint:
        result["endpoint"] = endpoint
    return result


def unsupported_vultr_surface(surface: str) -> dict[str, Any]:
    """Return an explicit unsupported marker without constructing a client."""

    return _unsupported_result(surface)


def _result(
    *,
    status: str,
    asset_type: str,
    endpoint: str | None,
    records: Iterable[Mapping[str, Any]] = (),
    reason: str | None = None,
    error_code: str | None = None,
    partial: bool = False,
) -> dict[str, Any]:
    safe_records = [
        dict(_safe_value(record) or {})
        for record in list(records)[:VULTR_MAX_ITEMS]
        if isinstance(record, Mapping)
    ]
    payload: dict[str, Any] = {
        "status": status,
        "partial": bool(partial or status == "partial"),
        "asset_type": asset_type,
        "records": safe_records,
        # ``items`` is a compatibility spelling for generic inventory code.
        "items": safe_records,
    }
    if endpoint:
        payload["endpoint"] = _endpoint_path(endpoint)
    if reason:
        payload["reason"] = _safe_text(reason)
    if error_code:
        payload["errorCode"] = str(error_code)[:128]
    return payload


def _extract_singleton(
    payload: Mapping[str, Any],
    collection_key: str,
    asset_type: str,
) -> Mapping[str, Any]:
    value = payload.get(collection_key)
    if value is None and collection_key == "plan":
        value = payload.get("account_plan")
    if value is None and collection_key == "limits":
        value = payload.get("account_limits")
    if value is None and collection_key == "bandwidth":
        value = payload.get("account_bandwidth")
    if not isinstance(value, Mapping):
        # The documented single-object endpoints have occasionally returned
        # their object without a wrapper.  Only accept it when at least one
        # allowlisted field is present; an arbitrary response is not an empty
        # account.
        if any(field in payload for field in _SAFE_FIELDS.get(asset_type, ())):
            value = payload
        else:
            raise VultrInvalidResponse(
                "Vultr returned an incomplete account-operation response"
            )
    return value


def _collection_items(
    payload: Mapping[str, Any],
    collection_keys: Sequence[str],
) -> list[Mapping[str, Any]]:
    for collection_key in collection_keys:
        if collection_key in payload:
            values = payload[collection_key]
            if not isinstance(values, list):
                raise VultrInvalidResponse("Vultr returned an invalid account-operation collection")
            if any(not isinstance(value, Mapping) for value in values):
                raise VultrInvalidResponse("Vultr returned an invalid account-operation record")
            return list(values)
    raise VultrInvalidResponse("Vultr returned an incomplete account-operation collection")


def _cursor_from_next(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) > VULTR_MAX_CURSOR_LENGTH:
        raise VultrPartialCollection("Vultr returned an unsafe pagination cursor")
    value = value.strip()
    if not value:
        return None
    if "?" in value or "://" in value:
        parsed = urlsplit(value)
        query = parse_qs(parsed.query, keep_blank_values=True)
        forbidden = set(query) & {
            "api_key",
            "access_token",
            "authorization",
            "credential",
            "key",
            "secret",
            "signature",
            "token",
        }
        if forbidden:
            raise VultrPartialCollection("Vultr returned an unsafe pagination link")
        values = query.get("cursor")
        if not values or not values[0]:
            return None
        value = values[0]
    if "#" in value or "\n" in value or "\r" in value:
        raise VultrPartialCollection("Vultr returned an invalid pagination cursor")
    return value


def _next_cursor(payload: Mapping[str, Any], item_count: int, per_page: int) -> str | None:
    meta = payload.get("meta")
    if meta is None:
        # A short page is a complete bounded response.  A full page without
        # pagination metadata is partial rather than silently truncated.
        if item_count < per_page:
            return None
        raise VultrPartialCollection("Vultr returned incomplete pagination metadata")
    if not isinstance(meta, Mapping):
        raise VultrPartialCollection("Vultr returned invalid pagination metadata")
    links = meta.get("links")
    if links is None:
        if item_count < per_page:
            return None
        raise VultrPartialCollection("Vultr returned incomplete pagination links")
    if not isinstance(links, Mapping):
        raise VultrPartialCollection("Vultr returned invalid pagination links")
    return _cursor_from_next(links.get("next"))


def _validate_bounds(per_page: int, max_pages: int) -> tuple[int, int]:
    if isinstance(per_page, bool) or not isinstance(per_page, int) or not 1 <= per_page <= VULTR_MAX_PER_PAGE:
        raise VultrInvalidResponse("Vultr page size is outside the safety bound")
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= VULTR_MAX_PAGES:
        raise VultrInvalidResponse("Vultr page bound is outside the safety bound")
    return per_page, max_pages


def _collection_result(
    client: Any,
    *,
    endpoint: str,
    collection_keys: Sequence[str],
    asset_type: str,
    per_page: int,
    max_pages: int,
) -> dict[str, Any]:
    per_page, max_pages = _validate_bounds(per_page, max_pages)
    records: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()

    for page_number in range(max_pages):
        params: dict[str, Any] = {"per_page": per_page}
        if cursor:
            params["cursor"] = cursor
        try:
            payload = _get(client, endpoint, params)
            raw_items = _collection_items(payload, collection_keys)
            safe_items = [
                _safe_record(asset_type, item)
                for item in raw_items
            ]
            if len(records) + len(safe_items) > VULTR_MAX_ITEMS:
                records.extend(safe_items[: max(0, VULTR_MAX_ITEMS - len(records))])
                return _result(
                    status="partial",
                    asset_type=asset_type,
                    endpoint=endpoint,
                    records=records,
                    reason="collection_item_bound",
                    partial=True,
                )
            records.extend(safe_items)
            next_cursor = _next_cursor(payload, len(raw_items), per_page)
        except VultrPartialCollection as error:
            if records:
                return _result(
                    status="partial",
                    asset_type=asset_type,
                    endpoint=endpoint,
                    records=records,
                    reason=str(error),
                    partial=True,
                )
            return _result(
                status="error",
                asset_type=asset_type,
                endpoint=endpoint,
                reason="incomplete_pagination",
                error_code="incomplete_pagination",
            )
        except Exception as error:
            if records:
                return _result(
                    status="partial",
                    asset_type=asset_type,
                    endpoint=endpoint,
                    records=records,
                    reason="provider_page_unavailable",
                    error_code=vultr_error_code(error),
                    partial=True,
                )
            code = vultr_error_code(error)
            if code == "not_found" and asset_type in {
                VULTR_ACCOUNT_LOG,
                VULTR_IAM_USER,
                VULTR_BGP_SESSION,
            }:
                return _result(
                    status="unsupported",
                    asset_type=asset_type,
                    endpoint=endpoint,
                    reason="documented GET endpoint is unavailable for this account",
                )
            return _result(
                status="error",
                asset_type=asset_type,
                endpoint=endpoint,
                reason="provider_request_failed",
                error_code=code,
            )

        if not next_cursor:
            return _result(
                status="complete",
                asset_type=asset_type,
                endpoint=endpoint,
                records=records,
            )
        if next_cursor in seen_cursors:
            return _result(
                status="partial" if records else "error",
                asset_type=asset_type,
                endpoint=endpoint,
                records=records,
                reason="repeated_pagination_cursor",
                error_code="invalid_pagination",
                partial=bool(records),
            )
        seen_cursors.add(next_cursor)
        if page_number + 1 >= max_pages:
            return _result(
                status="partial",
                asset_type=asset_type,
                endpoint=endpoint,
                records=records,
                reason="pagination_page_bound",
                error_code="pagination_bound",
                partial=True,
            )
        cursor = next_cursor

    return _result(
        status="partial",
        asset_type=asset_type,
        endpoint=endpoint,
        records=records,
        reason="pagination_page_bound",
        error_code="pagination_bound",
        partial=True,
    )


def _singleton_result(client: Any, spec_key: str) -> dict[str, Any]:
    spec = _spec_value(spec_key)
    endpoint = getattr(spec, "endpoint", None) or VULTR_DOCUMENTED_GET_ENDPOINTS.get(spec_key)
    endpoint = _endpoint_path(endpoint)
    collection_key = str(getattr(spec, "collection_key", ""))
    asset_type = str(getattr(spec, "asset_type", None) or spec_key)
    try:
        payload = _get(client, endpoint)
        value = _extract_singleton(payload, collection_key, asset_type)
        record = _safe_record(asset_type, value)
        if not record:
            raise VultrInvalidResponse("Vultr returned no safe account-operation fields")
        return _result(
            status="complete",
            asset_type=asset_type,
            endpoint=endpoint,
            records=(record,),
        )
    except Exception as error:
        code = vultr_error_code(error)
        if code == "not_found" and spec_key in {"account_plan", "account_limits"}:
            return _result(
                status="unsupported",
                asset_type=asset_type,
                endpoint=endpoint,
                reason="documented GET endpoint is unavailable for this account",
            )
        return _result(
            status="error",
            asset_type=asset_type,
            endpoint=endpoint,
            reason="provider_request_failed" if code != "provider_error" else "invalid_provider_response",
            error_code=code,
        )


def fetch_vultr_account_operation(
    client: Any,
    resource_key: str,
    *,
    per_page: int = VULTR_DEFAULT_PER_PAGE,
    max_pages: int = VULTR_MAX_PAGES,
) -> dict[str, Any]:
    """Fetch one account-operation surface with explicit completion state."""

    key = _spec_key(resource_key)
    if key in {"account_profile", "account_plan", "account_limits", "account_bandwidth"}:
        return _singleton_result(client, key)
    spec = _spec_value(key)
    endpoint = _endpoint_path(getattr(spec, "endpoint", ""))
    asset_type = str(getattr(spec, "asset_type", key))
    keys = (str(getattr(spec, "collection_key", "")),)
    if key == "account_logs":
        keys = ("logs", "activities", "activity", "log")
    elif key == "bgp_sessions":
        keys = ("bgp_sessions", "sessions", "bgp")
    return _collection_result(
        client,
        endpoint=endpoint,
        collection_keys=keys,
        asset_type=asset_type,
        per_page=per_page,
        max_pages=max_pages,
    )


def collect_vultr_account_operations(
    client: Any,
    resource_keys: Iterable[str] | None = None,
    *,
    per_page: int = VULTR_DEFAULT_PER_PAGE,
    max_pages: int = VULTR_MAX_PAGES,
) -> dict[str, dict[str, Any]]:
    """Collect explicit account-operation specs without mutating provider state."""

    keys = list(resource_keys or VULTR_ACCOUNT_OPERATION_SPECS)
    results: dict[str, dict[str, Any]] = {}
    for key in keys:
        normalized = _spec_key(key)
        results[normalized] = fetch_vultr_account_operation(
            client,
            normalized,
            per_page=per_page,
            max_pages=max_pages,
        )
    return results


def _client_and_key(first: Any, second: Any) -> tuple[Any, str]:
    if isinstance(first, str) and not isinstance(second, str):
        return second, _spec_key(first)
    return first, _spec_key(second)


def list_vultr_account_operation_records(
    client_or_key: Any,
    resource_key_or_client: Any,
    *,
    per_page: int = VULTR_DEFAULT_PER_PAGE,
    max_pages: int = VULTR_MAX_PAGES,
) -> dict[str, Any]:
    """Compatibility wrapper accepting either ``(client, key)`` or ``(key, client)``."""

    client, key = _client_and_key(client_or_key, resource_key_or_client)
    return fetch_vultr_account_operation(
        client,
        key,
        per_page=per_page,
        max_pages=max_pages,
    )


def get_vultr_account_profile(client: Any) -> dict[str, Any]:
    return fetch_vultr_account_operation(client, "account_profile")


def get_vultr_account_plan(client: Any) -> dict[str, Any]:
    return fetch_vultr_account_operation(client, "account_plan")


def get_vultr_account_limits(client: Any) -> dict[str, Any]:
    return fetch_vultr_account_operation(client, "account_limits")


def get_vultr_account_bandwidth(client: Any) -> dict[str, Any]:
    return fetch_vultr_account_operation(client, "account_bandwidth")


def list_vultr_billing_transactions(
    client: Any,
    *,
    per_page: int = VULTR_DEFAULT_PER_PAGE,
    max_pages: int = VULTR_MAX_PAGES,
) -> dict[str, Any]:
    return fetch_vultr_account_operation(
        client,
        "billing_transactions",
        per_page=per_page,
        max_pages=max_pages,
    )


def list_vultr_account_logs(
    client: Any,
    *,
    per_page: int = VULTR_DEFAULT_PER_PAGE,
    max_pages: int = VULTR_MAX_PAGES,
) -> dict[str, Any]:
    return fetch_vultr_account_operation(
        client,
        "account_logs",
        per_page=per_page,
        max_pages=max_pages,
    )


def list_vultr_iam_users(
    client: Any,
    *,
    per_page: int = VULTR_DEFAULT_PER_PAGE,
    max_pages: int = VULTR_MAX_PAGES,
) -> dict[str, Any]:
    return fetch_vultr_account_operation(
        client,
        "iam_users",
        per_page=per_page,
        max_pages=max_pages,
    )


def list_vultr_bgp_sessions(
    client: Any,
    *,
    per_page: int = VULTR_DEFAULT_PER_PAGE,
    max_pages: int = VULTR_MAX_PAGES,
) -> dict[str, Any]:
    return fetch_vultr_account_operation(
        client,
        "bgp_sessions",
        per_page=per_page,
        max_pages=max_pages,
    )


def _safe_identifier(value: Any, label: str) -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise VultrInvalidResponse(f"Vultr {label} is invalid")
    text = str(value).strip()
    if not text or len(text) > 255 or "/" in text or "?" in text or "#" in text:
        raise VultrInvalidResponse(f"Vultr {label} is invalid")
    return text


_ACTION_PARENT_ENDPOINTS = {
    "instance": "instances",
    "instances": "instances",
    "server": "instances",
    "block": "blocks",
    "blocks": "blocks",
    "database": "databases",
    "databases": "databases",
    "bare_metal": "bare-metals",
    "bare_metals": "bare-metals",
    "load_balancer": "load-balancers",
    "load_balancers": "load-balancers",
    "kubernetes_cluster": "kubernetes/clusters",
    "kubernetes_clusters": "kubernetes/clusters",
}


def _action_endpoint(resource_type: Any, resource_id: Any, action_id: Any) -> str:
    normalized_type = str(resource_type or "instance").strip().lower().replace("-", "_")
    parent = _ACTION_PARENT_ENDPOINTS.get(normalized_type)
    if not parent:
        raise VultrUnsupportedEndpoint("Vultr action parent type is unsupported")
    safe_resource_id = _safe_identifier(resource_id, "action resource identifier")
    safe_action_id = _safe_identifier(action_id, "action identifier")
    return f"{parent}/{safe_resource_id}/actions/{safe_action_id}"


def get_vultr_action_status(
    client: Any,
    resource_id: Any,
    action_id: Any,
    *,
    resource_type: str = "instance",
) -> dict[str, Any]:
    """Read one action scoped to a known parent resource.

    Account-wide action enumeration is intentionally unsupported.  A caller
    must supply both the parent resource ID and the action ID so this helper
    cannot accidentally poll an unrelated operation.
    """

    endpoint = _action_endpoint(resource_type, resource_id, action_id)
    try:
        payload = _get(client, endpoint)
        value = payload.get("action") or payload.get("operation")
        if not isinstance(value, Mapping):
            if any(field in payload for field in _SAFE_FIELDS[VULTR_OPERATION]):
                value = payload
            else:
                raise VultrInvalidResponse("Vultr returned an incomplete action response")
        record = _safe_record(VULTR_OPERATION, value)
        if not record:
            raise VultrInvalidResponse("Vultr returned no safe action fields")
        return _result(
            status="complete",
            asset_type=VULTR_ACTION,
            endpoint=endpoint,
            records=(record,),
        )
    except Exception as error:
        return _result(
            status="error",
            asset_type=VULTR_ACTION,
            endpoint=endpoint,
            reason="provider_request_failed",
            error_code=vultr_error_code(error),
        )


def get_vultr_operation_status(
    client: Any,
    resource_id: Any,
    operation_id: Any,
    *,
    resource_type: str = "instance",
) -> dict[str, Any]:
    return get_vultr_action_status(
        client,
        resource_id,
        operation_id,
        resource_type=resource_type,
    )


def get_vultr_api_key_metadata(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    """Explicitly decline API-key retrieval; key material is never a resource."""

    return _unsupported_result(VULTR_API_KEY_METADATA, asset_type=VULTR_API_KEY_METADATA)


def get_vultr_support_status(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return _unsupported_result(VULTR_SUPPORT_STATUS, asset_type=VULTR_SUPPORT_STATUS)


# Compatibility aliases for callers that use ``fetch`` naming.
fetch_vultr_account_profile = get_vultr_account_profile
fetch_vultr_account_plan = get_vultr_account_plan
fetch_vultr_account_limits = get_vultr_account_limits
fetch_vultr_account_bandwidth = get_vultr_account_bandwidth
fetch_vultr_account_logs = list_vultr_account_logs
fetch_vultr_billing_transactions = list_vultr_billing_transactions
fetch_vultr_iam_users = list_vultr_iam_users
fetch_vultr_bgp_sessions = list_vultr_bgp_sessions
check_vultr_action = get_vultr_action_status


__all__ = [
    "DEFAULT_PER_PAGE",
    "MAX_ITEMS_PER_COLLECTION",
    "MAX_PAGES_PER_COLLECTION",
    "MAX_PER_PAGE",
    "RESOURCE_SPECS",
    "VULTR_ACCOUNT_OPERATION_SPECS",
    "VULTR_ACCOUNT_OPERATIONS_ASSET_TYPES",
    "VULTR_ACCOUNT_OPERATIONS_RESOURCE_SPECS",
    "VULTR_ACCOUNT_PROFILE",
    "VULTR_ACCOUNT_PLAN",
    "VULTR_ACCOUNT_LIMITS",
    "VULTR_ACCOUNT_LOG",
    "VULTR_API_KEY_METADATA",
    "VULTR_BGP_SESSION",
    "VULTR_BANDWIDTH_METRIC",
    "VULTR_BILLING_TRANSACTION",
    "VULTR_DOCUMENTED_GET_ENDPOINTS",
    "VULTR_IAM_USER",
    "VULTR_MAX_ITEMS",
    "VULTR_MAX_PAGES",
    "VULTR_MAX_PER_PAGE",
    "VULTR_OPERATION",
    "VULTR_STATUS_INCIDENT",
    "VULTR_SUPPORT_STATUS",
    "VULTR_UNSUPPORTED_ACCOUNT_SURFACES",
    "VultrAccountOperationsError",
    "VultrClient",
    "VultrCredentialsUnavailable",
    "VultrInvalidResponse",
    "VultrPartialCollection",
    "VultrResourceSpec",
    "VultrUnsupportedEndpoint",
    "collect_vultr_account_operations",
    "fetch_vultr_account_operation",
    "get_vultr_account_bandwidth",
    "get_vultr_account_limits",
    "get_vultr_account_plan",
    "get_vultr_account_profile",
    "get_vultr_action_status",
    "get_vultr_api_key_metadata",
    "get_vultr_operation_status",
    "get_vultr_support_status",
    "list_vultr_account_logs",
    "list_vultr_account_operation_records",
    "list_vultr_bgp_sessions",
    "list_vultr_billing_transactions",
    "list_vultr_iam_users",
    "safe_vultr_record",
    "unsupported_vultr_surface",
    "vultr_client_from_credentials",
    "vultr_error_code",
]
