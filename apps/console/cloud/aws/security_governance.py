"""Read-only AWS security and governance inventory.

This module intentionally keeps the Priority 2 security/control-plane surface
separate from the shared AWS asset registry.  The integration lane can import
the public model/type maps and call :func:`sync_aws_security_governance_assets`
without giving this adapter permission to mutate either AWS or CloudMoo
inventory state outside the normal persistence boundary.

The AWS calls in this file are limited to list/describe/get/batch-get APIs.
Provider collections and child metadata are bounded before persistence.  A
failed or malformed collection is never reconciled as empty, and every
provider error is returned in the sync summary so unsupported or denied
services remain visible to operators.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
import hashlib
import logging
import re
from urllib.parse import quote

from botocore.exceptions import BotoCoreError, ClientError
from django.db import models

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
from apps.monitoring.metadata import redact_error_message, redact_sensitive_metadata


logger = logging.getLogger(__name__)


# Public asset types.  The short aliases below keep the integration boundary
# stable if callers use the service API names (SecurityHub, Inspector2, or
# Macie2) rather than the user-facing AWS product names.
AWS_IAM_USER = "aws_iam_user"
AWS_IAM_ROLE = "aws_iam_role"
AWS_IAM_POLICY = "aws_iam_policy"
AWS_KMS_KEY = "aws_kms_key"
AWS_KMS_ALIAS = "aws_kms_alias"
AWS_CLOUDTRAIL_TRAIL = "aws_cloudtrail_trail"
AWS_CONFIG_RULE = "aws_config_rule"
AWS_CONFIG_RECORDER = "aws_config_recorder"
AWS_GUARDDUTY_DETECTOR = "aws_guardduty_detector"
AWS_SECURITY_HUB = "aws_security_hub"
AWS_INSPECTOR = "aws_inspector"
AWS_MACIE = "aws_macie"
AWS_FIREWALL_MANAGER_POLICY = "aws_firewall_manager_policy"

# API-oriented aliases.  They intentionally resolve to the canonical asset
# type instead of creating duplicate persistence families.
AWS_SECURITYHUB_HUB = AWS_SECURITY_HUB
AWS_INSPECTOR2 = AWS_INSPECTOR
AWS_MACIE2 = AWS_MACIE
AWS_FIREWALL_MANAGER = AWS_FIREWALL_MANAGER_POLICY

AWS_GLOBAL_REGION = "global"
AWS_SECURITY_GOVERNANCE_GLOBAL_REGION = AWS_GLOBAL_REGION

AWS_SECURITY_GOVERNANCE_GLOBAL_ASSET_TYPES = (
    AWS_IAM_USER,
    AWS_IAM_ROLE,
    AWS_IAM_POLICY,
)
AWS_SECURITY_GOVERNANCE_REGIONAL_ASSET_TYPES = (
    AWS_KMS_KEY,
    AWS_KMS_ALIAS,
    AWS_CLOUDTRAIL_TRAIL,
    AWS_CONFIG_RULE,
    AWS_CONFIG_RECORDER,
    AWS_GUARDDUTY_DETECTOR,
    AWS_SECURITY_HUB,
    AWS_INSPECTOR,
    AWS_MACIE,
    AWS_FIREWALL_MANAGER_POLICY,
)
AWS_SECURITY_GOVERNANCE_ASSET_TYPES = (
    *AWS_SECURITY_GOVERNANCE_GLOBAL_ASSET_TYPES,
    *AWS_SECURITY_GOVERNANCE_REGIONAL_ASSET_TYPES,
)
SECURITY_GOVERNANCE_ASSET_TYPES = AWS_SECURITY_GOVERNANCE_ASSET_TYPES

NORMALIZED_SECURITY_GOVERNANCE_STATUSES = (
    "available",
    "compliant",
    "degraded",
    "disabled",
    "failed",
    "missing",
    "non_compliant",
    "pending",
    "provider_error",
    "stopped",
    "expired",
    "unknown",
)


# Bounds are deliberately conservative.  The inventory is metadata, not a
# bulk export service, and child calls (describe/get/status) are bounded by
# the corresponding collection limit.
MAX_PAGES_PER_COLLECTION = 100
MAX_ITEMS_PER_COLLECTION = 2_000
MAX_CHILD_READS = 2_000
MAX_METADATA_ITEMS = 80
MAX_METADATA_LIST_ITEMS = 100
MAX_METADATA_DEPTH = 5
MAX_METADATA_STRING = 2_048
MAX_PROVIDER_ID_LENGTH = 4_096
MAX_REGION_LENGTH = 32
MAX_COMPLIANCE_SUMMARY_ITEMS = 100

_REGION_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)+$")
_SAFE_OPERATION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


# This is an allowlist, rather than an assertion based only on a denylist.
# It makes the read-only boundary auditable when a new AWS API is added.
AWS_SECURITY_GOVERNANCE_READ_OPERATION_PREFIXES = (
    "batch_get_",
    "describe_",
    "get_",
    "list_",
)
AWS_SECURITY_GOVERNANCE_MUTATING_OPERATION_PREFIXES = (
    "abort",
    "accept",
    "allocate",
    "associate",
    "attach",
    "authorize",
    "cancel",
    "change",
    "complete",
    "copy",
    "create",
    "deregister",
    "delete",
    "detach",
    "disable",
    "disassociate",
    "enable",
    "execute",
    "import",
    "invite",
    "modify",
    "publish",
    "put",
    "reboot",
    "register",
    "reject",
    "release",
    "remove",
    "replace",
    "reset",
    "restore",
    "resume",
    "revoke",
    "run",
    "send",
    "set",
    "start",
    "stop",
    "suspend",
    "tag",
    "terminate",
    "untag",
    "update",
    "upload",
)


class _InventoryIncomplete(CloudInventoryTransientError):
    """The provider response crossed a safety or completeness boundary."""


def _owner_region_identifier_constraint(name):
    return models.UniqueConstraint(
        fields=("owner", "region", "unique_id"),
        name=f"aws_security_{name}_owner_region_uid_uniq",
    )


class CoreAWSSecurityGovernanceAsset(UtilAsset):
    """Common identity and monitoring context for security metadata assets."""

    unique_id = models.CharField(max_length=255)
    region = models.CharField(max_length=MAX_REGION_LENGTH, db_index=True)
    owner = models.ForeignKey(
        CoreAWSAccount,
        on_delete=models.CASCADE,
        related_name="%(class)s_assets",
    )
    provider_status = models.CharField(max_length=64, blank=True, default="")

    asset_type = None
    provider_type = None

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self.type and self.asset_type:
            self.type = self.asset_type
        if self.metadata is not None:
            self.metadata = redact_sensitive_metadata(self.metadata)
        return super().save(*args, **kwargs)

    @property
    def monitoring_credentials(self):
        """Return ephemeral check context without persisting credentials."""
        metadata = self.metadata if isinstance(self.metadata, dict) else {}
        provider_id = (
            metadata.get("_cloudmoo_raw_id")
            or metadata.get("_cloudmoo_provider_id")
            or self.unique_id
        )
        return {
            "access_key": self.owner.access_key,
            "secret_key": self.owner.secret_key,
            "region": self.region,
            "resource_region": self.region,
            "endpoint_region": (
                getattr(self.owner, "region", None)
                if self.region == AWS_GLOBAL_REGION
                else self.region
            ),
            "provider_id": provider_id,
            "resource_name": metadata.get("_cloudmoo_resource_name") or self.name,
            "asset_type": self.type or self.asset_type,
            "provider_type": self.provider_type or self.asset_type,
            "metadata": metadata,
        }

    @property
    def provider_url(self):
        asset_type = self.type or self.asset_type
        service = _CONSOLE_SERVICE_PATHS.get(asset_type, "securityhub")
        region = self.region
        if region == AWS_GLOBAL_REGION:
            region = getattr(self.owner, "region", "us-east-1") or "us-east-1"
        safe_region = quote(str(region), safe="-._~")
        if asset_type in AWS_SECURITY_GOVERNANCE_GLOBAL_ASSET_TYPES:
            return f"https://console.aws.amazon.com/{service}/home"
        return (
            f"https://{safe_region}.console.aws.amazon.com/{service}/home"
            f"?region={safe_region}"
        )

    def check_status(self):
        from apps.monitoring.checks.aws_security_governance import (
            AWS_SECURITY_GOVERNANCE_CHECKS,
        )

        asset_type = self.type or self.asset_type
        checker = AWS_SECURITY_GOVERNANCE_CHECKS.get(asset_type)
        if checker is None:
            return "provider_error", {"errorCode": "UnsupportedAssetType"}
        return checker(self.unique_id, self.monitoring_credentials)

    def __str__(self):
        return self.name


class CoreAWSIAMUser(CoreAWSSecurityGovernanceAsset):
    asset_type = provider_type = AWS_IAM_USER

    class Meta:
        db_table = "core_aws_iam_user"
        constraints = [_owner_region_identifier_constraint("iam_user")]


class CoreAWSIAMRole(CoreAWSSecurityGovernanceAsset):
    asset_type = provider_type = AWS_IAM_ROLE

    class Meta:
        db_table = "core_aws_iam_role"
        constraints = [_owner_region_identifier_constraint("iam_role")]


class CoreAWSIAMPolicy(CoreAWSSecurityGovernanceAsset):
    asset_type = provider_type = AWS_IAM_POLICY

    class Meta:
        db_table = "core_aws_iam_policy"
        constraints = [_owner_region_identifier_constraint("iam_policy")]


class CoreAWSKMSKey(CoreAWSSecurityGovernanceAsset):
    asset_type = provider_type = AWS_KMS_KEY

    class Meta:
        db_table = "core_aws_kms_key"
        constraints = [_owner_region_identifier_constraint("kms_key")]


class CoreAWSKMSAlias(CoreAWSSecurityGovernanceAsset):
    asset_type = provider_type = AWS_KMS_ALIAS

    class Meta:
        db_table = "core_aws_kms_alias"
        constraints = [_owner_region_identifier_constraint("kms_alias")]


class CoreAWSCloudTrailTrail(CoreAWSSecurityGovernanceAsset):
    asset_type = provider_type = AWS_CLOUDTRAIL_TRAIL

    class Meta:
        db_table = "core_aws_cloudtrail_trail"
        constraints = [_owner_region_identifier_constraint("cloudtrail_trail")]


class CoreAWSConfigRule(CoreAWSSecurityGovernanceAsset):
    asset_type = provider_type = AWS_CONFIG_RULE

    class Meta:
        db_table = "core_aws_config_rule"
        constraints = [_owner_region_identifier_constraint("config_rule")]


class CoreAWSConfigRecorder(CoreAWSSecurityGovernanceAsset):
    asset_type = provider_type = AWS_CONFIG_RECORDER

    class Meta:
        db_table = "core_aws_config_recorder"
        constraints = [_owner_region_identifier_constraint("config_recorder")]


class CoreAWSGuardDutyDetector(CoreAWSSecurityGovernanceAsset):
    asset_type = provider_type = AWS_GUARDDUTY_DETECTOR

    class Meta:
        db_table = "core_aws_guardduty_detector"
        constraints = [_owner_region_identifier_constraint("guardduty_detector")]


class CoreAWSSecurityHub(CoreAWSSecurityGovernanceAsset):
    asset_type = provider_type = AWS_SECURITY_HUB

    class Meta:
        db_table = "core_aws_security_hub"
        constraints = [_owner_region_identifier_constraint("security_hub")]


class CoreAWSInspector(CoreAWSSecurityGovernanceAsset):
    asset_type = provider_type = AWS_INSPECTOR

    class Meta:
        db_table = "core_aws_inspector"
        constraints = [_owner_region_identifier_constraint("inspector")]


class CoreAWSMacie(CoreAWSSecurityGovernanceAsset):
    asset_type = provider_type = AWS_MACIE

    class Meta:
        db_table = "core_aws_macie"
        constraints = [_owner_region_identifier_constraint("macie")]


class CoreAWSFirewallManagerPolicy(CoreAWSSecurityGovernanceAsset):
    asset_type = provider_type = AWS_FIREWALL_MANAGER_POLICY

    class Meta:
        db_table = "core_aws_firewall_manager_policy"
        constraints = [_owner_region_identifier_constraint("firewall_manager")]


# Common spelling aliases are useful to integration code and do not register
# duplicate Django models.
CoreAWSIamUser = CoreAWSIAMUser
CoreAWSIamRole = CoreAWSIAMRole
CoreAWSIamPolicy = CoreAWSIAMPolicy
CoreAWSKmsKey = CoreAWSKMSKey
CoreAWSKmsAlias = CoreAWSKMSAlias
CoreAWSCloudtrailTrail = CoreAWSCloudTrailTrail
CoreAWSGuarddutyDetector = CoreAWSGuardDutyDetector
CoreAWSSecurityHubHub = CoreAWSSecurityHub
CoreAWSSecurityhub = CoreAWSSecurityHub
CoreAWSInspector2 = CoreAWSInspector
CoreAWSMacie2 = CoreAWSMacie
CoreAWSFirewallManager = CoreAWSFirewallManagerPolicy


AWS_SECURITY_GOVERNANCE_ASSET_MODELS = {
    AWS_IAM_USER: CoreAWSIAMUser,
    AWS_IAM_ROLE: CoreAWSIAMRole,
    AWS_IAM_POLICY: CoreAWSIAMPolicy,
    AWS_KMS_KEY: CoreAWSKMSKey,
    AWS_KMS_ALIAS: CoreAWSKMSAlias,
    AWS_CLOUDTRAIL_TRAIL: CoreAWSCloudTrailTrail,
    AWS_CONFIG_RULE: CoreAWSConfigRule,
    AWS_CONFIG_RECORDER: CoreAWSConfigRecorder,
    AWS_GUARDDUTY_DETECTOR: CoreAWSGuardDutyDetector,
    AWS_SECURITY_HUB: CoreAWSSecurityHub,
    AWS_INSPECTOR: CoreAWSInspector,
    AWS_MACIE: CoreAWSMacie,
    AWS_FIREWALL_MANAGER_POLICY: CoreAWSFirewallManagerPolicy,
}
AWS_SECURITY_GOVERNANCE_MODELS = AWS_SECURITY_GOVERNANCE_ASSET_MODELS


_SERVICE_FOR_ASSET_TYPE = {
    AWS_IAM_USER: "iam",
    AWS_IAM_ROLE: "iam",
    AWS_IAM_POLICY: "iam",
    AWS_KMS_KEY: "kms",
    AWS_KMS_ALIAS: "kms",
    AWS_CLOUDTRAIL_TRAIL: "cloudtrail",
    AWS_CONFIG_RULE: "config",
    AWS_CONFIG_RECORDER: "config",
    AWS_GUARDDUTY_DETECTOR: "guardduty",
    AWS_SECURITY_HUB: "securityhub",
    AWS_INSPECTOR: "inspector2",
    AWS_MACIE: "macie2",
    AWS_FIREWALL_MANAGER_POLICY: "fms",
}

_CONSOLE_SERVICE_PATHS = {
    **_SERVICE_FOR_ASSET_TYPE,
    AWS_INSPECTOR: "inspector/v2",
    AWS_MACIE: "macie",
}


AWS_SECURITY_GOVERNANCE_ENDPOINTS = {
    asset_type: {
        "scope": "global" if asset_type in AWS_SECURITY_GOVERNANCE_GLOBAL_ASSET_TYPES else "regional",
        "service": _SERVICE_FOR_ASSET_TYPE[asset_type],
    }
    for asset_type in AWS_SECURITY_GOVERNANCE_ASSET_TYPES
}


def assert_read_only_operation(operation_name):
    """Reject an AWS operation outside the explicit read-only boundary."""
    if not isinstance(operation_name, str) or not _SAFE_OPERATION_RE.fullmatch(operation_name):
        raise ValueError("AWS operation is invalid")
    normalized = operation_name.lower()
    if any(normalized.startswith(prefix) for prefix in AWS_SECURITY_GOVERNANCE_MUTATING_OPERATION_PREFIXES):
        raise ValueError("AWS mutation operations are not supported")
    if not normalized.startswith(AWS_SECURITY_GOVERNANCE_READ_OPERATION_PREFIXES):
        raise ValueError("AWS operation is not allowlisted as read-only")


# Private spelling is convenient for tests and keeps the call sites visually
# obvious when reviewing a new provider operation.
_assert_read_only_operation = assert_read_only_operation


def _read_call(client, operation_name, **kwargs):
    assert_read_only_operation(operation_name)
    method = getattr(client, operation_name)
    response = method(**kwargs)
    if not isinstance(response, Mapping):
        raise _InventoryIncomplete(f"AWS returned an invalid {operation_name} response")
    return response


def _pages(client, operation_name, **kwargs):
    """Yield a bounded, mapping-only stream from a shared AWS paginator."""
    for page_number, page in enumerate(
        iter_pages(client, operation_name, **kwargs),
        start=1,
    ):
        if page_number > MAX_PAGES_PER_COLLECTION:
            raise _InventoryIncomplete(
                f"AWS {operation_name} pagination exceeded the safety bound"
            )
        if not isinstance(page, Mapping):
            raise _InventoryIncomplete(
                f"AWS returned an invalid {operation_name} page"
            )
        yield page


def _collection(client, operation_name, response_key, context, **kwargs):
    """Return a bounded required collection, never treating omission as empty."""
    items = []
    for page in _pages(client, operation_name, **kwargs):
        try:
            values = require_collection(page, response_key, context)
        except CloudInventoryTransientError as error:
            raise _InventoryIncomplete(str(error)) from error
        if len(values) > MAX_ITEMS_PER_COLLECTION:
            raise _InventoryIncomplete(f"AWS {context} page exceeded the safety bound")
        items.extend(values)
        if len(items) > MAX_ITEMS_PER_COLLECTION:
            raise _InventoryIncomplete(f"AWS {context} exceeded the safety bound")
    return items


def _bounded_value(value, depth=0):
    """Bound an already serialized value and apply the shared redaction path."""
    if depth > MAX_METADATA_DEPTH:
        return "<truncated>"
    if isinstance(value, Mapping):
        result = {}
        for index, (key, child) in enumerate(value.items()):
            if index >= MAX_METADATA_ITEMS:
                result["_cloudmoo_truncated_items"] = True
                break
            result[str(key)[:128]] = _bounded_value(child, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        result = [
            _bounded_value(item, depth + 1)
            for item in value[:MAX_METADATA_LIST_ITEMS]
        ]
        if len(value) > MAX_METADATA_LIST_ITEMS:
            result.append("<truncated>")
        return result
    if isinstance(value, str):
        return value[:MAX_METADATA_STRING]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:MAX_METADATA_STRING]


def _safe_metadata(payload, fields, *, context, extra=None):
    """Allowlist fields, serialize, redact, and bound provider metadata."""
    if not isinstance(payload, Mapping):
        raise _InventoryIncomplete(f"AWS returned an invalid {context} object")
    selected = {field: payload[field] for field in fields if field in payload}
    if extra:
        selected.update(extra)
    return _bounded_value(redact_sensitive_metadata(serialize_aws(selected)))


def _identifier(payload, fields, context):
    if not isinstance(payload, Mapping):
        raise _InventoryIncomplete(f"AWS returned an invalid {context} object")
    for field in fields:
        value = payload.get(field)
        if value is None:
            continue
        value = str(value).strip()
        if value:
            if len(value) > MAX_PROVIDER_ID_LENGTH:
                raise _InventoryIncomplete(f"AWS returned an oversized {context} identifier")
            return value
    raise _InventoryIncomplete(f"AWS returned a {context} without an identifier")


def _display_name(payload, raw_id, fields=()):
    for field in fields:
        value = payload.get(field) if isinstance(payload, Mapping) else None
        if value not in (None, ""):
            return str(value)[:100]
    return str(raw_id)[:100]


def _stable_id(region, raw_id):
    """Build the stable owner/region/provider-ID local identity."""
    region = str(region).strip()
    raw_id = str(raw_id).strip()
    qualified = f"{region}|{raw_id}"
    if len(qualified) <= 255:
        return qualified
    digest = hashlib.sha256(qualified.encode("utf-8")).hexdigest()
    return f"{region}|sha256:{digest}"[:255]


def _status_value(value, default="unknown"):
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return "enabled" if value else "disabled"
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")[:64] or default


def _record(
    asset_type,
    region,
    payload,
    *,
    identifier_fields,
    name_fields=(),
    allowed_fields=(),
    provider_status=None,
    extra=None,
):
    raw_id = _identifier(payload, identifier_fields, asset_type)
    metadata_extra = {
        "_cloudmoo_asset_type": asset_type,
        "_cloudmoo_region": region,
        "_cloudmoo_raw_id": raw_id,
        "_cloudmoo_provider_id": raw_id,
        "_cloudmoo_resource_name": _display_name(payload, raw_id, name_fields),
    }
    if extra:
        metadata_extra.update(extra)
    metadata = _safe_metadata(
        payload,
        allowed_fields,
        context=asset_type,
        extra=metadata_extra,
    )
    return {
        "unique_id": _stable_id(region, raw_id),
        "raw_id": raw_id,
        "name": _display_name(payload, raw_id, name_fields),
        "metadata": metadata,
        "provider_status": _status_value(provider_status),
    }


def _error_code(error):
    try:
        return str(aws_error_code(getattr(error, "__cause__", None) or error))[:128]
    except Exception:
        return type(error).__name__[:128]


def _error_kind(error):
    if isinstance(error, _InventoryIncomplete):
        return "incomplete_inventory"
    if is_transient_aws_error(error):
        return "transient"
    if isinstance(error, (ClientError, BotoCoreError)):
        return "provider"
    return "adapter"


def _error_status(error):
    code = _error_code(error).lower()
    if code in {
        "accessdenied",
        "accessdeniedexception",
        "authfailure",
        "expiredtoken",
        "invalidclienttokenid",
        "unauthorizedoperation",
        "unrecognizedclientexception",
    }:
        return "provider_error"
    if "notfound" in code or code in {"nosuchentity", "resourcenotfound"}:
        return "missing"
    if isinstance(error, _InventoryIncomplete) or is_transient_aws_error(error):
        return "degraded"
    return "provider_error"


def _error_record(asset_type, region, error, *, kind=None):
    return {
        "assetType": asset_type,
        "region": region,
        "errorCode": _error_code(error),
        "kind": kind or _error_kind(error),
        "status": _error_status(error),
        "error": redact_error_message(error),
    }


def _warning_metadata(metadata, error, key="_cloudmoo_detail_error"):
    metadata[key] = _error_code(error)
    return metadata


def _normalise_regions(raw_regions):
    if not isinstance(raw_regions, (list, tuple, set, frozenset)):
        raise _InventoryIncomplete("AWS enabled regions are invalid")
    regions = []
    for item in raw_regions:
        if isinstance(item, Mapping):
            item = item.get("RegionName") or item.get("region") or item.get("name")
        if not isinstance(item, str):
            raise _InventoryIncomplete("AWS enabled regions are invalid")
        value = item.strip()
        if (
            not value
            or len(value) > MAX_REGION_LENGTH
            or _REGION_RE.fullmatch(value) is None
        ):
            raise _InventoryIncomplete("AWS enabled regions are invalid")
        regions.append(value)
    return sorted(set(regions))


def _client_region(account):
    """Use the account's valid AWS Region for global service endpoints."""
    value = getattr(account, "region", None)
    return value if isinstance(value, str) and value.strip() else None


