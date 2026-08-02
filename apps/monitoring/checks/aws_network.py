"""Read-only AWS network and EC2 dependency status checks."""

import boto3

from botocore.exceptions import BotoCoreError, ClientError

from apps.console.cloud.aws.discovery import (
    aws_error_code,
    is_transient_aws_error,
    require_collection,
    serialize_aws,
)
from apps.console.cloud.aws.models import AWS_CLIENT_CONFIG
from apps.console.cloud.models import CloudInventoryTransientError


_NOT_FOUND_CODES = frozenset(
    {
        "InvalidAMIID.NotFound",
        "InvalidGroupId.NotFound",
        "InvalidInternetGatewayID.NotFound",
        "InvalidNetworkAclID.NotFound",
        "InvalidNetworkInterfaceID.NotFound",
        "InvalidRouteTableID.NotFound",
        "InvalidSubnetID.NotFound",
        "InvalidTransitGatewayAttachmentID.NotFound",
        "InvalidVpcID.NotFound",
        "InvalidVpcPeeringConnectionID.NotFound",
        "InvalidVpnConnectionID.NotFound",
        "InvalidFlowLogId.NotFound",
        "InvalidLaunchTemplateId.NotFound",
        "InvalidNatGatewayID.NotFound",
        "ResourceNotFoundException",
        "AutoScalingGroupNotFound",
        "AutoScalingGroupNotFoundException",
    }
)
_AUTH_CODES = frozenset(
    {
        "AccessDenied",
        "AccessDeniedException",
        "AuthFailure",
        "ExpiredToken",
        "InvalidClientTokenId",
        "UnauthorizedOperation",
        "UnrecognizedClientException",
    }
)


def _parse_identity(unique_id, credentials):
    """Return ``(region, raw_id)`` without trusting a cross-region request."""
    if not isinstance(unique_id, str) or not unique_id.strip():
        raise ValueError("AWS resource identifier is invalid")

    credentials = credentials if isinstance(credentials, dict) else {}
    stored_regions = {
        str(value).strip()
        for value in (
            credentials.get("resource_region"),
            credentials.get("region"),
        )
        if value
    }
    metadata = credentials.get("metadata")
    if isinstance(metadata, dict) and metadata.get("_cloudmoo_region"):
        stored_regions.add(str(metadata["_cloudmoo_region"]).strip())
    if len(stored_regions) > 1:
        raise ValueError("AWS resource region context is inconsistent")
    stored_region = next(iter(stored_regions), "")

    value = unique_id.strip()
    identity_region = ""
    raw_id = value
    parts = value.split("|")
    if value.startswith("arn:"):
        # AWS ARNs are already globally unique for the resource families that
        # use them.  The regional client still comes from stored context.
        raw_id = value
    elif len(parts) >= 2:
        if parts[0].startswith("aws_") and len(parts) >= 3:
            identity_region = parts[1].strip()
            raw_id = "|".join(parts[2:]).strip()
        else:
            identity_region = parts[0].strip()
            raw_id = "|".join(parts[1:]).strip()

    region = stored_region or identity_region
    if not region or not raw_id:
        raise ValueError("AWS resource regional identity is invalid")
    if identity_region and stored_region and identity_region != stored_region:
        raise ValueError("AWS resource region does not match stored context")
    return region, raw_id


def _credentials(credentials, region):
    if not isinstance(credentials, dict):
        raise ValueError("AWS monitoring credentials are invalid")
    access_key = credentials.get("access_key") or credentials.get("aws_access_key_id")
    secret_key = credentials.get("secret_key") or credentials.get("aws_secret_access_key")
    if not access_key or not secret_key or not region:
        raise ValueError("AWS monitoring credentials are incomplete")
    return access_key, secret_key


def _client(credentials, service, region):
    access_key, secret_key = _credentials(credentials, region)
    return boto3.client(
        service,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=AWS_CLIENT_CONFIG,
    )


def _normal_status(value, fallback="available"):
    if value in (None, ""):
        return fallback
    value = str(value).strip().lower()
    return value.replace(" ", "_").replace("-", "_") or fallback


def _status_from_record(asset_type, record):
    if not isinstance(record, dict):
        raise CloudInventoryTransientError("AWS returned an invalid status resource")

    if asset_type == "vpc_peering":
        status = record.get("Status")
        if isinstance(status, dict):
            return _normal_status(status.get("Code"))
        return _normal_status(status)
    if asset_type == "flow_log":
        return _normal_status(record.get("FlowLogStatus") or record.get("DeliverLogsStatus"))
    if asset_type == "network_interface":
        return _normal_status(record.get("Status"))
    if asset_type in {
        "nat_gateway",
        "transit_gateway_attachment",
        "vpn_connection",
        "ami",
        "ebs_attachment",
    }:
        return _normal_status(record.get("State"))
    if asset_type == "auto_scaling_group":
        # DescribeAutoScalingGroups has no single health field.  A returned
        # group is available to monitor; instance health is a separate API.
        return "active"
    if asset_type == "launch_template":
        return "available"
    return _normal_status(record.get("State"))


