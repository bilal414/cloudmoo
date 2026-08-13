"""Shared, read-only foundations for the additional Vultr inventory surface.

The legacy Vultr models predate the inventory safety boundary used by the
newer provider adapters.  This module is intentionally independent of those
sync methods: it only exposes bounded ``GET`` requests, validates the exact
Vultr response envelopes, and reconciles a complete collection one model at a
time.  A malformed page is never treated as an empty collection.

Only endpoint shapes that are known to be part of the Vultr v2 API are
accepted by :class:`VultrReadOnlyClient`.  Resource modules can declare a
family as unsupported without inventing an endpoint; unsupported resources
therefore cannot accidentally make a network request or mark local assets as
missing.
"""

from __future__ import annotations

import copy
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Callable, Iterator
from urllib.parse import quote, unquote, urlsplit

import requests
from django.db import models

from apps.console.cloud.models import CloudInventoryTransientError
from apps.console.cloud.vultr.models import CoreVultrAccount
from apps.console.utils.models import UtilAsset


VULTR_API_BASE = "https://api.vultr.com/v2"
VULTR_CONSOLE_BASE = "https://my.vultr.com"
VULTR_TIMEOUT_SECONDS = 15
VULTR_DEFAULT_PER_PAGE = 100
VULTR_MAX_PER_PAGE = 500
VULTR_MAX_PAGES = 100
VULTR_MAX_ITEMS = 10000
VULTR_MAX_RETRIES = 2
VULTR_RETRY_BACKOFF_SECONDS = 0.25
VULTR_MAX_CURSOR_LENGTH = 1024
VULTR_MAX_RESOURCE_ID_LENGTH = 255
VULTR_ALLOWED_QUERY_PARAMS = frozenset({"per_page", "cursor"})


class VultrInventoryError(CloudInventoryTransientError):
    """A safe, credential-free error from the Vultr inventory adapter."""


