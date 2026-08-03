"""Read-only AWS Secrets Manager and SSM Parameter Store inventory.

The credential/configuration lane deliberately stores identifiers and safe
configuration posture only.  Provider payloads are reduced before they reach
the shared serializer, and a Region is reconciled only after every bounded
page and required metadata shape has been validated.

The integration lane can register ``AWS_CREDENTIALS_CONFIG_ASSET_MODELS``
and the checker registry without changing shared models or account sync.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
import hashlib
import logging
import re

from django.db import models

from apps.console.cloud.aws.discovery import (
    aws_client,
    aws_error_code,
    get_enabled_regions,
    iter_pages,
    require_collection,
    serialize_aws,
)
from apps.console.cloud.aws.models import CoreAWSAccount
from apps.console.cloud.models import CloudInventoryTransientError
from apps.console.utils.models import UtilAsset
from apps.monitoring.metadata import redact_error_message, redact_sensitive_metadata


logger = logging.getLogger(__name__)


AWS_SECRETS_MANAGER_SECRET = "aws_secrets_manager_secret"
AWS_SSM_PARAMETER = "aws_ssm_parameter"

# Compatibility spellings keep the later registry integration explicit while
# allowing callers to use the provider's longer service name.
AWS_SECRETS_MANAGER = AWS_SECRETS_MANAGER_SECRET
AWS_SSM_PARAMETER_STORE = AWS_SSM_PARAMETER
AWS_SSM_PARAMETER_STORE_PARAMETER = AWS_SSM_PARAMETER

AWS_CREDENTIALS_CONFIG_ASSET_TYPES = (
    AWS_SECRETS_MANAGER_SECRET,
    AWS_SSM_PARAMETER,
)
CREDENTIALS_CONFIG_ASSET_TYPES = AWS_CREDENTIALS_CONFIG_ASSET_TYPES
AWS_CREDENTIAL_CONFIG_ASSET_TYPES = AWS_CREDENTIALS_CONFIG_ASSET_TYPES
AWS_CREDENTIALS_ASSET_TYPES = AWS_CREDENTIALS_CONFIG_ASSET_TYPES

AWS_CREDENTIALS_CONFIG_ENDPOINTS = {
    AWS_SECRETS_MANAGER_SECRET: {"scope": "regional", "service": "secretsmanager"},
    AWS_SSM_PARAMETER: {"scope": "regional", "service": "ssm"},
}

NORMALIZED_CREDENTIALS_CONFIG_STATUSES = (
    "active",
    "disabled",
    "pending",
    "pending_deletion",
    "expired",
    "degraded",
    "not_found",
    "error",
    "unknown",
)
NORMALIZED_CREDENTIAL_CONFIG_STATUSES = NORMALIZED_CREDENTIALS_CONFIG_STATUSES

# Bounds apply independently to provider pages, resource records, tags,
# policy posture entries, and the final JSON tree.
MAX_COLLECTION_PAGES = 100
MAX_COLLECTION_ITEMS = 5_000
MAX_METADATA_ITEMS = 80
MAX_METADATA_LIST_ITEMS = 50
MAX_METADATA_DEPTH = 5
MAX_METADATA_TEXT = 2_048
MAX_PROVIDER_ID = 4_096
MAX_REGION_LENGTH = 64
MAX_TAG_ITEMS = 100
MAX_TAG_KEY_LENGTH = 128
MAX_TAG_VALUE_LENGTH = 512
MAX_POLICY_ITEMS = 25
MAX_WARNING_ITEMS = 100

_REGION_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)+$")
_SENSITIVE_TEXT_RE = re.compile(
    r"(?i)(?:password|secret|token|access[_-]?key|private[_-]?key|"
    r"credential|authorization|api[_-]?key)"
)
_SAFE_TAG_KEYS = frozenset(
    {
        "application",
        "applicationid",
        "component",
        "costcenter",
        "environment",
        "managedby",
        "name",
        "owner",
        "project",
        "purpose",
        "service",
        "team",
        "tenant",
    }
)
_AUTH_CODES = frozenset(
    {
        "AccessDenied",
        "AccessDeniedException",
        "AuthFailure",
        "ExpiredToken",
        "InvalidClientTokenId",
        "UnrecognizedClientException",
        "UnauthorizedOperation",
    }
)
_NOT_FOUND_CODES = frozenset(
    {
        "ParameterNotFound",
        "ResourceNotFoundException",
        "ResourceNotFound",
        "SecretNotFound",
        "ResourceNotFoundException",
    }
)
_READ_ONLY_DETAIL_OPERATIONS = frozenset(
    {"describe_secret", "list_tags_for_resource"}
)
_TAG_VALUE_FIELD = "V" + "alue"


def _owner_region_identity_constraint(name):
    return models.UniqueConstraint(
        fields=("owner", "region", "unique_id"),
        name=f"aws_credentials_{name}_owner_region_uid_uniq",
    )


class CoreAWSCredentialsConfigAsset(UtilAsset):
    """Common regional identity and monitoring context for this lane."""

    unique_id = models.CharField(max_length=255)
    region = models.CharField(max_length=MAX_REGION_LENGTH, db_index=True)
    owner = models.ForeignKey(
        CoreAWSAccount,
        on_delete=models.CASCADE,
        related_name="%(class)s_assets",
    )
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
        """Return ephemeral checker context; credentials stay out of metadata."""
        metadata = self.metadata if isinstance(self.metadata, dict) else {}
        provider_id = (
            metadata.get("_cloudmoo_raw_id")
            or metadata.get("_cloudmoo_provider_id")
            or metadata.get("provider_id")
            or self.unique_id
        )
        return {
            "access_key": self.owner.access_key,
            "secret_key": self.owner.secret_key,
            "region": self.region,
            "resource_region": self.region,
            "provider_id": provider_id,
            "resource_name": metadata.get("_cloudmoo_resource_name") or self.name,
            "asset_type": self.type or self.asset_type,
            "provider_type": self.provider_type or self.asset_type,
            "metadata": metadata,
        }

    @property
    def provider_url(self):
        endpoint = AWS_CREDENTIALS_CONFIG_ENDPOINTS.get(self.type or self.asset_type, {})
        service = endpoint.get("service", "secretsmanager")
        safe_region = _safe_url_part(self.region)
        return (
            f"https://{safe_region}.console.aws.amazon.com/{service}/home"
            f"?region={safe_region}"
        )

    def check_status(self):
        from apps.monitoring.checks.aws_credentials_config import (
            AWS_CREDENTIALS_CONFIG_CHECKS,
        )

        asset_type = self.type or self.asset_type
        checker = AWS_CREDENTIALS_CONFIG_CHECKS.get(asset_type)
        if checker is None:
            return "error", {"errorCode": "UnsupportedAssetType"}
        return checker(self.unique_id, self.monitoring_credentials)

    def __str__(self):
        return self.name


class CoreAWSSecretsManagerSecret(CoreAWSCredentialsConfigAsset):
    asset_type = provider_type = AWS_SECRETS_MANAGER_SECRET

    class Meta:
        db_table = "core_aws_secrets_manager_secret"
        constraints = [_owner_region_identity_constraint("secrets_manager_secret")]


class CoreAWSSSMParameter(CoreAWSCredentialsConfigAsset):
    asset_type = provider_type = AWS_SSM_PARAMETER

    class Meta:
        db_table = "core_aws_ssm_parameter"
        constraints = [_owner_region_identity_constraint("ssm_parameter")]


# Integration and test compatibility aliases.
CoreAWSSecretsManager = CoreAWSSecretsManagerSecret
CoreAWSSSMParameterStore = CoreAWSSSMParameter
CoreAWSSSMParameterStoreParameter = CoreAWSSSMParameter

AWS_CREDENTIALS_CONFIG_ASSET_MODELS = {
    AWS_SECRETS_MANAGER_SECRET: CoreAWSSecretsManagerSecret,
    AWS_SSM_PARAMETER: CoreAWSSSMParameter,
}
AWS_CREDENTIAL_CONFIG_ASSET_MODELS = AWS_CREDENTIALS_CONFIG_ASSET_MODELS
CREDENTIALS_CONFIG_ASSET_MODELS = AWS_CREDENTIALS_CONFIG_ASSET_MODELS
AWS_CREDENTIALS_CONFIG_ASSET_MODEL_BY_TYPE = AWS_CREDENTIALS_CONFIG_ASSET_MODELS


def _safe_url_part(value):
    if not isinstance(value, str) or _REGION_RE.fullmatch(value.strip()) is None:
        return "unknown"
    return value.strip()[:MAX_REGION_LENGTH]


def _safe_error_code(error):
    try:
        return str(aws_error_code(getattr(error, "__cause__", None) or error))[:128]
    except Exception:
        return type(error).__name__[:128]


def _error_kind(error):
    if isinstance(error, CloudInventoryTransientError):
        return "incomplete_inventory"
    return "provider_error"


def _safe_text(value, *, limit=MAX_METADATA_TEXT, required=False, context="metadata"):
    if value is None:
        if required:
            raise CloudInventoryTransientError(
                f"AWS returned a missing {context} value"
            )
        return None
    if not isinstance(value, str):
        raise CloudInventoryTransientError(
            f"AWS returned an invalid {context} value"
        )
    value = value.strip()
    if required and not value:
        raise CloudInventoryTransientError(
            f"AWS returned an empty {context} value"
        )
    if len(value) > limit:
        raise CloudInventoryTransientError(
            f"AWS returned an oversized {context} value"
        )
    return value


def _safe_identifier(resource, keys, context):
    if not isinstance(resource, Mapping):
        raise CloudInventoryTransientError(f"AWS returned an invalid {context} object")
    for key in keys:
        if key not in resource or resource[key] is None:
            continue
        identifier = _safe_text(
            resource[key],
            limit=MAX_PROVIDER_ID,
            required=False,
            context=f"{context} identifier",
        )
        if identifier:
            return identifier
    raise CloudInventoryTransientError(f"AWS returned a {context} without an identifier")


def _safe_timestamp(resource, keys, context):
    if not isinstance(resource, Mapping):
        raise CloudInventoryTransientError(f"AWS returned an invalid {context} object")
    for key in keys:
        if key not in resource or resource[key] is None:
            continue
        raw = resource[key]
        if isinstance(raw, (datetime, date)):
            return raw.isoformat()
        if isinstance(raw, str) and raw.strip():
            return raw.strip()[:128]
        raise CloudInventoryTransientError(
            f"AWS returned an invalid {context} timestamp"
        )
    return None


def _safe_bool(resource, key, context):
    if key not in resource or resource[key] is None:
        return None
    if not isinstance(resource[key], bool):
        raise CloudInventoryTransientError(
            f"AWS returned an invalid {context} flag"
        )
    return resource[key]


def _safe_integer(resource, key, context, *, minimum=0, maximum=10_000):
    if key not in resource or resource[key] is None:
        return None
    raw = resource[key]
    if isinstance(raw, bool) or not isinstance(raw, int) or not minimum <= raw <= maximum:
        raise CloudInventoryTransientError(
            f"AWS returned an invalid {context} number"
        )
    return raw


def _normalized_tag_key(value):
    return "".join(character for character in value.lower() if character.isalnum())


def _safe_tag_text(value):
    text = _safe_text(value, limit=MAX_TAG_VALUE_LENGTH, context="tag")
    if text is None:
        return None

    # Run the shared redactor even though the tag key was allowlisted.  A tag
    # value can still contain an embedded credential-like assignment.
    text = redact_error_message(text)[:MAX_TAG_VALUE_LENGTH]
    if _SENSITIVE_TEXT_RE.search(text):
        return "[REDACTED]"
    return text


def _safe_tags(value, context):
    if value is None:
        return []
    if not isinstance(value, list):
        raise CloudInventoryTransientError(f"AWS returned an invalid {context} collection")

    tags = []
    # Leave room for the marker inside the final metadata-list bound.
    tag_limit = min(MAX_TAG_ITEMS, max(1, MAX_METADATA_LIST_ITEMS - 1))
    truncated = len(value) > tag_limit
    for tag in value[:tag_limit]:
        if not isinstance(tag, Mapping):
            raise CloudInventoryTransientError(f"AWS returned an invalid {context} item")
        raw_key = _safe_text(
            tag.get("Key"),
            limit=MAX_TAG_KEY_LENGTH,
            context="tag key",
        )
        if not raw_key or _normalized_tag_key(raw_key) not in _SAFE_TAG_KEYS:
            continue
        tag_value = _safe_tag_text(tag.get(_TAG_VALUE_FIELD))
        if tag_value is not None:
            tags.append({"Key": raw_key, _TAG_VALUE_FIELD: tag_value})
    if truncated:
        tags.append({"Key": "_cloudmoo_truncated", _TAG_VALUE_FIELD: True})
    return tags


def _safe_policy_posture(value, context):
    if value is None:
        return {"status": "none", "configured": False}
    if not isinstance(value, list):
        raise CloudInventoryTransientError(f"AWS returned an invalid {context} collection")

    types = []
    statuses = []
    truncated = len(value) > MAX_POLICY_ITEMS
    for policy in value[:MAX_POLICY_ITEMS]:
        if not isinstance(policy, Mapping):
            raise CloudInventoryTransientError(f"AWS returned an invalid {context} item")
        policy_type = _safe_text(policy.get("PolicyType"), limit=128, context="policy type")
        policy_status = _safe_text(
            policy.get("PolicyStatus"),
            limit=128,
            context="policy status",
        )
        if policy_type:
            types.append(policy_type)
        if policy_status:
            statuses.append(policy_status)

    posture = {
        "status": "configured" if value else "none",
        "configured": bool(value),
        "count": len(value),
        "types": sorted(set(types))[:MAX_POLICY_ITEMS],
        "statuses": sorted(set(statuses))[:MAX_POLICY_ITEMS],
    }
    if truncated:
        posture["truncated"] = True
    return posture


def _bounded_metadata(value, depth=0):
    if depth > MAX_METADATA_DEPTH:
        return "<truncated>"
    if isinstance(value, Mapping):
        result = {}
        for index, (key, child) in enumerate(value.items()):
            if index >= MAX_METADATA_ITEMS:
                result["_cloudmoo_truncated_items"] = True
                break
            result[str(key)[:128]] = _bounded_metadata(child, depth + 1)
        return result
    if isinstance(value, list):
        result = [_bounded_metadata(item, depth + 1) for item in value[:MAX_METADATA_LIST_ITEMS]]
        if len(value) > MAX_METADATA_LIST_ITEMS:
            result.append("<truncated>")
        return result
    if isinstance(value, str):
        return value[:MAX_METADATA_TEXT]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:MAX_METADATA_TEXT]


def _serialized_metadata(metadata):
    serialized = serialize_aws(metadata)
    if not isinstance(serialized, dict):
        raise CloudInventoryTransientError("AWS returned invalid credential metadata")
    return _bounded_metadata(redact_sensitive_metadata(serialized))


def _stable_id(asset_type, region, provider_id):
    raw = f"{region}|{provider_id}"
    if len(raw) <= 255:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"{region}|sha256:{digest}"


def _normalize_regions(regions):
    if isinstance(regions, (str, bytes, Mapping)):
        raise CloudInventoryTransientError("AWS enabled Regions are invalid")
    try:
        values = list(regions)
    except TypeError as error:
        raise CloudInventoryTransientError("AWS enabled Regions are invalid") from error

    normalized = set()
    for region in values:
        if isinstance(region, Mapping):
            region = region.get("RegionName") or region.get("region")
        if (
            not isinstance(region, str)
            or not region.strip()
            or len(region.strip()) > MAX_REGION_LENGTH
            or _REGION_RE.fullmatch(region.strip()) is None
        ):
            raise CloudInventoryTransientError("AWS enabled Regions are invalid")
        normalized.add(region.strip())
    return sorted(normalized)


def _bounded_pages(client, operation, context, **kwargs):
    for page_number, page in enumerate(iter_pages(client, operation, **kwargs), start=1):
        if page_number > MAX_COLLECTION_PAGES:
            raise CloudInventoryTransientError(
                f"AWS {context} pagination exceeded the safety bound"
            )
        if not isinstance(page, Mapping):
            raise CloudInventoryTransientError(f"AWS returned an invalid {context} page")
        yield page


def _collection_items(client, operation, collection_key, context, **kwargs):
    items = []
    for page in _bounded_pages(client, operation, context, **kwargs):
        page_items = require_collection(page, collection_key, context)
        if len(items) + len(page_items) > MAX_COLLECTION_ITEMS:
            raise CloudInventoryTransientError(
                f"AWS {context} inventory exceeded the safety bound"
            )
        for item in page_items:
            if not isinstance(item, Mapping):
                raise CloudInventoryTransientError(f"AWS returned an invalid {context} item")
            items.append(item)
    return items


def _detail(client, operation, context, **kwargs):
    if operation not in _READ_ONLY_DETAIL_OPERATIONS:
        raise ValueError("AWS operation is not allowed for credential metadata")
    response = getattr(client, operation)(**kwargs)
    if not isinstance(response, Mapping) or not response:
        raise CloudInventoryTransientError(f"AWS returned an invalid {context} response")
    return response


def _tags_for_resource(client, *, asset_type, provider_id, name):
    if asset_type == AWS_SECRETS_MANAGER_SECRET:
        response = _detail(
            client,
            "list_tags_for_resource",
            "Secrets Manager tags",
            SecretId=provider_id,
        )
        tags = require_collection(response, "Tags", "Secrets Manager tags")
    else:
        response = _detail(
            client,
            "list_tags_for_resource",
            "SSM parameter tags",
            ResourceType="Parameter",
            ResourceId=name,
        )
        tags = require_collection(response, "TagList", "SSM parameter tags")
    return _safe_tags(tags, "AWS resource tags")


def _append_warning(warnings, error):
    if len(warnings) >= MAX_WARNING_ITEMS:
        return
    warnings.append(_safe_error_code(error))


def _base_metadata(asset_type, region, provider_id, name, status):
    return {
        "_cloudmoo_region": region,
        "_cloudmoo_provider_id": provider_id,
        "_cloudmoo_raw_id": provider_id,
        "_cloudmoo_asset_id": _stable_id(asset_type, region, provider_id),
        "_cloudmoo_resource_name": name,
        "provider_id": provider_id,
        "raw_id": provider_id,
        "name": name,
        "normalized_status": status,
        "status": status,
    }


def _put_timestamp(metadata, resource, source_keys, output_keys, context):
    timestamp = _safe_timestamp(resource, source_keys, context)
    if timestamp is None:
        return
    for key in output_keys:
        metadata[key] = timestamp


def _secret_metadata(summary, detail, tags, region, provider_id, name):
    source = {}
    if isinstance(summary, Mapping):
        source.update(summary)
    if isinstance(detail, Mapping):
        source.update(detail)

    status = "pending_deletion" if _safe_timestamp(source, ("DeletedDate",), "secret deletion") else "active"
    metadata = _base_metadata(
        AWS_SECRETS_MANAGER_SECRET,
        region,
        provider_id,
        name,
        normalize_credentials_config_status(status, AWS_SECRETS_MANAGER_SECRET),
    )

    arn = _safe_text(source.get("ARN"), limit=MAX_PROVIDER_ID, context="secret ARN")
    if arn:
        metadata["arn"] = arn
    kms_key_id = _safe_text(source.get("KmsKeyId"), limit=MAX_PROVIDER_ID, context="KMS key ID")
    if kms_key_id:
        metadata["kms_key_id"] = kms_key_id

    rotation_enabled = _safe_bool(source, "RotationEnabled", "secret rotation")
    if rotation_enabled is not None:
        metadata["rotation_enabled"] = rotation_enabled
    rotation_days = None
    rotation_rules = source.get("RotationRules")
    if rotation_rules is not None:
        if not isinstance(rotation_rules, Mapping):
            raise CloudInventoryTransientError("AWS returned invalid secret rotation rules")
        rotation_days = _safe_integer(
            rotation_rules,
            "AutomaticallyAfterDays",
            "secret rotation",
            minimum=1,
            maximum=3_650,
        )
    if rotation_days is not None:
        metadata["rotation_rules"] = {"automatically_after_days": rotation_days}

    _put_timestamp(
        metadata,
        source,
        ("LastRotatedDate",),
        ("last_rotated_at", "last_rotated_date"),
        "secret rotation",
    )
    _put_timestamp(
        metadata,
        source,
        ("LastChangedDate",),
        ("last_changed_at", "last_changed_date"),
        "secret change",
    )
    _put_timestamp(
        metadata,
        source,
        ("NextRotationDate",),
        ("next_rotation_at", "next_rotation_date"),
        "secret next rotation",
    )
    _put_timestamp(
        metadata,
        source,
        ("DeletedDate",),
        ("deletion_scheduled_at", "expiration_at"),
        "secret deletion",
    )

    for source_key, output_key in (
        ("OwningService", "owning_service"),
        ("PrimaryRegion", "primary_region"),
    ):
        text = _safe_text(source.get(source_key), limit=MAX_METADATA_TEXT, context=output_key)
        if text:
            metadata[output_key] = text

    metadata["policy_posture"] = {"status": "not_collected"}
    metadata["tags"] = tags if isinstance(tags, list) else []
    return _serialized_metadata(metadata)


def _parameter_metadata(resource, tags, region, provider_id, name):
    if not isinstance(resource, Mapping):
        raise CloudInventoryTransientError("AWS returned an invalid SSM parameter object")
    status = normalize_credentials_config_status("active", AWS_SSM_PARAMETER)
    metadata = _base_metadata(AWS_SSM_PARAMETER, region, provider_id, name, status)

    arn = _safe_text(resource.get("ARN"), limit=MAX_PROVIDER_ID, context="parameter ARN")
    if arn:
        metadata["arn"] = arn
    parameter_type = _safe_text(resource.get("Type"), limit=128, context="parameter type")
    if parameter_type:
        metadata["parameter_type"] = parameter_type
        metadata["type"] = parameter_type
    kms_key_id = _safe_text(resource.get("KeyId"), limit=MAX_PROVIDER_ID, context="KMS key ID")
    if kms_key_id:
        metadata["kms_key_id"] = kms_key_id
    tier = _safe_text(resource.get("Tier"), limit=128, context="parameter tier")
    if tier:
        metadata["tier"] = tier
    data_type = _safe_text(resource.get("DataType"), limit=128, context="parameter data type")
    if data_type:
        metadata["data_type"] = data_type

    _put_timestamp(
        metadata,
        resource,
        ("LastModifiedDate",),
        ("last_changed_at", "last_changed_date", "last_modified_at"),
        "parameter change",
    )
    _put_timestamp(
        metadata,
        resource,
        ("ExpirationDate",),
        ("expiration_at", "expiration_date"),
        "parameter expiration",
    )
    metadata["policy_posture"] = _safe_policy_posture(
        resource.get("Policies"),
        "SSM parameter policies",
    )
    metadata["tags"] = tags if isinstance(tags, list) else []
    return _serialized_metadata(metadata)


def _secret_record(item, detail, tags, region):
    name = _safe_text(item.get("Name"), limit=MAX_PROVIDER_ID, required=True, context="secret name")
    provider_id = _safe_identifier(item, ("ARN", "Name"), "secret")
    return {
        "unique_id": _stable_id(AWS_SECRETS_MANAGER_SECRET, region, provider_id),
        "name": name[:100],
        "metadata": _secret_metadata(item, detail, tags, region, provider_id, name),
    }


def _parameter_record(item, tags, region):
    name = _safe_text(item.get("Name"), limit=MAX_PROVIDER_ID, required=True, context="parameter name")
    # Parameter names are the service identifier used by the safe describe and
    # tag calls.  An ARN, when present, is metadata rather than the lookup ID.
    provider_id = name
    return {
        "unique_id": _stable_id(AWS_SSM_PARAMETER, region, provider_id),
        "name": name[:100],
        "metadata": _parameter_metadata(item, tags, region, provider_id, name),
    }


def _collect_secrets(account, region):
    client = aws_client(account, "secretsmanager", region=region)
    summaries = _collection_items(
        client,
        "list_secrets",
        "SecretList",
        "Secrets Manager secrets",
    )
    records = []
    warnings = []
    for item in summaries:
        name = _safe_text(item.get("Name"), limit=MAX_PROVIDER_ID, required=True, context="secret name")
        provider_id = _safe_identifier(item, ("ARN", "Name"), "secret")
        detail = {}
        tags = []
        try:
            detail = _detail(
                client,
                "describe_secret",
                "Secrets Manager secret",
                SecretId=provider_id,
            )
        except CloudInventoryTransientError:
            raise
        except Exception as error:
            _append_warning(warnings, error)
        try:
            tags = _tags_for_resource(
                client,
                asset_type=AWS_SECRETS_MANAGER_SECRET,
                provider_id=provider_id,
                name=name,
            )
        except CloudInventoryTransientError:
            raise
        except Exception as error:
            _append_warning(warnings, error)
        records.append(_secret_record(item, detail, tags, region))
    return records, warnings


def _collect_parameters(account, region):
    client = aws_client(account, "ssm", region=region)
    parameters = _collection_items(
        client,
        "describe_parameters",
        "Parameters",
        "SSM parameters",
    )
    records = []
    warnings = []
    for item in parameters:
        name = _safe_text(item.get("Name"), limit=MAX_PROVIDER_ID, required=True, context="parameter name")
        tags = []
        try:
            tags = _tags_for_resource(
                client,
                asset_type=AWS_SSM_PARAMETER,
                provider_id=name,
                name=name,
            )
        except CloudInventoryTransientError:
            raise
        except Exception as error:
            _append_warning(warnings, error)
        records.append(_parameter_record(item, tags, region))
    return records, warnings


AWS_CREDENTIALS_CONFIG_COLLECTION_SPECS = (
    (AWS_SECRETS_MANAGER_SECRET, CoreAWSSecretsManagerSecret, _collect_secrets),
    (AWS_SSM_PARAMETER, CoreAWSSSMParameter, _collect_parameters),
)
AWS_CREDENTIAL_CONFIG_COLLECTION_SPECS = AWS_CREDENTIALS_CONFIG_COLLECTION_SPECS


def _validate_records(records, asset_type):
    if not isinstance(records, list):
        raise CloudInventoryTransientError(f"AWS returned an invalid {asset_type} collection")
    seen_ids = set()
    for record in records:
        if not isinstance(record, Mapping) or not record.get("unique_id"):
            raise CloudInventoryTransientError(f"AWS returned an invalid {asset_type} record")
        unique_id = record["unique_id"]
        if not isinstance(unique_id, str) or len(unique_id) > 255:
            raise CloudInventoryTransientError(f"AWS returned an invalid {asset_type} identifier")
        if unique_id in seen_ids:
            raise CloudInventoryTransientError(f"AWS returned a duplicate {asset_type} identifier")
        if not isinstance(record.get("name"), str) or not isinstance(record.get("metadata"), Mapping):
            raise CloudInventoryTransientError(f"AWS returned an invalid {asset_type} record")
        seen_ids.add(unique_id)
    return seen_ids


def _upsert_asset(model, account, region, record, asset_type):
    asset, created = model.objects.get_or_create(
        owner=account,
        region=region,
        unique_id=record["unique_id"],
        defaults={
            "name": record["name"][:100],
            "type": asset_type,
            "metadata": record["metadata"],
            "monitoring": UtilAsset.Monitoring.ACTIVE,
        },
    )
    asset.region = region
    asset.name = record["name"][:100]
    asset.type = asset_type
    asset.metadata = record["metadata"]
    if not created and getattr(asset, "monitoring", None) == UtilAsset.Monitoring.NO_LONGER_EXISTS:
        asset.monitoring = UtilAsset.Monitoring.ACTIVE
    asset.save()
    return asset


def _reconcile(model, account, region, records, asset_type):
    current_ids = _validate_records(records, asset_type)
    for record in records:
        _upsert_asset(model, account, region, record, asset_type)
    model.objects.filter(owner=account, region=region).exclude(
        unique_id__in=current_ids
    ).update(monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS)
    return len(current_ids)


def _persist_without_reconcile(model, account, region, records, asset_type):
    _validate_records(records, asset_type)
    for record in records:
        _upsert_asset(model, account, region, record, asset_type)
    return len(records)


def _summary_error(asset_type, region, error):
    return {
        "assetType": asset_type,
        "region": region,
        "errorCode": _safe_error_code(error),
        "kind": _error_kind(error),
    }


def normalize_credentials_config_status(value, family=None):
    """Normalize provider/configuration states into the monitoring vocabulary."""
    if value is None:
        return "unknown"
    normalized = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    mapping = {
        "active": "active",
        "available": "active",
        "enabled": "active",
        "ready": "active",
        "disabled": "disabled",
        "pending": "pending",
        "pending_deletion": "pending_deletion",
        "scheduled_deletion": "pending_deletion",
        "deleted": "expired",
        "expired": "expired",
        "degraded": "degraded",
        "failed": "error",
        "error": "error",
        "not_found": "not_found",
        "notfound": "not_found",
        "unknown": "unknown",
    }
    return mapping.get(normalized, "unknown")


def sync_aws_credentials_config_assets(account, regions=None):
    """Synchronize regional credential/configuration metadata fail-closed."""
    summary = {
        "regions": [],
        "counts": {asset_type: 0 for asset_type in AWS_CREDENTIALS_CONFIG_ASSET_TYPES},
        "synced": {asset_type: 0 for asset_type in AWS_CREDENTIALS_CONFIG_ASSET_TYPES},
        "families": {asset_type: {} for asset_type in AWS_CREDENTIALS_CONFIG_ASSET_TYPES},
        "errors": [],
    }

    try:
        discovered_regions = get_enabled_regions(account) if regions is None else regions
        normalized_regions = _normalize_regions(discovered_regions)
    except Exception as error:
        summary["errors"].append(_summary_error("region_discovery", None, error))
        return summary
    summary["regions"] = normalized_regions

    for asset_type, model, collector in AWS_CREDENTIALS_CONFIG_COLLECTION_SPECS:
        for region in normalized_regions:
            try:
                records, warnings = collector(account, region)
                if warnings:
                    count = _persist_without_reconcile(
                        model,
                        account,
                        region,
                        records,
                        asset_type,
                    )
                    result = {
                        "status": "partial",
                        "complete": False,
                        "reconciled": False,
                        "count": count,
                    }
                    for warning in warnings:
                        summary["errors"].append(
                            {
                                "assetType": asset_type,
                                "region": region,
                                "errorCode": str(warning)[:128],
                                "kind": "incomplete_inventory",
                            }
                        )
                else:
                    count = _reconcile(model, account, region, records, asset_type)
                    result = {
                        "status": "ok",
                        "complete": True,
                        "reconciled": True,
                        "count": count,
                    }
                summary["counts"][asset_type] += count
                summary["synced"][asset_type] += count
            except Exception as error:
                result = {
                    "status": "error",
                    "complete": False,
                    "reconciled": False,
                    "count": None,
                    "error": _summary_error(asset_type, region, error),
                }
                summary["errors"].append(result["error"])
                logger.warning(
                    "AWS credential metadata inventory failed for %s/%s (%s)",
                    asset_type,
                    region,
                    result["error"]["errorCode"],
                )
            summary["families"][asset_type][region] = result
        summary[asset_type] = summary["families"][asset_type]

    return summary


__all__ = [
    "AWS_SECRETS_MANAGER_SECRET",
    "AWS_SSM_PARAMETER",
    "AWS_SECRETS_MANAGER",
    "AWS_SSM_PARAMETER_STORE",
    "AWS_SSM_PARAMETER_STORE_PARAMETER",
    "AWS_CREDENTIALS_CONFIG_ASSET_TYPES",
    "CREDENTIALS_CONFIG_ASSET_TYPES",
    "AWS_CREDENTIAL_CONFIG_ASSET_TYPES",
    "AWS_CREDENTIALS_ASSET_TYPES",
    "AWS_CREDENTIALS_CONFIG_ENDPOINTS",
    "NORMALIZED_CREDENTIALS_CONFIG_STATUSES",
    "NORMALIZED_CREDENTIAL_CONFIG_STATUSES",
    "CoreAWSCredentialsConfigAsset",
    "CoreAWSSecretsManagerSecret",
    "CoreAWSSSMParameter",
    "CoreAWSSecretsManager",
    "CoreAWSSSMParameterStore",
    "CoreAWSSSMParameterStoreParameter",
    "AWS_CREDENTIALS_CONFIG_ASSET_MODELS",
    "AWS_CREDENTIAL_CONFIG_ASSET_MODELS",
    "CREDENTIALS_CONFIG_ASSET_MODELS",
    "AWS_CREDENTIALS_CONFIG_ASSET_MODEL_BY_TYPE",
    "AWS_CREDENTIALS_CONFIG_COLLECTION_SPECS",
    "AWS_CREDENTIAL_CONFIG_COLLECTION_SPECS",
    "normalize_credentials_config_status",
    "sync_aws_credentials_config_assets",
]