def _error_status(error):
    """Normalize errors without returning provider messages or credentials."""
    code = aws_error_code(error)
    normalized_code = str(code).lower()
    if code in _NOT_FOUND_CODES or normalized_code.endswith(".notfound"):
        classification = "not_found"
        status = "not_found"
    elif code in _AUTH_CODES:
        classification = "provider"
        status = "invalid_access_token"
    elif isinstance(error, CloudInventoryTransientError):
        classification = "transient"
        status = "error"
    elif is_transient_aws_error(error):
        classification = "transient"
        # The monitoring engine already treats ``error`` as non-alerting;
        # retain that normalized status and expose the safe subtype in data.
        status = "error"
    elif isinstance(error, (ClientError, BotoCoreError)):
        classification = "provider"
        status = "error"
    else:
        classification = "adapter"
        status = "error"
    return status, {
        "error": {
            "code": str(code)[:128],
            "classification": classification,
        }
    }


def _success_metadata(asset_type, record, region, **extra):
    metadata = {
        asset_type: serialize_aws(record),
        "_cloudmoo_region": region,
    }
    metadata.update(extra)
    return serialize_aws(metadata)


def _check_describe(asset_type, unique_id, credentials):
    spec = _STATUS_SPECS[asset_type]
    try:
        region, raw_id = _parse_identity(unique_id, credentials)
        client = _client(credentials, spec["service"], region)
        response = getattr(client, spec["operation"])(
            **{spec["request_key"]: [raw_id]}
        )
        collection = require_collection(
            response,
            spec["response_key"],
            f"{asset_type} status",
        )
        if not collection:
            return "not_found", {"error": {"code": "not_found", "classification": "not_found"}}
        record = collection[0]
        status = _status_from_record(asset_type, record)
        return status, _success_metadata(asset_type, record, region)
    except Exception as error:
        return _error_status(error)


def _check_ebs_attachment(unique_id, credentials):
    try:
        region, raw_id = _parse_identity(unique_id, credentials)
        client = _client(credentials, "ec2", region)
        context = credentials.get("metadata") if isinstance(credentials, dict) else {}
        context = context if isinstance(context, dict) else {}
        volume_id = context.get("_cloudmoo_volume_id")
        attachment_id = context.get("_cloudmoo_attachment_id") or None
        instance_id = context.get("_cloudmoo_instance_id") or None
        device = context.get("_cloudmoo_device") or None

        # The composite key is sufficient when a caller has only the stored
        # regional ID.  Metadata remains authoritative when AttachmentId was
        # used as the local raw ID.
        raw_parts = raw_id.split("|")
        if not volume_id and len(raw_parts) >= 3 and raw_parts[0].startswith("vol-"):
            volume_id, instance_id, device = raw_parts[:3]

        if volume_id:
            response = client.describe_volumes(VolumeIds=[str(volume_id)])
            pages = [response]
        else:
            # A manually supplied attachment ID has no describe-by-attachment
            # operation.  Use the paginated read-only volume collection and
            # locate the attachment without mutating provider state.
            from apps.console.cloud.aws.discovery import iter_pages

            pages = iter_pages(client, "describe_volumes")

        for page in pages:
            volumes = require_collection(page, "Volumes", "ebs_attachment status")
            for volume in volumes:
                if not isinstance(volume, dict):
                    raise CloudInventoryTransientError(
                        "AWS returned an invalid ebs_attachment status resource"
                    )
                candidate_volume_id = volume.get("VolumeId")
                attachments = require_collection(
                    volume,
                    "Attachments",
                    "ebs_attachment status",
                )
                for attachment in attachments:
                    if not isinstance(attachment, dict):
                        raise CloudInventoryTransientError(
                            "AWS returned an invalid ebs_attachment status resource"
                        )
                    same_attachment = attachment_id and str(
                        attachment.get("AttachmentId") or ""
                    ) == str(attachment_id)
                    same_composite = (
                        (not attachment_id)
                        and instance_id
                        and device
                        and str(attachment.get("InstanceId") or "") == str(instance_id)
                        and str(attachment.get("Device") or "") == str(device)
                    )
                    if not (same_attachment or same_composite):
                        continue
                    return _normal_status(attachment.get("State")), _success_metadata(
                        "ebs_attachment",
                        attachment,
                        region,
                        volume=volume,
                        _cloudmoo_volume_id=candidate_volume_id,
                    )

        return "not_found", {"error": {"code": "not_found", "classification": "not_found"}}
    except Exception as error:
        return _error_status(error)