class VultrAPIError(VultrInventoryError):
    """A bounded API failure whose status code is safe to inspect."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


# These are deliberately broader than the existing shared redactor.  Vultr
# resources can contain user-data, kubeconfigs, VNC links, and signed download
# URLs in addition to ordinary token/password fields.
_SENSITIVE_KEY_PARTS = (
    "password",
    "secret",
    "token",
    "credential",
    "authorization",
    "apikey",
    "accesskey",
    "privatekey",
    "publickey",
    "kubeconfig",
    "userdat",
    "vnc",
    "signature",
    "presigned",
    "signedurl",
    "temporaryurl",
)
_SIGNED_URL_PATTERN = re.compile(
    r"(?i)(?:[?&](?:x-amz-(?:algorithm|credential|date|expires|signature|security-token)|"
    r"(?:aws_)?(?:signature|expires|security[_-]?token)|sig|token)=)"
)
_ID_COMPONENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")


def _normalized_key(key: Any) -> str:
    return "".join(character for character in str(key).lower() if character.isalnum())


def _is_sensitive_key(key: Any) -> bool:
    normalized = _normalized_key(key)
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _is_signed_url(value: str) -> bool:
    if not value.lower().startswith(("http://", "https://")):
        return False
    return bool(_SIGNED_URL_PATTERN.search(value))


def redact_vultr_metadata(value: Any, *, _depth: int = 0) -> Any:
    """Recursively redact Vultr credential-like and signed-link fields.

    The function returns a new value and never mutates a provider response or
    a caller-owned object.  It is intentionally usable for both persisted
    model metadata and monitoring responses.
    """

    if _depth > 12:
        return "[TRUNCATED]"
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, child in value.items():
            key_string = str(key)
            if _is_sensitive_key(key_string):
                redacted[key_string] = "[REDACTED]"
            else:
                redacted[key_string] = redact_vultr_metadata(child, _depth=_depth + 1)
        return redacted
    if isinstance(value, (list, tuple)):
        return [redact_vultr_metadata(item, _depth=_depth + 1) for item in value]
    if isinstance(value, str) and _is_signed_url(value):
        return "[REDACTED]"
    return value


# Friendly aliases for code that uses the provider-neutral terminology.
redact_sensitive_metadata = redact_vultr_metadata
redact_vultr_response = redact_vultr_metadata


def validate_vultr_endpoint(endpoint: str) -> str:
    """Validate and return a relative path from the fixed Vultr v2 base.

    This is an allowlist, not merely a path sanitizer.  The ID-bearing forms
    below cover only read-only compute endpoints used by this package.
    """

    if not isinstance(endpoint, str):
        raise VultrInventoryError("Vultr inventory endpoint is unsupported")
    candidate = endpoint.strip()
    if (
        not candidate
        or candidate.startswith("/")
        or "\\" in candidate
        or "?" in candidate
        or "#" in candidate
        or "://" in candidate
    ):
        raise VultrInventoryError("Vultr inventory endpoint is unsupported")
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise VultrInventoryError("Vultr inventory endpoint is unsupported")
    if candidate.startswith("v2/"):
        raise VultrInventoryError("Vultr inventory endpoint is unsupported")

    decoded = unquote(candidate)
    parts = decoded.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise VultrInventoryError("Vultr inventory endpoint is unsupported")
    if len(decoded) > 1024:
        raise VultrInventoryError("Vultr inventory endpoint is unsupported")

    def valid_id(part: str) -> bool:
        return (
            len(part) <= VULTR_MAX_RESOURCE_ID_LENGTH
            and bool(_ID_COMPONENT_PATTERN.fullmatch(part))
            and part not in {".", ".."}
        )

    allowed = False
    if decoded in {
        "account",
        "account/plan",
        "account/limits",
        "account/bandwidth",
        "account/transactions",
        "account/log",
        "account/users",
        "bgp",
        "instances",
        "bare-metals",
        "blocks",
        "blocks/snapshots",
        "backups",
        "plans",
        "databases",
        "load-balancers",
        "vpc2",
        "nat-gateways",
        "firewalls",
        "reserved-ips",
        "domains",
        "cdn",
        "ssl/certificates",
        "kubernetes/clusters",
        "object-storage",
        "object-storage/clusters",
        "object-storage/tiers",
        "registry",
        "registries",
        "inference",
        "regions",
    }:
        allowed = True
    elif len(parts) == 2 and parts[0] in {
        "instances",
        "bare-metals",
        "blocks",
        "backups",
        "plans",
        "databases",
        "load-balancers",
        "vpc2",
        "nat-gateways",
        "firewalls",
        "reserved-ips",
        "domains",
        "cdn",
        "registry",
        "inference",
        "regions",
        "object-storage",
        "object-storage/clusters",
        "object-storage/tiers",
    }:
        allowed = valid_id(parts[1])
    elif len(parts) == 3 and parts[0] == "registry" and parts[2] == "repositories":
        allowed = valid_id(parts[1])
    elif len(parts) == 3 and parts[0] == "object-storage" and parts[1] in {"clusters", "tiers"}:
        allowed = valid_id(parts[2])
    elif len(parts) == 3 and parts[0] in {"firewalls", "domains"} and parts[2] in {"rules", "records"}:
        allowed = valid_id(parts[1])
    elif len(parts) == 3 and parts[0] == "load-balancers" and parts[2] == "health":
        allowed = valid_id(parts[1])
    elif len(parts) == 3 and parts[0] == "inference" and parts[2] == "health":
        allowed = valid_id(parts[1])
    elif len(parts) == 3 and parts[0] == "blocks" and parts[1] == "snapshots":
        allowed = valid_id(parts[2])
    elif len(parts) == 4 and parts[0] in {
        "instances",
        "blocks",
        "databases",
        "bare-metals",
        "load-balancers",
    } and parts[2] == "actions":
        allowed = valid_id(parts[1]) and valid_id(parts[3])
    elif len(parts) == 4 and parts[0] in {"firewalls", "domains"} and parts[2] in {"rules", "records"}:
        allowed = valid_id(parts[1]) and valid_id(parts[3])
    elif len(parts) == 3 and parts[2] == "bandwidth" and parts[0] in {
        "instances",
        "bare-metals",
    }:
        allowed = valid_id(parts[1])
    elif len(parts) == 3 and parts[0] == "kubernetes" and parts[1] == "clusters":
        allowed = valid_id(parts[2])
    elif len(parts) == 4 and parts[:3] == ["kubernetes", "clusters", parts[2]] and parts[3] in {"node-pools", "health"}:
        allowed = valid_id(parts[2])
    elif len(parts) == 5 and parts[:2] == ["kubernetes", "clusters"] and parts[3] == "actions":
        allowed = valid_id(parts[2]) and valid_id(parts[4])
    elif len(parts) == 4 and parts[0] == "registry" and parts[2] == "repositories":
        allowed = valid_id(parts[1]) and valid_id(parts[3])
    elif len(parts) == 5 and parts[0] == "registry" and parts[2] == "repositories" and parts[4] == "artifacts":
        allowed = valid_id(parts[1]) and valid_id(parts[3])
    elif len(parts) == 6 and parts[0] == "registry" and parts[2] == "repositories" and parts[4] == "artifacts":
        allowed = valid_id(parts[1]) and valid_id(parts[3]) and valid_id(parts[5])

    if not allowed:
        raise VultrInventoryError("Vultr inventory endpoint is unsupported")
    return candidate


# Public spelling used by tests and integration code.
allowed_vultr_endpoint = validate_vultr_endpoint


class VultrReadOnlyClient:
    """A bounded GET-only client for the allowlisted Vultr v2 paths."""

    def __init__(
        self,
        access_token: str,
        *,
        timeout: float | tuple[float, float] = VULTR_TIMEOUT_SECONDS,
        max_retries: int = VULTR_MAX_RETRIES,
        retry_backoff: float = VULTR_RETRY_BACKOFF_SECONDS,
        sleep: Callable[[float], Any] | None = None,
        get: Callable[..., Any] | None = None,
    ) -> None:
        if not isinstance(access_token, str) or not access_token.strip():
            raise VultrInventoryError("Vultr credentials are unavailable")
        if isinstance(timeout, bool) or (
            not isinstance(timeout, (int, float, tuple, list))
        ):
            raise VultrInventoryError("Vultr request timeout is invalid")
        if isinstance(timeout, (int, float)) and timeout <= 0:
            raise VultrInventoryError("Vultr request timeout is invalid")
        if isinstance(timeout, (tuple, list)) and (
            len(timeout) != 2
            or any(isinstance(part, bool) or not isinstance(part, (int, float)) or part <= 0 for part in timeout)
        ):
            raise VultrInventoryError("Vultr request timeout is invalid")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or not 0 <= max_retries <= 5:
            raise VultrInventoryError("Vultr retry limit is invalid")
        if isinstance(retry_backoff, bool) or not isinstance(retry_backoff, (int, float)) or retry_backoff < 0:
            raise VultrInventoryError("Vultr retry backoff is invalid")
        if sleep is not None and not callable(sleep):
            raise VultrInventoryError("Vultr retry handler is invalid")

        self._access_token = access_token
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff = float(retry_backoff)
        self._sleep = sleep or time.sleep
        self._get = get or requests.get

    def request(self, method: str, endpoint: str, *, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Reject every method except GET before any transport is touched."""

        if str(method or "").upper() != "GET":
            raise VultrInventoryError("Vultr inventory client only supports GET")
        return self.get_json(endpoint, params=params)

    def get(self, endpoint: str, *, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self.get_json(endpoint, params=params)

    def list_collection(self, spec: "VultrResourceSpec", **kwargs: Any) -> list[dict[str, Any]]:
        """Return normalized records for a declared collection spec."""

        return collect_vultr_resource_records(self, spec, **kwargs)

    def get_json(self, endpoint: str, *, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        path = validate_vultr_endpoint(endpoint)
        request_params = _validate_query_params(params)
        url = f"{VULTR_API_BASE}/{path}"
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        for attempt in range(self.max_retries + 1):
            try:
                response = self._get(
                    url,
                    headers=headers,
                    params=request_params,
                    timeout=self.timeout,
                )
            except requests.RequestException as error:
                # Connection resets and timeouts are transient just like a
                # 429/5xx response. Retry them within the same small bounded
                # budget, while keeping provider/transport details out of the
                # exception that reaches inventory or monitoring persistence.
                if attempt < self.max_retries:
                    if self.retry_backoff:
                        self._sleep(self.retry_backoff * (2 ** attempt))
                    continue
                raise VultrAPIError("Vultr inventory request failed") from error
            except Exception as error:
                # Do not expose transport exception text: custom transports
                # may include URLs, headers, or response bodies.
                raise VultrAPIError("Vultr inventory request failed") from error

            status_code = getattr(response, "status_code", None)
            if isinstance(status_code, int) and (status_code == 429 or status_code >= 500):
                close = getattr(response, "close", None)
                if callable(close):
                    close()
                if attempt < self.max_retries:
                    if self.retry_backoff:
                        self._sleep(self.retry_backoff * (2 ** attempt))
                    continue
                raise VultrAPIError(
                    "Vultr inventory provider is temporarily unavailable",
                    status_code=status_code,
                )

            if not isinstance(status_code, int) or not 200 <= status_code < 300:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
                raise VultrAPIError(
                    "Vultr inventory request was rejected",
                    status_code=status_code if isinstance(status_code, int) else None,
                )

            try:
                payload = response.json()
            except Exception as error:
                raise VultrAPIError("Vultr returned malformed JSON", status_code=status_code) from error
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
            if not isinstance(payload, dict):
                raise VultrAPIError("Vultr returned an invalid response envelope", status_code=status_code)
            return payload

        # The loop always returns or raises; this is a defensive guard for
        # static analyzers and unusual custom integer subclasses.
        raise VultrAPIError("Vultr inventory request failed")


def _validate_query_params(params: Mapping[str, Any] | None) -> dict[str, Any]:
    """Allow only the bounded pagination parameters used by this adapter."""

    if params is None:
        return {}
    if not isinstance(params, Mapping):
        raise VultrInventoryError("Vultr inventory query parameters are invalid")
    unknown = set(params) - VULTR_ALLOWED_QUERY_PARAMS
    if unknown or any(not isinstance(key, str) for key in params):
        raise VultrInventoryError("Vultr inventory query parameters are unsupported")

    validated: dict[str, Any] = {}
    if "per_page" in params:
        per_page = params["per_page"]
        if isinstance(per_page, bool) or not isinstance(per_page, int) or not 1 <= per_page <= VULTR_MAX_PER_PAGE:
            raise VultrInventoryError("Vultr inventory page size is invalid")
        validated["per_page"] = per_page
    if "cursor" in params:
        cursor = params["cursor"]
        if (
            not isinstance(cursor, str)
            or not cursor
            or len(cursor) > VULTR_MAX_CURSOR_LENGTH
            or cursor != cursor.strip()
            or any(character in cursor for character in "\r\n?#")
        ):
            raise VultrInventoryError("Vultr inventory pagination cursor is invalid")
        validated["cursor"] = cursor
    return validated


def client_for_vultr_account(
    account_or_client: Any,
    *,
    client: VultrReadOnlyClient | Any | None = None,
) -> VultrReadOnlyClient | Any:
    """Resolve a real account or a test-injected GET-only client."""

    if client is not None:
        return client
    if isinstance(account_or_client, VultrReadOnlyClient):
        return account_or_client
    token = getattr(account_or_client, "access_token", None)
    return VultrReadOnlyClient(token)


def _validated_page_options(per_page: int, max_pages: int, max_items: int) -> None:
    if isinstance(per_page, bool) or not isinstance(per_page, int) or not 1 <= per_page <= VULTR_MAX_PER_PAGE:
        raise VultrInventoryError("Vultr inventory page size is invalid")
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 1000:
        raise VultrInventoryError("Vultr inventory page limit is invalid")
    if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 100000:
        raise VultrInventoryError("Vultr inventory item limit is invalid")


def _collection_from_payload(payload: Any, collection_key: str) -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping):
        raise VultrInventoryError("Vultr returned an invalid inventory response")
    if not isinstance(collection_key, str) or not collection_key or "." in collection_key:
        raise VultrInventoryError("Vultr inventory collection key is invalid")
    items = payload.get(collection_key)
    if not isinstance(items, list):
        raise VultrInventoryError("Vultr returned an incomplete inventory collection")
    if any(not isinstance(item, dict) for item in items):
        raise VultrInventoryError("Vultr returned a malformed inventory item")
    return items


def _next_cursor(payload: Mapping[str, Any], *, item_count: int) -> str | None:
    meta = payload.get("meta")
    if not isinstance(meta, Mapping):
        raise VultrInventoryError("Vultr returned an incomplete pagination response")
    links = meta.get("links")
    if links is None:
        # Some current Vultr collection endpoints, notably managed
        # databases, return a terminal ``meta.total`` without cursor links.
        # Accept it only when the validated items cover the declared total;
        # never treat an incomplete page as authoritative.
        total = meta.get("total")
        if isinstance(total, int) and not isinstance(total, bool) and total >= 0 and item_count >= total:
            return None
        raise VultrInventoryError("Vultr returned an incomplete pagination response")
    if not isinstance(links, Mapping):
        raise VultrInventoryError("Vultr returned an incomplete pagination response")
    cursor = links.get("next")
    if cursor in (None, ""):
        return None
    if not isinstance(cursor, str) or not cursor.strip() or len(cursor) > VULTR_MAX_CURSOR_LENGTH:
        raise VultrInventoryError("Vultr returned an invalid pagination cursor")
    if cursor != cursor.strip() or "\r" in cursor or "\n" in cursor:
        raise VultrInventoryError("Vultr returned an invalid pagination cursor")
    return cursor


def iter_vultr_collection(
    account_or_client: Any,
    endpoint: str,
    collection_key: str,
    *,
    per_page: int = VULTR_DEFAULT_PER_PAGE,
    max_pages: int = VULTR_MAX_PAGES,
    max_items: int = VULTR_MAX_ITEMS,
    client: VultrReadOnlyClient | Any | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield a complete, cursor-paginated collection through GET only."""

    _validated_page_options(per_page, max_pages, max_items)
    path = validate_vultr_endpoint(endpoint)
    transport = client_for_vultr_account(account_or_client, client=client)
    cursor: str | None = None
    seen_cursors: set[str] = set()
    page_count = 0
    item_count = 0

    while True:
        if page_count >= max_pages:
            raise VultrInventoryError("Vultr inventory pagination limit exceeded")
        params: dict[str, Any] = {"per_page": per_page}
        if cursor is not None:
            if cursor in seen_cursors:
                raise VultrInventoryError("Vultr returned a repeated pagination cursor")
            seen_cursors.add(cursor)
            params["cursor"] = cursor

        try:
            payload = transport.get_json(path, params=params)
        except VultrInventoryError:
            raise
        except Exception as error:
            raise VultrInventoryError("Vultr inventory request failed") from error

        items = _collection_from_payload(payload, collection_key)
        page_count += 1
        item_count += len(items)
        if item_count > max_items:
            raise VultrInventoryError("Vultr inventory item limit exceeded")
        for item in items:
            # Returning provider-owned dicts would allow a normalization step
            # to mutate a mocked response.  Keep this boundary immutable from
            # the caller's perspective.
            yield copy.deepcopy(item)

        next_cursor = _next_cursor(payload, item_count=item_count)
        if next_cursor is None:
            return
        if next_cursor in seen_cursors:
            raise VultrInventoryError("Vultr returned a repeated pagination cursor")
        if page_count >= max_pages:
            raise VultrInventoryError("Vultr inventory pagination limit exceeded")
        cursor = next_cursor


def list_vultr_collection(
    account_or_client: Any,
    endpoint: str,
    collection_key: str,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Return a complete collection or raise without returning partial data."""

    return list(iter_vultr_collection(account_or_client, endpoint, collection_key, **kwargs))


paginate_vultr_collection = iter_vultr_collection
fetch_vultr_collection = list_vultr_collection

# Stable public compatibility name used by the other Vultr family workers.
VultrClient = VultrReadOnlyClient


def _owner_uid_constraint(name: str) -> models.UniqueConstraint:
    return models.UniqueConstraint(
        fields=("owner", "unique_id"),
        name=f"%(app_label)s_%(class)s_{name}_owner_uid_uniq",
    )


class CoreVultrResource(UtilAsset):
    """Abstract, redacting inventory asset shared by Vultr resource models."""

    owner = models.ForeignKey(
        CoreVultrAccount,
        on_delete=models.CASCADE,
        related_name="%(class)s_assets",
    )
    unique_id = models.CharField(max_length=VULTR_MAX_RESOURCE_ID_LENGTH)

    provider_type: str | None = None
    asset_type: str | None = None
    api_endpoint: str | None = None
    response_key: str | None = None

    class Meta:
        abstract = True
        constraints = [_owner_uid_constraint("resource")]

    def __str__(self) -> str:
        return self.name

    @property
    def provider_url(self) -> str:
        """Return a console URL only for the known instance UI shape.

        Vultr's console routes for several newer product families are not a
        stable public API contract.  Unknown families intentionally resolve
        to the console home rather than interpolating an untrusted endpoint.
        """

        safe_id = quote(str(self.unique_id), safe="")
        if self.api_endpoint == "instances":
            return f"{VULTR_CONSOLE_BASE}/instances/instance-id/{safe_id}/"
        return f"{VULTR_CONSOLE_BASE}/"

    @property
    def monitoring_credentials(self) -> dict[str, Any]:
        """Build checker context on demand; no token is stored on the asset."""

        return {
            "access_token": self.owner.access_token,
            "resource_id": self.unique_id,
            "asset_type": self.asset_type,
            "provider_type": self.provider_type,
            "api_endpoint": self.api_endpoint,
        }

    def save(self, *args: Any, **kwargs: Any) -> Any:
        if self.metadata is not None:
            self.metadata = redact_vultr_metadata(self.metadata)
        return super().save(*args, **kwargs)


@dataclass(frozen=True)
class VultrResourceSpec:
    """Immutable contract for one inventory family."""

    key: str
    endpoint: str | None
    collection_key: str | None
    model: type[CoreVultrResource]
    asset_type: str | None = None
    provider_type: str | None = None
    identifier_fields: tuple[str, ...] = ("id",)
    name_fields: tuple[str, ...] = ("label", "name", "description", "id")
    response_key: str | None = None
    metadata_key: str | None = None
    monitoring_default: str = UtilAsset.Monitoring.ACTIVE
    supported: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key.strip():
            raise ValueError("Vultr resource spec key is invalid")
        if self.supported and (not self.endpoint or not self.collection_key):
            raise ValueError("supported Vultr resource specs require an endpoint and collection")

    @property
    def api_endpoint(self) -> str | None:
        return self.endpoint

    @property
    def effective_asset_type(self) -> str:
        return self.asset_type or self.model.asset_type or self.key

    @property
    def effective_provider_type(self) -> str:
        return self.provider_type or self.model.provider_type or f"vultr_{self.key}"

    @property
    def effective_response_key(self) -> str:
        return self.response_key or self.key

    @property
    def effective_metadata_key(self) -> str:
        return self.metadata_key or self.effective_asset_type


VULTR_RESOURCE_SPECS: dict[str, VultrResourceSpec] = {}


def register_vultr_resource_specs(specs: Mapping[str, VultrResourceSpec]) -> None:
    """Register resource specs for the generic helpers without importing a module eagerly."""

    for key, spec in specs.items():
        if not isinstance(key, str) or not isinstance(spec, VultrResourceSpec):
            raise VultrInventoryError("Vultr resource spec is invalid")
    VULTR_RESOURCE_SPECS.update(specs)


def resource_spec(resource: str | VultrResourceSpec) -> VultrResourceSpec:
    if isinstance(resource, VultrResourceSpec):
        return resource
    if not isinstance(resource, str):
        raise VultrInventoryError("Vultr resource type is unsupported")
    key = resource.strip().lower().replace("-", "_").replace(" ", "_")
    spec = VULTR_RESOURCE_SPECS.get(key)
    if spec is None:
        # Runtime import avoids a package-initialization cycle while keeping
        # direct callers friendly when they pass a canonical resource name.
        try:
            from apps.console.cloud.vultr.resources_compute import RESOURCE_ALIASES, RESOURCE_SPECS

            key = RESOURCE_ALIASES.get(key, key)
            spec = RESOURCE_SPECS.get(key)
        except (ImportError, AttributeError):
            spec = None
    if spec is None:
        raise VultrInventoryError("Vultr resource type is unsupported")
    return spec


def _extract_identifier(item: Mapping[str, Any], fields: Sequence[str], key: str) -> str:
    for field in fields:
        value = item.get(field)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (str, int)) and str(value).strip():
            identifier = str(value).strip()
            if len(identifier) > VULTR_MAX_RESOURCE_ID_LENGTH:
                raise VultrInventoryError(f"Vultr returned an oversized {key} identifier")
            if "/" in unquote(identifier) or "\\" in unquote(identifier):
                raise VultrInventoryError(f"Vultr returned an invalid {key} identifier")
            return identifier
    raise VultrInventoryError(f"Vultr returned a {key} resource without an identifier")


def _display_name(item: Mapping[str, Any], identifier: str, fields: Sequence[str]) -> str:
    for field in fields:
        value = item.get(field)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
            return str(value).strip()[:100]
    return identifier[:100]


def normalize_vultr_record(item: Any, spec: VultrResourceSpec) -> dict[str, Any]:
    """Normalize one item into a redacted, persistence-ready record."""

    if not isinstance(item, Mapping):
        raise VultrInventoryError(f"Vultr returned a malformed {spec.key} resource")
    raw = copy.deepcopy(dict(item))
    identifier = _extract_identifier(raw, spec.identifier_fields, spec.key)
    metadata = redact_vultr_metadata(raw)
    if not isinstance(metadata, dict):
        raise VultrInventoryError(f"Vultr returned malformed {spec.key} metadata")
    metadata.update(
        {
            "_cloudmoo_provider_type": spec.effective_provider_type,
            "_cloudmoo_asset_type": spec.effective_asset_type,
            "_cloudmoo_endpoint": spec.endpoint,
            "_cloudmoo_raw_id": identifier,
        }
    )
    return {
        "unique_id": identifier,
        "name": _display_name(raw, identifier, spec.name_fields),
        "metadata": metadata,
        "raw": copy.deepcopy(metadata),
    }


def collect_vultr_resource_records(
    account_or_client: Any,
    resource: str | VultrResourceSpec,
    *,
    per_page: int = VULTR_DEFAULT_PER_PAGE,
    max_pages: int = VULTR_MAX_PAGES,
    max_items: int = VULTR_MAX_ITEMS,
    client: VultrReadOnlyClient | Any | None = None,
) -> list[dict[str, Any]]:
    """Fetch and normalize one complete collection without touching a model."""

    spec = resource_spec(resource)
    if not spec.supported or not spec.endpoint or not spec.collection_key:
        raise VultrInventoryError("Vultr resource type is unsupported")
    items = list_vultr_collection(
        account_or_client,
        spec.endpoint,
        spec.collection_key,
        per_page=per_page,
        max_pages=max_pages,
        max_items=max_items,
        client=client,
    )
    records: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    for item in items:
        record = normalize_vultr_record(item, spec)
        identifier = record["unique_id"]
        if identifier in identifiers:
            raise VultrInventoryError(f"Vultr returned a duplicate {spec.key} identifier")
        identifiers.add(identifier)
        records.append(record)
    return records


def collect_vultr_inventory(
    account_or_client: Any,
    resources: Iterable[str | VultrResourceSpec] | None = None,
    **kwargs: Any,
) -> dict[str, list[dict[str, Any]]]:
    """Collect every selected family before any reconciliation occurs."""

    if resources is None:
        selected = [spec for spec in VULTR_RESOURCE_SPECS.values() if spec.supported]
        if not selected:
            try:
                from apps.console.cloud.vultr.resources_compute import RESOURCE_SPECS

                selected = [spec for spec in RESOURCE_SPECS.values() if spec.supported]
            except (ImportError, AttributeError):
                selected = []
    else:
        selected = [resource_spec(resource) for resource in resources]
    keys = [spec.key for spec in selected]
    if len(keys) != len(set(keys)):
        raise VultrInventoryError("Vultr inventory contains duplicate resource families")

    result: dict[str, list[dict[str, Any]]] = {}
    for spec in selected:
        result[spec.key] = collect_vultr_resource_records(account_or_client, spec, **kwargs)
    return result


def _validate_supplied_records(
    spec: VultrResourceSpec,
    records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes, bytearray)):
        raise VultrInventoryError(f"Vultr returned an invalid {spec.key} record collection")
    validated: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping):
            raise VultrInventoryError(f"Vultr returned an invalid {spec.key} record")
        identifier = record.get("unique_id")
        name = record.get("name")
        metadata = record.get("metadata")
        if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > VULTR_MAX_RESOURCE_ID_LENGTH:
            raise VultrInventoryError(f"Vultr returned an invalid {spec.key} identifier")
        if not isinstance(name, str) or not name.strip() or not isinstance(metadata, Mapping):
            raise VultrInventoryError(f"Vultr returned an invalid {spec.key} record")
        normalized_id = identifier.strip()
        if normalized_id in identifiers:
            raise VultrInventoryError(f"Vultr returned a duplicate {spec.key} identifier")
        identifiers.add(normalized_id)
        safe_metadata = redact_vultr_metadata(copy.deepcopy(dict(metadata)))
        validated.append(
            {
                "unique_id": normalized_id,
                "name": name.strip()[:100],
                "metadata": safe_metadata,
                "raw": copy.deepcopy(safe_metadata),
            }
        )
    return validated


