"""Read-only status checks for AWS security and governance assets."""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace

from botocore.exceptions import BotoCoreError, ClientError

from apps.console.cloud.aws.discovery import (
    aws_client,
    aws_error_code,
    is_transient_aws_error,
    iter_pages,
    require_collection,
    serialize_aws,
)
from apps.console.cloud.aws.security_governance import (
    AWS_CLOUDTRAIL_TRAIL,
    AWS_CONFIG_RECORDER,
    AWS_CONFIG_RULE,
    AWS_FIREWALL_MANAGER_POLICY,
    AWS_GUARDDUTY_DETECTOR,
    AWS_GLOBAL_REGION,
    AWS_IAM_POLICY,
    AWS_IAM_ROLE,
    AWS_IAM_USER,
    AWS_INSPECTOR,
    AWS_KMS_ALIAS,
    AWS_KMS_KEY,
    AWS_MACIE,
    AWS_SECURITY_GOVERNANCE_ASSET_TYPES,
    AWS_SECURITY_HUB,
    MAX_ITEMS_PER_COLLECTION,
    MAX_PAGES_PER_COLLECTION,
    _safe_metadata,
    _status_value,
    assert_read_only_operation,
)
from apps.monitoring.metadata import redact_error_message, redact_sensitive_metadata


_NOT_FOUND_CODES = frozenset({
    "nosuchentity",
    "notfound",
    "notfoundexception",
    "resourcenotfound",
    "resourcenotfoundexception",
    "statedoesnotexist",
    "trailnotfoundexception",
})
_ACCESS_CODES = frozenset({
    "accessdenied",
    "accessdeniedexception",
    "authfailure",
    "expiredtoken",
    "invalidclienttokenid",
    "unauthorizedoperation",
    "unrecognizedclientexception",
})


def _read(client, operation, **kwargs):
    """Invoke one explicitly allowlisted read operation."""
    assert_read_only_operation(operation)
    response = getattr(client, operation)(**kwargs)
    if not isinstance(response, Mapping):
        raise ValueError(f"AWS returned an invalid {operation} response")
    return response


def _pages(client, operation, **kwargs):
    for page_number, page in enumerate(iter_pages(client, operation, **kwargs), start=1):
        if page_number > MAX_PAGES_PER_COLLECTION:
            raise ValueError(f"AWS {operation} pagination exceeded the safety bound")
        if not isinstance(page, Mapping):
            raise ValueError(f"AWS returned an invalid {operation} page")
        yield page


def _collection(client, operation, response_key, context, **kwargs):
    values = []
    for page in _pages(client, operation, **kwargs):
        page_values = require_collection(page, response_key, context)
        values.extend(page_values)
        if len(values) > MAX_ITEMS_PER_COLLECTION:
            raise ValueError(f"AWS {context} exceeded the safety bound")
    return values


def _context(credentials):
    if not isinstance(credentials, Mapping):
        raise ValueError("AWS security-governance credentials are not configured")
    region = credentials.get("resource_region") or credentials.get("region")
    if not isinstance(region, str) or not region.strip():
        raise ValueError("AWS security-governance credentials are missing a Region")
    endpoint_region = region
    if region == AWS_GLOBAL_REGION:
        endpoint_region = credentials.get("endpoint_region") or "us-east-1"
    access_key = credentials.get("access_key")
    secret_key = credentials.get("secret_key")
    if not access_key or not secret_key:
        raise ValueError("AWS security-governance credentials are incomplete")
    metadata = credentials.get("metadata")
    if not isinstance(metadata, Mapping):
        metadata = {}
    return str(region), str(endpoint_region), dict(metadata), access_key, secret_key


def _client(credentials, service, endpoint_region):
    access_key = credentials.get("access_key")
    secret_key = credentials.get("secret_key")
    context = SimpleNamespace(
        access_key=access_key,
        secret_key=secret_key,
        region=endpoint_region,
    )
    return aws_client(context, service, region=endpoint_region)


def _raw_id(unique_id, metadata):
    value = (
        metadata.get("_cloudmoo_raw_id")
        or metadata.get("_cloudmoo_provider_id")
        or unique_id
    )
    value = str(value)
    if "|" in value:
        prefix, suffix = value.split("|", 1)
        if prefix and (prefix == AWS_GLOBAL_REGION or "-" in prefix):
            return suffix
    return value


def _resource_name(unique_id, metadata, fields=()):
    for field in fields:
        value = metadata.get(field)
        if value not in (None, ""):
            return str(value)
    value = _raw_id(unique_id, metadata)
    return value.rsplit("/", 1)[-1] if value.startswith("arn:") else value


