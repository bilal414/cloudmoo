"""Read-only AWS DNS, edge, WAF, accelerator, and certificate inventory.

The edge services intentionally live in this module instead of the legacy AWS
model module.  That keeps the adapter independently deployable while the
integration lane wires the new asset types into the shared registries and
database migrations.

Only AWS list/get/describe APIs are used here.  In particular, Route 53 record
sets are inspected directly; health-check target bodies and CloudFront logs
are never fetched.
"""

import hashlib
import logging
from urllib.parse import quote

from botocore.exceptions import BotoCoreError, ClientError
from django.db import models

from apps.console.cloud.aws.models import CoreAWSACMCertificate, CoreAWSAccount
from apps.console.cloud.models import CloudInventoryTransientError
from apps.console.utils.models import UtilAsset

from .discovery import (
    aws_client,
    aws_error_code,
    get_enabled_regions,
    iter_pages,
    require_collection,
    serialize_aws,
)


logger = logging.getLogger(__name__)


ROUTE53_GLOBAL_ENDPOINT = "global"
CLOUDFRONT_CONTROL_PLANE_REGION = "us-east-1"
GLOBAL_ACCELERATOR_CONTROL_PLANE_REGION = "us-west-2"
WAF_CLOUDFRONT_SCOPE = "CLOUDFRONT"
WAF_REGIONAL_SCOPE = "REGIONAL"

# These limits bound both pagination and per-resource child calls.  A limit
# hit is treated as an incomplete collection, so it can never cause a local
# row to be marked missing merely because the provider returned too much data.
MAX_COLLECTION_PAGES = 100
MAX_COLLECTION_ITEMS = 10000
MAX_RECORD_PAGES = 100
MAX_RECORD_ITEMS_PER_ZONE = 10000
MAX_WEB_ACL_DETAILS = 500
MAX_ACCELERATOR_LISTENER_PAGES = 100
MAX_ACCELERATOR_LISTENER_ITEMS = 1000
MAX_WAF_RULES = 200
MAX_ACCELERATOR_LISTENERS = 200


AWS_ROUTE53_ZONE = "aws_route53_zone"
AWS_ROUTE53_RECORD = "aws_route53_record"
AWS_CLOUDFRONT_DISTRIBUTION = "aws_cloudfront_distribution"
AWS_CLOUDFRONT_ORIGIN_ACCESS_CONTROL = "aws_cloudfront_origin_access_control"
AWS_WAF_WEB_ACL = "aws_waf_web_acl"
AWS_GLOBAL_ACCELERATOR = "aws_global_accelerator"
AWS_ACM_CERTIFICATE = "acm_certificate"

AWS_EDGE_ASSET_TYPES = (
    AWS_ROUTE53_ZONE,
    AWS_ROUTE53_RECORD,
    AWS_CLOUDFRONT_DISTRIBUTION,
    AWS_CLOUDFRONT_ORIGIN_ACCESS_CONTROL,
    AWS_WAF_WEB_ACL,
    AWS_GLOBAL_ACCELERATOR,
)
EDGE_ASSET_TYPES = AWS_EDGE_ASSET_TYPES
ASSET_TYPE_ROUTE53_ZONE = AWS_ROUTE53_ZONE
ASSET_TYPE_ROUTE53_RECORD = AWS_ROUTE53_RECORD
ASSET_TYPE_CLOUDFRONT_DISTRIBUTION = AWS_CLOUDFRONT_DISTRIBUTION
ASSET_TYPE_CLOUDFRONT_ORIGIN_ACCESS_CONTROL = AWS_CLOUDFRONT_ORIGIN_ACCESS_CONTROL
ASSET_TYPE_WAF_WEB_ACL = AWS_WAF_WEB_ACL
ASSET_TYPE_GLOBAL_ACCELERATOR = AWS_GLOBAL_ACCELERATOR


def _owner_identifier_constraint(name):
    return models.UniqueConstraint(
        fields=("owner", "unique_id"),
        name=f"aws_edge_{name}_uid_uniq",
    )