def _sync_vultr_records(
    account: CoreVultrAccount | Any,
    spec: VultrResourceSpec,
    records: Sequence[Mapping[str, Any]],
) -> int:
    """Reconcile a validated complete family using model saves only."""

    validated = _validate_supplied_records(spec, records)
    model = spec.model
    current_ids: set[str] = set()
    for record in validated:
        identifier = record["unique_id"]
        current_ids.add(identifier)
        defaults = {
            "name": record["name"],
            "monitoring": spec.monitoring_default,
            "type": spec.effective_asset_type,
            "metadata": redact_vultr_metadata(record["metadata"]),
        }
        asset, created = model.objects.get_or_create(
            owner=account,
            unique_id=identifier,
            defaults=defaults,
        )
        if not created:
            asset.name = defaults["name"]
            asset.type = defaults["type"]
            asset.metadata = defaults["metadata"]
            if asset.monitoring == model.Monitoring.NO_LONGER_EXISTS:
                asset.monitoring = spec.monitoring_default
            asset.save()

    # A complete, validated empty collection is authoritative.  Iterating and
    # saving each missing asset keeps schedule/state hooks intact and avoids a
    # bulk update that would bypass model behavior.
    for asset in model.objects.filter(owner=account):
        if asset.unique_id not in current_ids and asset.monitoring != model.Monitoring.NO_LONGER_EXISTS:
            asset.monitoring = model.Monitoring.NO_LONGER_EXISTS
            asset.save()
    return len(validated)


