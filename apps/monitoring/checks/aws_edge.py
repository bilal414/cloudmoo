"""Read-only AWS DNS and edge status checks."""

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from apps.console.cloud.aws.discovery import aws_client, aws_error_code, serialize_aws
from apps.monitoring.checks.base import REQUEST_TIMEOUT_SECONDS, classify_aws_error


AWS_CLIENT_CONFIG = Config(
    connect_timeout=5,
    read_timeout=REQUEST_TIMEOUT_SECONDS,
    retries={"mode": "standard", "max_attempts": 2},
)


ROUTE53_GLOBAL_ENDPOINT = "global"
CLOUDFRONT_CONTROL_PLANE_REGION = "us-east-1"
GLOBAL_ACCELERATOR_CONTROL_PLANE_REGION = "us-west-2"
WAF_CLOUDFRONT_SCOPE = "CLOUDFRONT"
WAF_REGIONAL_SCOPE = "REGIONAL"

AWS_ROUTE53_ZONE = "aws_route53_zone"
AWS_ROUTE53_RECORD = "aws_route53_record"
AWS_CLOUDFRONT_DISTRIBUTION = "aws_cloudfront_distribution"
AWS_CLOUDFRONT_ORIGIN_ACCESS_CONTROL = "aws_cloudfront_origin_access_control"
AWS_WAF_WEB_ACL = "aws_waf_web_acl"
AWS_GLOBAL_ACCELERATOR = "aws_global_accelerator"

AWS_EDGE_ASSET_TYPES = (
    AWS_ROUTE53_ZONE,
    AWS_ROUTE53_RECORD,
    AWS_CLOUDFRONT_DISTRIBUTION,
    AWS_CLOUDFRONT_ORIGIN_ACCESS_CONTROL,
    AWS_WAF_WEB_ACL,
    AWS_GLOBAL_ACCELERATOR,
)


NOT_FOUND_CODES = {
    "NoSuchHostedZone",
    "NoSuchDistribution",
    "NoSuchOriginAccessControl",
    "WAFNonexistentItemException",
    "AcceleratorNotFoundException",
    "ResourceNotFoundException",
    "NotFoundException",
}
AUTH_CODES = {
    "AccessDenied",
    "AccessDeniedException",
    "AuthFailure",
    "ExpiredToken",
    "InvalidClientTokenId",
    "UnrecognizedClientException",
}


def _metadata(credentials):
    if not isinstance(credentials, dict):
        return {}
    value = credentials.get("metadata")
    if isinstance(value, dict):
        return value
    # Keeping this fallback makes the checker usable by integration callers
    # that pass the persisted context fields directly.
    return credentials


def _client(credentials, service, region=None):
    if not isinstance(credentials, dict):
        raise ValueError("AWS credentials are not configured")
    access_key = credentials.get("access_key")
    secret_key = credentials.get("secret_key")
    if not access_key or not secret_key:
        raise ValueError("AWS credentials are incomplete")
    account = type(
        "_CredentialAccount",
        (),
        {
            "access_key": access_key,
            "secret_key": secret_key,
            "region": credentials.get("region") or credentials.get("resource_region"),
        },
    )()
    # The shared helper owns bounded client construction.  The local boto3
    # fallback is only for a caller that supplies credentials without a valid
    # account-shaped Region (and remains read-only).
    try:
        return aws_client(account, service, region=region)
    except (AttributeError, TypeError, ValueError):
        kwargs = {
            "aws_access_key_id": access_key,
            "aws_secret_access_key": secret_key,
            "region_name": region,
            "config": AWS_CLIENT_CONFIG,
        }
        if credentials.get("session_token"):
            kwargs["aws_session_token"] = credentials["session_token"]
        return boto3.client(service, **kwargs)


def _resource_id(unique_id, metadata, *keys):
    for key in keys:
        value = metadata.get(key)
        if value:
            return str(value)
    return str(unique_id)