class CoreAWSEdgeAsset(UtilAsset):
    """Common behavior for the concrete edge asset records."""

    class Meta:
        abstract = True

    asset_type = None

    def save(self, *args, **kwargs):
        if not self.type and self.asset_type:
            self.type = self.asset_type
        return super().save(*args, **kwargs)

    @property
    def edge_metadata(self):
        return self.metadata if isinstance(self.metadata, dict) else {}

    @property
    def edge_region(self):
        metadata = self.edge_metadata
        return (
            metadata.get("_cloudmoo_region")
            or metadata.get("_cloudmoo_resource_region")
            or self.owner.region
        )

    @property
    def edge_resource_name(self):
        metadata = self.edge_metadata
        return str(
            metadata.get("_cloudmoo_name")
            or metadata.get("Name")
            or metadata.get("name")
            or self.name
        )

    @property
    def provider_url(self):
        metadata = self.edge_metadata
        region = quote(str(self.edge_region), safe="-")

        if self.type == AWS_ROUTE53_ZONE:
            resource_id = metadata.get("resource_id") or self.unique_id
            return f"https://console.aws.amazon.com/route53/v2/hostedzones#ListRecordSets/{quote(str(resource_id), safe='-')}"
        if self.type == AWS_ROUTE53_RECORD:
            resource_id = metadata.get("hosted_zone_id") or ""
            return f"https://console.aws.amazon.com/route53/v2/hostedzones#ListRecordSets/{quote(str(resource_id), safe='-')}"
        if self.type in {AWS_CLOUDFRONT_DISTRIBUTION, AWS_CLOUDFRONT_ORIGIN_ACCESS_CONTROL}:
            return f"https://{region}.console.aws.amazon.com/cloudfront/v4/home"
        if self.type == AWS_WAF_WEB_ACL:
            return f"https://{region}.console.aws.amazon.com/wafv2/homev2/web-acls"
        if self.type == AWS_GLOBAL_ACCELERATOR:
            return f"https://{region}.console.aws.amazon.com/globalaccelerator/home"
        return None

    @property
    def monitoring_credentials(self):
        metadata = self.edge_metadata
        return {
            "access_key": self.owner.access_key,
            "secret_key": self.owner.secret_key,
            # The checker uses the metadata scope to select the documented
            # global control-plane region.  This fallback is only the account's
            # ordinary region for credentials compatibility.
            "region": self.edge_region,
            "resource_region": self.edge_region,
            "resource_name": self.edge_resource_name,
            "asset_type": self.type,
            "metadata": metadata,
        }

    def check_status(self):
        from apps.monitoring.checks.aws_edge import check_edge_resource_status

        return check_edge_resource_status(
            self.type,
            self.unique_id,
            self.monitoring_credentials,
        )


class CoreAWSRoute53Zone(CoreAWSEdgeAsset):
    asset_type = AWS_ROUTE53_ZONE
    owner = models.ForeignKey(
        CoreAWSAccount,
        on_delete=models.CASCADE,
        related_name="route53_zones",
    )

    class Meta:
        db_table = "core_aws_route53_zone"
        constraints = [_owner_identifier_constraint("route53_zone")]


class CoreAWSRoute53Record(CoreAWSEdgeAsset):
    asset_type = AWS_ROUTE53_RECORD
    owner = models.ForeignKey(
        CoreAWSAccount,
        on_delete=models.CASCADE,
        related_name="route53_records",
    )

    class Meta:
        db_table = "core_aws_route53_record"
        constraints = [_owner_identifier_constraint("route53_record")]


class CoreAWSCloudFrontDistribution(CoreAWSEdgeAsset):
    asset_type = AWS_CLOUDFRONT_DISTRIBUTION
    owner = models.ForeignKey(
        CoreAWSAccount,
        on_delete=models.CASCADE,
        related_name="cloudfront_distributions",
    )

    class Meta:
        db_table = "core_aws_cloudfront_distribution"
        constraints = [_owner_identifier_constraint("cloudfront_distribution")]


class CoreAWSCloudFrontOriginAccessControl(CoreAWSEdgeAsset):
    asset_type = AWS_CLOUDFRONT_ORIGIN_ACCESS_CONTROL
    owner = models.ForeignKey(
        CoreAWSAccount,
        on_delete=models.CASCADE,
        related_name="cloudfront_origin_access_controls",
    )

    class Meta:
        db_table = "core_aws_cloudfront_origin_access_control"
        constraints = [_owner_identifier_constraint("cloudfront_oac")]


class CoreAWSWAFWebACL(CoreAWSEdgeAsset):
    asset_type = AWS_WAF_WEB_ACL
    owner = models.ForeignKey(
        CoreAWSAccount,
        on_delete=models.CASCADE,
        related_name="waf_web_acls",
    )

    class Meta:
        db_table = "core_aws_waf_web_acl"
        constraints = [_owner_identifier_constraint("waf_web_acl")]


class CoreAWSGlobalAccelerator(CoreAWSEdgeAsset):
    asset_type = AWS_GLOBAL_ACCELERATOR
    owner = models.ForeignKey(
        CoreAWSAccount,
        on_delete=models.CASCADE,
        related_name="global_accelerators",
    )

    class Meta:
        db_table = "core_aws_global_accelerator"
        constraints = [_owner_identifier_constraint("global_accelerator")]


