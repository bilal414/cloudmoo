"""Read-only Vultr v2 inventory for data, network, and edge resources.

This adapter intentionally owns only the resource families that are useful to
the monitoring surface and have stable Vultr v2 GET contracts.  The common
transport/model/reconciliation primitives live in ``resources_base``; this
module supplies the family registry, the parent/child collection reads, and
the provider-specific redaction rules.

Every collection is treated as authoritative only after its complete
pagination sequence and every item have been validated.  A malformed,
partial, or unsupported response raises ``CloudInventoryTransientError`` so a
provider outage cannot make existing local assets appear deleted.

There are deliberately no create, update, delete, purge, attach, or action
endpoints in this module.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any
from urllib.parse import quote, urlparse, parse_qs

from apps.console.cloud.models import CloudInventoryTransientError, require_inventory_list
from apps.console.cloud.vultr.models import CoreVultrAccount, CoreVultrDatabase
from apps.console.cloud.vultr.resources_base import (
    CoreVultrResource,
    VultrClient,
    VultrResourceSpec,
    reconcile_collection,
)
from apps.console.utils.models import UtilAsset
from apps.monitoring.metadata import redact_sensitive_metadata


VULTR_API_BASE = "https://api.vultr.com/v2"
VULTR_DEFAULT_PER_PAGE = 100
VULTR_MAX_PAGES = 100
VULTR_TIMEOUT_SECONDS = 15

MAX_IDENTIFIER_LENGTH = 255
MAX_NAME_LENGTH = 100
MAX_NESTED_DEPTH = 8
MAX_NESTED_ITEMS = 256
MAX_NESTED_TEXT_LENGTH = 4096


def _owner_uid_constraint(name: str):
    """Return a compact owner/resource uniqueness constraint for new models."""
    # The concrete resource classes are intentionally kept here rather than
    # in models.py.  The parent integration can add migrations and shared
    # relations without changing the transport contract in this module.
    from django.db import models

    return models.UniqueConstraint(
        fields=("owner", "unique_id"),
        name=f"vultr_{name}_owner_uid_uniq",
    )


class CoreVultrLoadBalancer(CoreVultrResource):
    provider_type = "vultr_load_balancer"
    asset_type = UtilAsset.Type.LOAD_BALANCER
    api_endpoint = "load-balancers"

    class Meta:
        db_table = "core_vultr_load_balancer"
        constraints = [_owner_uid_constraint("load_balancer")]


class CoreVultrVPC(CoreVultrResource):
    provider_type = "vultr_vpc"
    asset_type = UtilAsset.Type.VPC
    api_endpoint = "vpc2"

    class Meta:
        db_table = "core_vultr_vpc"
        constraints = [_owner_uid_constraint("vpc")]


class CoreVultrNATGateway(CoreVultrResource):
    provider_type = "vultr_nat_gateway"
    asset_type = UtilAsset.Type.NAT_GATEWAY
    api_endpoint = "nat-gateways"

    class Meta:
        db_table = "core_vultr_nat_gateway"
        constraints = [_owner_uid_constraint("nat_gateway")]


class CoreVultrFirewall(CoreVultrResource):
    provider_type = "vultr_firewall"
    asset_type = UtilAsset.Type.FIREWALL
    api_endpoint = "firewalls"

    class Meta:
        db_table = "core_vultr_firewall"
        constraints = [_owner_uid_constraint("firewall")]


class CoreVultrFirewallRule(CoreVultrResource):
    """A safe, separately monitorable summary of one firewall rule."""

    provider_type = "vultr_firewall_rule"
    # There is no shared firewall-rule choice in the historical UtilAsset
    # enum.  Keep the persisted value provider-qualified until the shared
    # integration adds a provider-neutral choice, while exposing aliases
    # below for callers that use ``firewall_rule``.
    asset_type = "vultr_firewall_rule"
    api_endpoint = "firewalls"

    @property
    def monitoring_credentials(self) -> dict[str, Any]:
        credentials = dict(super().monitoring_credentials)
        metadata = self.metadata if isinstance(self.metadata, dict) else {}
        credentials["firewall_group_id"] = metadata.get("_cloudmoo_firewall_group_id")
        return credentials

    class Meta:
        db_table = "core_vultr_firewall_rule"
        constraints = [_owner_uid_constraint("firewall_rule")]


class CoreVultrReservedIP(CoreVultrResource):
    provider_type = "vultr_reserved_ip"
    asset_type = UtilAsset.Type.RESERVED_IP
    api_endpoint = "reserved-ips"

    class Meta:
        db_table = "core_vultr_reserved_ip"
        constraints = [_owner_uid_constraint("reserved_ip")]


class CoreVultrDomain(CoreVultrResource):
    provider_type = "vultr_domain"
    asset_type = UtilAsset.Type.DOMAIN
    api_endpoint = "domains"

    class Meta:
        db_table = "core_vultr_domain"
        constraints = [_owner_uid_constraint("domain")]


class CoreVultrDNSRecord(CoreVultrResource):
    provider_type = "vultr_dns_record"
    asset_type = UtilAsset.Type.DNS_RECORD
    api_endpoint = "domains"

    @property
    def monitoring_credentials(self) -> dict[str, Any]:
        credentials = dict(super().monitoring_credentials)
        metadata = self.metadata if isinstance(self.metadata, dict) else {}
        credentials["domain"] = metadata.get("_cloudmoo_domain")
        return credentials

    class Meta:
        db_table = "core_vultr_dns_record"
        constraints = [_owner_uid_constraint("dns_record")]


class CoreVultrCDNZone(CoreVultrResource):
    """One table for both Vultr CDN pull and push zones."""

    provider_type = "vultr_cdn_zone"
    asset_type = UtilAsset.Type.CDN_ENDPOINT
    api_endpoint = "cdn"

    class Meta:
        db_table = "core_vultr_cdn_zone"
        constraints = [_owner_uid_constraint("cdn_zone")]


class CoreVultrCertificate(CoreVultrResource):
    provider_type = "vultr_tls_certificate"
    asset_type = UtilAsset.Type.CERTIFICATE
    api_endpoint = "ssl/certificates"

    class Meta:
        db_table = "core_vultr_certificate"
        constraints = [_owner_uid_constraint("certificate")]


# The existing model owns the already-migrated Vultr database table.  These
# aliases make the family name explicit without introducing a second Django
# model/table or changing apps/console/cloud/vultr/models.py.
CoreVultrManagedDatabase = CoreVultrDatabase
CoreVultrTLSCertificate = CoreVultrCertificate
CoreVultrVPCNetwork = CoreVultrVPC
CoreVultrVpc = CoreVultrVPC
CoreVultrNatGateway = CoreVultrNATGateway
CoreVultrFirewallGroup = CoreVultrFirewall
CoreVultrReservedIp = CoreVultrReservedIP
CoreVultrDnsRecord = CoreVultrDNSRecord
CoreVultrDNSZone = CoreVultrDomain
CoreVultrCDNPullZone = CoreVultrCDNZone
CoreVultrCDNPushZone = CoreVultrCDNZone

# The old database model predates CoreVultrResource.  Runtime class
# attributes let the shared spec/reconciliation helper treat it like the
# newer families; records are sanitized before they are handed to the helper.
CoreVultrDatabase.provider_type = "vultr_managed_database"
CoreVultrDatabase.asset_type = UtilAsset.Type.DATABASE
CoreVultrDatabase.api_endpoint = "databases"


VULTR_RESOURCE_MODELS: dict[str, type[CoreVultrResource]] = {
    "database": CoreVultrDatabase,
    "load_balancer": CoreVultrLoadBalancer,
    "vpc": CoreVultrVPC,
    "nat_gateway": CoreVultrNATGateway,
    "firewall": CoreVultrFirewall,
    "firewall_rule": CoreVultrFirewallRule,
    "reserved_ip": CoreVultrReservedIP,
    "domain": CoreVultrDomain,
    "dns_record": CoreVultrDNSRecord,
    "cdn_endpoint": CoreVultrCDNZone,
    "certificate": CoreVultrCertificate,
}


VULTR_RESOURCE_SPECS: dict[str, VultrResourceSpec] = {
    "database": VultrResourceSpec(
        "database",
        "databases",
        "databases",
        CoreVultrDatabase,
        name_fields=("label", "name", "hostname", "domain", "ip"),
    ),
    "load_balancer": VultrResourceSpec(
        "load_balancer",
        "load-balancers",
        "load_balancers",
        CoreVultrLoadBalancer,
    ),
    "vpc": VultrResourceSpec(
        "vpc",
        "vpc2",
        "vpcs",
        CoreVultrVPC,
    ),
    "nat_gateway": VultrResourceSpec(
        "nat_gateway",
        "nat-gateways",
        "nat_gateways",
        CoreVultrNATGateway,
    ),
    "firewall": VultrResourceSpec(
        "firewall",
        "firewalls",
        "firewall_groups",
        CoreVultrFirewall,
        name_fields=("description", "label", "name", "id"),
    ),
    "firewall_rule": VultrResourceSpec(
        "firewall_rule",
        "firewalls",
        "rules",
        CoreVultrFirewallRule,
        identifier_fields=("_cloudmoo_identifier", "id"),
        name_fields=("description", "notes", "action", "protocol", "id"),
    ),
    "reserved_ip": VultrResourceSpec(
        "reserved_ip",
        "reserved-ips",
        "reserved_ips",
        CoreVultrReservedIP,
        identifier_fields=("id", "ip"),
    ),
    "domain": VultrResourceSpec(
        "domain",
        "domains",
        "domains",
        CoreVultrDomain,
        identifier_fields=("id", "domain", "name"),
    ),
    "dns_record": VultrResourceSpec(
        "dns_record",
        "domains",
        "records",
        CoreVultrDNSRecord,
        identifier_fields=("_cloudmoo_identifier", "id", "record_id"),
        name_fields=("name", "type", "data", "id"),
    ),
    "cdn_endpoint": VultrResourceSpec(
        "cdn_endpoint",
        "cdn",
        "cdn_zones",
        CoreVultrCDNZone,
        identifier_fields=("id", "cdn_zone_id", "zone_id"),
        name_fields=("label", "name", "domain", "endpoint", "origin", "id"),
    ),
    "certificate": VultrResourceSpec(
        "certificate",
        "ssl/certificates",
        "certificates",
        CoreVultrCertificate,
        name_fields=("label", "name", "domain", "common_name", "id"),
    ),
}

# Public short aliases used by account synchronization code.
RESOURCE_SPECS = VULTR_RESOURCE_SPECS
RESOURCE_MODELS = VULTR_RESOURCE_MODELS


VULTR_RESOURCE_ALIASES = {
    "managed_database": "database",
    "databases": "database",
    "load_balancers": "load_balancer",
    "vpcs": "vpc",
    "vpc2": "vpc",
    "nat_gateways": "nat_gateway",
    "firewall_group": "firewall",
    "firewall_groups": "firewall",
    "firewall_rules": "firewall_rule",
    "reserved_ips": "reserved_ip",
    "zone": "domain",
    "dns_zone": "domain",
    "zones": "domain",
    "records": "dns_record",
    "domain_record": "dns_record",
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


# These are the only collection paths the adapter may request.  Parent IDs
# are interpolated only after strict identifier validation and URL-encoding.
VULTR_FIXED_COLLECTION_ENDPOINTS = frozenset(
    {
        "databases",
        "load-balancers",
        "vpc2",
        "nat-gateways",
        "firewalls",
        "reserved-ips",
        "domains",
        "cdn",
        "ssl/certificates",
    }
)
VULTR_DETAIL_RESPONSE_KEYS = {
    "database": "database",
    "load_balancer": "load_balancer",
    "vpc": "vpc",
    "nat_gateway": "nat_gateway",
    "firewall": "firewall_group",
    "reserved_ip": "reserved_ip",
    "domain": "domain",
    "cdn_endpoint": "cdn_zone",
    "certificate": "certificate",
}


_SIGNED_URL_QUERY_PARTS = frozenset(
    {"signature", "x-amz-signature", "x-goog-signature", "expires", "x-amz-expires", "token", "sig"}
)
_SENSITIVE_FIELD_EXACT = frozenset(
    {
        "password",
        "passwd",
        "passphrase",
        "secret",
        "token",
        "apitoken",
        "accesstoken",
        "refreshtoken",
        "secretkey",
        "privatekey",
        "credential",
        "credentials",
        "authorization",
        "connectionstring",
        "connectionuri",
        "dsn",
        "certificate",
        "certificatebody",
        "certificatepem",
        "privatekeypem",
        "pem",
        "signedurl",
        "presignedurl",
        "signature",
    }
)
_SENSITIVE_FIELD_PARTS = (
    "password",
    "passphrase",
    "certificate",
    "connection",
    "credential",
    "secret",
    "token",
    "privatekey",
    "connectionstring",
    "connectionuri",
    "presignedurl",
    "signedurl",
    "accesstoken",
    "refreshtoken",
    "apitoken",
    "authorization",
)


def _normalized_field_name(value: Any) -> str:
    return "".join(character for character in str(value).lower() if character.isalnum())


def _is_sensitive_field(key: Any, value: Any = None) -> bool:
    normalized = _normalized_field_name(key)
    if normalized in _SENSITIVE_FIELD_EXACT or any(part in normalized for part in _SENSITIVE_FIELD_PARTS):
        return True
    if normalized in {"url", "uri", "downloadurl", "endpoint"} and isinstance(value, str):
        parsed = urlparse(value)
        query_keys = {_normalized_field_name(item) for item in parse_qs(parsed.query)}
        if query_keys.intersection(_SIGNED_URL_QUERY_PARTS):
            return True
    return False


def _safe_nested_record(value: Any, *, depth: int = 0) -> Any:
    """Copy JSON-like provider data while dropping credential/key material.

    This helper is intentionally stricter than the shared redactor: a
    certificate body, connection string, or signed URL is removed rather than
    retained under a ``[REDACTED]`` key.  It also bounds nesting and collection
    size so an unexpected provider payload cannot create an unbounded metadata
    object.
    """

    if depth > MAX_NESTED_DEPTH:
        raise CloudInventoryTransientError("Vultr returned an oversized nested resource")
    if isinstance(value, Mapping):
        if len(value) > MAX_NESTED_ITEMS:
            raise CloudInventoryTransientError("Vultr returned an oversized resource object")
        result: dict[str, Any] = {}
        for raw_key, raw_child in value.items():
            if not isinstance(raw_key, (str, int, float, bool)):
                raise CloudInventoryTransientError("Vultr returned an invalid metadata key")
            if _is_sensitive_field(raw_key, raw_child):
                continue
            child = _safe_nested_record(raw_child, depth=depth + 1)
            if isinstance(child, str) and len(child) > MAX_NESTED_TEXT_LENGTH:
                child = child[:MAX_NESTED_TEXT_LENGTH]
            result[str(raw_key)] = child
        # The shared helper preserves the application's established handling
        # for environment-variable maps and any future sensitive key aliases.
        return redact_sensitive_metadata(result)
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_NESTED_ITEMS:
            raise CloudInventoryTransientError("Vultr returned an oversized resource collection")
        return [_safe_nested_record(item, depth=depth + 1) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    # Provider JSON should not contain arbitrary Python objects.  Failing
    # closed is safer than serializing an object representation that could
    # accidentally include a request URL or credential.
    raise CloudInventoryTransientError("Vultr returned an invalid resource value")


def normalize_vultr_nested_record(value: Any) -> Any:
    """Public safe nested-record normalizer used by inventory and checks."""
    return _safe_nested_record(copy.deepcopy(value))


def _strict_identifier(value: Any, context: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise CloudInventoryTransientError(f"Vultr returned an invalid {context} identifier")
    identifier = str(value).strip()
    if not identifier or len(identifier) > MAX_IDENTIFIER_LENGTH:
        raise CloudInventoryTransientError(f"Vultr returned an invalid {context} identifier")
    return identifier


def _extract_identifier(item: Mapping[str, Any], fields: Sequence[str], context: str) -> str:
    for field in fields:
        value = item.get(field)
        if value is None:
            continue
        try:
            return _strict_identifier(value, context)
        except CloudInventoryTransientError:
            # A null/invalid preferred field must not silently turn a malformed
            # provider object into a different resource.  Only absent fields
            # are eligible for fallback.
            raise
    raise CloudInventoryTransientError(f"Vultr returned a {context} without an identifier")


def _display_name(item: Mapping[str, Any], identifier: str, fields: Sequence[str]) -> str:
    for field in fields:
        value = item.get(field)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            name = str(value).strip()
            if name:
                return name[:MAX_NAME_LENGTH]
    return identifier[:MAX_NAME_LENGTH]


def _safe_rule_summary(item: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only fields useful for firewall auditing and monitoring."""
    allowed = (
        "id",
        "ip_type",
        "protocol",
        "source",
        "source_port",
        "port",
        "ports",
        "subnet",
        "subnets",
        "action",
        "notes",
        "description",
        "position",
        "priority",
        "status",
        "date_created",
        "date_updated",
    )
    return _safe_nested_record({key: item[key] for key in allowed if key in item})