def _iam_records(client, asset_type):
    specs = {
        AWS_IAM_USER: (
            "list_users",
            "Users",
            ("Arn", "UserId", "UserName"),
            ("UserName", "Arn", "Path"),
            (
                "Path",
                "UserName",
                "UserId",
                "Arn",
                "CreateDate",
                "PasswordLastUsed",
                "PermissionsBoundary",
                "Tags",
            ),
        ),
        AWS_IAM_ROLE: (
            "list_roles",
            "Roles",
            ("Arn", "RoleId", "RoleName"),
            ("RoleName", "Arn", "Path"),
            (
                "Path",
                "RoleName",
                "RoleId",
                "Arn",
                "CreateDate",
                "MaxSessionDuration",
                "Description",
                "PermissionsBoundary",
                "Tags",
            ),
        ),
        AWS_IAM_POLICY: (
            "list_policies",
            "Policies",
            ("Arn", "PolicyId", "PolicyName"),
            ("PolicyName", "Arn", "Path"),
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
                "Tags",
            ),
        ),
    }
    operation, response_key, identifier_fields, name_fields, allowed_fields = specs[asset_type]
    kwargs = {"Scope": "Local"} if asset_type == AWS_IAM_POLICY else {}
    items = _collection(client, operation, response_key, f"{asset_type} inventory", **kwargs)
    records = []
    for item in items:
        if not isinstance(item, Mapping):
            raise _InventoryIncomplete(f"AWS returned an invalid {asset_type} object")
        records.append(
            _record(
                asset_type,
                AWS_GLOBAL_REGION,
                item,
                identifier_fields=identifier_fields,
                name_fields=name_fields,
                allowed_fields=allowed_fields,
                provider_status="available",
            )
        )
    return records, []


