"""Read-only AWS network and EC2 dependency inventory.

The legacy AWS adapter stores the first generation of EC2 assets in one
module.  This adapter deliberately keeps the networking/dependency surface in
its own models so it can be introduced without changing shared asset choices
or migrations.  Every provider identifier is scoped to its AWS region and
every provider payload crosses the shared AWS serializer before it is stored.

Only Describe/List/Get-style AWS operations are used here.  In particular,
an incomplete collection is never treated as empty: reconciliation happens
only after the whole family/region collection has been validated.
"""

import hashlib
import logging
from urllib.parse import quote

from django.db import models
from botocore.exceptions import BotoCoreError, ClientError

from apps.console.cloud.aws.discovery import (
    aws_client,
    aws_error_code,
    get_enabled_regions,
    is_transient_aws_error,
    iter_pages,
    require_collection,
    serialize_aws,
)
from apps.console.cloud.aws.models import CoreAWSAccount
from apps.console.cloud.models import CloudInventoryTransientError
from apps.console.utils.models import UtilAsset
from apps.monitoring.metadata import redact_sensitive_metadata


logger = logging.getLogger(__name__)


def _owner_identifier_constraint(name):
    return models.UniqueConstraint(
        fields=("owner", "unique_id"),
        name=f"aws_network_{name}_owner_uid_uniq",
    )


class CoreAWSRegionalAsset(UtilAsset):
    """Common fields and monitoring context for regional AWS resources."""

    # UtilAsset's historical limit is too small for a namespaced ASG name or
    # launch-template identifier.  The raw provider ID remains in metadata
    # when the bounded local key has to use a digest.
    unique_id = models.CharField(max_length=255)
    region = models.CharField(max_length=64)
    owner = models.ForeignKey(
        CoreAWSAccount,
        on_delete=models.CASCADE,
        related_name="%(class)s_assets",
    )

    # Subclasses provide both values.  ``provider_type`` is intentionally
    # provider-qualified even though shared UtilAsset choices are updated by
    # the integration lane later.
    provider_type = None
    asset_type = None

    class Meta:
        abstract = True

    @property
    def monitoring_credentials(self):
        """Return the regional context consumed by the read-only checker."""
        return {
            "access_key": self.owner.access_key,
            "secret_key": self.owner.secret_key,
            "region": self.region,
            "resource_region": self.region,
            "asset_type": self.asset_type,
            "provider_type": self.provider_type,
            "metadata": self.metadata if isinstance(self.metadata, dict) else {},
        }

    @property
    def provider_url(self):
        region = quote(str(self.region), safe="-")
        return f"https://{region}.console.aws.amazon.com/vpc/home?region={region}"

    def check_status(self):
        from apps.monitoring.checks.aws_network import check_aws_asset_status

        return check_aws_asset_status(
            self.asset_type,
            self.unique_id,
            self.monitoring_credentials,
        )

    def save(self, *args, **kwargs):
        # UtilAsset.save already applies the shared redaction boundary.  Keep
        # this explicit at the AWS base as well so metadata is safe even if a
        # future shared model changes its persistence hook.
        if self.metadata is not None:
            self.metadata = redact_sensitive_metadata(self.metadata)
        return super().save(*args, **kwargs)


class CoreAWSVPC(CoreAWSRegionalAsset):
    provider_type = "aws_vpc"
    asset_type = "vpc"

    class Meta:
        db_table = "core_aws_vpc"
        constraints = [_owner_identifier_constraint("vpc")]


class CoreAWSSubnet(CoreAWSRegionalAsset):
    provider_type = "aws_subnet"
    asset_type = "subnet"

    class Meta:
        db_table = "core_aws_subnet"
        constraints = [_owner_identifier_constraint("subnet")]


class CoreAWSRouteTable(CoreAWSRegionalAsset):
    provider_type = "aws_route_table"
    asset_type = "route_table"

    class Meta:
        db_table = "core_aws_route_table"
        constraints = [_owner_identifier_constraint("route_table")]


class CoreAWSInternetGateway(CoreAWSRegionalAsset):
    provider_type = "aws_internet_gateway"
    asset_type = "internet_gateway"

    class Meta:
        db_table = "core_aws_internet_gateway"
        constraints = [_owner_identifier_constraint("internet_gateway")]