def _certificate_summary(item: Mapping[str, Any]) -> dict[str, Any]:
    """Exclude PEM/body fields even when a provider uses an unusual key."""
    allowed = (
        "id",
        "name",
        "label",
        "type",
        "state",
        "status",
        "domains",
        "domain",
        "common_name",
        "issuer",
        "algorithm",
        "sha1_fingerprint",
        "fingerprint",
        "date_created",
        "date_updated",
        "not_before",
        "not_after",
        "not_valid_before",
        "not_valid_after",
        "expires_at",
        "expiration",
        "expiration_date",
    )
    return _safe_nested_record({key: item[key] for key in allowed if key in item})


def _normalized_record(
    item: Any,
    spec: VultrResourceSpec,
    *,
    parent_id: str | None = None,
    domain_name: str | None = None,
) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise CloudInventoryTransientError(f"Vultr returned an invalid {spec.key} resource object")
    raw = copy.deepcopy(dict(item))

    if spec.key == "firewall_rule":
        raw = _safe_rule_summary(raw)
        if parent_id is None:
            raise CloudInventoryTransientError("Vultr returned a firewall rule without its group")
        parent_id = _strict_identifier(parent_id, "firewall group")
        rule_id = _extract_identifier(raw, ("id",), "firewall rule")
        raw["_cloudmoo_identifier"] = f"{parent_id}:{rule_id}"
        raw["_cloudmoo_firewall_group_id"] = parent_id
    elif spec.key == "certificate":
        raw = _certificate_summary(raw)
    else:
        raw = _safe_nested_record(raw)

    if spec.key == "dns_record":
        if domain_name is None:
            raise CloudInventoryTransientError("Vultr returned a DNS record without its domain")
        domain_name = _strict_identifier(domain_name, "DNS domain")
        record_id = _extract_identifier(raw, ("id", "record_id"), "DNS record")
        raw["_cloudmoo_identifier"] = f"{domain_name}:{record_id}"
        raw["_cloudmoo_domain"] = domain_name

    identifier = _extract_identifier(raw, spec.identifier_fields, spec.key)
    metadata = _safe_nested_record(raw)
    if not isinstance(metadata, dict):
        raise CloudInventoryTransientError(f"Vultr returned invalid {spec.key} metadata")

    if spec.key == "domain":
        domain_value = raw.get("domain") or raw.get("name")
        if isinstance(domain_value, str) and domain_value.strip():
            metadata["_cloudmoo_domain_name"] = domain_value.strip()

    provider_type = getattr(spec.model, "provider_type", None) or f"vultr_{spec.key}"
    asset_type = getattr(spec.model, "asset_type", None) or spec.key
    metadata.update(
        {
            "_cloudmoo_provider_type": str(provider_type),
            "_cloudmoo_asset_type": str(asset_type),
            "_cloudmoo_endpoint": spec.endpoint,
            "_cloudmoo_raw_id": identifier,
        }
    )
    return {
        "unique_id": identifier,
        "name": _display_name(raw, identifier, spec.name_fields),
        "metadata": metadata,
        # Never expose the original provider object through a convenience
        # field.  Callers receive the same safe shape that can be persisted.
        "raw": copy.deepcopy(metadata),
    }