def _kms_key_records(client, region):
    items = _collection(client, "list_keys", "Keys", "KMS key inventory")
    if len(items) > MAX_CHILD_READS:
        raise _InventoryIncomplete("AWS KMS key detail reads exceeded the safety bound")
    records = []
    warnings = []
    for item in items:
        if not isinstance(item, Mapping):
            raise _InventoryIncomplete("AWS returned an invalid KMS key object")
        raw_id = _identifier(item, ("KeyArn", "KeyId"), AWS_KMS_KEY)
        key_id = _identifier(item, ("KeyId", "KeyArn"), AWS_KMS_KEY)
        payload = dict(item)
        status = "unknown"
        try:
            detail_response = _read_call(client, "describe_key", KeyId=key_id)
            detail = detail_response.get("KeyMetadata")
            if not isinstance(detail, Mapping):
                raise _InventoryIncomplete("AWS returned an invalid KMS key detail")
            payload.update(detail)
            status = detail.get("KeyState") or status
        except Exception as error:
            warnings.append(error)
            _warning_metadata(payload, error)
        record = _record(
            AWS_KMS_KEY,
            region,
            payload,
            identifier_fields=("KeyArn", "KeyId"),
            name_fields=("Description", "KeyId", "KeyArn"),
            allowed_fields=(
                "KeyId",
                "KeyArn",
                "Arn",
                "AWSAccountId",
                "CreationDate",
                "Enabled",
                "Description",
                "KeyUsage",
                "KeySpec",
                "KeyState",
                "Origin",
                "ExpirationModel",
                "DeletionDate",
                "KeyManager",
                "CustomerMasterKeySpec",
                "EncryptionAlgorithms",
                "SigningAlgorithms",
                "MultiRegion",
                "MultiRegionKeyType",
                "PrimaryKey",
                "PendingDeletionWindowInDays",
                "Tags",
            ),
            provider_status=status,
        )
        # Preserve the exact provider identity chosen before detail enrichment
        # even when a fixture omits KeyArn from the describe response.
        record["raw_id"] = raw_id
        record["unique_id"] = _stable_id(region, raw_id)
        record["metadata"]["_cloudmoo_raw_id"] = raw_id
        record["metadata"]["_cloudmoo_provider_id"] = raw_id
        records.append(record)
    return records, warnings


