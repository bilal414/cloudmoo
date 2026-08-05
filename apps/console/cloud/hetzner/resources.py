"""Read-only inventory for Hetzner Cloud resources.

The original Hetzner adapter only inventories servers and volumes.  This
module is deliberately independent of that first-generation code so it can be
wired into ``CoreHetznerAccount.sync_assets`` without changing the existing
tables in the same migration.  It has three small responsibilities:

* make bounded GET-only collection requests;
* turn complete provider collections into normalized, redacted records; and
* reconcile one Django asset model at a time only after its complete
  collection has been fetched successfully.

No create/update/delete endpoint is exposed here.  A malformed or partial
response raises ``CloudInventoryTransientError`` instead of being interpreted
as an empty inventory, which protects locally-known assets from being marked
as gone during a provider/API failure.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Mapping, Sequence
from urllib.parse import quote

import requests
import boto3
from botocore.config import Config
from django.db import models

from apps.console.cloud.models import (
    CloudInventoryTransientError,
    require_inventory_list,
)
from apps.console.cloud.hetzner.models import CoreHetznerAccount
from apps.console.utils.models import UtilAsset
from apps.monitoring.metadata import redact_sensitive_metadata


logger = logging.getLogger(__name__)

HETZNER_API_BASE = "https://api.hetzner.cloud/v1"
HETZNER_CONSOLE_BASE = "https://console.hetzner.cloud"
HETZNER_DEFAULT_PER_PAGE = 50
HETZNER_MAX_PAGES = 100
HETZNER_TIMEOUT_SECONDS = 15
OBJECT_STORAGE_REGIONS = frozenset({"fsn1", "nbg1", "hel1"})
OBJECT_STORAGE_CLIENT_CONFIG = Config(
    connect_timeout=5,
    read_timeout=15,
    retries={"mode": "standard", "max_attempts": 2},
    signature_version="s3v4",
)


class _HetznerResourceSpec:
    """Immutable description of a list endpoint and its asset model."""

    __slots__ = (
        "key",
        "endpoint",
        "collection_key",
        "model",
        "identifier_fields",
        "name_fields",
        "asset_type",
        "monitoring_default",
    )

    def __init__(
        self,
        key: str,
        endpoint: str,
        collection_key: str,
        model: type["CoreHetznerResource"],
        identifier_fields: Sequence[str] = ("id",),
        name_fields: Sequence[str] = ("name",),
        asset_type: str | None = None,
        monitoring_default: str | None = None,
    ) -> None:
        self.key = key
        self.endpoint = endpoint
        self.collection_key = collection_key
        self.model = model
        self.identifier_fields = tuple(identifier_fields)
        self.name_fields = tuple(name_fields)
        self.asset_type = asset_type or key
        self.monitoring_default = monitoring_default or UtilAsset.Monitoring.ACTIVE

    def __repr__(self) -> str:  # pragma: no cover - useful only while debugging
        return f"HetznerResourceSpec({self.key!r}, endpoint={self.endpoint!r})"


# Public alias for callers that want to introspect the inventory contract.
HetznerResourceSpec = _HetznerResourceSpec


def _owner_uid_constraint(name: str) -> models.UniqueConstraint:
    return models.UniqueConstraint(
        fields=("owner", "unique_id"),
        name=f"hetzner_{name}_owner_uid_uniq",
    )


class CoreHetznerResource(UtilAsset):
    """Common fields for the additional Hetzner Cloud asset families."""

    owner = models.ForeignKey(
        CoreHetznerAccount,
        on_delete=models.CASCADE,
        related_name="%(class)s_assets",
    )
    # Hetzner action IDs and some provider-generated identifiers are not
    # guaranteed to fit the historical UtilAsset limit of 100 characters.
    unique_id = models.CharField(max_length=255)

    # ``provider_type`` is provider-qualified, while ``asset_type`` is the
    # provider-neutral value the parent agent can add to shared registries.
    provider_type: str | None = None
    asset_type: str | None = None
    api_endpoint: str | None = None

    class Meta:
        abstract = True

    def __str__(self) -> str:
        return self.name

    @property
    def provider_url(self) -> str:
        """Return a safe console landing URL without assuming a project ID."""
        if self.api_endpoint:
            resource_id = quote(str(self.unique_id), safe="")
            return f"{HETZNER_CONSOLE_BASE}/?resource={quote(self.api_endpoint, safe='')}/{resource_id}"
        return HETZNER_CONSOLE_BASE

    @property
    def monitoring_credentials(self) -> dict[str, Any]:
        """Return ephemeral checker context; credentials are never persisted."""
        return {
            "access_token": self.owner.access_token,
            "resource_id": self.unique_id,
            "endpoint": self.api_endpoint,
            "asset_type": self.asset_type,
        }

    def save(self, *args: Any, **kwargs: Any) -> Any:
        if self.metadata is not None:
            self.metadata = redact_sensitive_metadata(self.metadata)
        return super().save(*args, **kwargs)


class CoreHetznerPrimaryIP(CoreHetznerResource):
    provider_type = "hetzner_primary_ip"
    asset_type = "primary_ip"
    api_endpoint = "primary_ips"

    class Meta:
        db_table = "core_hetzner_primary_ip"
        constraints = [_owner_uid_constraint("primary_ip")]


class CoreHetznerFloatingIP(CoreHetznerResource):
    provider_type = "hetzner_floating_ip"
    asset_type = "floating_ip"
    api_endpoint = "floating_ips"

    class Meta:
        db_table = "core_hetzner_floating_ip"
        constraints = [_owner_uid_constraint("floating_ip")]


class CoreHetznerNetwork(CoreHetznerResource):
    provider_type = "hetzner_network"
    asset_type = "network"
    api_endpoint = "networks"

    class Meta:
        db_table = "core_hetzner_network"
        constraints = [_owner_uid_constraint("network")]


class CoreHetznerFirewall(CoreHetznerResource):
    provider_type = "hetzner_firewall"
    asset_type = "firewall"
    api_endpoint = "firewalls"

    class Meta:
        db_table = "core_hetzner_firewall"
        constraints = [_owner_uid_constraint("firewall")]


class CoreHetznerLoadBalancer(CoreHetznerResource):
    provider_type = "hetzner_load_balancer"
    asset_type = "load_balancer"
    api_endpoint = "load_balancers"

    class Meta:
        db_table = "core_hetzner_load_balancer"
        constraints = [_owner_uid_constraint("load_balancer")]


class CoreHetznerPlacementGroup(CoreHetznerResource):
    provider_type = "hetzner_placement_group"
    asset_type = "placement_group"
    api_endpoint = "placement_groups"

    class Meta:
        db_table = "core_hetzner_placement_group"
        constraints = [_owner_uid_constraint("placement_group")]


class CoreHetznerImage(CoreHetznerResource):
    provider_type = "hetzner_image"
    asset_type = "image"
    api_endpoint = "images"

    class Meta:
        db_table = "core_hetzner_image"
        constraints = [_owner_uid_constraint("image")]


class CoreHetznerCertificate(CoreHetznerResource):
    provider_type = "hetzner_certificate"
    asset_type = "certificate"
    api_endpoint = "certificates"

    class Meta:
        db_table = "core_hetzner_certificate"
        constraints = [_owner_uid_constraint("certificate")]


class CoreHetznerLocation(CoreHetznerResource):
    provider_type = "hetzner_location"
    asset_type = "location"
    api_endpoint = "locations"

    class Meta:
        db_table = "core_hetzner_location"
        constraints = [_owner_uid_constraint("location")]


class CoreHetznerDatacenter(CoreHetznerResource):
    provider_type = "hetzner_datacenter"
    asset_type = "datacenter"
    api_endpoint = "datacenters"

    class Meta:
        db_table = "core_hetzner_datacenter"
        constraints = [_owner_uid_constraint("datacenter")]


class CoreHetznerServerType(CoreHetznerResource):
    provider_type = "hetzner_server_type"
    asset_type = "server_type"
    api_endpoint = "server_types"

    class Meta:
        db_table = "core_hetzner_server_type"
        constraints = [_owner_uid_constraint("server_type")]


class CoreHetznerISO(CoreHetznerResource):
    provider_type = "hetzner_iso"
    asset_type = "iso"
    api_endpoint = "isos"

    class Meta:
        db_table = "core_hetzner_iso"
        constraints = [_owner_uid_constraint("iso")]


class CoreHetznerSSHKey(CoreHetznerResource):
    provider_type = "hetzner_ssh_key"
    asset_type = "ssh_key"
    api_endpoint = "ssh_keys"

    class Meta:
        db_table = "core_hetzner_ssh_key"
        constraints = [_owner_uid_constraint("ssh_key")]


class CoreHetznerLoadBalancerType(CoreHetznerResource):
    provider_type = "hetzner_load_balancer_type"
    asset_type = "load_balancer_type"
    api_endpoint = "load_balancer_types"

    class Meta:
        db_table = "core_hetzner_load_balancer_type"
        constraints = [_owner_uid_constraint("load_balancer_type")]


class CoreHetznerZone(CoreHetznerResource):
    provider_type = "hetzner_zone"
    asset_type = "zone"
    api_endpoint = "zones"

    class Meta:
        db_table = "core_hetzner_zone"
        constraints = [_owner_uid_constraint("zone")]


class CoreHetznerRRSet(CoreHetznerResource):
    provider_type = "hetzner_rrset"
    asset_type = "rrset"
    api_endpoint = "zones"

    @property
    def monitoring_credentials(self) -> dict[str, Any]:
        credentials = super().monitoring_credentials
        metadata = self.metadata if isinstance(self.metadata, dict) else {}
        credentials.update({
            "zone_id": metadata.get("_cloudmoo_zone_id"),
            "rr_name": metadata.get("_cloudmoo_rr_name"),
            "rr_type": metadata.get("_cloudmoo_rr_type"),
        })
        return credentials

    class Meta:
        db_table = "core_hetzner_rrset"
        constraints = [_owner_uid_constraint("rrset")]


class CoreHetznerObjectStorageBucket(CoreHetznerResource):
    """Optional S3-compatible Object Storage bucket inventory."""

    provider_type = "hetzner_object_storage_bucket"
    asset_type = UtilAsset.Type.OBJECT_STORAGE
    api_endpoint = "object-storage"

    @property
    def provider_url(self) -> str:
        metadata = self.metadata if isinstance(self.metadata, dict) else {}
        region = str(
            metadata.get("region") or self.owner.object_storage_region or "fsn1"
        ).strip().lower()
        return f"https://console.hetzner.cloud/object-storage/{region}/{quote(self.unique_id, safe='')}"

    @property
    def monitoring_credentials(self) -> dict[str, Any]:
        credentials = self.owner.object_storage_credentials
        metadata = self.metadata if isinstance(self.metadata, dict) else {}
        region = metadata.get("region")
        if isinstance(region, str) and region.strip().lower() in OBJECT_STORAGE_REGIONS:
            credentials["region"] = region.strip().lower()
        credentials.update({"bucket": self.unique_id})
        return credentials

    class Meta:
        db_table = "core_hetzner_object_storage_bucket"
        constraints = [_owner_uid_constraint("object_storage_bucket")]


class CoreHetznerAction(CoreHetznerResource):
    provider_type = "hetzner_action"
    asset_type = "action"
    api_endpoint = "actions"

    class Meta:
        db_table = "core_hetzner_action"
        constraints = [_owner_uid_constraint("action")]


# Common spelling aliases make the module friendly to callers that mirror the
# API names exactly, without registering duplicate Django models.
CoreHetznerPrimaryIp = CoreHetznerPrimaryIP
CoreHetznerFloatingIp = CoreHetznerFloatingIP
CoreHetznerIso = CoreHetznerISO
CoreHetznerDatacenter = CoreHetznerDatacenter


HETZNER_RESOURCE_MODELS = {
    "primary_ip": CoreHetznerPrimaryIP,
    "floating_ip": CoreHetznerFloatingIP,
    "network": CoreHetznerNetwork,
    "firewall": CoreHetznerFirewall,
    "load_balancer": CoreHetznerLoadBalancer,
    "placement_group": CoreHetznerPlacementGroup,
    "image": CoreHetznerImage,
    "certificate": CoreHetznerCertificate,
    "location": CoreHetznerLocation,
    "datacenter": CoreHetznerDatacenter,
    "server_type": CoreHetznerServerType,
    "iso": CoreHetznerISO,
    "ssh_key": CoreHetznerSSHKey,
    "load_balancer_type": CoreHetznerLoadBalancerType,
    "zone": CoreHetznerZone,
    "rrset": CoreHetznerRRSet,
    "object_storage": CoreHetznerObjectStorageBucket,
    "action": CoreHetznerAction,
}


HETZNER_RESOURCE_SPECS = {
    "primary_ip": HetznerResourceSpec(
        "primary_ip",
        "primary_ips",
        "primary_ips",
        CoreHetznerPrimaryIP,
        name_fields=("name", "ip", "assignee_type"),
    ),
    "floating_ip": HetznerResourceSpec(
        "floating_ip",
        "floating_ips",
        "floating_ips",
        CoreHetznerFloatingIP,
        name_fields=("name", "description", "ip"),
    ),
    "network": HetznerResourceSpec(
        "network", "networks", "networks", CoreHetznerNetwork
    ),
    "firewall": HetznerResourceSpec(
        "firewall", "firewalls", "firewalls", CoreHetznerFirewall
    ),
    "load_balancer": HetznerResourceSpec(
        "load_balancer",
        "load_balancers",
        "load_balancers",
        CoreHetznerLoadBalancer,
    ),
    "placement_group": HetznerResourceSpec(
        "placement_group",
        "placement_groups",
        "placement_groups",
        CoreHetznerPlacementGroup,
    ),
    "image": HetznerResourceSpec(
        "image",
        "images",
        "images",
        CoreHetznerImage,
        name_fields=("name", "description", "slug"),
    ),
    "certificate": HetznerResourceSpec(
        "certificate",
        "certificates",
        "certificates",
        CoreHetznerCertificate,
        name_fields=("name", "type"),
    ),
    "location": HetznerResourceSpec(
        "location", "locations", "locations", CoreHetznerLocation,
        monitoring_default=UtilAsset.Monitoring.DISABLED,
    ),
    "datacenter": HetznerResourceSpec(
        "datacenter", "datacenters", "datacenters", CoreHetznerDatacenter,
        monitoring_default=UtilAsset.Monitoring.DISABLED,
    ),
    "server_type": HetznerResourceSpec(
        "server_type",
        "server_types",
        "server_types",
        CoreHetznerServerType,
        name_fields=("name", "description"),
        monitoring_default=UtilAsset.Monitoring.DISABLED,
    ),
    "iso": HetznerResourceSpec(
        "iso", "isos", "isos", CoreHetznerISO, name_fields=("name", "description"),
        monitoring_default=UtilAsset.Monitoring.DISABLED,
    ),
    "ssh_key": HetznerResourceSpec(
        "ssh_key", "ssh_keys", "ssh_keys", CoreHetznerSSHKey,
        name_fields=("name", "fingerprint"),
        monitoring_default=UtilAsset.Monitoring.DISABLED,
    ),
    "load_balancer_type": HetznerResourceSpec(
        "load_balancer_type", "load_balancer_types", "load_balancer_types",
        CoreHetznerLoadBalancerType, name_fields=("name", "description"),
        monitoring_default=UtilAsset.Monitoring.DISABLED,
    ),
    "zone": HetznerResourceSpec(
        "zone", "zones", "zones", CoreHetznerZone, name_fields=("name", "mode"),
    ),
    "rrset": HetznerResourceSpec(
        "rrset", "zones", "rrsets", CoreHetznerRRSet,
        identifier_fields=("_cloudmoo_identifier",),
        name_fields=("name", "type"),
    ),
}

# Action history is intentionally not a normal inventory family.  Hetzner
# removed unfiltered global action listing; callers may reconcile explicitly
# supplied action records from a controlled test ledger, but the account sync
# must never issue GET /actions without known IDs.
HETZNER_ACTION_SPEC = HetznerResourceSpec(
    "action",
    "actions",
    "actions",
    CoreHetznerAction,
    name_fields=("command", "status", "progress"),
)
HETZNER_OBJECT_STORAGE_SPEC = HetznerResourceSpec(
    "object_storage",
    "object-storage",
    "Buckets",
    CoreHetznerObjectStorageBucket,
    identifier_fields=("Name",),
    name_fields=("Name",),
)

# Short public alias used by integration code.
RESOURCE_SPECS = HETZNER_RESOURCE_SPECS


def _provider_error(message: str, error: BaseException | None = None) -> CloudInventoryTransientError:
    """Build a stable, credential-free provider error."""
    # Deliberately do not interpolate ``error``: requests/provider exception
    # strings can echo an Authorization header or a URL query value.
    return CloudInventoryTransientError(message)


def _endpoint_path(endpoint: str) -> str:
    if not isinstance(endpoint, str):
        raise CloudInventoryTransientError("Hetzner inventory endpoint is invalid")
    normalized = endpoint.strip().strip("/")
    if not normalized or "?" in normalized or "#" in normalized:
        raise CloudInventoryTransientError("Hetzner inventory endpoint is invalid")
    # Resource endpoints are fixed API paths.  Reject path traversal and
    # nested paths here so a future caller cannot turn this read-only helper
    # into an arbitrary URL client.
    if any(part in {".", ".."} for part in normalized.split("/")):
        raise CloudInventoryTransientError("Hetzner inventory endpoint is invalid")
    return normalized


def _request_json(
    account: Any,
    endpoint: str,
    params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Fetch one JSON page through an account's GET helper or safe fallback."""
    endpoint = _endpoint_path(endpoint)
    request_params = dict(params or {})

    account_call = getattr(account, "_make_api_call", None)
    if callable(account_call):
        try:
            payload = account_call(endpoint, params=request_params)
        except CloudInventoryTransientError:
            raise
        except Exception as error:
            raise _provider_error("Hetzner inventory request failed", error) from error
    else:
        access_token = getattr(account, "access_token", None)
        if not isinstance(access_token, str) or not access_token:
            raise CloudInventoryTransientError("Hetzner inventory credentials are unavailable")
        try:
            response = requests.get(
                f"{HETZNER_API_BASE}/{endpoint}",
                headers={"Authorization": f"Bearer {access_token}"},
                params=request_params,
                timeout=HETZNER_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()
        except CloudInventoryTransientError:
            raise
        except Exception as error:
            raise _provider_error("Hetzner inventory request failed", error) from error

    if not isinstance(payload, dict):
        raise CloudInventoryTransientError("Hetzner returned an invalid inventory response")
    return payload


def _strict_int(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CloudInventoryTransientError(f"Hetzner returned an invalid {context}")
    return value


def _next_page(
    payload: Mapping[str, Any],
    current_page: int,
    page_size: int,
    page_item_count: int,
    fetched_count: int,
) -> int | None:
    """Validate Hetzner pagination, accepting older short-page responses."""
    if "meta" not in payload:
        # The original adapter relied on short pages.  Preserve compatibility
        # for that response shape, but never silently stop after a full page.
        return current_page + 1 if page_item_count >= page_size else None

    meta = payload.get("meta")
    if not isinstance(meta, dict):
        raise CloudInventoryTransientError("Hetzner returned an invalid pagination response")
    pagination = meta.get("pagination")
    if not isinstance(pagination, dict):
        raise CloudInventoryTransientError("Hetzner returned an incomplete pagination response")

    total_entries = pagination.get("total_entries")
    if total_entries is not None:
        total_entries = _strict_int(total_entries, "pagination total")
        if total_entries < fetched_count:
            raise CloudInventoryTransientError("Hetzner returned an invalid pagination total")

    last_page = pagination.get("last_page")
    if last_page is not None:
        last_page = _strict_int(last_page, "pagination last page")
        if last_page < current_page:
            raise CloudInventoryTransientError("Hetzner returned an invalid pagination sequence")

    raw_next = pagination.get("next_page")
    if raw_next in (None, ""):
        if total_entries is not None and total_entries > fetched_count:
            raise CloudInventoryTransientError("Hetzner returned an incomplete pagination response")
        if last_page is not None and current_page < last_page:
            raise CloudInventoryTransientError("Hetzner returned an incomplete pagination response")
        return None

    next_page = _strict_int(raw_next, "pagination next page")
    if next_page <= current_page:
        raise CloudInventoryTransientError("Hetzner returned an invalid pagination sequence")
    if last_page is not None and next_page > last_page:
        raise CloudInventoryTransientError("Hetzner returned an invalid pagination sequence")
    return next_page


def iter_hetzner_collection(
    account: Any,
    endpoint: str,
    collection_key: str | None = None,
    *,
    per_page: int = HETZNER_DEFAULT_PER_PAGE,
    max_pages: int = HETZNER_MAX_PAGES,
) -> Iterator[dict[str, Any]]:
    """Yield every item from a bounded Hetzner collection.

    ``account`` may be a real ``CoreHetznerAccount`` or a small test double
    exposing its existing ``_make_api_call`` method.  Only ``GET`` is used by
    the fallback transport, and the account helper in ``models.py`` is also
    GET-only.
    """
    endpoint = _endpoint_path(endpoint)
    if collection_key is None:
        collection_key = endpoint.rsplit("/", 1)[-1]
    if not isinstance(collection_key, str) or not collection_key:
        raise CloudInventoryTransientError("Hetzner inventory collection key is invalid")
    if isinstance(per_page, bool) or not isinstance(per_page, int) or not 1 <= per_page <= 100:
        raise CloudInventoryTransientError("Hetzner inventory page size is invalid")
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 1000:
        raise CloudInventoryTransientError("Hetzner inventory page bound is invalid")

    page = 1
    seen_pages: set[int] = set()
    fetched_count = 0

    while page is not None:
        if page in seen_pages or len(seen_pages) >= max_pages:
            raise CloudInventoryTransientError("Hetzner returned an invalid pagination sequence")
        seen_pages.add(page)

        payload = _request_json(account, endpoint, {"page": page, "per_page": per_page})
        items = require_inventory_list(payload, [collection_key], "Hetzner")
        for item in items:
            if not isinstance(item, dict):
                raise CloudInventoryTransientError("Hetzner returned an invalid inventory item")
            yield item
        fetched_count += len(items)
        next_page = _next_page(payload, page, per_page, len(items), fetched_count)
        if next_page is not None and len(seen_pages) >= max_pages:
            raise CloudInventoryTransientError("Hetzner inventory pagination bound exceeded")
        page = next_page


def list_hetzner_collection(
    account: Any,
    endpoint: str,
    collection_key: str | None = None,
    *,
    per_page: int = HETZNER_DEFAULT_PER_PAGE,
    max_pages: int = HETZNER_MAX_PAGES,
) -> list[dict[str, Any]]:
    """Return a complete, validated collection as a list."""
    return list(
        iter_hetzner_collection(
            account,
            endpoint,
            collection_key,
            per_page=per_page,
            max_pages=max_pages,
        )
    )


# Friendly aliases for parent adapters and tests that use ``paginate`` naming.
paginate_hetzner_collection = iter_hetzner_collection
fetch_hetzner_collection = list_hetzner_collection


def _extract_identifier(item: Mapping[str, Any], fields: Sequence[str], context: str) -> str:
    for field in fields:
        value = item.get(field)
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, (str, int)) and str(value).strip():
            identifier = str(value).strip()
            if len(identifier) > 255:
                raise CloudInventoryTransientError(f"Hetzner returned an oversized {context} identifier")
            return identifier
    raise CloudInventoryTransientError(f"Hetzner returned a {context} without an identifier")


def _display_name(
    item: Mapping[str, Any],
    identifier: str,
    fields: Sequence[str],
) -> str:
    for field in fields:
        value = item.get(field)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
            return str(value).strip()[:100]
    return identifier[:100]


def _normalized_record(item: Any, spec: _HetznerResourceSpec) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise CloudInventoryTransientError(f"Hetzner returned an invalid {spec.key} resource object")
    # Copy before adding adapter metadata so the caller's mocked/provider
    # payload is never mutated during normalization.
    raw = copy.deepcopy(item)
    if spec.key == "ssh_key":
        # The public key is not a credential, but CloudMoo only needs its
        # fingerprint and labels for inventory/audit. Do not persist complete
        # key material or future provider-added fields.
        raw = {
            key: raw[key]
            for key in ("id", "name", "fingerprint", "labels", "created")
            if key in raw
        }
    elif spec.key == "certificate":
        # Uploaded certificate responses may contain PEM material. Keep only
        # lifecycle/ownership fields; private key material is never stored.
        raw = {
            key: raw[key]
            for key in (
                "id", "name", "type", "status", "domains", "labels",
                "protection", "created", "not_valid_before", "not_valid_after",
                "expires_at", "sha1_fingerprint", "algorithm",
            )
            if key in raw
        }
    identifier = _extract_identifier(raw, spec.identifier_fields, spec.key)
    metadata = redact_sensitive_metadata(raw)
    if not isinstance(metadata, dict):  # defensive: raw was already checked
        raise CloudInventoryTransientError(f"Hetzner returned an invalid {spec.key} metadata object")
    metadata.update(
        {
            "_cloudmoo_provider_type": spec.model.provider_type,
            "_cloudmoo_asset_type": spec.asset_type,
            "_cloudmoo_endpoint": spec.endpoint,
            "_cloudmoo_raw_id": identifier,
        }
    )
    return {
        "unique_id": identifier,
        "name": _display_name(raw, identifier, spec.name_fields),
        "metadata": metadata,
        # The raw convenience view is redacted as well.  Returning an
        # unredacted copy here would make a read-only inventory helper a
        # credential leak even though the persisted metadata is safe.
        "raw": copy.deepcopy(metadata),
    }


def resource_spec(resource: str | _HetznerResourceSpec) -> _HetznerResourceSpec:
    """Resolve a public resource key or spec, failing closed for unknown keys."""
    if isinstance(resource, _HetznerResourceSpec):
        return resource
    if not isinstance(resource, str):
        raise CloudInventoryTransientError("Hetzner resource type is invalid")
    key = resource.strip().lower()
    if key in {"action", "actions"}:
        return HETZNER_ACTION_SPEC
    spec = HETZNER_RESOURCE_SPECS.get(key)
    if spec is None:
        # Accept API endpoint names as a convenience for parent sync code.
        spec = next(
            (candidate for candidate in HETZNER_RESOURCE_SPECS.values() if candidate.endpoint == key),
            None,
        )
    if spec is None:
        raise CloudInventoryTransientError("Hetzner resource type is unsupported")
    return spec


def collect_hetzner_resource_records(
    account: Any,
    resource: str | _HetznerResourceSpec,
    *,
    per_page: int = HETZNER_DEFAULT_PER_PAGE,
    max_pages: int = HETZNER_MAX_PAGES,
) -> list[dict[str, Any]]:
    """Fetch and normalize one complete resource family without DB writes."""
    spec = resource_spec(resource)
    if spec.key == "action":
        raise CloudInventoryTransientError(
            "Hetzner action inventory requires explicit action IDs"
        )
    if spec.key == "rrset":
        raise CloudInventoryTransientError(
            "Hetzner RRSet inventory requires a parent zone context"
        )
    records: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    for item in iter_hetzner_collection(
        account,
        spec.endpoint,
        spec.collection_key,
        per_page=per_page,
        max_pages=max_pages,
    ):
        record = _normalized_record(item, spec)
        identifier = record["unique_id"]
        if identifier in identifiers:
            raise CloudInventoryTransientError(f"Hetzner returned a duplicate {spec.key} identifier")
        identifiers.add(identifier)
        records.append(record)
    return records


def collect_hetzner_action_record(
    account: Any,
    action_id: str | int,
) -> dict[str, Any]:
    """Fetch one explicitly identified Action without global enumeration."""
    if isinstance(action_id, bool) or not isinstance(action_id, (str, int)):
        raise CloudInventoryTransientError("Hetzner action identifier is invalid")
    normalized_id = str(action_id).strip()
    if not normalized_id or len(normalized_id) > 255:
        raise CloudInventoryTransientError("Hetzner action identifier is invalid")
    payload = _request_json(
        account,
        f"actions/{quote(normalized_id, safe='')}",
    )
    action = payload.get("action")
    if not isinstance(action, dict):
        raise CloudInventoryTransientError("Hetzner returned an invalid action response")
    record = _normalized_record(action, HETZNER_ACTION_SPEC)
    if record["unique_id"] != normalized_id:
        raise CloudInventoryTransientError("Hetzner action response identifier mismatch")
    return record


def sync_hetzner_action(
    account: CoreHetznerAccount,
    action_id: str | int,
) -> int:
    """Persist one known Action without treating it as a complete collection."""
    record = collect_hetzner_action_record(account, action_id)
    model = HETZNER_ACTION_SPEC.model
    defaults = {
        "name": str(record["name"])[:100],
        "monitoring": HETZNER_ACTION_SPEC.monitoring_default,
        "type": HETZNER_ACTION_SPEC.asset_type,
        "metadata": redact_sensitive_metadata(record["metadata"]),
    }
    asset, created = model.objects.get_or_create(
        owner=account,
        unique_id=record["unique_id"],
        defaults=defaults,
    )
    if not created:
        asset.name = defaults["name"]
        asset.type = defaults["type"]
        asset.metadata = defaults["metadata"]
        if asset.monitoring == model.Monitoring.NO_LONGER_EXISTS:
            asset.monitoring = model.Monitoring.ACTIVE
        asset.save()
    return 1


def collect_hetzner_inventory(
    account: Any,
    resources: Iterable[str | _HetznerResourceSpec] | None = None,
    *,
    per_page: int = HETZNER_DEFAULT_PER_PAGE,
    max_pages: int = HETZNER_MAX_PAGES,
) -> dict[str, list[dict[str, Any]]]:
    """Fetch all requested families before any reconciliation can occur."""
    selected = (
        list(HETZNER_RESOURCE_SPECS.values())
        if resources is None
        else [resource_spec(resource) for resource in resources]
    )
    # Duplicate selections could otherwise cause a second family pass to
    # mark a valid asset absent, so reject them before making requests.
    keys = [spec.key for spec in selected]
    if len(keys) != len(set(keys)):
        raise CloudInventoryTransientError("Hetzner inventory contains duplicate resource families")
    if any(spec.key == "action" for spec in selected):
        raise CloudInventoryTransientError(
            "Hetzner action inventory requires explicit action IDs"
        )

    # This intentionally completes every read before any model manager is
    # touched. A later malformed family therefore cannot partially reconcile
    # an earlier family in the same account sync.
    result: dict[str, list[dict[str, Any]]] = {}
    wants_rrsets = any(spec.key == "rrset" for spec in selected)
    if wants_rrsets and not any(spec.key == "zone" for spec in selected):
        raise CloudInventoryTransientError(
            "Hetzner RRSet inventory requires the parent zone collection"
        )

    for spec in selected:
        if spec.key == "rrset":
            continue
        result[spec.key] = collect_hetzner_resource_records(
            account,
            spec,
            per_page=per_page,
            max_pages=max_pages,
        )

    if wants_rrsets:
        zones = result.get("zone", [])
        rrset_records: list[dict[str, Any]] = []
        secondary_zone_seen = False
        for zone in zones:
            zone_metadata = zone.get("metadata") if isinstance(zone, dict) else None
            mode = zone_metadata.get("mode") if isinstance(zone_metadata, dict) else None
            if mode not in {"primary", "secondary"}:
                raise CloudInventoryTransientError(
                    "Hetzner returned a zone without a valid mode"
                )
            if mode == "secondary":
                # RRSet reads are not supported for secondary zones. Do not
                # reconcile any RRSet family when visibility is partial.
                secondary_zone_seen = True
                break

            zone_id = zone["unique_id"]
            rrsets = list_hetzner_collection(
                account,
                f"zones/{quote(str(zone_id), safe='')}/rrsets",
                "rrsets",
                per_page=100,
                max_pages=max_pages,
            )
            for rrset in rrsets:
                if not isinstance(rrset, dict):
                    raise CloudInventoryTransientError(
                        "Hetzner returned an invalid RRSet object"
                    )
                rr_name = rrset.get("name")
                rr_type = rrset.get("type")
                records = rrset.get("records")
                if (
                    not isinstance(rr_name, str)
                    or not rr_name.strip()
                    or not isinstance(rr_type, str)
                    or not rr_type.strip()
                    or not isinstance(records, list)
                ):
                    raise CloudInventoryTransientError(
                        "Hetzner returned an invalid RRSet object"
                    )
                identifier = f"{zone_id}:{rr_name}:{rr_type}"
                if len(identifier) > 255:
                    raise CloudInventoryTransientError(
                        "Hetzner returned an oversized RRSet identifier"
                    )
                metadata = redact_sensitive_metadata(copy.deepcopy(rrset))
                metadata.update({
                    "_cloudmoo_zone_id": str(zone_id),
                    "_cloudmoo_rr_name": rr_name,
                    "_cloudmoo_rr_type": rr_type,
                    "_cloudmoo_identifier": identifier,
                })
                rrset_records.append({
                    "unique_id": identifier,
                    "name": f"{rr_name} {rr_type}"[:100],
                    "metadata": metadata,
                    "raw": copy.deepcopy(metadata),
                })

        # A secondary zone means the complete RRSet collection is not
        # observable through this API. Omit the family so existing RRsets are
        # preserved rather than falsely marked as absent.
        if not secondary_zone_seen:
            result["rrset"] = rrset_records
    return result


def _sync_records(
    account: CoreHetznerAccount,
    spec: _HetznerResourceSpec,
    records: Sequence[Mapping[str, Any]],
) -> int:
    """Reconcile a validated family and only then mark absent assets."""
    model = spec.model
    current_ids: list[str] = []
    for record in records:
        identifier = str(record["unique_id"])
        current_ids.append(identifier)
        defaults = {
            "name": str(record["name"])[:100],
            "monitoring": spec.monitoring_default,
            "type": spec.asset_type,
            "metadata": redact_sensitive_metadata(record["metadata"]),
        }
        asset, created = model.objects.get_or_create(
            owner=account,
            unique_id=identifier,
            defaults=defaults,
        )
        if not created:
            asset.name = defaults["name"]
            asset.type = spec.asset_type
            asset.metadata = defaults["metadata"]
            # Explicitly disabled assets remain disabled; only a previously
            # absent resource is revived after it appears in a full listing.
            if asset.monitoring == model.Monitoring.NO_LONGER_EXISTS:
                asset.monitoring = spec.monitoring_default
            asset.save()

    # An empty list is authoritative only because collection retrieval and
    # item validation completed first.  Exceptions above leave this untouched.
    model.objects.filter(owner=account).exclude(unique_id__in=current_ids).update(
        monitoring=model.Monitoring.NO_LONGER_EXISTS
    )
    return len(current_ids)


def sync_hetzner_resource(
    account: CoreHetznerAccount,
    resource: str | _HetznerResourceSpec,
    *,
    records: Sequence[Mapping[str, Any]] | None = None,
    per_page: int = HETZNER_DEFAULT_PER_PAGE,
    max_pages: int = HETZNER_MAX_PAGES,
) -> int:
    """Fetch (unless supplied) and reconcile one resource family."""
    spec = resource_spec(resource)
    if spec.key == "action" and records is None:
        raise CloudInventoryTransientError(
            "Hetzner action inventory requires explicit action records"
        )
    validated_records = (
        collect_hetzner_resource_records(
            account,
            spec,
            per_page=per_page,
            max_pages=max_pages,
        )
        if records is None
        else list(records)
    )
    # Re-run the structural/duplicate checks for caller-supplied records so a
    # parent adapter cannot accidentally bypass the fail-closed boundary.
    identifiers: set[str] = set()
    for record in validated_records:
        if not isinstance(record, Mapping):
            raise CloudInventoryTransientError(f"Hetzner returned an invalid {spec.key} record")
        identifier = _extract_identifier(record, ("unique_id",), spec.key)
        if identifier in identifiers:
            raise CloudInventoryTransientError(f"Hetzner returned a duplicate {spec.key} identifier")
        identifiers.add(identifier)
        if not isinstance(record.get("name"), str) or not isinstance(record.get("metadata"), dict):
            raise CloudInventoryTransientError(f"Hetzner returned an invalid {spec.key} record")
    return _sync_records(account, spec, validated_records)


def sync_hetzner_resources(
    account: CoreHetznerAccount,
    resources: Iterable[str | _HetznerResourceSpec] | None = None,
    *,
    per_page: int = HETZNER_DEFAULT_PER_PAGE,
    max_pages: int = HETZNER_MAX_PAGES,
) -> dict[str, int]:
    """Inventory and reconcile the requested additional Hetzner resources."""
    inventory = collect_hetzner_inventory(
        account,
        resources,
        per_page=per_page,
        max_pages=max_pages,
    )
    counts: dict[str, int] = {}
    for key, records in inventory.items():
        counts[key] = sync_hetzner_resource(
            account,
            key,
            records=records,
            per_page=per_page,
            max_pages=max_pages,
        )
    return counts


# Names that read naturally in a provider account's ``sync_assets`` method.
sync_hetzner_assets = sync_hetzner_resources
sync_hetzner_inventory_assets = sync_hetzner_resources


def _object_storage_client(account: CoreHetznerAccount):
    region = str(account.object_storage_region or "").strip().lower()
    if region not in OBJECT_STORAGE_REGIONS:
        raise CloudInventoryTransientError(
            "Hetzner Object Storage region is unsupported"
        )
    if not account.object_storage_configured:
        raise CloudInventoryTransientError(
            "Hetzner Object Storage credentials are unavailable"
        )
    return boto3.client(
        "s3",
        region_name="us-east-1",
        endpoint_url=f"https://{region}.your-objectstorage.com",
        aws_access_key_id=account.object_storage_access_key,
        aws_secret_access_key=account.object_storage_secret_key,
        config=OBJECT_STORAGE_CLIENT_CONFIG,
    )


def sync_hetzner_object_storage_assets(account: CoreHetznerAccount) -> int:
    """Inventory Object Storage buckets without reading object contents.

    Hetzner exposes Object Storage through S3 rather than the Cloud API. The
    optional credentials are therefore independent from the Cloud token. If
    they are absent, existing local bucket assets are disabled (not marked
    deleted) so a temporary configuration gap cannot erase inventory.
    """
    model = CoreHetznerObjectStorageBucket
    if not account.object_storage_configured:
        # Use model saves instead of a bulk update so any existing monitoring
        # schedule is disabled consistently with a user-driven edit. Do not
        # mark the provider buckets missing: the optional credential gap is a
        # local configuration state, not evidence that a bucket was deleted.
        for asset in model.objects.filter(owner=account).exclude(
            monitoring=model.Monitoring.NO_LONGER_EXISTS
        ):
            asset.monitoring = model.Monitoring.DISABLED
            asset.save(update_fields=["monitoring"])
        return 0

    try:
        region = str(account.object_storage_region).strip().lower()
        response = _object_storage_client(account).list_buckets()
        buckets = response.get("Buckets") if isinstance(response, dict) else None
        if not isinstance(buckets, list):
            raise CloudInventoryTransientError(
                "Hetzner Object Storage returned an invalid bucket collection"
            )
        records = []
        for bucket in buckets:
            if not isinstance(bucket, dict) or not isinstance(bucket.get("Name"), str):
                raise CloudInventoryTransientError(
                    "Hetzner Object Storage returned an invalid bucket"
                )
            name = bucket["Name"].strip()
            if not name or len(name) > 255:
                raise CloudInventoryTransientError(
                    "Hetzner Object Storage returned an invalid bucket name"
                )
            bucket_region = bucket.get("BucketRegion") or bucket.get("Region") or region
            if not isinstance(bucket_region, str):
                raise CloudInventoryTransientError(
                    "Hetzner Object Storage returned an invalid bucket region"
                )
            bucket_region = bucket_region.strip().lower()
            if bucket_region not in OBJECT_STORAGE_REGIONS:
                raise CloudInventoryTransientError(
                    "Hetzner Object Storage returned an unsupported bucket region"
                )
            created = bucket.get("CreationDate")
            if hasattr(created, "isoformat"):
                created = created.isoformat()
            metadata = {
                "Name": name,
                "CreationDate": created,
                "region": bucket_region,
                "endpoint": f"https://{bucket_region}.your-objectstorage.com",
            }
            records.append({
                "unique_id": name,
                "name": name[:100],
                "metadata": redact_sensitive_metadata(metadata),
                "raw": redact_sensitive_metadata(metadata),
            })
        return _sync_records(account, HETZNER_OBJECT_STORAGE_SPEC, records)
    except CloudInventoryTransientError:
        raise
    except Exception as error:
        # Never interpolate the boto exception; S3 clients may include signed
        # request material or endpoint credentials in their string form.
        logger.warning("Hetzner Object Storage inventory failed: %s", type(error).__name__)
        raise CloudInventoryTransientError(
            "Hetzner Object Storage inventory temporarily unavailable"
        ) from error


__all__ = [
    "HETZNER_API_BASE",
    "HETZNER_DEFAULT_PER_PAGE",
    "HETZNER_MAX_PAGES",
    "HetznerResourceSpec",
    "RESOURCE_SPECS",
    "HETZNER_RESOURCE_SPECS",
    "HETZNER_RESOURCE_MODELS",
    "CoreHetznerResource",
    "CoreHetznerPrimaryIP",
    "CoreHetznerFloatingIP",
    "CoreHetznerNetwork",
    "CoreHetznerFirewall",
    "CoreHetznerLoadBalancer",
    "CoreHetznerPlacementGroup",
    "CoreHetznerImage",
    "CoreHetznerCertificate",
    "CoreHetznerLocation",
    "CoreHetznerDatacenter",
    "CoreHetznerServerType",
    "CoreHetznerISO",
    "CoreHetznerSSHKey",
    "CoreHetznerLoadBalancerType",
    "CoreHetznerZone",
    "CoreHetznerRRSet",
    "CoreHetznerObjectStorageBucket",
    "CoreHetznerAction",
    "HETZNER_ACTION_SPEC",
    "HETZNER_OBJECT_STORAGE_SPEC",
    "iter_hetzner_collection",
    "list_hetzner_collection",
    "paginate_hetzner_collection",
    "fetch_hetzner_collection",
    "collect_hetzner_resource_records",
    "collect_hetzner_action_record",
    "collect_hetzner_inventory",
    "sync_hetzner_action",
    "sync_hetzner_resource",
    "sync_hetzner_resources",
    "sync_hetzner_assets",
    "sync_hetzner_inventory_assets",
    "sync_hetzner_object_storage_assets",
]