def resource_spec(resource: str | VultrResourceSpec) -> VultrResourceSpec:
    """Resolve a canonical or aliased resource key, failing closed."""
    if isinstance(resource, VultrResourceSpec):
        return resource
    if not isinstance(resource, str):
        raise CloudInventoryTransientError("Vultr resource type is invalid")
    key = resource.strip().lower().replace(" ", "_")
    key = VULTR_RESOURCE_ALIASES.get(key, key)
    spec = VULTR_RESOURCE_SPECS.get(key)
    if spec is None:
        raise CloudInventoryTransientError("Vultr resource type is unsupported")
    return spec


def _resolve_client(account: Any, client: Any = None) -> Any:
    if client is not None:
        return client
    # Test doubles and callers can provide a client directly as the account
    # argument.  A real CoreVultrAccount is converted through the common
    # client, never through its legacy unconstrained sync helpers.
    if callable(getattr(account, "get", None)):
        return account
    token = getattr(account, "access_token", None) or getattr(account, "api_token", None)
    if not isinstance(token, str) or not token.strip():
        raise CloudInventoryTransientError("Vultr inventory credentials are unavailable")
    try:
        return VultrClient(token)
    except CloudInventoryTransientError:
        raise
    except Exception as error:
        raise CloudInventoryTransientError("Vultr inventory client is unavailable") from error