def _kms_alias_records(client, region):
    items = _collection(client, "list_aliases", "Aliases", "KMS alias inventory")
    records = []
    for item in items:
        if not isinstance(item, Mapping):
            raise _InventoryIncomplete("AWS returned an invalid KMS alias object")
        target = item.get("TargetKeyId")
        records.append(
            _record(
                AWS_KMS_ALIAS,
                region,
                item,
                identifier_fields=("AliasArn", "AliasName"),
                name_fields=("AliasName", "AliasArn"),
                allowed_fields=(
                    "AliasName",
                    "AliasArn",
                    "TargetKeyId",
                    "CreationDate",
                    "LastUpdatedDate",
                ),
                provider_status="available" if target else "degraded",
            )
        )
    return records, []


def _cloudtrail_records(client, region):
    items = _collection(
        client,
        "describe_trails",
        "trailList",
        "CloudTrail trail inventory",
        includeShadowTrails=False,
    )
    if len(items) > MAX_CHILD_READS:
        raise _InventoryIncomplete("AWS CloudTrail detail reads exceeded the safety bound")
    records = []
    warnings = []
    for item in items:
        if not isinstance(item, Mapping):
            raise _InventoryIncomplete("AWS returned an invalid CloudTrail trail object")
        name = _identifier(item, ("Name", "TrailARN"), AWS_CLOUDTRAIL_TRAIL)
        payload = dict(item)
        status_payload = {}
        try:
            detail = _read_call(client, "get_trail", Name=name)
            if not isinstance(detail, Mapping):
                raise _InventoryIncomplete("AWS returned an invalid CloudTrail detail")
            payload.update(detail)
        except Exception as error:
            warnings.append(error)
            _warning_metadata(payload, error)
        try:
            status_payload = dict(_read_call(client, "get_trail_status", Name=name))
            payload["_cloudmoo_status"] = {
                key: (
                    redact_error_message(status_payload[key])
                    if key == "LatestDeliveryError"
                    else status_payload[key]
                )
                for key in (
                    "IsLogging",
                    "LatestDeliveryError",
                    "LatestDeliveryTime",
                    "LatestDeliveryAttemptTime",
                    "StartLoggingTime",
                    "StopLoggingTime",
                    "TimeLoggingStarted",
                    "TimeLoggingStopped",
                )
                if key in status_payload
            }
        except Exception as error:
            warnings.append(error)
            _warning_metadata(payload, error, "_cloudmoo_status_error")
        trail_status = (
            "logging"
            if status_payload.get("IsLogging") is True
            else "not_logging"
            if status_payload
            else "unknown"
        )
        records.append(
            _record(
                AWS_CLOUDTRAIL_TRAIL,
                region,
                payload,
                identifier_fields=("TrailARN", "Name"),
                name_fields=("Name", "TrailARN"),
                allowed_fields=(
                    "Name",
                    "TrailARN",
                    "S3BucketName",
                    "S3KeyPrefix",
                    "S3KmsKeyId",
                    "KMSKeyId",
                    "IncludeGlobalServiceEvents",
                    "IsMultiRegionTrail",
                    "HomeRegion",
                    "LogFileValidationEnabled",
                    "CloudWatchLogsLogGroupArn",
                    "CloudWatchLogsRoleArn",
                    "SnsTopicARN",
                    "HasCustomEventSelectors",
                    "HasInsightSelectors",
                    "_cloudmoo_status",
                ),
                provider_status=trail_status,
            )
        )
    return records, warnings