def _region_for(asset_type, credentials, metadata):
    if asset_type == AWS_ROUTE53_ZONE or asset_type == AWS_ROUTE53_RECORD:
        # None asks boto3 for the Route 53 global endpoint rather than the
        # account's ordinary regional endpoint.
        return None
    if asset_type in {
        AWS_CLOUDFRONT_DISTRIBUTION,
        AWS_CLOUDFRONT_ORIGIN_ACCESS_CONTROL,
    }:
        return CLOUDFRONT_CONTROL_PLANE_REGION
    if asset_type == AWS_GLOBAL_ACCELERATOR:
        return GLOBAL_ACCELERATOR_CONTROL_PLANE_REGION
    if asset_type == AWS_WAF_WEB_ACL:
        if metadata.get("_cloudmoo_scope") == WAF_CLOUDFRONT_SCOPE:
            return CLOUDFRONT_CONTROL_PLANE_REGION
        return (
            metadata.get("_cloudmoo_region")
            or credentials.get("resource_region")
            or credentials.get("region")
        )
    return credentials.get("resource_region") or credentials.get("region")


def _error_code(error):
    try:
        return str(aws_error_code(error))
    except Exception:
        return type(error).__name__


def _error_status(error):
    code = _error_code(error)
    if code in NOT_FOUND_CODES:
        return "not_found"
    if code in AUTH_CODES:
        return "invalid_access_token"
    # Preserve the shared AWS classification for the existing EC2-style
    # error names as well.
    return classify_aws_error(error)


def _error_result(error):
    return _error_status(error), {"errorCode": _error_code(error)}


def _provider_status(resource, *keys):
    if not isinstance(resource, dict):
        return None
    for key in keys:
        value = resource.get(key)
        if value is not None and str(value):
            return str(value)
    return None


def _normalize_provider_status(value, default="active"):
    """Normalize provider states while retaining the raw state in metadata."""

    raw = str(value or default)
    normalized = raw.strip().lower().replace(" ", "_").replace("-", "_")
    if normalized in {"deployed", "complete", "completed", "succeeded", "success"}:
        return "deployed"
    if normalized in {
        "active",
        "available",
        "healthy",
        "operational",
        "issued",
        "ready",
    }:
        return "active"
    if normalized in {
        "inprogress",
        "in_progress",
        "pending",
        "processing",
        "deploying",
        "initializing",
        "provisioning",
        "updating",
    }:
        return "pending"
    if normalized in {"failed", "failure", "error", "errored"}:
        return "failed"
    if normalized in {"notfound", "not_found", "missing", "deleted"}:
        return "not_found"
    return normalized


def _payload(asset_type, resource, raw_status=None, **extra):
    value = serialize_aws(resource if isinstance(resource, dict) else {})
    if not isinstance(value, dict):
        value = {"resource": value}
    value = dict(value)
    if raw_status is not None:
        value["providerStatus"] = raw_status
    value.update(extra)
    return {asset_type: serialize_aws(_bound_value(value))}


def _bound_value(value, depth=0):
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


def _check_route53_zone(unique_id, credentials):
    metadata = _metadata(credentials)
    zone_id = _resource_id(unique_id, metadata, "resource_id", "hosted_zone_id")
    client = _client(credentials, "route53", region=None)
    response = client.get_hosted_zone(Id=zone_id)
    zone = response.get("HostedZone") if isinstance(response, dict) else None
    if not isinstance(zone, dict):
        raise ValueError("AWS Route 53 returned an incomplete hosted zone response")
    detail = dict(zone)
    if isinstance(response.get("DelegationSet"), dict):
        detail["DelegationSet"] = response["DelegationSet"]
    if isinstance(response.get("VPCs"), list):
        detail["VPCs"] = response["VPCs"]
    return "available", _payload(AWS_ROUTE53_ZONE, detail, endpoint=ROUTE53_GLOBAL_ENDPOINT)