def _validate_endpoint(endpoint: str, *, collection: bool = True) -> str:
    if not isinstance(endpoint, str):
        raise CloudInventoryTransientError("Vultr inventory endpoint is invalid")
    normalized = endpoint.strip().strip("/")
    if not normalized or "?" in normalized or "#" in normalized:
        raise CloudInventoryTransientError("Vultr inventory endpoint is invalid")
    if any(part in {".", ".."} for part in normalized.split("/")):
        raise CloudInventoryTransientError("Vultr inventory endpoint is invalid")
    if collection:
        nested_allowed = re.fullmatch(r"(?:firewalls|domains)/[^/]+/(?:rules|records)", normalized)
        if normalized not in VULTR_FIXED_COLLECTION_ENDPOINTS and nested_allowed is None:
            raise CloudInventoryTransientError("Vultr inventory endpoint is unsupported")
    return normalized


def _request_json(client: Any, endpoint: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    endpoint = _validate_endpoint(endpoint, collection=False)
    getter = getattr(client, "get", None)
    if not callable(getter):
        raise CloudInventoryTransientError("Vultr inventory client is invalid")
    try:
        payload = getter(endpoint, params=dict(params or {}))
    except CloudInventoryTransientError:
        raise
    except Exception as error:
        # Provider/client exception text may contain an Authorization header
        # or a signed URL; keep it out of the monitoring and retry surface.
        raise CloudInventoryTransientError("Vultr inventory request failed") from error
    if not isinstance(payload, Mapping):
        raise CloudInventoryTransientError("Vultr returned an invalid inventory response")
    return dict(payload)


def _next_cursor(payload: Mapping[str, Any], *, item_count: int) -> str | None:
    meta = payload.get("meta")
    if not isinstance(meta, Mapping):
        raise CloudInventoryTransientError("Vultr returned an incomplete pagination response")
    links = meta.get("links")
    if links is None:
        # The live Vultr databases endpoint currently returns ``meta.total``
        # without a ``meta.links`` cursor when the complete collection fits in
        # one page.  Accept that explicit terminal form only when the total is
        # covered by the items already validated; otherwise fail closed rather
        # than silently dropping a later page.
        total = meta.get("total")
        if isinstance(total, int) and not isinstance(total, bool) and total >= 0 and item_count >= total:
            return None
        raise CloudInventoryTransientError("Vultr returned an incomplete pagination response")
    if not isinstance(links, Mapping) or "next" not in links:
        raise CloudInventoryTransientError("Vultr returned an incomplete pagination response")
    value = links.get("next")
    if value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) > MAX_NESTED_TEXT_LENGTH:
        raise CloudInventoryTransientError("Vultr returned an invalid pagination cursor")
    return value