def _config_rule_records(client, region):
    rules = _collection(
        client,
        "describe_config_rules",
        "ConfigRules",
        "AWS Config rule inventory",
    )
    compliance_by_name = {}
    warnings = []
    try:
        compliance_items = _collection(
            client,
            "describe_compliance_by_config_rule",
            "ComplianceByConfigRules",
            "AWS Config compliance inventory",
        )
        for item in compliance_items:
            if not isinstance(item, Mapping):
                raise _InventoryIncomplete("AWS returned an invalid Config compliance object")
            name = item.get("ConfigRuleName")
            compliance = item.get("Compliance")
            if isinstance(name, str) and isinstance(compliance, Mapping):
                compliance_by_name[name] = dict(compliance)
    except Exception as error:
        warnings.append(error)
    records = []
    for item in rules:
        if not isinstance(item, Mapping):
            raise _InventoryIncomplete("AWS returned an invalid Config rule object")
        rule_name = _identifier(item, ("ConfigRuleArn", "ConfigRuleName", "ConfigRuleId"), AWS_CONFIG_RULE)
        compliance = compliance_by_name.get(item.get("ConfigRuleName"), {})
        payload = dict(item)
        if compliance:
            payload["_cloudmoo_compliance"] = {
                key: compliance[key]
                for key in (
                    "ComplianceType",
                    "ComplianceContributorCount",
                )
                if key in compliance
            }
        elif warnings:
            payload["_cloudmoo_compliance_error"] = _error_code(warnings[0])
        compliance_type = compliance.get("ComplianceType") if compliance else None
        provider_status = _status_value(compliance_type or item.get("ConfigRuleState"))
        records.append(
            _record(
                AWS_CONFIG_RULE,
                region,
                payload,
                identifier_fields=("ConfigRuleArn", "ConfigRuleName", "ConfigRuleId"),
                name_fields=("ConfigRuleName", "ConfigRuleArn"),
                allowed_fields=(
                    "ConfigRuleName",
                    "ConfigRuleArn",
                    "ConfigRuleId",
                    "Description",
                    "ConfigRuleState",
                    "EvaluationModes",
                    "MaximumExecutionFrequency",
                    "Source",
                    "Scope",
                    "CreatedBy",
                    "CreatedTime",
                    "LastUpdatedBy",
                    "LastUpdatedTime",
                    "_cloudmoo_compliance",
                    "_cloudmoo_compliance_error",
                ),
                provider_status=provider_status,
            )
        )
    return records, warnings


def _config_recorder_records(client, region):
    recorders = _collection(
        client,
        "describe_configuration_recorders",
        "ConfigurationRecorders",
        "AWS Config recorder inventory",
    )
    warnings = []
    status_by_name = {}
    try:
        status_response = _read_call(client, "describe_configuration_recorder_status")
        statuses = status_response.get("ConfigurationRecordersStatus")
        if not isinstance(statuses, list):
            raise _InventoryIncomplete("AWS returned an invalid Config recorder status collection")
        for status in statuses:
            if not isinstance(status, Mapping):
                raise _InventoryIncomplete("AWS returned an invalid Config recorder status")
            name = status.get("name") or status.get("Name") or status.get("ConfigurationRecorderName")
            if name:
                status_by_name[str(name)] = dict(status)
    except Exception as error:
        warnings.append(error)
    records = []
    for item in recorders:
        if not isinstance(item, Mapping):
            raise _InventoryIncomplete("AWS returned an invalid Config recorder object")
        name = _identifier(item, ("name", "Name", "ConfigurationRecorderName"), AWS_CONFIG_RECORDER)
        status = status_by_name.get(name, {})
        if not status and not warnings:
            warnings.append(_InventoryIncomplete("AWS returned no status for a Config recorder"))
        payload = dict(item)
        if status:
            payload["_cloudmoo_status"] = status
        elif warnings:
            payload["_cloudmoo_status_error"] = _error_code(warnings[0])
        records.append(
            _record(
                AWS_CONFIG_RECORDER,
                region,
                payload,
                identifier_fields=("name", "Name", "ConfigurationRecorderName"),
                name_fields=("name", "Name", "ConfigurationRecorderName"),
                allowed_fields=(
                    "name",
                    "Name",
                    "roleARN",
                    "roleArn",
                    "recordingGroup",
                    "_cloudmoo_status",
                    "_cloudmoo_status_error",
                ),
                provider_status=(
                    "recording"
                    if status.get("recording") is True
                    else "not_recording"
                    if status
                    else "unknown"
                ),
            )
        )
    return records, warnings


def _guardduty_records(client, region):
    detector_ids = _collection(
        client,
        "list_detectors",
        "DetectorIds",
        "GuardDuty detector inventory",
    )
    if len(detector_ids) > MAX_CHILD_READS:
        raise _InventoryIncomplete("AWS GuardDuty detail reads exceeded the safety bound")
    records = []
    warnings = []
    for detector_id in detector_ids:
        if not isinstance(detector_id, str) or not detector_id.strip():
            raise _InventoryIncomplete("AWS returned an invalid GuardDuty detector identifier")
        detector_id = detector_id.strip()
        payload = {"DetectorId": detector_id}
        try:
            detail = _read_call(client, "get_detector", DetectorId=detector_id)
            payload.update(detail)
        except Exception as error:
            warnings.append(error)
            _warning_metadata(payload, error)
        records.append(
            _record(
                AWS_GUARDDUTY_DETECTOR,
                region,
                payload,
                identifier_fields=("DetectorId",),
                name_fields=("DetectorId",),
                allowed_fields=(
                    "DetectorId",
                    "Status",
                    "FindingPublishingFrequency",
                    "CreatedAt",
                    "UpdatedAt",
                    "ServiceRole",
                    "DataSources",
                    "Features",
                    "Tags",
                    "_cloudmoo_detail_error",
                ),
                provider_status=payload.get("Status"),
            )
        )
    return records, warnings