def _check_route53_record(unique_id, credentials):
    metadata = _metadata(credentials)
    zone_id = metadata.get("hosted_zone_id")
    record_name = metadata.get("record_name") or metadata.get("Name")
    record_type = metadata.get("record_type") or metadata.get("Type")
    set_identifier = metadata.get("set_identifier") or metadata.get("SetIdentifier")
    if not zone_id or not record_name or not record_type:
        raise ValueError("Route 53 record context is incomplete")

    client = _client(credentials, "route53", region=None)
    request = {
        "HostedZoneId": str(zone_id),
        "StartRecordName": str(record_name),
        "StartRecordType": str(record_type),
    }
    if set_identifier:
        request["StartRecordIdentifier"] = str(set_identifier)
    response = client.list_resource_record_sets(**request)
    records = response.get("ResourceRecordSets") if isinstance(response, dict) else None
    if not isinstance(records, list):
        raise ValueError("AWS Route 53 returned an incomplete record-set response")

    for record in records:
        if not isinstance(record, dict):
            continue
        if (
            str(record.get("Name")) == str(record_name)
            and str(record.get("Type")) == str(record_type)
            and str(record.get("SetIdentifier") or "") == str(set_identifier or "")
        ):
            # Do not call get_health_check or fetch any health-check target
            # body.  Route 53 record availability is a configuration check.
            return "available", _payload(
                AWS_ROUTE53_RECORD,
                record,
                endpoint=ROUTE53_GLOBAL_ENDPOINT,
                configuration="present",
            )
    return "not_found", {
        "errorCode": "NoSuchRecordSet",
        AWS_ROUTE53_RECORD: {
            "hostedZoneId": str(zone_id),
            "recordName": str(record_name),
            "recordType": str(record_type),
        },
    }


def _check_cloudfront_distribution(unique_id, credentials):
    metadata = _metadata(credentials)
    distribution_id = _resource_id(unique_id, metadata, "resource_id", "distribution_id", "Id")
    client = _client(
        credentials,
        "cloudfront",
        region=CLOUDFRONT_CONTROL_PLANE_REGION,
    )
    response = client.get_distribution(Id=distribution_id)
    distribution = response.get("Distribution") if isinstance(response, dict) else None
    if not isinstance(distribution, dict):
        raise ValueError("AWS CloudFront returned an incomplete distribution response")
    raw_status = _provider_status(distribution, "Status", "status")
    status = _normalize_provider_status(raw_status, default="active")
    return status, _payload(
        AWS_CLOUDFRONT_DISTRIBUTION,
        distribution,
        raw_status,
        endpoint=CLOUDFRONT_CONTROL_PLANE_REGION,
    )


def _check_cloudfront_origin_access_control(unique_id, credentials):
    metadata = _metadata(credentials)
    oac_id = _resource_id(unique_id, metadata, "resource_id", "origin_access_control_id", "Id")
    client = _client(
        credentials,
        "cloudfront",
        region=CLOUDFRONT_CONTROL_PLANE_REGION,
    )
    response = client.get_origin_access_control(Id=oac_id)
    oac = response.get("OriginAccessControl") if isinstance(response, dict) else None
    if not isinstance(oac, dict):
        raise ValueError("AWS CloudFront returned an incomplete origin access control response")
    raw_status = _provider_status(oac, "Status", "status")
    status = _normalize_provider_status(raw_status, default="active")
    return status, _payload(
        AWS_CLOUDFRONT_ORIGIN_ACCESS_CONTROL,
        oac,
        raw_status,
        endpoint=CLOUDFRONT_CONTROL_PLANE_REGION,
    )


def _check_waf_web_acl(unique_id, credentials):
    metadata = _metadata(credentials)
    scope = str(metadata.get("_cloudmoo_scope") or metadata.get("scope") or WAF_REGIONAL_SCOPE)
    region = _region_for(AWS_WAF_WEB_ACL, credentials, metadata)
    web_acl_id = _resource_id(unique_id, metadata, "resource_id", "web_acl_id", "Id")
    name = str(metadata.get("web_acl_name") or metadata.get("Name") or metadata.get("name") or "")
    if not name:
        raise ValueError("AWS WAF Web ACL context is incomplete")
    client = _client(credentials, "wafv2", region=region)
    response = client.get_web_acl(Name=name, Scope=scope, Id=web_acl_id)
    web_acl = response.get("WebACL") if isinstance(response, dict) else None
    if not isinstance(web_acl, dict):
        raise ValueError("AWS WAF returned an incomplete Web ACL response")
    raw_status = _provider_status(web_acl, "Status", "status") or "ACTIVE"
    status = _normalize_provider_status(raw_status)
    return status, _payload(
        AWS_WAF_WEB_ACL,
        web_acl,
        raw_status,
        endpoint=region,
        scope=scope,
    )