def iter_vultr_collection(
    client: Any,
    endpoint: str,
    collection_key: str,
    *,
    per_page: int = VULTR_DEFAULT_PER_PAGE,
    max_pages: int = VULTR_MAX_PAGES,
):
    """Yield a complete Vultr cursor-paginated collection using GET only."""
    endpoint = _validate_endpoint(endpoint)
    if not isinstance(collection_key, str) or not collection_key.strip():
        raise CloudInventoryTransientError("Vultr inventory collection key is invalid")
    if isinstance(per_page, bool) or not isinstance(per_page, int) or not 1 <= per_page <= 100:
        raise CloudInventoryTransientError("Vultr inventory page size is invalid")
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 1000:
        raise CloudInventoryTransientError("Vultr inventory page bound is invalid")

    cursor: str | None = None
    seen_cursors: set[str] = set()
    page_count = 0
    item_count = 0
    while True:
        if page_count >= max_pages:
            raise CloudInventoryTransientError("Vultr inventory pagination bound exceeded")
        page_count += 1
        params: dict[str, Any] = {"per_page": per_page}
        if cursor:
            params["cursor"] = cursor
        payload = _request_json(client, endpoint, params)
        # Vultr's firewall-rule endpoint uses ``firewall_rules`` while older
        # fixtures and some API surfaces use ``rules``.  Accept only this
        # explicit provider envelope alias; omitted or malformed collections
        # still fail closed rather than being treated as empty.
        collection_path = [collection_key]
        if collection_key == "rules" and "rules" not in payload and "firewall_rules" in payload:
            collection_path = ["firewall_rules"]
        items = require_inventory_list(payload, collection_path, "Vultr")
        item_count += len(items)
        for item in items:
            if not isinstance(item, Mapping):
                raise CloudInventoryTransientError("Vultr returned an invalid inventory item")
            yield dict(item)
        next_cursor = _next_cursor(payload, item_count=item_count)
        if next_cursor is None:
            break
        if next_cursor in seen_cursors or next_cursor == cursor:
            raise CloudInventoryTransientError("Vultr returned an invalid pagination sequence")
        seen_cursors.add(next_cursor)
        cursor = next_cursor