def _safe_payload(asset_type, payload, fields, **extra):
    value = _safe_metadata(
        payload,
        fields,
        context=f"{asset_type} status",
        extra=extra,
    )
    return {asset_type: redact_sensitive_metadata(serialize_aws(value))}


def _error_status(error):
    code = str(aws_error_code(getattr(error, "__cause__", None) or error)).lower()
    if code in _NOT_FOUND_CODES or "notfound" in code or "doesnotexist" in code:
        return "missing"
    if code in _ACCESS_CODES or "accessdenied" in code or "unauthorized" in code:
        return "provider_error"
    if is_transient_aws_error(error):
        return "degraded"
    if isinstance(error, (ClientError, BotoCoreError)):
        return "provider_error"
    return "degraded"


def _provider_error(error):
    code = str(aws_error_code(getattr(error, "__cause__", None) or error))[:128]
    return _error_status(error), {
        "errorCode": code,
        "error": redact_error_message(error),
    }


def _status(value, *, default="unknown"):
    normalized = _status_value(value, default=default)
    if normalized in {"enabled", "active", "available", "issued", "healthy", "ok", "running", "logging", "recording", "succeeded", "success", "complete", "completed"}:
        return "available"
    if normalized in {"compliant"}:
        return "compliant"
    if normalized in {"non_compliant", "noncompliant", "unhealthy", "impaired"}:
        return "non_compliant" if normalized in {"non_compliant", "noncompliant"} else "degraded"
    if normalized in {"pending", "creating", "updating", "activating", "provisioning", "in_progress", "queued"}:
        return "pending"
    if normalized in {"disabled", "inactive", "paused", "stopped", "not_recording", "not_logging"}:
        return "stopped" if normalized in {"stopped", "paused", "not_recording", "not_logging"} else "disabled"
    if normalized in {"failed", "failure", "error", "blocked"}:
        return "failed"
    if normalized in {"expired", "pending_deletion", "pending_deletion_window"}:
        return "expired"
    return default


def _first_status(payload, fields):
    for field in fields:
        value = payload.get(field)
        if value not in (None, ""):
            return value
    return None


def check_aws_iam_user_status(unique_id, credentials):
    try:
        region, endpoint_region, metadata, _access, _secret = _context(credentials)
        name = _resource_name(unique_id, metadata, ("UserName", "user_name"))
        response = _read(_client(credentials, "iam", endpoint_region), "get_user", UserName=name)
        user = response.get("User")
        if not isinstance(user, Mapping):
            raise ValueError("AWS returned an invalid IAM user response")
        return "available", _safe_payload(
            AWS_IAM_USER,
            user,
            ("Path", "UserName", "UserId", "Arn", "CreateDate", "PasswordLastUsed"),
            region=region,
        )
    except Exception as error:
        return _provider_error(error)


def check_aws_iam_role_status(unique_id, credentials):
    try:
        region, endpoint_region, metadata, _access, _secret = _context(credentials)
        name = _resource_name(unique_id, metadata, ("RoleName", "role_name"))
        response = _read(_client(credentials, "iam", endpoint_region), "get_role", RoleName=name)
        role = response.get("Role")
        if not isinstance(role, Mapping):
            raise ValueError("AWS returned an invalid IAM role response")
        return "available", _safe_payload(
            AWS_IAM_ROLE,
            role,
            ("Path", "RoleName", "RoleId", "Arn", "CreateDate", "MaxSessionDuration", "Description"),
            region=region,
        )
    except Exception as error:
        return _provider_error(error)


def check_aws_iam_policy_status(unique_id, credentials):
    try:
        region, endpoint_region, metadata, _access, _secret = _context(credentials)
        policy_arn = metadata.get("Arn") or metadata.get("PolicyArn") or _raw_id(unique_id, metadata)
        response = _read(
            _client(credentials, "iam", endpoint_region),
            "get_policy",
            PolicyArn=policy_arn,
        )
        policy = response.get("Policy")
        if not isinstance(policy, Mapping):
            raise ValueError("AWS returned an invalid IAM policy response")
        return "available", _safe_payload(
            AWS_IAM_POLICY,
            policy,
            (
                "PolicyName",
                "PolicyId",
                "Arn",
                "Path",
                "DefaultVersionId",
                "AttachmentCount",
                "PermissionsBoundaryUsageCount",
                "IsAttachable",
                "Description",
                "CreateDate",
                "UpdateDate",
            ),
            region=region,
        )
    except Exception as error:
        return _provider_error(error)