class CoreAWSNATGateway(CoreAWSRegionalAsset):
    provider_type = "aws_nat_gateway"
    asset_type = "nat_gateway"

    class Meta:
        db_table = "core_aws_nat_gateway"
        constraints = [_owner_identifier_constraint("nat_gateway")]


class CoreAWSNetworkACL(CoreAWSRegionalAsset):
    provider_type = "aws_network_acl"
    asset_type = "network_acl"

    class Meta:
        db_table = "core_aws_network_acl"
        constraints = [_owner_identifier_constraint("network_acl")]


class CoreAWSNetworkInterface(CoreAWSRegionalAsset):
    provider_type = "aws_network_interface"
    asset_type = "network_interface"

    class Meta:
        db_table = "core_aws_network_interface"
        constraints = [_owner_identifier_constraint("network_interface")]


class CoreAWSVPCPeering(CoreAWSRegionalAsset):
    provider_type = "aws_vpc_peering"
    asset_type = "vpc_peering"

    class Meta:
        db_table = "core_aws_vpc_peering"
        constraints = [_owner_identifier_constraint("vpc_peering")]


class CoreAWSTransitGatewayAttachment(CoreAWSRegionalAsset):
    provider_type = "aws_transit_gateway_attachment"
    asset_type = "transit_gateway_attachment"

    class Meta:
        db_table = "core_aws_transit_gateway_attachment"
        constraints = [_owner_identifier_constraint("transit_gateway_attachment")]


class CoreAWSVPNConnection(CoreAWSRegionalAsset):
    provider_type = "aws_vpn_connection"
    asset_type = "vpn_connection"

    class Meta:
        db_table = "core_aws_vpn_connection"
        constraints = [_owner_identifier_constraint("vpn_connection")]


class CoreAWSFlowLog(CoreAWSRegionalAsset):
    provider_type = "aws_flow_log"
    asset_type = "flow_log"

    class Meta:
        db_table = "core_aws_flow_log"
        constraints = [_owner_identifier_constraint("flow_log")]


class CoreAWSAutoScalingGroup(CoreAWSRegionalAsset):
    provider_type = "aws_auto_scaling_group"
    asset_type = "auto_scaling_group"

    class Meta:
        db_table = "core_aws_auto_scaling_group"
        constraints = [_owner_identifier_constraint("auto_scaling_group")]


class CoreAWSLaunchTemplate(CoreAWSRegionalAsset):
    provider_type = "aws_launch_template"
    asset_type = "launch_template"

    class Meta:
        db_table = "core_aws_launch_template"
        constraints = [_owner_identifier_constraint("launch_template")]


class CoreAWSAMI(CoreAWSRegionalAsset):
    provider_type = "aws_ami"
    asset_type = "ami"

    class Meta:
        db_table = "core_aws_ami"
        constraints = [_owner_identifier_constraint("ami")]


class CoreAWSEBSVolumeAttachment(CoreAWSRegionalAsset):
    provider_type = "aws_ebs_attachment"
    asset_type = "ebs_attachment"

    class Meta:
        db_table = "core_aws_ebs_volume_attachment"
        constraints = [_owner_identifier_constraint("ebs_attachment")]


# Naming aliases keep the adapter easy to consume from callers that use the
# long AWS API resource names without registering duplicate Django models.
CoreAWSVPCPeeringConnection = CoreAWSVPCPeering
CoreAWSNetworkAcl = CoreAWSNetworkACL
CoreAWSNatGateway = CoreAWSNATGateway
CoreAWSVPCFlowLog = CoreAWSFlowLog
CoreAWSImage = CoreAWSAMI
CoreAWSEBSAttachment = CoreAWSEBSVolumeAttachment
CoreAWSEBSVolumeAttachments = CoreAWSEBSVolumeAttachment


def _resource_key(region, raw_id):
    """Build a stable regional key while retaining long raw IDs in metadata."""
    region = str(region).strip()
    raw_id = str(raw_id).strip()
    value = f"{region}|{raw_id}"
    if len(value) <= 255:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"{region}|sha256:{digest}"[:255]


def _display_name(item, raw_id, *keys):
    if isinstance(item, dict):
        for key in keys:
            value = item.get(key)
            if value not in (None, ""):
                return str(value)[:100]
        tags = item.get("Tags")
        if isinstance(tags, list):
            for tag in tags:
                if isinstance(tag, dict) and tag.get("Key") == "Name" and tag.get("Value"):
                    return str(tag["Value"])[:100]
    return str(raw_id)[:100]