EDGE_MODEL_BY_TYPE = {
    AWS_ROUTE53_ZONE: CoreAWSRoute53Zone,
    AWS_ROUTE53_RECORD: CoreAWSRoute53Record,
    AWS_CLOUDFRONT_DISTRIBUTION: CoreAWSCloudFrontDistribution,
    AWS_CLOUDFRONT_ORIGIN_ACCESS_CONTROL: CoreAWSCloudFrontOriginAccessControl,
    AWS_WAF_WEB_ACL: CoreAWSWAFWebACL,
    AWS_GLOBAL_ACCELERATOR: CoreAWSGlobalAccelerator,
}
AWS_EDGE_ASSET_MODELS = EDGE_MODEL_BY_TYPE

# Compatibility aliases used by callers that spell out the AWS API object
# names.  They point at the same concrete Django model and do not register
# duplicate tables.
CoreAWSRoute53HostedZone = CoreAWSRoute53Zone
CoreAWSCloudFrontOAC = CoreAWSCloudFrontOriginAccessControl
CoreAWSWAFWebAcl = CoreAWSWAFWebACL


def _inventory_error(context, error):
    code = aws_error_code(error)
    return CloudInventoryTransientError(
        f"AWS {context} inventory failed ({code})"
    )


def _call_inventory(operation, context, callback):
    try:
        return callback()
    except CloudInventoryTransientError:
        raise
    except (ClientError, BotoCoreError) as error:
        raise _inventory_error(context or operation, error) from error
    except (KeyError, TypeError, ValueError) as error:
        raise CloudInventoryTransientError(
            f"AWS {context or operation} returned an invalid response"
        ) from error


def _required_collection(payload, path, context):
    """Validate a collection through the shared discovery helper.

    CloudFront's list APIs wrap their arrays in ``DistributionList`` and
    ``OriginAccessControlList`` objects.  Walking to the immediate parent
    here keeps the discovery helper's interface the single source of truth
    for collection validation while supporting both flat and wrapped APIs.
    """

    if isinstance(path, str):
        path = (path,)
    current = payload
    for part in path[:-1]:
        if not isinstance(current, dict) or part not in current:
            raise CloudInventoryTransientError(
                f"AWS {context} returned an incomplete collection"
            )
        current = current[part]

    key = path[-1]
    try:
        collection = require_collection(current, key, context)
    except TypeError:
        # The discovery helper is intentionally small and older callers may
        # expose the two-argument form.  This is only a call-shape adapter;
        # validation remains in require_collection.
        collection = require_collection(current, key)

    if not isinstance(collection, list):
        raise CloudInventoryTransientError(
            f"AWS {context} returned an invalid collection"
        )
    return collection


def _iter_collection(client, operation, path, context, *, kwargs=None, max_pages=None, max_items=None):
    """Yield a bounded, validated paginated AWS collection."""

    max_pages = max_pages or MAX_COLLECTION_PAGES
    max_items = max_items or MAX_COLLECTION_ITEMS
    request = dict(kwargs or {})
    item_count = 0
    page_count = 0

    try:
        try:
            pages = iter_pages(client, operation, **request)
        except TypeError:
            # Keep compatibility with the equivalent discovery helper shape
            # that accepts the collection path as its third positional value.
            pages = iter_pages(client, operation, path, **request)
        for page in pages:
            page_count += 1
            if page_count > max_pages:
                raise CloudInventoryTransientError(
                    f"AWS {context} exceeded the pagination bound"
                )

            if isinstance(page, list):
                values = page
            else:
                values = _required_collection(page, path, context)

            for value in values:
                item_count += 1
                if item_count > max_items:
                    raise CloudInventoryTransientError(
                        f"AWS {context} exceeded the item bound"
                    )
                yield value
    except CloudInventoryTransientError:
        raise
    except (ClientError, BotoCoreError) as error:
        raise _inventory_error(context, error) from error
    except (KeyError, TypeError, ValueError) as error:
        raise CloudInventoryTransientError(
            f"AWS {context} returned an invalid paginated response"
        ) from error


def _serialize_metadata(resource, **context):
    """Serialize a provider allowlist plus adapter context.

    ``serialize_aws`` owns datetime conversion, redaction, and size bounds.
    The adapter only adds non-sensitive identity/scope fields needed by the
    monitor and does not retain credentials or private-key material.
    """

    value = serialize_aws(resource if isinstance(resource, dict) else {})
    if not isinstance(value, dict):
        value = {"value": value}
    value = dict(value)
    value.update(context)
    return serialize_aws(_bound_value(value))


def _bound_value(value, depth=0):
    """Apply a final structural bound after the shared redaction pass."""

    if depth >= 8:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        return {
            key: _bound_value(child, depth + 1)
            for key, child in list(value.items())[:200]
        }
    if isinstance(value, (list, tuple)):
        return [_bound_value(child, depth + 1) for child in list(value)[:200]]
    if isinstance(value, str):
        return value[:4096]
    return value


def _display_name(value, fallback):
    value = str(value or fallback)
    return value[:100]