def reconcile_collection(
    account: CoreVultrAccount | Any,
    spec: VultrResourceSpec,
    records: Sequence[Mapping[str, Any]],
    client: VultrReadOnlyClient | Any | None = None,
) -> int:
    """Reconcile already-complete records through the shared save boundary."""

    del client  # The records have already been collected by the caller.
    return _sync_vultr_records(account, resource_spec(spec), records)


def sync_vultr_resource(
    account_or_client: Any,
    resource: str | VultrResourceSpec,
    *,
    records: Sequence[Mapping[str, Any]] | None = None,
    client: VultrReadOnlyClient | Any | None = None,
    **kwargs: Any,
) -> int:
    """Collect (unless supplied) and reconcile one complete resource family."""

    spec = resource_spec(resource)
    if not spec.supported:
        raise VultrInventoryError("Vultr resource type is unsupported")
    complete_records = (
        collect_vultr_resource_records(account_or_client, spec, client=client, **kwargs)
        if records is None
        else _validate_supplied_records(spec, records)
    )
    return _sync_vultr_records(account_or_client, spec, complete_records)


def sync_vultr_resources(
    account_or_client: Any,
    resources: Iterable[str | VultrResourceSpec] | None = None,
    *,
    client: VultrReadOnlyClient | Any | None = None,
    **kwargs: Any,
) -> dict[str, int]:
    """Collect all families first, then reconcile them after every read passes."""

    inventory = collect_vultr_inventory(account_or_client, resources, client=client, **kwargs)
    counts: dict[str, int] = {}
    for key, records in inventory.items():
        counts[key] = sync_vultr_resource(
            account_or_client,
            key,
            records=records,
            client=client,
            **kwargs,
        )
    return counts