def _security_hub_records(client, region):
    payload = dict(_read_call(client, "describe_hub"))
    warnings = []
    try:
        standards = _collection(
            client,
            "get_enabled_standards",
            "StandardsSubscriptions",
            "Security Hub standards inventory",
        )
        payload["_cloudmoo_enabled_standards"] = [
            {
                key: item[key]
                for key in (
                    "StandardsSubscriptionArn",
                    "StandardsArn",
                    "StandardsStatus",
                    "StandardsControlsUpdatable",
                    "StandardsManagedBy",
                )
                if isinstance(item, Mapping) and key in item
            }
            for item in standards
            if isinstance(item, Mapping)
        ]
    except Exception as error:
        warnings.append(error)
        _warning_metadata(payload, error, "_cloudmoo_standards_error")
    if not payload.get("HubArn"):
        payload["HubArn"] = f"{region}|security-hub"
    records = [
        _record(
            AWS_SECURITY_HUB,
            region,
            payload,
            identifier_fields=("HubArn",),
            name_fields=("HubArn",),
            allowed_fields=(
                "HubArn",
                "SubscribedAt",
                "AutoEnableControls",
                "ControlFindingGenerator",
                "MemberAccountLimitReached",
                "_cloudmoo_enabled_standards",
                "_cloudmoo_standards_error",
            ),
            provider_status=payload.get("Status", "available"),
        )
    ]
    return records, warnings


def _account_id_hint(account):
    for field in ("account_id", "aws_account_id", "provider_account_id"):
        value = getattr(account, field, None)
        if value not in (None, ""):
            return str(value)
    return None


def _inspector_records(client, region, account):
    payload = dict(_read_call(client, "batch_get_account_status"))
    accounts = payload.get("accounts")
    if not isinstance(accounts, list):
        raise _InventoryIncomplete("AWS returned an invalid Inspector account status collection")
    if not accounts:
        failed = payload.get("failedAccounts")
        if isinstance(failed, list) and failed:
            raise _InventoryIncomplete("AWS Inspector account status request failed")
        raise _InventoryIncomplete("AWS returned no Inspector account status")
    hint = _account_id_hint(account)
    selected = next(
        (
            item
            for item in accounts
            if isinstance(item, Mapping)
            and hint
            and str(item.get("accountId") or item.get("AccountId") or "") == hint
        ),
        accounts[0],
    )
    if not isinstance(selected, Mapping):
        raise _InventoryIncomplete("AWS returned an invalid Inspector account status")
    payload = dict(selected)
    warnings = []
    try:
        configuration = _read_call(client, "get_configuration")
        payload["_cloudmoo_configuration"] = dict(configuration)
    except Exception as error:
        warnings.append(error)
        _warning_metadata(payload, error, "_cloudmoo_configuration_error")
    raw_id = str(payload.get("accountId") or payload.get("AccountId") or hint or "inspector").strip()
    payload["accountId"] = raw_id
    records = [
        _record(
            AWS_INSPECTOR,
            region,
            payload,
            identifier_fields=("accountId",),
            name_fields=("accountId",),
            allowed_fields=(
                "accountId",
                "state",
                "status",
                "resourceState",
                "lastUpdatedAt",
                "_cloudmoo_configuration",
                "_cloudmoo_configuration_error",
            ),
            provider_status=payload.get("state") or payload.get("status"),
        )
    ]
    return records, warnings


def _macie_records(client, region):
    payload = dict(_read_call(client, "get_macie_session"))
    # A Macie session response is account-scoped and normally has no ARN.  A
    # region-qualified logical ID remains stable and contains no secret.
    payload.setdefault("id", "macie")
    records = [
        _record(
            AWS_MACIE,
            region,
            payload,
            identifier_fields=("accountId", "id"),
            name_fields=("accountId", "id"),
            allowed_fields=(
                "accountId",
                "status",
                "createdAt",
                "updatedAt",
                "findingPublishingFrequency",
                "serviceRole",
                "_cloudmoo_detail_error",
            ),
            provider_status=payload.get("status"),
        )
    ]
    return records, []


def _fms_compliance_summary(items):
    counts = Counter()
    member_accounts = []
    for item in items:
        if not isinstance(item, Mapping):
            raise _InventoryIncomplete("AWS returned an invalid Firewall Manager compliance object")
        status = _status_value(item.get("ComplianceStatus") or item.get("Status"))
        counts[status] += 1
        member = item.get("MemberAccount") or item.get("AccountId")
        if member and len(member_accounts) < MAX_COMPLIANCE_SUMMARY_ITEMS:
            member_accounts.append(str(member)[:128])
    return {
        "total": len(items),
        "byStatus": dict(sorted(counts.items())),
        "memberAccounts": member_accounts,
        "truncated": len(items) > MAX_COMPLIANCE_SUMMARY_ITEMS,
    }


def _fms_records(client, region):
    policies = _collection(
        client,
        "list_policies",
        "PolicyList",
        "Firewall Manager policy inventory",
    )
    if len(policies) > MAX_CHILD_READS:
        raise _InventoryIncomplete("AWS Firewall Manager detail reads exceeded the safety bound")
    records = []
    warnings = []
    for item in policies:
        if not isinstance(item, Mapping):
            raise _InventoryIncomplete("AWS returned an invalid Firewall Manager policy object")
        policy_id = _identifier(item, ("PolicyId", "PolicyArn", "PolicyName"), AWS_FIREWALL_MANAGER_POLICY)
        payload = dict(item)
        try:
            detail_response = _read_call(client, "get_policy", PolicyId=policy_id)
            detail = detail_response.get("Policy")
            if not isinstance(detail, Mapping):
                raise _InventoryIncomplete("AWS returned an invalid Firewall Manager policy detail")
            # Do not merge the complete policy object.  It can contain the
            # arbitrary ManagedServiceData policy document.
            for key in (
                "PolicyId",
                "PolicyName",
                "PolicyArn",
                "ResourceType",
                "ResourceTypeList",
                "RemediationEnabled",
                "DeleteUnusedFMManagedResources",
                "ResourceSetIds",
                "ExcludeResourceTags",
                "IncludeMap",
                "ExcludeMap",
                "PolicyStatus",
                "ResourceTags",
            ):
                if key in detail:
                    payload[key] = detail[key]
        except Exception as error:
            warnings.append(error)
            _warning_metadata(payload, error)
        try:
            compliance_items = _collection(
                client,
                "list_compliance_status",
                "PolicyComplianceStatusList",
                "Firewall Manager compliance inventory",
                PolicyId=policy_id,
            )
            payload["_cloudmoo_compliance_summary"] = _fms_compliance_summary(compliance_items)
        except Exception as error:
            warnings.append(error)
            _warning_metadata(payload, error, "_cloudmoo_compliance_error")
        records.append(
            _record(
                AWS_FIREWALL_MANAGER_POLICY,
                region,
                payload,
                identifier_fields=("PolicyId", "PolicyArn", "PolicyName"),
                name_fields=("PolicyName", "PolicyArn", "PolicyId"),
                allowed_fields=(
                    "PolicyId",
                    "PolicyName",
                    "PolicyArn",
                    "ResourceType",
                    "ResourceTypeList",
                    "RemediationEnabled",
                    "DeleteUnusedFMManagedResources",
                    "ResourceSetIds",
                    "ExcludeResourceTags",
                    "IncludeMap",
                    "ExcludeMap",
                    "PolicyStatus",
                    "ResourceTags",
                    "_cloudmoo_compliance_summary",
                    "_cloudmoo_compliance_error",
                    "_cloudmoo_detail_error",
                ),
                provider_status=payload.get("PolicyStatus") or "available",
            )
        )
    return records, warnings