def _metadata(item, provider_type, region, raw_id, identifier_field, **extra):
    value = serialize_aws(item)
    if not isinstance(value, dict):
        raise CloudInventoryTransientError("AWS returned an invalid resource object")
    value.update(
        {
            "_cloudmoo_provider_type": provider_type,
            "_cloudmoo_region": region,
            "_cloudmoo_raw_id": raw_id,
            "_cloudmoo_identifier_field": identifier_field,
        }
    )
    value.update(extra)
    return redact_sensitive_metadata(value)


def _require_item_identifier(item, identifier_field, context):
    if not isinstance(item, dict):
        raise CloudInventoryTransientError(
            f"AWS returned an invalid {context} resource object"
        )
    value = item.get(identifier_field)
    if value in (None, ""):
        raise CloudInventoryTransientError(
            f"AWS returned a {context} resource without an identifier"
        )
    return str(value)


def _iter_collection(client, spec):
    """Yield a validated collection from every page of one AWS operation."""
    for page in iter_pages(client, spec["operation"], **spec.get("kwargs", {})):
        collection = require_collection(
            page,
            spec["response_key"],
            f"{spec['provider_type']} inventory",
        )
        for item in collection:
            yield item


def _normal_record(item, spec, region):
    raw_id = _require_item_identifier(item, spec["identifier"], spec["provider_type"])
    return {
        "unique_id": _resource_key(region, raw_id),
        "raw_id": raw_id,
        "name": _display_name(item, raw_id, *spec.get("name_keys", ())),
        "metadata": _metadata(
            item,
            spec["provider_type"],
            region,
            raw_id,
            spec["identifier"],
        ),
    }


def _ebs_attachment_records(items, spec, region):
    records = []
    for volume in items:
        volume_id = _require_item_identifier(volume, "VolumeId", "aws_ebs_attachment")
        attachments = require_collection(
            volume,
            "Attachments",
            "aws_ebs_attachment inventory",
        )
        for attachment in attachments:
            if not isinstance(attachment, dict):
                raise CloudInventoryTransientError(
                    "AWS returned an invalid aws_ebs_attachment resource object"
                )
            instance_id = attachment.get("InstanceId") or "unattached"
            device = attachment.get("Device") or "unknown-device"
            attachment_id = attachment.get("AttachmentId")
            # AttachmentId is normally present.  The composite fallback keeps
            # a stable identity for older/fake API responses while retaining
            # every raw component needed by the status checker.
            raw_id = str(attachment_id or f"{volume_id}|{instance_id}|{device}")
            metadata = _metadata(
                attachment,
                spec["provider_type"],
                region,
                raw_id,
                "AttachmentId",
                _cloudmoo_volume_id=volume_id,
                _cloudmoo_instance_id=str(instance_id),
                _cloudmoo_device=str(device),
                _cloudmoo_attachment_id=str(attachment_id or ""),
                _cloudmoo_volume=serialize_aws(volume),
            )
            records.append(
                {
                    "unique_id": _resource_key(region, raw_id),
                    "raw_id": raw_id,
                    "name": f"{volume_id} / {instance_id}"[:100],
                    "metadata": metadata,
                }
            )
    return records


def _upsert_records(account, model, provider_type, region, records):
    # ``provider_type`` identifies the AWS API family, while the shared asset
    # registry deliberately uses provider-neutral values for network assets
    # (for example, ``vpc`` and ``nat_gateway``).  Persist the latter so
    # scheduling, dashboard inventory, and migration choices agree with the
    # relation type returned by CoreCloud.
    asset_type = getattr(model, "asset_type", None) or provider_type
    current_ids = []
    for record in records:
        current_ids.append(record["unique_id"])
        defaults = {
            "region": region,
            "name": record["name"],
            "monitoring": model.Monitoring.ACTIVE,
            "type": asset_type,
            "metadata": record["metadata"],
        }
        asset, created = model.objects.get_or_create(
            owner=account,
            unique_id=record["unique_id"],
            defaults=defaults,
        )
        if not created:
            asset.region = region
            asset.name = record["name"]
            asset.type = asset_type
            asset.metadata = record["metadata"]
            if asset.monitoring == model.Monitoring.NO_LONGER_EXISTS:
                asset.monitoring = model.Monitoring.ACTIVE
            asset.save()

    # A successful empty collection is authoritative, but only for this
    # family and region.  Failed regions never reach this function.
    model.objects.filter(owner=account, region=region).exclude(
        unique_id__in=current_ids
    ).update(monitoring=model.Monitoring.NO_LONGER_EXISTS)
    return len(records)