def _zone_id(value):
    return str(value or "").rsplit("/", 1)[-1]


def _route53_record_unique_id(hosted_zone_id, name, record_type, set_identifier=None):
    """Return a stable Route 53 record key bounded to the model's limit."""

    hosted_zone_id = _zone_id(hosted_zone_id)
    name = str(name or "")
    record_type = str(record_type or "")
    set_identifier = "" if set_identifier is None else str(set_identifier)
    composite = "|".join((hosted_zone_id, name, record_type, set_identifier))
    candidate = ":".join(
        (hosted_zone_id, name, record_type)
        + ((set_identifier,) if set_identifier else ())
    )
    if len(candidate) <= 100:
        return candidate
    digest = hashlib.sha256(composite.encode("utf-8")).hexdigest()[:48]
    return f"{hosted_zone_id[:35]}:record:{digest}"[:100]


_route53_record_key = _route53_record_unique_id
route53_record_unique_id = _route53_record_unique_id


def _stable_scoped_id(scope, region, resource_id):
    raw = ":".join(str(value or "") for value in (scope, region, resource_id))
    if len(raw) <= 100:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:48]
    return f"{str(scope)[:20]}:{digest}"


def _client(account, service, region=None):
    if region is None:
        return aws_client(account, service)
    return aws_client(account, service, region=region)


def _regions(account):
    regions = get_enabled_regions(account)
    if not isinstance(regions, (list, tuple, set)):
        raise CloudInventoryTransientError(
            "AWS enabled-region discovery returned an invalid collection"
        )
    normalized = sorted({str(region) for region in regions if region})
    if not normalized:
        raise CloudInventoryTransientError(
            "AWS enabled-region discovery returned no Regions"
        )
    return normalized


def _select_record(record):
    # ResourceRecords and AliasTarget are configuration data, not health-check
    # bodies.  Keep only fields useful for inventory and checks.
    allowed = (
        "Name",
        "Type",
        "TTL",
        "SetIdentifier",
        "Weight",
        "Region",
        "Failover",
        "GeoLocation",
        "MultiValueAnswer",
        "AliasTarget",
        "ResourceRecords",
        "HealthCheckId",
    )
    return {key: record[key] for key in allowed if key in record}


def _select_zone(zone):
    allowed = (
        "Id",
        "Name",
        "CallerReference",
        "Config",
        "ResourceRecordSetCount",
        "LinkedService",
    )
    return {key: zone[key] for key in allowed if key in zone}


def _select_distribution(distribution):
    allowed = (
        "Id",
        "ARN",
        "Status",
        "LastModifiedTime",
        "DomainName",
        "Aliases",
        "Origins",
        "DefaultCacheBehavior",
        "CacheBehaviors",
        "Comment",
        "Enabled",
        "PriceClass",
        "ViewerCertificate",
        "WebACLId",
        "Restrictions",
        "HttpVersion",
        "IsIPV6Enabled",
        "Staging",
    )
    selected = {key: distribution[key] for key in allowed if key in distribution}

    # CloudFront origins can contain arbitrary custom-header values.  They
    # are not needed for inventory/status checks and must never be persisted.
    origins = distribution.get("Origins")
    if isinstance(origins, dict) and isinstance(origins.get("Items"), list):
        origin_items = []
        for origin in origins["Items"][:MAX_COLLECTION_ITEMS]:
            if not isinstance(origin, dict):
                continue
            origin_allowed = (
                "Id",
                "DomainName",
                "OriginPath",
                "ConnectionAttempts",
                "ConnectionTimeout",
                "OriginShield",
                "OriginAccessControlId",
                "CustomOriginConfig",
                "S3OriginConfig",
            )
            origin_items.append({key: origin[key] for key in origin_allowed if key in origin})
        selected["Origins"] = dict(origins)
        selected["Origins"]["Items"] = origin_items
    return selected


def _select_oac(oac):
    allowed = ("Id", "Name", "Description", "SigningProtocol", "SigningBehavior", "OriginAccessControlOriginType")
    return {key: oac[key] for key in allowed if key in oac}


def _select_web_acl(web_acl):
    allowed = (
        "Name",
        "Id",
        "ARN",
        "Description",
        "DefaultAction",
        "VisibilityConfig",
        "Capacity",
        "Rules",
        "ManagedByFirewallManager",
    )
    selected = {key: web_acl[key] for key in allowed if key in web_acl}
    if isinstance(selected.get("Rules"), list):
        selected["Rules"] = selected["Rules"][:MAX_WAF_RULES]
    return selected


def _select_accelerator(accelerator):
    allowed = (
        "AcceleratorArn",
        "Name",
        "Status",
        "Enabled",
        "IpSets",
        "CreatedTime",
        "LastModifiedTime",
        "DnsName",
        "DualStackDnsName",
    )
    return {key: accelerator[key] for key in allowed if key in accelerator}