_IAM_FAMILY_SPECS = (
    (AWS_IAM_USER, CoreAWSIAMUser, "iam"),
    (AWS_IAM_ROLE, CoreAWSIAMRole, "iam"),
    (AWS_IAM_POLICY, CoreAWSIAMPolicy, "iam"),
)
_REGIONAL_FAMILY_SPECS = (
    (AWS_KMS_KEY, CoreAWSKMSKey, "kms", _kms_key_records),
    (AWS_KMS_ALIAS, CoreAWSKMSAlias, "kms", _kms_alias_records),
    (AWS_CLOUDTRAIL_TRAIL, CoreAWSCloudTrailTrail, "cloudtrail", _cloudtrail_records),
    (AWS_CONFIG_RULE, CoreAWSConfigRule, "config", _config_rule_records),
    (AWS_CONFIG_RECORDER, CoreAWSConfigRecorder, "config", _config_recorder_records),
    (AWS_GUARDDUTY_DETECTOR, CoreAWSGuardDutyDetector, "guardduty", _guardduty_records),
    (AWS_SECURITY_HUB, CoreAWSSecurityHub, "securityhub", _security_hub_records),
    (AWS_INSPECTOR, CoreAWSInspector, "inspector2", _inspector_records),
    (AWS_MACIE, CoreAWSMacie, "macie2", _macie_records),
    (AWS_FIREWALL_MANAGER_POLICY, CoreAWSFirewallManagerPolicy, "fms", _fms_records),
)


# A declarative view is useful to the integration lane and provides a simple
# audit surface for read-only operation review.  Child calls are listed here
# even where a service's main collection is a singleton detail call.
AWS_SECURITY_GOVERNANCE_COLLECTION_SPECS = (
    {
        "asset_type": AWS_IAM_USER,
        "scope": "global",
        "service": "iam",
        "operations": ("list_users", "get_user"),
    },
    {
        "asset_type": AWS_IAM_ROLE,
        "scope": "global",
        "service": "iam",
        "operations": ("list_roles", "get_role"),
    },
    {
        "asset_type": AWS_IAM_POLICY,
        "scope": "global",
        "service": "iam",
        "operations": ("list_policies", "get_policy"),
    },
    {
        "asset_type": AWS_KMS_KEY,
        "scope": "regional",
        "service": "kms",
        "operations": ("list_keys", "describe_key"),
    },
    {
        "asset_type": AWS_KMS_ALIAS,
        "scope": "regional",
        "service": "kms",
        "operations": ("list_aliases",),
    },
    {
        "asset_type": AWS_CLOUDTRAIL_TRAIL,
        "scope": "regional",
        "service": "cloudtrail",
        "operations": ("describe_trails", "get_trail", "get_trail_status"),
    },
    {
        "asset_type": AWS_CONFIG_RULE,
        "scope": "regional",
        "service": "config",
        "operations": ("describe_config_rules", "describe_compliance_by_config_rule"),
    },
    {
        "asset_type": AWS_CONFIG_RECORDER,
        "scope": "regional",
        "service": "config",
        "operations": ("describe_configuration_recorders", "describe_configuration_recorder_status"),
    },
    {
        "asset_type": AWS_GUARDDUTY_DETECTOR,
        "scope": "regional",
        "service": "guardduty",
        "operations": ("list_detectors", "get_detector"),
    },
    {
        "asset_type": AWS_SECURITY_HUB,
        "scope": "regional",
        "service": "securityhub",
        "operations": ("describe_hub", "get_enabled_standards"),
    },
    {
        "asset_type": AWS_INSPECTOR,
        "scope": "regional",
        "service": "inspector2",
        "operations": ("batch_get_account_status", "get_configuration"),
    },
    {
        "asset_type": AWS_MACIE,
        "scope": "regional",
        "service": "macie2",
        "operations": ("get_macie_session",),
    },
    {
        "asset_type": AWS_FIREWALL_MANAGER_POLICY,
        "scope": "regional",
        "service": "fms",
        "operations": ("list_policies", "get_policy", "list_compliance_status"),
    },
)
AWS_SECURITY_GOVERNANCE_SERVICE_SPECS = AWS_SECURITY_GOVERNANCE_COLLECTION_SPECS


def _persist_records(model, account, region, records, *, reconcile):
    """Persist validated records and reconcile only a complete scope."""
    seen_ids = set()
    for record in records:
        if not isinstance(record, Mapping) or not record.get("unique_id"):
            raise _InventoryIncomplete("AWS returned an invalid security asset record")
        unique_id = str(record["unique_id"])
        if unique_id in seen_ids:
            raise _InventoryIncomplete("AWS returned a duplicate security asset identifier")
        seen_ids.add(unique_id)

    for record in records:
        defaults = {
            "name": str(record.get("name") or record["unique_id"])[:100],
            "type": getattr(model, "asset_type", None),
            "metadata": redact_sensitive_metadata(record.get("metadata") or {}),
            "provider_status": str(record.get("provider_status") or "unknown")[:64],
            "monitoring": model.Monitoring.ACTIVE,
        }
        asset, created = model.objects.get_or_create(
            owner=account,
            region=region,
            unique_id=str(record["unique_id"]),
            defaults=defaults,
        )
        if not created:
            for field, value in defaults.items():
                setattr(asset, field, value)
        asset.region = region
        if created:
            # ``region`` is a lookup field, but assigning it explicitly keeps
            # this path friendly to simple in-memory managers used by tests.
            asset.region = region
        if not created:
            if asset.monitoring == model.Monitoring.NO_LONGER_EXISTS:
                asset.monitoring = model.Monitoring.ACTIVE
            asset.save()

    if reconcile:
        model.objects.filter(owner=account, region=region).exclude(
            unique_id__in=seen_ids
        ).update(monitoring=model.Monitoring.NO_LONGER_EXISTS)
    return len(records)


def _family_failure(summary, asset_type, region, error):
    result = {
        "status": _error_status(error),
        "complete": False,
        "reconciled": False,
        "count": None,
        "error": _error_record(asset_type, region, error),
    }
    summary["families"][asset_type][region] = result
    summary[asset_type][region] = result
    summary["errors"].append(result["error"])


def _family_success(summary, account, asset_type, model, region, records, warnings):
    try:
        count = _persist_records(
            model,
            account,
            region,
            records,
            reconcile=not warnings,
        )
    except Exception as error:
        _family_failure(summary, asset_type, region, error)
        return

    for warning in warnings:
        summary["errors"].append(
            _error_record(asset_type, region, warning, kind="incomplete_inventory")
        )
    result = {
        "status": "degraded" if warnings else "ok",
        "complete": not warnings,
        "reconciled": not warnings,
        "count": count,
    }
    if warnings:
        result["warningCount"] = len(warnings)
    summary["families"][asset_type][region] = result
    summary[asset_type][region] = result
    summary["counts"][asset_type] += count
    summary["synced"][asset_type] += count