# Integration-friendly aliases.
reconcile_vultr_resource = sync_vultr_resource
reconcile_vultr_resources = sync_vultr_resources
sync_vultr_assets = sync_vultr_resources
sync_vultr_inventory_assets = sync_vultr_resources


__all__ = [
    "CoreVultrResource",
    "VULTR_API_BASE",
    "VULTR_CONSOLE_BASE",
    "VULTR_DEFAULT_PER_PAGE",
    "VULTR_MAX_ITEMS",
    "VULTR_MAX_PAGES",
    "VULTR_MAX_PER_PAGE",
    "VULTR_ALLOWED_QUERY_PARAMS",
    "VULTR_MAX_RETRIES",
    "VULTR_RESOURCE_SPECS",
    "VULTR_TIMEOUT_SECONDS",
    "VultrAPIError",
    "VultrClient",
    "VultrInventoryError",
    "VultrReadOnlyClient",
    "VultrResourceSpec",
    "allowed_vultr_endpoint",
    "client_for_vultr_account",
    "collect_vultr_inventory",
    "collect_vultr_resource_records",
    "fetch_vultr_collection",
    "iter_vultr_collection",
    "list_vultr_collection",
    "normalize_vultr_record",
    "paginate_vultr_collection",
    "reconcile_vultr_resource",
    "reconcile_vultr_resources",
    "reconcile_collection",
    "redact_sensitive_metadata",
    "redact_vultr_metadata",
    "redact_vultr_response",
    "register_vultr_resource_specs",
    "resource_spec",
    "sync_vultr_assets",
    "sync_vultr_inventory_assets",
    "sync_vultr_resource",
    "sync_vultr_resources",
    "validate_vultr_endpoint",
]