def _check_global_accelerator(unique_id, credentials):
    metadata = _metadata(credentials)
    accelerator_arn = _resource_id(
        unique_id,
        metadata,
        "resource_id",
        "accelerator_arn",
        "AcceleratorArn",
    )
    client = _client(
        credentials,
        "globalaccelerator",
        region=GLOBAL_ACCELERATOR_CONTROL_PLANE_REGION,
    )
    response = client.describe_accelerator(AcceleratorArn=accelerator_arn)
    accelerator = response.get("Accelerator") if isinstance(response, dict) else None
    if not isinstance(accelerator, dict):
        raise ValueError("AWS Global Accelerator returned an incomplete accelerator response")
    raw_status = _provider_status(accelerator, "Status", "status")
    status = _normalize_provider_status(raw_status, default="active")
    return status, _payload(
        AWS_GLOBAL_ACCELERATOR,
        accelerator,
        raw_status,
        endpoint=GLOBAL_ACCELERATOR_CONTROL_PLANE_REGION,
    )


CHECKERS = {
    AWS_ROUTE53_ZONE: _check_route53_zone,
    AWS_ROUTE53_RECORD: _check_route53_record,
    AWS_CLOUDFRONT_DISTRIBUTION: _check_cloudfront_distribution,
    AWS_CLOUDFRONT_ORIGIN_ACCESS_CONTROL: _check_cloudfront_origin_access_control,
    AWS_WAF_WEB_ACL: _check_waf_web_acl,
    AWS_GLOBAL_ACCELERATOR: _check_global_accelerator,
}


def check_edge_resource_status(asset_type, unique_id, credentials):
    """Run one edge status/configuration check using read-only APIs."""

    checker = CHECKERS.get(asset_type)
    if checker is None:
        return "error", {"errorCode": "UnsupportedAssetType"}
    try:
        return checker(unique_id, credentials)
    except (ClientError, BotoCoreError) as error:
        return _error_result(error)
    except (KeyError, TypeError, ValueError) as error:
        return "error", {"errorCode": type(error).__name__}
    except Exception as error:
        return "error", {"errorCode": type(error).__name__}


check_aws_edge_asset_status = check_edge_resource_status
check_aws_asset_status = check_edge_resource_status


def _make_check(asset_type):
    def check(unique_id, credentials):
        return check_edge_resource_status(asset_type, unique_id, credentials)

    check.__name__ = f"check_aws_{asset_type}_status"
    return check


for _asset_type in CHECKERS:
    _check = _make_check(_asset_type)
    # New edge asset types carry the AWS namespace in their persisted value,
    # while the public checker convention already supplies the ``aws_``
    # provider prefix.  Expose both spellings during the integration rollout.
    globals()[f"check_aws_{_asset_type}_status"] = _check
    globals()[f"check_aws_{_asset_type.removeprefix('aws_')}_status"] = _check


# The explicit mapping is the integration-lane hook.  The aliases keep the
# lane independent from the name chosen for other provider check registries.
AWS_EDGE_CHECKS = {
    asset_type: globals()[f"check_aws_{asset_type.removeprefix('aws_')}_status"]
    for asset_type in CHECKERS
}
AWS_EDGE_STATUS_CHECKS = AWS_EDGE_CHECKS
AWS_EDGE_CHECK_FUNCTIONS = AWS_EDGE_CHECKS


__all__ = [
    "AWS_EDGE_CHECKS",
    "AWS_EDGE_CHECK_FUNCTIONS",
    "AWS_EDGE_STATUS_CHECKS",
    "AWS_EDGE_ASSET_TYPES",
    "CHECKERS",
    "check_aws_asset_status",
    "check_aws_edge_asset_status",
    "check_aws_aws_cloudfront_distribution_status",
    "check_aws_aws_cloudfront_origin_access_control_status",
    "check_aws_aws_global_accelerator_status",
    "check_aws_aws_route53_record_status",
    "check_aws_aws_route53_zone_status",
    "check_aws_aws_waf_web_acl_status",
    "check_aws_cloudfront_distribution_status",
    "check_aws_cloudfront_origin_access_control_status",
    "check_aws_global_accelerator_status",
    "check_aws_route53_record_status",
    "check_aws_route53_zone_status",
    "check_aws_waf_web_acl_status",
    "check_edge_resource_status",
]