def _sync_family(summary, account, asset_type, model, service, region, collector, client):
    try:
        if asset_type == AWS_INSPECTOR:
            records, warnings = collector(client, region, account)
        else:
            records, warnings = collector(client, region)
        _family_success(summary, account, asset_type, model, region, records, warnings)
    except Exception as error:
        _family_failure(summary, asset_type, region, error)
        logger.warning(
            "AWS security-governance collection failed for %s/%s (%s)",
            asset_type,
            region,
            _error_code(error),
        )


def _new_summary(regions):
    summary = {
        "regions": list(regions),
        "globalRegion": AWS_GLOBAL_REGION,
        "counts": {asset_type: 0 for asset_type in AWS_SECURITY_GOVERNANCE_ASSET_TYPES},
        "synced": {asset_type: 0 for asset_type in AWS_SECURITY_GOVERNANCE_ASSET_TYPES},
        "families": {asset_type: {} for asset_type in AWS_SECURITY_GOVERNANCE_ASSET_TYPES},
        "errors": [],
    }
    for asset_type in AWS_SECURITY_GOVERNANCE_ASSET_TYPES:
        summary[asset_type] = summary["families"][asset_type]
    return summary


def sync_aws_security_governance_assets(account, regions=None):
    """Synchronize requested security/governance families read-only.

    ``regions`` is an optional explicit enabled-region set for deterministic
    callers and tests.  IAM is always collected once at the account/global
    scope and uses ``global`` as its persisted Region identity.  Regional
    families run for every normalized Region and only a complete family scope
    is reconciled.
    """
    if regions is None:
        try:
            normalized_regions = _normalise_regions(get_enabled_regions(account))
            region_error = None
        except Exception as error:
            normalized_regions = []
            region_error = error
    else:
        try:
            normalized_regions = _normalise_regions(regions)
            region_error = None
        except Exception as error:
            normalized_regions = []
            region_error = error

    summary = _new_summary(normalized_regions)
    if region_error is not None:
        summary["errors"].append(_error_record("region_discovery", None, region_error))

    clients = {}
    client_errors = {}

    def get_client(service, endpoint_region):
        key = (service, endpoint_region)
        if key in client_errors:
            raise client_errors[key]
        if key not in clients:
            try:
                clients[key] = aws_client(account, service, region=endpoint_region)
            except Exception as error:
                client_errors[key] = error
                raise
        return clients[key]

    # IAM is global-style.  The endpoint still receives a valid configured
    # Region because "global" is a model identity, not a boto3 endpoint.
    iam_endpoint_region = _client_region(account)
    for asset_type, model, service in _IAM_FAMILY_SPECS:
        try:
            client = get_client(service, iam_endpoint_region)
            records, warnings = _iam_records(client, asset_type)
            _family_success(
                summary,
                account,
                asset_type,
                model,
                AWS_GLOBAL_REGION,
                records,
                warnings,
            )
        except Exception as error:
            _family_failure(summary, asset_type, AWS_GLOBAL_REGION, error)
            logger.warning(
                "AWS global security-governance collection failed for %s (%s)",
                asset_type,
                _error_code(error),
            )

    for region in normalized_regions:
        for asset_type, model, service, collector in _REGIONAL_FAMILY_SPECS:
            try:
                client = get_client(service, region)
            except Exception as error:
                _family_failure(summary, asset_type, region, error)
                continue
            _sync_family(summary, account, asset_type, model, service, region, collector, client)

    return summary


# Compatibility names used by integration lanes that group security controls
# under "governance" or "security" rather than the full module name.
sync_aws_security_governance_inventory = sync_aws_security_governance_assets
sync_aws_security_assets = sync_aws_security_governance_assets
AWS_SECURITY_GOVERNANCE_TYPES = AWS_SECURITY_GOVERNANCE_ASSET_TYPES


__all__ = [
    "AWS_GLOBAL_REGION",
    "AWS_SECURITY_GOVERNANCE_GLOBAL_REGION",
    "AWS_IAM_USER",
    "AWS_IAM_ROLE",
    "AWS_IAM_POLICY",
    "AWS_KMS_KEY",
    "AWS_KMS_ALIAS",
    "AWS_CLOUDTRAIL_TRAIL",
    "AWS_CONFIG_RULE",
    "AWS_CONFIG_RECORDER",
    "AWS_GUARDDUTY_DETECTOR",
    "AWS_SECURITY_HUB",
    "AWS_SECURITYHUB_HUB",
    "AWS_INSPECTOR",
    "AWS_INSPECTOR2",
    "AWS_MACIE",
    "AWS_MACIE2",
    "AWS_FIREWALL_MANAGER_POLICY",
    "AWS_FIREWALL_MANAGER",
    "AWS_SECURITY_GOVERNANCE_ASSET_TYPES",
    "AWS_SECURITY_GOVERNANCE_GLOBAL_ASSET_TYPES",
    "AWS_SECURITY_GOVERNANCE_REGIONAL_ASSET_TYPES",
    "SECURITY_GOVERNANCE_ASSET_TYPES",
    "NORMALIZED_SECURITY_GOVERNANCE_STATUSES",
    "AWS_SECURITY_GOVERNANCE_ASSET_MODELS",
    "AWS_SECURITY_GOVERNANCE_MODELS",
    "AWS_SECURITY_GOVERNANCE_ENDPOINTS",
    "AWS_SECURITY_GOVERNANCE_COLLECTION_SPECS",
    "AWS_SECURITY_GOVERNANCE_SERVICE_SPECS",
    "AWS_SECURITY_GOVERNANCE_READ_OPERATION_PREFIXES",
    "AWS_SECURITY_GOVERNANCE_MUTATING_OPERATION_PREFIXES",
    "MAX_PAGES_PER_COLLECTION",
    "MAX_ITEMS_PER_COLLECTION",
    "MAX_METADATA_ITEMS",
    "MAX_METADATA_LIST_ITEMS",
    "MAX_METADATA_DEPTH",
    "MAX_METADATA_STRING",
    "CoreAWSSecurityGovernanceAsset",
    "CoreAWSIAMUser",
    "CoreAWSIAMRole",
    "CoreAWSIAMPolicy",
    "CoreAWSKMSKey",
    "CoreAWSKMSAlias",
    "CoreAWSCloudTrailTrail",
    "CoreAWSConfigRule",
    "CoreAWSConfigRecorder",
    "CoreAWSGuardDutyDetector",
    "CoreAWSSecurityHub",
    "CoreAWSInspector",
    "CoreAWSMacie",
    "CoreAWSFirewallManagerPolicy",
    "CoreAWSIamUser",
    "CoreAWSIamRole",
    "CoreAWSIamPolicy",
    "CoreAWSKmsKey",
    "CoreAWSKmsAlias",
    "CoreAWSCloudtrailTrail",
    "CoreAWSGuarddutyDetector",
    "CoreAWSSecurityHubHub",
    "CoreAWSSecurityhub",
    "CoreAWSInspector2",
    "CoreAWSMacie2",
    "CoreAWSFirewallManager",
    "assert_read_only_operation",
    "sync_aws_security_governance_assets",
    "sync_aws_security_governance_inventory",
    "sync_aws_security_assets",
]