def check_aws_instance_health_status(unique_id, credentials):
    """Check EC2 system and instance health using only DescribeInstanceStatus."""
    try:
        region, raw_id = _parse_identity(unique_id, credentials)
        client = _client(credentials, "ec2", region)
        response = client.describe_instance_status(
            InstanceIds=[raw_id],
            IncludeAllInstances=True,
        )
        statuses = require_collection(
            response,
            "InstanceStatuses",
            "instance health status",
        )
        if not statuses:
            return "not_found", {"error": {"code": "not_found", "classification": "not_found"}}
        status_data = statuses[0]
        if not isinstance(status_data, dict):
            raise CloudInventoryTransientError("AWS returned an invalid instance health status")

        system_status = status_data.get("SystemStatus")
        instance_status = status_data.get("InstanceStatus")
        system_value = (
            system_status.get("Status")
            if isinstance(system_status, dict)
            else system_status
        )
        instance_value = (
            instance_status.get("Status")
            if isinstance(instance_status, dict)
            else instance_status
        )
        normalized = {
            _normal_status(system_value, "unknown"),
            _normal_status(instance_value, "unknown"),
        }
        if normalized == {"ok"}:
            status = "ok"
        elif "impaired" in normalized:
            status = "impaired"
        elif normalized & {"initializing", "insufficient_data"}:
            status = "initializing"
        else:
            status = "unknown"
        return status, _success_metadata(
            "instance_health",
            status_data,
            region,
            _cloudmoo_instance_id=raw_id,
        )
    except Exception as error:
        return _error_status(error)


_STATUS_SPECS = {
    "vpc": {
        "service": "ec2",
        "operation": "describe_vpcs",
        "request_key": "VpcIds",
        "response_key": "Vpcs",
    },
    "subnet": {
        "service": "ec2",
        "operation": "describe_subnets",
        "request_key": "SubnetIds",
        "response_key": "Subnets",
    },
    "route_table": {
        "service": "ec2",
        "operation": "describe_route_tables",
        "request_key": "RouteTableIds",
        "response_key": "RouteTables",
    },
    "internet_gateway": {
        "service": "ec2",
        "operation": "describe_internet_gateways",
        "request_key": "InternetGatewayIds",
        "response_key": "InternetGateways",
    },
    "nat_gateway": {
        "service": "ec2",
        "operation": "describe_nat_gateways",
        "request_key": "NatGatewayIds",
        "response_key": "NatGateways",
    },
    "network_acl": {
        "service": "ec2",
        "operation": "describe_network_acls",
        "request_key": "NetworkAclIds",
        "response_key": "NetworkAcls",
    },
    "network_interface": {
        "service": "ec2",
        "operation": "describe_network_interfaces",
        "request_key": "NetworkInterfaceIds",
        "response_key": "NetworkInterfaces",
    },
    "vpc_peering": {
        "service": "ec2",
        "operation": "describe_vpc_peering_connections",
        "request_key": "VpcPeeringConnectionIds",
        "response_key": "VpcPeeringConnections",
    },
    "transit_gateway_attachment": {
        "service": "ec2",
        "operation": "describe_transit_gateway_attachments",
        "request_key": "TransitGatewayAttachmentIds",
        "response_key": "TransitGatewayAttachments",
    },
    "vpn_connection": {
        "service": "ec2",
        "operation": "describe_vpn_connections",
        "request_key": "VpnConnectionIds",
        "response_key": "VpnConnections",
    },
    "flow_log": {
        "service": "ec2",
        "operation": "describe_flow_logs",
        "request_key": "FlowLogIds",
        "response_key": "FlowLogs",
    },
    "auto_scaling_group": {
        "service": "autoscaling",
        "operation": "describe_auto_scaling_groups",
        "request_key": "AutoScalingGroupNames",
        "response_key": "AutoScalingGroups",
    },
    "launch_template": {
        "service": "ec2",
        "operation": "describe_launch_templates",
        "request_key": "LaunchTemplateIds",
        "response_key": "LaunchTemplates",
    },
    "ami": {
        "service": "ec2",
        "operation": "describe_images",
        "request_key": "ImageIds",
        "response_key": "Images",
    },
}


def check_aws_vpc_status(unique_id, credentials):
    return _check_describe("vpc", unique_id, credentials)


def check_aws_subnet_status(unique_id, credentials):
    return _check_describe("subnet", unique_id, credentials)


def check_aws_route_table_status(unique_id, credentials):
    return _check_describe("route_table", unique_id, credentials)