def _select_listener(listener):
    allowed = ("ListenerArn", "PortRanges", "Protocol", "ClientAffinity")
    return {key: listener[key] for key in allowed if key in listener}


def _select_certificate(certificate):
    allowed = (
        "CertificateArn",
        "DomainName",
        "SubjectAlternativeNames",
        "Status",
        "Type",
        "KeyAlgorithm",
        "SignatureAlgorithm",
        "Issuer",
        "CreatedAt",
        "IssuedAt",
        "ImportedAt",
        "NotBefore",
        "NotAfter",
        "RevokedAt",
        "RevocationReason",
        "InUseBy",
        "RenewalEligibility",
        "RenewalSummary",
        "DomainValidationOptions",
        "Options",
    )
    return {key: certificate[key] for key in allowed if key in certificate}


def _upsert_asset(model, account, unique_id, name, asset_type, metadata):
    defaults = {
        "name": _display_name(name, unique_id),
        "monitoring": UtilAsset.Monitoring.ACTIVE,
        "type": asset_type,
        "metadata": metadata,
    }
    asset, created = model.objects.get_or_create(
        owner=account,
        unique_id=unique_id,
        defaults=defaults,
    )
    asset.name = _display_name(name, unique_id)
    asset.type = asset_type
    asset.metadata = metadata
    if not created and asset.monitoring == UtilAsset.Monitoring.NO_LONGER_EXISTS:
        asset.monitoring = UtilAsset.Monitoring.ACTIVE
    asset.save()
    return asset


def _reconcile(model, account, current_ids):
    model.objects.filter(owner=account).exclude(
        unique_id__in=current_ids
    ).update(monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS)


def _collect_route53(account):
    client = _client(account, "route53")
    zones = list(_iter_collection(
        client,
        "list_hosted_zones",
        "HostedZones",
        "Route 53 hosted zones",
    ))
    zone_assets = []
    record_assets = []

    for zone in zones:
        if not isinstance(zone, dict):
            raise CloudInventoryTransientError(
                "AWS Route 53 returned an invalid hosted zone"
            )
        hosted_zone_id = _zone_id(zone.get("Id"))
        if not hosted_zone_id or not zone.get("Name"):
            raise CloudInventoryTransientError(
                "AWS Route 53 returned a hosted zone without identity"
            )
        zone_assets.append((
            hosted_zone_id,
            _display_name(zone.get("Name"), hosted_zone_id),
            _serialize_metadata(
                _select_zone(zone),
                _cloudmoo_region=ROUTE53_GLOBAL_ENDPOINT,
                _cloudmoo_endpoint="route53",
                _cloudmoo_name=str(zone.get("Name")),
                resource_id=hosted_zone_id,
            ),
        ))

        records = list(_iter_collection(
            client,
            "list_resource_record_sets",
            "ResourceRecordSets",
            f"Route 53 records for {hosted_zone_id}",
            kwargs={"HostedZoneId": hosted_zone_id},
            max_pages=MAX_RECORD_PAGES,
            max_items=MAX_RECORD_ITEMS_PER_ZONE,
        ))
        for record in records:
            if not isinstance(record, dict):
                raise CloudInventoryTransientError(
                    f"AWS Route 53 returned an invalid record in {hosted_zone_id}"
                )
            name = record.get("Name")
            record_type = record.get("Type")
            if not name or not record_type:
                raise CloudInventoryTransientError(
                    f"AWS Route 53 returned a record without identity in {hosted_zone_id}"
                )
            set_identifier = record.get("SetIdentifier")
            unique_id = _route53_record_unique_id(
                hosted_zone_id,
                name,
                record_type,
                set_identifier,
            )
            record_assets.append((
                unique_id,
                _display_name(name, unique_id),
                _serialize_metadata(
                    _select_record(record),
                    _cloudmoo_region=ROUTE53_GLOBAL_ENDPOINT,
                    _cloudmoo_endpoint="route53",
                    _cloudmoo_name=str(name),
                    resource_id=unique_id,
                    hosted_zone_id=hosted_zone_id,
                    record_name=str(name),
                    record_type=str(record_type),
                    set_identifier=str(set_identifier) if set_identifier is not None else "",
                ),
            ))
    return zone_assets, record_assets