def check_aws_kms_key_status(unique_id, credentials):
    try:
        region, endpoint_region, metadata, _access, _secret = _context(credentials)
        response = _read(
            _client(credentials, "kms", endpoint_region),
            "describe_key",
            KeyId=metadata.get("KeyId") or metadata.get("KeyArn") or _raw_id(unique_id, metadata),
        )
        key = response.get("KeyMetadata")
        if not isinstance(key, Mapping):
            raise ValueError("AWS returned an invalid KMS key response")
        return _status(key.get("KeyState"), default="degraded"), _safe_payload(
            AWS_KMS_KEY,
            key,
            (
                "KeyId",
                "KeyArn",
                "CreationDate",
                "Enabled",
                "Description",
                "KeyUsage",
                "KeySpec",
                "KeyState",
                "Origin",
                "KeyManager",
                "MultiRegion",
                "MultiRegionKeyType",
                "DeletionDate",
            ),
            region=region,
        )
    except Exception as error:
        return _provider_error(error)


def check_aws_kms_alias_status(unique_id, credentials):
    try:
        region, endpoint_region, metadata, _access, _secret = _context(credentials)
        target_key = metadata.get("TargetKeyId")
        client = _client(credentials, "kms", endpoint_region)
        if target_key:
            key_response = _read(client, "describe_key", KeyId=target_key)
            key = key_response.get("KeyMetadata")
            if not isinstance(key, Mapping):
                raise ValueError("AWS returned an invalid KMS alias target response")
            status = _status(key.get("KeyState"), default="degraded")
            payload = dict(metadata)
            payload["TargetKeyId"] = target_key
            payload["KeyState"] = key.get("KeyState")
        else:
            aliases = _collection(client, "list_aliases", "Aliases", "KMS alias status")
            alias_name = metadata.get("AliasName") or _raw_id(unique_id, metadata)
            match = next(
                (item for item in aliases if isinstance(item, Mapping) and item.get("AliasName") == alias_name),
                None,
            )
            if match is None:
                return "missing", {"errorCode": "NotFound"}
            status = "available" if match.get("TargetKeyId") else "degraded"
            payload = match
        return status, _safe_payload(
            AWS_KMS_ALIAS,
            payload,
            ("AliasName", "AliasArn", "TargetKeyId", "CreationDate", "LastUpdatedDate", "KeyState"),
            region=region,
        )
    except Exception as error:
        return _provider_error(error)


def check_aws_cloudtrail_trail_status(unique_id, credentials):
    try:
        region, endpoint_region, metadata, _access, _secret = _context(credentials)
        name = _resource_name(unique_id, metadata, ("Name", "TrailARN"))
        response = _read(
            _client(credentials, "cloudtrail", endpoint_region),
            "get_trail_status",
            Name=name,
        )
        is_logging = response.get("IsLogging")
        status = "available" if is_logging is True else "degraded" if is_logging is False else "unknown"
        safe_response = dict(response)
        if "LatestDeliveryError" in safe_response:
            safe_response["LatestDeliveryError"] = redact_error_message(
                safe_response["LatestDeliveryError"]
            )
        return status, _safe_payload(
            AWS_CLOUDTRAIL_TRAIL,
            safe_response,
            (
                "IsLogging",
                "LatestDeliveryError",
                "LatestDeliveryTime",
                "LatestDeliveryAttemptTime",
                "StartLoggingTime",
                "StopLoggingTime",
                "TimeLoggingStarted",
                "TimeLoggingStopped",
            ),
            region=region,
        )
    except Exception as error:
        return _provider_error(error)


def check_aws_config_rule_status(unique_id, credentials):
    try:
        region, endpoint_region, metadata, _access, _secret = _context(credentials)
        name = _resource_name(unique_id, metadata, ("ConfigRuleName", "ConfigRuleArn"))
        client = _client(credentials, "config", endpoint_region)
        items = _collection(
            client,
            "describe_compliance_by_config_rule",
            "ComplianceByConfigRules",
            "AWS Config rule status",
            ConfigRuleNames=[name],
        )
        if not items:
            return "missing", {"errorCode": "ResourceNotFoundException"}
        item = items[0]
        if not isinstance(item, Mapping):
            raise ValueError("AWS returned an invalid Config rule status")
        compliance = item.get("Compliance")
        if not isinstance(compliance, Mapping):
            raise ValueError("AWS returned an invalid Config compliance status")
        compliance_type = compliance.get("ComplianceType")
        if str(compliance_type).upper() == "COMPLIANT":
            status = "compliant"
        elif str(compliance_type).upper() == "NON_COMPLIANT":
            status = "non_compliant"
        elif str(compliance_type).upper() == "INSUFFICIENT_DATA":
            status = "degraded"
        else:
            status = "unknown"
        return status, _safe_payload(
            AWS_CONFIG_RULE,
            item,
            ("ConfigRuleName", "Compliance"),
            region=region,
        )
    except Exception as error:
        return _provider_error(error)