def check_aws_internet_gateway_status(unique_id, credentials):
    return _check_describe("internet_gateway", unique_id, credentials)


def check_aws_nat_gateway_status(unique_id, credentials):
    return _check_describe("nat_gateway", unique_id, credentials)


def check_aws_network_acl_status(unique_id, credentials):
    return _check_describe("network_acl", unique_id, credentials)


def check_aws_network_interface_status(unique_id, credentials):
    return _check_describe("network_interface", unique_id, credentials)


def check_aws_vpc_peering_status(unique_id, credentials):
    return _check_describe("vpc_peering", unique_id, credentials)


def check_aws_transit_gateway_attachment_status(unique_id, credentials):
    return _check_describe("transit_gateway_attachment", unique_id, credentials)


def check_aws_vpn_connection_status(unique_id, credentials):
    return _check_describe("vpn_connection", unique_id, credentials)


def check_aws_flow_log_status(unique_id, credentials):
    return _check_describe("flow_log", unique_id, credentials)


def check_aws_auto_scaling_group_status(unique_id, credentials):
    return _check_describe("auto_scaling_group", unique_id, credentials)


def check_aws_launch_template_status(unique_id, credentials):
    return _check_describe("launch_template", unique_id, credentials)


def check_aws_ami_status(unique_id, credentials):
    return _check_describe("ami", unique_id, credentials)


def check_aws_ebs_attachment_status(unique_id, credentials):
    return _check_ebs_attachment(unique_id, credentials)


def check_aws_asset_status(asset_type, unique_id, credentials):
    """Dispatch a model's generic or provider-qualified asset type."""
    normalized = str(asset_type or "").strip().lower()
    if normalized.startswith("aws_"):
        normalized = normalized[4:]
    if normalized in {"vpc_peering_connection", "vpc_peering"}:
        normalized = "vpc_peering"
    if normalized in {"image", "ami"}:
        normalized = "ami"
    if normalized in {"ebs_volume_attachment", "ebs_attachment"}:
        normalized = "ebs_attachment"
    if normalized == "instance_health":
        return check_aws_instance_health_status(unique_id, credentials)
    if normalized not in _STATUS_SPECS and normalized != "ebs_attachment":
        raise ValueError(f"Unsupported AWS network asset type: {asset_type}")
    return globals()[f"check_aws_{normalized}_status"](unique_id, credentials)


# Provider-qualified aliases make the functions directly addressable while
# the shared asset-type choice migration is being landed by integration.
check_aws_aws_vpc_status = check_aws_vpc_status
check_aws_aws_subnet_status = check_aws_subnet_status
check_aws_aws_route_table_status = check_aws_route_table_status
check_aws_aws_internet_gateway_status = check_aws_internet_gateway_status
check_aws_aws_nat_gateway_status = check_aws_nat_gateway_status
check_aws_aws_network_acl_status = check_aws_network_acl_status
check_aws_aws_network_interface_status = check_aws_network_interface_status
check_aws_aws_vpc_peering_status = check_aws_vpc_peering_status
check_aws_aws_transit_gateway_attachment_status = check_aws_transit_gateway_attachment_status
check_aws_aws_vpn_connection_status = check_aws_vpn_connection_status
check_aws_aws_flow_log_status = check_aws_flow_log_status
check_aws_aws_auto_scaling_group_status = check_aws_auto_scaling_group_status
check_aws_aws_launch_template_status = check_aws_launch_template_status
check_aws_aws_ami_status = check_aws_ami_status
check_aws_aws_ebs_attachment_status = check_aws_ebs_attachment_status


# Long-name aliases are useful to callers using the API's resource names.
check_aws_vpc_peering_connection_status = check_aws_vpc_peering_status
check_aws_ebs_volume_attachment_status = check_aws_ebs_attachment_status
check_aws_image_status = check_aws_ami_status


__all__ = [
    "check_aws_asset_status",
    "check_aws_instance_health_status",
    "check_aws_vpc_status",
    "check_aws_subnet_status",
    "check_aws_route_table_status",
    "check_aws_internet_gateway_status",
    "check_aws_nat_gateway_status",
    "check_aws_network_acl_status",
    "check_aws_network_interface_status",
    "check_aws_vpc_peering_status",
    "check_aws_vpc_peering_connection_status",
    "check_aws_transit_gateway_attachment_status",
    "check_aws_vpn_connection_status",
    "check_aws_flow_log_status",
    "check_aws_auto_scaling_group_status",
    "check_aws_launch_template_status",
    "check_aws_ami_status",
    "check_aws_image_status",
    "check_aws_ebs_attachment_status",
    "check_aws_ebs_volume_attachment_status",
]