def _collect_family(account, spec, region):
    client = aws_client(account, spec["service"], region=region)
    items = list(_iter_collection(client, spec))
    if spec.get("builder") == "ebs_attachments":
        records = _ebs_attachment_records(items, spec, region)
    else:
        records = [_normal_record(item, spec, region) for item in items]
    return records


def _error_summary(error):
    code = aws_error_code(error)
    if isinstance(error, CloudInventoryTransientError):
        kind = "incomplete_inventory"
    elif is_transient_aws_error(error):
        kind = "transient"
    elif isinstance(error, (ClientError, BotoCoreError)):
        kind = "provider"
    else:
        kind = "adapter"
    return {"code": code, "kind": kind}


_COLLECTION_SPECS = (
    {
        "provider_type": "aws_vpc",
        "model": CoreAWSVPC,
        "service": "ec2",
        "operation": "describe_vpcs",
        "response_key": "Vpcs",
        "identifier": "VpcId",
        "name_keys": ("VpcId",),
    },
    {
        "provider_type": "aws_subnet",
        "model": CoreAWSSubnet,
        "service": "ec2",
        "operation": "describe_subnets",
        "response_key": "Subnets",
        "identifier": "SubnetId",
        "name_keys": ("SubnetId",),
    },
    {
        "provider_type": "aws_route_table",
        "model": CoreAWSRouteTable,
        "service": "ec2",
        "operation": "describe_route_tables",
        "response_key": "RouteTables",
        "identifier": "RouteTableId",
        "name_keys": ("RouteTableId",),
    },
    {
        "provider_type": "aws_internet_gateway",
        "model": CoreAWSInternetGateway,
        "service": "ec2",
        "operation": "describe_internet_gateways",
        "response_key": "InternetGateways",
        "identifier": "InternetGatewayId",
        "name_keys": ("InternetGatewayId",),
    },
    {
        "provider_type": "aws_nat_gateway",
        "model": CoreAWSNATGateway,
        "service": "ec2",
        "operation": "describe_nat_gateways",
        "response_key": "NatGateways",
        "identifier": "NatGatewayId",
        "name_keys": ("NatGatewayId",),
    },
    {
        "provider_type": "aws_network_acl",
        "model": CoreAWSNetworkACL,
        "service": "ec2",
        "operation": "describe_network_acls",
        "response_key": "NetworkAcls",
        "identifier": "NetworkAclId",
        "name_keys": ("NetworkAclId",),
    },
    {
        "provider_type": "aws_network_interface",
        "model": CoreAWSNetworkInterface,
        "service": "ec2",
        "operation": "describe_network_interfaces",
        "response_key": "NetworkInterfaces",
        "identifier": "NetworkInterfaceId",
        "name_keys": ("NetworkInterfaceId",),
    },
    {
        "provider_type": "aws_vpc_peering",
        "model": CoreAWSVPCPeering,
        "service": "ec2",
        "operation": "describe_vpc_peering_connections",
        "response_key": "VpcPeeringConnections",
        "identifier": "VpcPeeringConnectionId",
        "name_keys": ("VpcPeeringConnectionId",),
    },
    {
        "provider_type": "aws_transit_gateway_attachment",
        "model": CoreAWSTransitGatewayAttachment,
        "service": "ec2",
        "operation": "describe_transit_gateway_attachments",
        "response_key": "TransitGatewayAttachments",
        "identifier": "TransitGatewayAttachmentId",
        "name_keys": ("TransitGatewayAttachmentId",),
    },
    {
        "provider_type": "aws_vpn_connection",
        "model": CoreAWSVPNConnection,
        "service": "ec2",
        "operation": "describe_vpn_connections",
        "response_key": "VpnConnections",
        "identifier": "VpnConnectionId",
        "name_keys": ("VpnConnectionId",),
    },
    {
        "provider_type": "aws_flow_log",
        "model": CoreAWSFlowLog,
        "service": "ec2",
        "operation": "describe_flow_logs",
        "response_key": "FlowLogs",
        "identifier": "FlowLogId",
        "name_keys": ("FlowLogId",),
    },
    {
        "provider_type": "aws_auto_scaling_group",
        "model": CoreAWSAutoScalingGroup,
        "service": "autoscaling",
        "operation": "describe_auto_scaling_groups",
        "response_key": "AutoScalingGroups",
        "identifier": "AutoScalingGroupName",
        "name_keys": ("AutoScalingGroupName",),
    },
    {
        "provider_type": "aws_launch_template",
        "model": CoreAWSLaunchTemplate,
        "service": "ec2",
        "operation": "describe_launch_templates",
        "response_key": "LaunchTemplates",
        "identifier": "LaunchTemplateId",
        "name_keys": ("LaunchTemplateName", "LaunchTemplateId"),
    },
    {
        "provider_type": "aws_ami",
        "model": CoreAWSAMI,
        "service": "ec2",
        "operation": "describe_images",
        "response_key": "Images",
        "identifier": "ImageId",
        "name_keys": ("Name", "ImageId"),
        "kwargs": {"Owners": ["self"]},
    },
    {
        "provider_type": "aws_ebs_attachment",
        "model": CoreAWSEBSVolumeAttachment,
        "service": "ec2",
        "operation": "describe_volumes",
        "response_key": "Volumes",
        "identifier": "VolumeId",
        "builder": "ebs_attachments",
    },
)