def check_aws_config_recorder_status(unique_id, credentials):
    try:
        region, endpoint_region, metadata, _access, _secret = _context(credentials)
        name = _resource_name(unique_id, metadata, ("name", "Name", "ConfigurationRecorderName"))
        response = _read(
            _client(credentials, "config", endpoint_region),
            "describe_configuration_recorder_status",
        )
        statuses = response.get("ConfigurationRecordersStatus")
        if not isinstance(statuses, list):
            raise ValueError("AWS returned an invalid Config recorder status collection")
        item = next(
            (
                value
                for value in statuses
                if isinstance(value, Mapping)
                and (value.get("name") or value.get("Name") or value.get("ConfigurationRecorderName")) == name
            ),
            None,
        )
        if item is None:
            return "missing", {"errorCode": "ResourceNotFoundException"}
        status = "available" if item.get("recording") is True else "degraded"
        return status, _safe_payload(
            AWS_CONFIG_RECORDER,
            item,
            ("name", "Name", "ConfigurationRecorderName", "recording", "lastStatus", "lastErrorCode", "lastStartTime", "lastStopTime"),
            region=region,
        )
    except Exception as error:
        return _provider_error(error)


def check_aws_guardduty_detector_status(unique_id, credentials):
    try:
        region, endpoint_region, metadata, _access, _secret = _context(credentials)
        detector_id = metadata.get("DetectorId") or _raw_id(unique_id, metadata)
        response = _read(
            _client(credentials, "guardduty", endpoint_region),
            "get_detector",
            DetectorId=detector_id,
        )
        status = _status(response.get("Status"), default="degraded")
        return status, _safe_payload(
            AWS_GUARDDUTY_DETECTOR,
            response,
            ("Status", "FindingPublishingFrequency", "CreatedAt", "UpdatedAt", "ServiceRole", "DataSources", "Features"),
            region=region,
        )
    except Exception as error:
        return _provider_error(error)


def check_aws_security_hub_status(unique_id, credentials):
    try:
        region, endpoint_region, _metadata, _access, _secret = _context(credentials)
        response = _read(_client(credentials, "securityhub", endpoint_region), "describe_hub")
        status = _status(response.get("Status") or "ACTIVE", default="degraded")
        return status, _safe_payload(
            AWS_SECURITY_HUB,
            response,
            ("HubArn", "SubscribedAt", "AutoEnableControls", "ControlFindingGenerator", "MemberAccountLimitReached", "Status"),
            region=region,
        )
    except Exception as error:
        return _provider_error(error)


def check_aws_inspector_status(unique_id, credentials):
    try:
        region, endpoint_region, _metadata, _access, _secret = _context(credentials)
        response = _read(_client(credentials, "inspector2", endpoint_region), "batch_get_account_status")
        accounts = response.get("accounts")
        if not isinstance(accounts, list):
            raise ValueError("AWS returned an invalid Inspector account status collection")
        if not accounts:
            return "missing", {"errorCode": "ResourceNotFoundException"}
        item = accounts[0]
        if not isinstance(item, Mapping):
            raise ValueError("AWS returned an invalid Inspector account status")
        status_value = _first_status(item, ("state", "status"))
        return _status(status_value, default="degraded"), _safe_payload(
            AWS_INSPECTOR,
            item,
            ("accountId", "state", "status", "resourceState", "lastUpdatedAt"),
            region=region,
        )
    except Exception as error:
        return _provider_error(error)


def check_aws_macie_status(unique_id, credentials):
    try:
        region, endpoint_region, _metadata, _access, _secret = _context(credentials)
        response = _read(_client(credentials, "macie2", endpoint_region), "get_macie_session")
        status = _status(response.get("status"), default="degraded")
        return status, _safe_payload(
            AWS_MACIE,
            response,
            ("accountId", "status", "createdAt", "updatedAt", "findingPublishingFrequency", "serviceRole"),
            region=region,
        )
    except Exception as error:
        return _provider_error(error)