def list_vultr_collection(
    client: Any,
    endpoint: str,
    collection_key: str,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    return list(iter_vultr_collection(client, endpoint, collection_key, **kwargs))


fetch_vultr_collection = list_vultr_collection
paginate_vultr_collection = iter_vultr_collection


def _collect_simple_records(client: Any, spec: VultrResourceSpec) -> list[dict[str, Any]]:
    identifiers: set[str] = set()
    records: list[dict[str, Any]] = []
    for item in iter_vultr_collection(client, spec.endpoint, spec.collection_key):
        record = _normalized_record(item, spec)
        if record["unique_id"] in identifiers:
            raise CloudInventoryTransientError(f"Vultr returned a duplicate {spec.key} identifier")
        identifiers.add(record["unique_id"])
        records.append(record)
    return records


def _domain_path_name(record: Mapping[str, Any]) -> str:
    metadata = record.get("metadata")
    if isinstance(metadata, Mapping):
        value = metadata.get("_cloudmoo_domain_name")
        if isinstance(value, str) and value.strip():
            return _strict_identifier(value, "DNS domain")
    return _strict_identifier(record.get("unique_id"), "DNS domain")


def _collect_firewall_rules(client: Any, firewall_records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    spec = VULTR_RESOURCE_SPECS["firewall_rule"]
    identifiers: set[str] = set()
    records: list[dict[str, Any]] = []
    for firewall in firewall_records:
        group_id = _strict_identifier(firewall.get("unique_id"), "firewall group")
        endpoint = f"firewalls/{quote(group_id, safe='')}/rules"
        for item in iter_vultr_collection(client, endpoint, spec.collection_key):
            record = _normalized_record(item, spec, parent_id=group_id)
            if record["unique_id"] in identifiers:
                raise CloudInventoryTransientError("Vultr returned a duplicate firewall rule identifier")
            identifiers.add(record["unique_id"])
            records.append(record)
    return records


def _collect_dns_records(client: Any, domain_records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    spec = VULTR_RESOURCE_SPECS["dns_record"]
    identifiers: set[str] = set()
    records: list[dict[str, Any]] = []
    for domain in domain_records:
        domain_name = _domain_path_name(domain)
        endpoint = f"domains/{quote(domain_name, safe='')}/records"
        for item in iter_vultr_collection(client, endpoint, spec.collection_key):
            record = _normalized_record(item, spec, domain_name=domain_name)
            if record["unique_id"] in identifiers:
                raise CloudInventoryTransientError("Vultr returned a duplicate DNS record identifier")
            identifiers.add(record["unique_id"])
            records.append(record)
    return records


def collect_vultr_resource_records(
    account: Any,
    resource: str | VultrResourceSpec,
    *,
    client: Any = None,
) -> list[dict[str, Any]]:
    """Fetch and normalize one non-parented Vultr resource family."""
    spec = resource_spec(resource)
    if spec.key in {"firewall_rule", "dns_record"}:
        raise CloudInventoryTransientError(
            f"Vultr {spec.key} inventory requires its parent collection"
        )
    resolved_client = _resolve_client(account, client)
    return _collect_simple_records(resolved_client, spec)


def collect_vultr_inventory(
    account: Any,
    resources: Iterable[str | VultrResourceSpec] | None = None,
    *,
    client: Any = None,
) -> dict[str, list[dict[str, Any]]]:
    """Fetch all requested families before any reconciliation can occur."""
    resolved_client = _resolve_client(account, client)
    requested = list(VULTR_RESOURCE_SPECS) if resources is None else list(resources)
    selected = [resource_spec(resource) for resource in requested]
    selected_keys = [spec.key for spec in selected]
    if len(selected_keys) != len(set(selected_keys)):
        raise CloudInventoryTransientError("Vultr inventory contains duplicate resource families")

    wants_firewall_rules = "firewall_rule" in selected_keys
    wants_dns_records = "dns_record" in selected_keys
    if wants_firewall_rules and "firewall" not in selected_keys:
        selected.insert(0, VULTR_RESOURCE_SPECS["firewall"])
    if wants_dns_records and "domain" not in selected_keys:
        selected.insert(0, VULTR_RESOURCE_SPECS["domain"])

    result: dict[str, list[dict[str, Any]]] = {}
    # No model manager is touched until this loop and all nested reads finish.
    for spec in selected:
        if spec.key in {"firewall_rule", "dns_record"}:
            continue
        result[spec.key] = _collect_simple_records(resolved_client, spec)

    if wants_firewall_rules:
        result["firewall_rule"] = _collect_firewall_rules(
            resolved_client,
            result.get("firewall", []),
        )
    if wants_dns_records:
        result["dns_record"] = _collect_dns_records(
            resolved_client,
            result.get("domain", []),
        )
    return result


def _validated_reconcile_records(
    spec: VultrResourceSpec,
    records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes, bytearray)):
        raise CloudInventoryTransientError(f"Vultr returned an invalid {spec.key} record collection")
    validated: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping):
            raise CloudInventoryTransientError(f"Vultr returned an invalid {spec.key} record")
        identifier = _strict_identifier(record.get("unique_id"), spec.key)
        name = record.get("name")
        metadata = record.get("metadata")
        if not isinstance(name, str) or not name.strip() or not isinstance(metadata, Mapping):
            raise CloudInventoryTransientError(f"Vultr returned an invalid {spec.key} record")
        if identifier in identifiers:
            raise CloudInventoryTransientError(f"Vultr returned a duplicate {spec.key} identifier")
        identifiers.add(identifier)
        validated.append(
            {
                "unique_id": identifier,
                "name": name.strip()[:MAX_NAME_LENGTH],
                "metadata": _safe_nested_record(copy.deepcopy(dict(metadata))),
            }
        )
    return validated


def sync_vultr_resource(
    account: CoreVultrAccount,
    resource: str | VultrResourceSpec,
    *,
    records: Sequence[Mapping[str, Any]] | None = None,
    client: Any = None,
) -> int:
    """Collect/reconcile one family through the shared base helper."""
    spec = resource_spec(resource)
    if records is None:
        if spec.key in {"firewall_rule", "dns_record"}:
            raise CloudInventoryTransientError(
                f"Vultr {spec.key} reconciliation requires a parent inventory pass"
            )
        resolved_client = _resolve_client(account, client)
        records = _collect_simple_records(resolved_client, spec)
        client = resolved_client
    validated = _validated_reconcile_records(spec, records)
    return reconcile_collection(account, spec, validated, client)


def sync_vultr_resources(
    account: CoreVultrAccount,
    resources: Iterable[str | VultrResourceSpec] | None = None,
    *,
    client: Any = None,
) -> dict[str, int]:
    """Inventory then reconcile all requested Vultr families."""
    resolved_client = _resolve_client(account, client)
    inventory = collect_vultr_inventory(account, resources, client=resolved_client)
    counts: dict[str, int] = {}
    for key, records in inventory.items():
        counts[key] = sync_vultr_resource(
            account,
            key,
            records=records,
            client=resolved_client,
        )
    return counts


sync_vultr_assets = sync_vultr_resources
sync_vultr_inventory_assets = sync_vultr_resources


def fetch_vultr_resource_record(
    account: Any,
    resource: str | VultrResourceSpec,
    unique_id: str | int,
    *,
    client: Any = None,
    parent_id: str | None = None,
    domain_name: str | None = None,
) -> dict[str, Any]:
    """Fetch one known resource by fixed detail endpoint and normalize it."""
    spec = resource_spec(resource)
    identifier = _strict_identifier(unique_id, spec.key)
    resolved_client = _resolve_client(account, client)
    if spec.key == "firewall_rule":
        if parent_id is None:
            raise CloudInventoryTransientError("Vultr firewall rule detail requires its group")
        endpoint = f"firewalls/{quote(_strict_identifier(parent_id, 'firewall group'), safe='')}/rules/{quote(identifier, safe='')}"
        response_key = "rule"
    elif spec.key == "dns_record":
        if domain_name is None:
            raise CloudInventoryTransientError("Vultr DNS record detail requires its domain")
        endpoint = f"domains/{quote(_strict_identifier(domain_name, 'DNS domain'), safe='')}/records/{quote(identifier, safe='')}"
        response_key = "record"
    else:
        endpoint = f"{spec.endpoint}/{quote(identifier, safe='')}"
        response_key = VULTR_DETAIL_RESPONSE_KEYS.get(spec.key)
    if response_key is None:
        raise CloudInventoryTransientError("Vultr resource detail endpoint is unsupported")
    _validate_endpoint(endpoint, collection=False)
    payload = _request_json(resolved_client, endpoint)
    item = payload.get(response_key)
    if not isinstance(item, Mapping):
        raise CloudInventoryTransientError(f"Vultr returned an invalid {spec.key} detail response")
    record = _normalized_record(
        item,
        spec,
        parent_id=parent_id,
        domain_name=domain_name,
    )
    if spec.key not in {"firewall_rule", "dns_record"} and record["unique_id"] != identifier:
        raise CloudInventoryTransientError(f"Vultr {spec.key} detail identifier mismatch")
    return record


collect_vultr_resource_record = fetch_vultr_resource_record
get_vultr_resource_record = fetch_vultr_resource_record


def collect_vultr_databases(account: Any, *, client: Any = None) -> list[dict[str, Any]]:
    """Compatibility helper for the pre-existing Vultr database model."""
    return collect_vultr_resource_records(account, "database", client=client)


sync_vultr_databases = lambda account, *, client=None: sync_vultr_resource(  # noqa: E731
    account,
    "database",
    client=client,
)


__all__ = [
    "VULTR_API_BASE",
    "VULTR_DEFAULT_PER_PAGE",
    "VULTR_FIXED_COLLECTION_ENDPOINTS",
    "VULTR_MAX_PAGES",
    "VULTR_RESOURCE_ALIASES",
    "VULTR_RESOURCE_MODELS",
    "VULTR_RESOURCE_SPECS",
    "VULTR_DETAIL_RESPONSE_KEYS",
    "RESOURCE_MODELS",
    "RESOURCE_SPECS",
    "VultrResourceSpec",
    "CoreVultrResource",
    "CoreVultrDatabase",
    "CoreVultrManagedDatabase",
    "CoreVultrLoadBalancer",
    "CoreVultrVPC",
    "CoreVultrVpc",
    "CoreVultrNATGateway",
    "CoreVultrNatGateway",
    "CoreVultrFirewall",
    "CoreVultrFirewallGroup",
    "CoreVultrFirewallRule",
    "CoreVultrReservedIP",
    "CoreVultrReservedIp",
    "CoreVultrDomain",
    "CoreVultrDNSZone",
    "CoreVultrDNSRecord",
    "CoreVultrDnsRecord",
    "CoreVultrCDNZone",
    "CoreVultrCDNPullZone",
    "CoreVultrCDNPushZone",
    "CoreVultrCertificate",
    "CoreVultrTLSCertificate",
    "resource_spec",
    "normalize_vultr_nested_record",
    "iter_vultr_collection",
    "list_vultr_collection",
    "fetch_vultr_collection",
    "paginate_vultr_collection",
    "collect_vultr_resource_records",
    "collect_vultr_resource_record",
    "fetch_vultr_resource_record",
    "get_vultr_resource_record",
    "collect_vultr_inventory",
    "sync_vultr_resource",
    "sync_vultr_resources",
    "sync_vultr_assets",
    "sync_vultr_inventory_assets",
    "collect_vultr_databases",
    "sync_vultr_databases",
    "reconcile_collection",
]