def _collect_cloudfront(account):
    client = _client(
        account,
        "cloudfront",
        region=CLOUDFRONT_CONTROL_PLANE_REGION,
    )
    distributions = list(_iter_collection(
        client,
        "list_distributions",
        ("DistributionList", "Items"),
        "CloudFront distributions",
    ))
    distribution_assets = []
    for distribution in distributions:
        if not isinstance(distribution, dict) or not distribution.get("Id"):
            raise CloudInventoryTransientError(
                "AWS CloudFront returned a distribution without identity"
            )
        distribution_id = str(distribution["Id"])
        name = distribution.get("Comment") or distribution.get("DomainName") or distribution_id
        distribution_assets.append((
            distribution_id,
            _display_name(name, distribution_id),
            _serialize_metadata(
                _select_distribution(distribution),
                _cloudmoo_region=CLOUDFRONT_CONTROL_PLANE_REGION,
                _cloudmoo_endpoint="cloudfront",
                _cloudmoo_name=str(name),
                resource_id=distribution_id,
            ),
        ))

    oacs = list(_iter_collection(
        client,
        "list_origin_access_controls",
        ("OriginAccessControlList", "Items"),
        "CloudFront origin access controls",
    ))
    oac_assets = []
    for oac in oacs:
        if not isinstance(oac, dict) or not oac.get("Id"):
            raise CloudInventoryTransientError(
                "AWS CloudFront returned an origin access control without identity"
            )
        oac_id = str(oac["Id"])
        name = oac.get("Name") or oac_id
        oac_assets.append((
            oac_id,
            _display_name(name, oac_id),
            _serialize_metadata(
                _select_oac(oac),
                _cloudmoo_region=CLOUDFRONT_CONTROL_PLANE_REGION,
                _cloudmoo_endpoint="cloudfront",
                _cloudmoo_name=str(name),
                resource_id=oac_id,
            ),
        ))
    return distribution_assets, oac_assets


def _get_web_acl_detail(client, summary, scope):
    if not hasattr(client, "get_web_acl"):
        return summary
    name = summary.get("Name")
    web_acl_id = summary.get("Id")
    if not name or not web_acl_id:
        raise CloudInventoryTransientError(
            "AWS WAF returned a Web ACL summary without identity"
        )
    response = client.get_web_acl(Name=name, Scope=scope, Id=web_acl_id)
    if not isinstance(response, dict) or not isinstance(response.get("WebACL"), dict):
        raise CloudInventoryTransientError(
            "AWS WAF returned an incomplete Web ACL detail response"
        )
    return response["WebACL"]


def _collect_waf(account, regions):
    assets = []
    for scope, scope_region, scoped_regions in (
        (WAF_CLOUDFRONT_SCOPE, CLOUDFRONT_CONTROL_PLANE_REGION, [CLOUDFRONT_CONTROL_PLANE_REGION]),
        (WAF_REGIONAL_SCOPE, None, regions),
    ):
        for region in scoped_regions:
            client = _client(account, "wafv2", region=region)
            summaries = list(_iter_collection(
                client,
                "list_web_acls",
                "WebACLs",
                f"AWS WAF {scope} Web ACLs in {region}",
                kwargs={"Scope": scope},
            ))
            if len(summaries) > MAX_WEB_ACL_DETAILS:
                raise CloudInventoryTransientError(
                    f"AWS WAF {scope} Web ACL detail bound exceeded in {region}"
                )
            for summary in summaries:
                if not isinstance(summary, dict) or not summary.get("Id") or not summary.get("Name"):
                    raise CloudInventoryTransientError(
                        f"AWS WAF {scope} returned a Web ACL without identity"
                    )
                detail = _get_web_acl_detail(client, summary, scope)
                source = detail if isinstance(detail, dict) else summary
                web_acl_id = str(summary["Id"])
                unique_id = _stable_scoped_id(scope, region, web_acl_id)
                name = str(summary["Name"])
                assets.append((
                    unique_id,
                    _display_name(name, web_acl_id),
                    _serialize_metadata(
                        _select_web_acl(source),
                        _cloudmoo_region=region,
                        _cloudmoo_endpoint="wafv2",
                        _cloudmoo_scope=scope,
                        _cloudmoo_name=name,
                        resource_id=web_acl_id,
                        web_acl_name=name,
                    ),
                ))
    return assets


def _collect_global_accelerators(account):
    client = _client(
        account,
        "globalaccelerator",
        region=GLOBAL_ACCELERATOR_CONTROL_PLANE_REGION,
    )
    accelerators = list(_iter_collection(
        client,
        "list_accelerators",
        "Accelerators",
        "Global Accelerator accelerators",
    ))
    assets = []
    for accelerator in accelerators:
        if not isinstance(accelerator, dict) or not accelerator.get("AcceleratorArn"):
            raise CloudInventoryTransientError(
                "AWS Global Accelerator returned an accelerator without identity"
            )
        arn = str(accelerator["AcceleratorArn"])
        listeners = []
        if hasattr(client, "list_listeners"):
            listeners = list(_iter_collection(
                client,
                "list_listeners",
                "Listeners",
                f"Global Accelerator listeners for {arn}",
                kwargs={"AcceleratorArn": arn},
                max_pages=MAX_ACCELERATOR_LISTENER_PAGES,
                max_items=MAX_ACCELERATOR_LISTENER_ITEMS,
            ))
        listener_payload = [
            _select_listener(listener)
            for listener in listeners
            if isinstance(listener, dict)
        ][:MAX_ACCELERATOR_LISTENERS]
        source = _select_accelerator(accelerator)
        source["Listeners"] = listener_payload
        source["ListenerCount"] = len(listener_payload)
        name = accelerator.get("Name") or arn.rsplit("/", 1)[-1]
        assets.append((
            arn,
            _display_name(name, arn),
            _serialize_metadata(
                source,
                _cloudmoo_region=GLOBAL_ACCELERATOR_CONTROL_PLANE_REGION,
                _cloudmoo_endpoint="globalaccelerator",
                _cloudmoo_name=str(name),
                resource_id=arn,
            ),
        ))
    return assets