def check_aws_firewall_manager_policy_status(unique_id, credentials):
    try:
        region, endpoint_region, metadata, _access, _secret = _context(credentials)
        policy_id = metadata.get("PolicyId") or metadata.get("PolicyArn") or _raw_id(unique_id, metadata)
        client = _client(credentials, "fms", endpoint_region)
        response = _read(client, "get_policy", PolicyId=policy_id)
        policy = response.get("Policy")
        if not isinstance(policy, Mapping):
            raise ValueError("AWS returned an invalid Firewall Manager policy response")
        items = _collection(
            client,
            "list_compliance_status",
            "PolicyComplianceStatusList",
            "Firewall Manager policy status",
            PolicyId=policy_id,
        )
        statuses = [
            str(item.get("ComplianceStatus") or item.get("Status") or "").upper()
            for item in items
            if isinstance(item, Mapping)
        ]
        if any(value == "NON_COMPLIANT" for value in statuses):
            status = "degraded"
        elif str(policy.get("PolicyStatus") or "ACTIVE").upper() not in {"ACTIVE", "ENABLED"}:
            status = _status(policy.get("PolicyStatus"), default="degraded")
        else:
            status = "available"
        summary = {"total": len(statuses), "nonCompliant": sum(value == "NON_COMPLIANT" for value in statuses)}
        return status, _safe_payload(
            AWS_FIREWALL_MANAGER_POLICY,
            policy,
            ("PolicyId", "PolicyName", "PolicyArn", "ResourceType", "ResourceTypeList", "RemediationEnabled", "PolicyStatus"),
            complianceSummary=summary,
            region=region,
        )
    except Exception as error:
        return _provider_error(error)


AWS_SECURITY_GOVERNANCE_CHECKS = {
    AWS_IAM_USER: check_aws_iam_user_status,
    AWS_IAM_ROLE: check_aws_iam_role_status,
    AWS_IAM_POLICY: check_aws_iam_policy_status,
    AWS_KMS_KEY: check_aws_kms_key_status,
    AWS_KMS_ALIAS: check_aws_kms_alias_status,
    AWS_CLOUDTRAIL_TRAIL: check_aws_cloudtrail_trail_status,
    AWS_CONFIG_RULE: check_aws_config_rule_status,
    AWS_CONFIG_RECORDER: check_aws_config_recorder_status,
    AWS_GUARDDUTY_DETECTOR: check_aws_guardduty_detector_status,
    AWS_SECURITY_HUB: check_aws_security_hub_status,
    AWS_INSPECTOR: check_aws_inspector_status,
    AWS_MACIE: check_aws_macie_status,
    AWS_FIREWALL_MANAGER_POLICY: check_aws_firewall_manager_policy_status,
}
AWS_SECURITY_GOVERNANCE_STATUS_CHECKS = AWS_SECURITY_GOVERNANCE_CHECKS
AWS_SECURITY_GOVERNANCE_CHECK_REGISTRY = AWS_SECURITY_GOVERNANCE_CHECKS
CHECK_REGISTRATION = AWS_SECURITY_GOVERNANCE_CHECKS


def check_aws_security_governance_asset_status(asset_type, unique_id, credentials):
    normalized = str(asset_type or "").strip().lower()
    checker = AWS_SECURITY_GOVERNANCE_CHECKS.get(normalized)
    if checker is None:
        raise ValueError(f"Unsupported AWS security-governance asset type: {asset_type}")
    return checker(unique_id, credentials)


# Generated names support both the provider-qualified convention used by the
# shared dispatcher and the short compatibility convention used by older
# adapters.  All aliases point at the same read-only checker.
for _asset_type, _checker in AWS_SECURITY_GOVERNANCE_CHECKS.items():
    globals()[f"check_aws_{_asset_type}_status"] = _checker
    if _asset_type.startswith("aws_"):
        globals()[f"check_aws_{_asset_type[4:]}_status"] = _checker


__all__ = [
    "AWS_SECURITY_GOVERNANCE_CHECKS",
    "AWS_SECURITY_GOVERNANCE_STATUS_CHECKS",
    "AWS_SECURITY_GOVERNANCE_CHECK_REGISTRY",
    "CHECK_REGISTRATION",
    "check_aws_security_governance_asset_status",
    "check_aws_iam_user_status",
    "check_aws_iam_role_status",
    "check_aws_iam_policy_status",
    "check_aws_kms_key_status",
    "check_aws_kms_alias_status",
    "check_aws_cloudtrail_trail_status",
    "check_aws_config_rule_status",
    "check_aws_config_recorder_status",
    "check_aws_guardduty_detector_status",
    "check_aws_security_hub_status",
    "check_aws_inspector_status",
    "check_aws_macie_status",
    "check_aws_firewall_manager_policy_status",
    *[f"check_aws_{asset_type}_status" for asset_type in AWS_SECURITY_GOVERNANCE_ASSET_TYPES],
    *[
        f"check_aws_{asset_type[4:]}_status"
        for asset_type in AWS_SECURITY_GOVERNANCE_ASSET_TYPES
        if asset_type.startswith("aws_")
    ],
]