# Public alias for integration code and tests that want to inspect the
# complete inventory contract without importing private implementation names.
AWS_NETWORK_COLLECTION_SPECS = _COLLECTION_SPECS


def sync_aws_network_assets(account):
    """Inventory AWS network/EC2 dependencies with regional fail-closed sync.

    The return value is intentionally useful to callers that need to expose
    partial progress: ``families[provider_type][region]`` contains a count,
    completion flag, and (when applicable) a safe error code/classification.
    """
    regions = get_enabled_regions(account)
    if not isinstance(regions, (list, tuple)):
        raise CloudInventoryTransientError("AWS enabled regions are invalid")
    regions = sorted({str(region).strip() for region in regions if str(region).strip()})

    summary = {
        "regions": regions,
        "families": {},
        "counts": {},
        "errors": [],
    }

    for spec in _COLLECTION_SPECS:
        provider_type = spec["provider_type"]
        family_summary = {}
        for region in regions:
            try:
                records = _collect_family(account, spec, region)
                count = _upsert_records(
                    account,
                    spec["model"],
                    provider_type,
                    region,
                    records,
                )
                family_summary[region] = {
                    "status": "ok",
                    "complete": True,
                    "reconciled": True,
                    "count": count,
                }
            except Exception as error:
                error_summary = _error_summary(error)
                family_summary[region] = {
                    "status": "error",
                    "complete": False,
                    "reconciled": False,
                    "count": None,
                    "error": error_summary,
                }
                summary["errors"].append(
                    {
                        "family": provider_type,
                        "region": region,
                        **error_summary,
                    }
                )
                logger.warning(
                    "AWS %s inventory failed for %s (%s)",
                    provider_type,
                    region,
                    error_summary["code"],
                )

        summary["families"][provider_type] = family_summary
        # Keep the provider-qualified family directly addressable as well as
        # under ``families`` for small callers that do not need the envelope.
        summary[provider_type] = family_summary
        summary["counts"][provider_type] = {
            region: result["count"] for region, result in family_summary.items()
        }

    return summary


__all__ = [
    "AWS_NETWORK_COLLECTION_SPECS",
    "CoreAWSRegionalAsset",
    "CoreAWSVPC",
    "CoreAWSSubnet",
    "CoreAWSRouteTable",
    "CoreAWSInternetGateway",
    "CoreAWSNATGateway",
    "CoreAWSNatGateway",
    "CoreAWSNetworkACL",
    "CoreAWSNetworkInterface",
    "CoreAWSVPCPeering",
    "CoreAWSVPCPeeringConnection",
    "CoreAWSTransitGatewayAttachment",
    "CoreAWSVPNConnection",
    "CoreAWSFlowLog",
    "CoreAWSVPCFlowLog",
    "CoreAWSAutoScalingGroup",
    "CoreAWSLaunchTemplate",
    "CoreAWSAMI",
    "CoreAWSImage",
    "CoreAWSEBSVolumeAttachment",
    "CoreAWSEBSAttachment",
    "sync_aws_network_assets",
]