def _collect_certificates(account, regions):
    assets = []
    for region in regions:
        client = _client(account, "acm", region=region)
        summaries = list(_iter_collection(
            client,
            "list_certificates",
            "CertificateSummaryList",
            f"ACM certificates in {region}",
        ))
        for summary in summaries:
            if not isinstance(summary, dict) or not summary.get("CertificateArn"):
                raise CloudInventoryTransientError(
                    f"AWS ACM returned a certificate summary without identity in {region}"
                )
            arn = str(summary["CertificateArn"])
            response = client.describe_certificate(CertificateArn=arn)
            if not isinstance(response, dict) or not isinstance(response.get("Certificate"), dict):
                raise CloudInventoryTransientError(
                    f"AWS ACM returned an incomplete certificate detail response in {region}"
                )
            certificate = response["Certificate"]
            if certificate.get("CertificateArn") and str(certificate["CertificateArn"]) != arn:
                raise CloudInventoryTransientError(
                    f"AWS ACM returned mismatched certificate identity in {region}"
                )
            name = certificate.get("DomainName") or summary.get("DomainName") or arn.rsplit("/", 1)[-1]
            assets.append((
                arn,
                _display_name(name, arn),
                _serialize_metadata(
                    _select_certificate(certificate),
                    _cloudmoo_region=region,
                    _cloudmoo_endpoint="acm",
                    _cloudmoo_name=str(name),
                    resource_id=arn,
                    certificate_arn=arn,
                ),
            ))
    return assets


def _apply_assets(account, model, asset_type, assets):
    current_ids = []
    for unique_id, name, metadata in assets:
        _upsert_asset(model, account, unique_id, name, asset_type, metadata)
        current_ids.append(unique_id)
    _reconcile(model, account, current_ids)
    return len(current_ids)


def _failure_summary(error):
    return {
        "code": aws_error_code(error),
        "kind": "incomplete_inventory" if isinstance(error, CloudInventoryTransientError) else "provider",
    }


def sync_aws_edge_assets(account):
    """Synchronize Route 53, CloudFront, WAF, and Global Accelerator assets.

    Each provider collection is staged completely before its local rows are
    reconciled.  A failure in one hosted-zone record listing, WAF Region, or
    child listener listing therefore cannot mark the unseen local assets as
    deleted.
    """

    errors = []
    try:
        regions = _regions(account)
    except Exception as error:
        failure = _failure_summary(error)
        return {
            "regions": [],
            "counts": {},
            "synced": {},
            "families": {},
            "errors": [{"family": "aws_regions", **failure}],
            "control_plane_regions": {
                "cloudfront": CLOUDFRONT_CONTROL_PLANE_REGION,
                "globalaccelerator": GLOBAL_ACCELERATOR_CONTROL_PLANE_REGION,
                "waf_cloudfront": CLOUDFRONT_CONTROL_PLANE_REGION,
            },
        }

    collections = {}
    for family, callback in (
        ("route53", lambda: _collect_route53(account)),
        ("cloudfront", lambda: _collect_cloudfront(account)),
        ("wafv2", lambda: _collect_waf(account, regions)),
        ("globalaccelerator", lambda: _collect_global_accelerators(account)),
    ):
        try:
            collections[family] = callback()
        except Exception as error:
            failure = _failure_summary(error)
            errors.append({"family": family, **failure})

    counts = {}
    families = {}

    def apply_family(asset_type, model, collection, family):
        if family not in collections:
            counts[asset_type] = None
            families[asset_type] = {
                "complete": False,
                "reconciled": False,
                "count": None,
            }
            return
        assets = collection
        count = _apply_assets(account, model, asset_type, assets)
        counts[asset_type] = count
        families[asset_type] = {
            "complete": True,
            "reconciled": True,
            "count": count,
        }

    route53 = collections.get("route53")
    apply_family(
        AWS_ROUTE53_ZONE,
        CoreAWSRoute53Zone,
        route53[0] if route53 is not None else None,
        "route53",
    )
    apply_family(
        AWS_ROUTE53_RECORD,
        CoreAWSRoute53Record,
        route53[1] if route53 is not None else None,
        "route53",
    )

    cloudfront = collections.get("cloudfront")
    apply_family(
        AWS_CLOUDFRONT_DISTRIBUTION,
        CoreAWSCloudFrontDistribution,
        cloudfront[0] if cloudfront is not None else None,
        "cloudfront",
    )
    apply_family(
        AWS_CLOUDFRONT_ORIGIN_ACCESS_CONTROL,
        CoreAWSCloudFrontOriginAccessControl,
        cloudfront[1] if cloudfront is not None else None,
        "cloudfront",
    )
    apply_family(AWS_WAF_WEB_ACL, CoreAWSWAFWebACL, collections.get("wafv2"), "wafv2")
    apply_family(
        AWS_GLOBAL_ACCELERATOR,
        CoreAWSGlobalAccelerator,
        collections.get("globalaccelerator"),
        "globalaccelerator",
    )

    return {
        "regions": regions,
        "counts": counts,
        "synced": counts,
        "families": families,
        "errors": errors,
        "control_plane_regions": {
            "cloudfront": CLOUDFRONT_CONTROL_PLANE_REGION,
            "globalaccelerator": GLOBAL_ACCELERATOR_CONTROL_PLANE_REGION,
            "waf_cloudfront": CLOUDFRONT_CONTROL_PLANE_REGION,
        },
    }


def sync_aws_regional_certificates(account):
    """Synchronize ACM certificates from every enabled AWS Region."""

    try:
        regions = _regions(account)
        certificate_assets = _collect_certificates(account, regions)
    except Exception as error:
        return {
            "regions": locals().get("regions", []),
            "count": None,
            "counts": {AWS_ACM_CERTIFICATE: None},
            "synced": None,
            "complete": False,
            "reconciled": False,
            "errors": [{"family": AWS_ACM_CERTIFICATE, **_failure_summary(error)}],
        }

    # This is intentionally one reconciliation after every Region and every
    # describe_certificate call has succeeded.  The legacy account sync only
    # queried one Region; this helper must never hide certificates from other
    # Regions after a partial response.
    count = 0
    current_ids = []
    for unique_id, name, metadata in certificate_assets:
        _upsert_asset(
            CoreAWSACMCertificate,
            account,
            unique_id,
            name,
            AWS_ACM_CERTIFICATE,
            metadata,
        )
        current_ids.append(unique_id)
        count += 1
    _reconcile(CoreAWSACMCertificate, account, current_ids)
    return {
        "regions": regions,
        "count": count,
        "counts": {AWS_ACM_CERTIFICATE: count},
        "synced": count,
        "complete": True,
        "reconciled": True,
        "errors": [],
    }


__all__ = [
    "AWS_ACM_CERTIFICATE",
    "ASSET_TYPE_ROUTE53_ZONE",
    "ASSET_TYPE_ROUTE53_RECORD",
    "ASSET_TYPE_CLOUDFRONT_DISTRIBUTION",
    "ASSET_TYPE_CLOUDFRONT_ORIGIN_ACCESS_CONTROL",
    "ASSET_TYPE_WAF_WEB_ACL",
    "ASSET_TYPE_GLOBAL_ACCELERATOR",
    "AWS_CLOUDFRONT_DISTRIBUTION",
    "AWS_CLOUDFRONT_ORIGIN_ACCESS_CONTROL",
    "AWS_EDGE_ASSET_TYPES",
    "EDGE_ASSET_TYPES",
    "AWS_GLOBAL_ACCELERATOR",
    "AWS_ROUTE53_RECORD",
    "AWS_ROUTE53_ZONE",
    "AWS_WAF_WEB_ACL",
    "CLOUDFRONT_CONTROL_PLANE_REGION",
    "CoreAWSEdgeAsset",
    "CoreAWSCloudFrontDistribution",
    "CoreAWSCloudFrontOriginAccessControl",
    "CoreAWSCloudFrontOAC",
    "CoreAWSGlobalAccelerator",
    "CoreAWSRoute53HostedZone",
    "CoreAWSRoute53Record",
    "CoreAWSRoute53Zone",
    "CoreAWSWAFWebACL",
    "CoreAWSWAFWebAcl",
    "EDGE_MODEL_BY_TYPE",
    "AWS_EDGE_ASSET_MODELS",
    "GLOBAL_ACCELERATOR_CONTROL_PLANE_REGION",
    "ROUTE53_GLOBAL_ENDPOINT",
    "WAF_CLOUDFRONT_SCOPE",
    "WAF_REGIONAL_SCOPE",
    "_route53_record_unique_id",
    "_route53_record_key",
    "route53_record_unique_id",
    "sync_aws_edge_assets",
    "sync_aws_regional_certificates",
]
