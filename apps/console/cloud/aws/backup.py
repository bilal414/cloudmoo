"""Read-only AWS Backup and snapshot inventory.

The AWS Backup control plane is regional.  This adapter deliberately keeps
the provider-facing work here instead of extending the legacy AWS account
model, so the integration lane can register the relations and migrations at
one boundary.

RDS DB and DB-cluster snapshots intentionally reuse ``CoreAWSSnapshot``.  A
snapshot row is identified by ``<region>|<provider-id>`` and carries the
following private metadata keys: ``_cloudmoo_region``,
``_cloudmoo_provider_id``, and ``_cloudmoo_snapshot_kind`` (``ebs``,
``rds_instance``, or ``rds_cluster``).  The integration lane should keep the
existing ``snapshot`` asset type/relation and route status checks using those
metadata keys; no second RDS snapshot model or global asset type is required.

Only AWS list/describe/get APIs are used.  Provider payloads are serialized
through the shared discovery helper and bounded again before they are stored.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime

from django.db import models
from django.utils.dateparse import parse_datetime

from apps.console.cloud.models import CloudInventoryTransientError
from apps.console.cloud.aws.models import CoreAWSAccount, CoreAWSSnapshot
from apps.console.utils.models import UtilAsset


logger = logging.getLogger(__name__)


from apps.console.cloud.aws.discovery import (
    aws_client,
    aws_error_code,
    get_enabled_regions,
    iter_pages,
    require_collection,
    serialize_aws,
)


MAX_COLLECTION_ITEMS = 10000
MAX_METADATA_ITEMS = 80
MAX_METADATA_LIST_ITEMS = 100
MAX_METADATA_STRING = 2048
MAX_PROVIDER_ID = 512
MAX_REGION_LENGTH = 32

_REGION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,31}$")
_SECRET_KEY_PARTS = (
    "accesskey",
    "secretkey",
    "password",
    "passphrase",
    "token",
    "privatekey",
    "credential",
    "authorization",
)

AWS_BACKUP_VAULT = "aws_backup_vault"
AWS_BACKUP_PLAN = "aws_backup_plan"
AWS_BACKUP_RECOVERY_POINT = "aws_backup_recovery_point"
AWS_BACKUP_JOB = "aws_backup_job"
AWS_BACKUP_COPY_JOB = "aws_backup_copy_job"

BACKUP_VAULT = "backup_vault"
BACKUP_PLAN = "backup_plan"
BACKUP_RECOVERY_POINT = "backup_recovery_point"
BACKUP_JOB = "backup_job"
BACKUP_COPY_JOB = "backup_copy_job"

BACKUP_ASSET_TYPES = (
    AWS_BACKUP_VAULT,
    AWS_BACKUP_PLAN,
    AWS_BACKUP_RECOVERY_POINT,
    AWS_BACKUP_JOB,
    AWS_BACKUP_COPY_JOB,
)
AWS_BACKUP_ASSET_TYPES = BACKUP_ASSET_TYPES


def _backup_constraint(name):
    return models.UniqueConstraint(
        fields=("owner", "unique_id"),
        name=f"aws_backup_{name}_uid_uniq",
    )


class CoreAWSBackupAsset(UtilAsset):
    """Common persisted fields for AWS Backup resources.

    ``UtilAsset.status`` is the monitoring heartbeat property, so provider
    state is stored separately as ``provider_status``.  ``lifecycle`` and
    ``retention`` remain JSON because AWS exposes both scalar and structured
    lifecycle forms across vaults, rules, jobs, and recovery points.
    """

    owner = models.ForeignKey(
        CoreAWSAccount,
        on_delete=models.CASCADE,
        related_name="%(class)s_assets",
    )
    region = models.CharField(max_length=MAX_REGION_LENGTH)
    provider_id = models.CharField(max_length=MAX_PROVIDER_ID)
    provider_status = models.CharField(max_length=64, blank=True, default="")
    lifecycle = models.JSONField(default=dict)
    retention = models.JSONField(default=dict)
    resource_type = models.CharField(max_length=128, blank=True, default="")
    provider_created_at = models.DateTimeField(null=True, blank=True)
    provider_completed_at = models.DateTimeField(null=True, blank=True)
    provider_expires_at = models.DateTimeField(null=True, blank=True)

    # The shared UtilAsset type field is intentionally left untouched.  The
    # integration lane owns the global choices; these class attributes carry
    # the provider-qualified values needed by this adapter in the meantime.
    asset_type = None

    class Meta:
        abstract = True

    @property
    def provider_url(self):
        return (
            f"https://{self.region}.console.aws.amazon.com/backup/home"
            f"?region={self.region}#/resources"
        )

    @property
    def monitoring_credentials(self):
        metadata = self.metadata if isinstance(self.metadata, dict) else {}
        return {
            "access_key": self.owner.access_key,
            "secret_key": self.owner.secret_key,
            "region": self.region,
            "resource_region": self.region,
            "provider_id": self.provider_id,
            "resource_name": self.provider_id,
            "asset_type": self.type or self.asset_type,
            "provider_type": self.asset_type,
            "backup_vault_name": metadata.get("_cloudmoo_backup_vault_name"),
            "snapshot_kind": metadata.get("_cloudmoo_snapshot_kind"),
            "metadata": metadata,
        }

    def check_status(self):
        from apps.monitoring.checks.aws_backup import AWS_BACKUP_STATUS_CHECKS

        checker = AWS_BACKUP_STATUS_CHECKS.get(self.type or self.asset_type)
        if checker is None:
            return "error", {"error_code": "unsupported_asset_type"}
        return checker(self.unique_id, self.monitoring_credentials)

    def save(self, *args, **kwargs):
        if not self.type and self.asset_type:
            self.type = self.asset_type
        return super().save(*args, **kwargs)


class CoreAWSBackupVault(CoreAWSBackupAsset):
    asset_type = AWS_BACKUP_VAULT

    class Meta:
        db_table = "core_aws_backup_vault"
        constraints = [_backup_constraint("vault")]


class CoreAWSBackupPlan(CoreAWSBackupAsset):
    asset_type = AWS_BACKUP_PLAN

    class Meta:
        db_table = "core_aws_backup_plan"
        constraints = [_backup_constraint("plan")]


class CoreAWSBackupRecoveryPoint(CoreAWSBackupAsset):
    asset_type = AWS_BACKUP_RECOVERY_POINT

    class Meta:
        db_table = "core_aws_backup_recovery_point"
        constraints = [_backup_constraint("recovery_point")]


class CoreAWSBackupJob(CoreAWSBackupAsset):
    asset_type = AWS_BACKUP_JOB

    class Meta:
        db_table = "core_aws_backup_job"
        constraints = [_backup_constraint("job")]


class CoreAWSBackupCopyJob(CoreAWSBackupAsset):
    asset_type = AWS_BACKUP_COPY_JOB

    class Meta:
        db_table = "core_aws_backup_copy_job"
        constraints = [_backup_constraint("copy_job")]


AWS_BACKUP_ASSET_MODELS = {
    AWS_BACKUP_VAULT: CoreAWSBackupVault,
    AWS_BACKUP_PLAN: CoreAWSBackupPlan,
    AWS_BACKUP_RECOVERY_POINT: CoreAWSBackupRecoveryPoint,
    AWS_BACKUP_JOB: CoreAWSBackupJob,
    AWS_BACKUP_COPY_JOB: CoreAWSBackupCopyJob,
}


def _bounded_value(value, depth=0):
    """Bound a serialized provider value without retaining secret-like keys."""
    if depth > 5:
        return "<truncated>"
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        result = {}
        for index, (key, child) in enumerate(value.items()):
            if index >= MAX_METADATA_ITEMS:
                result["_cloudmoo_truncated"] = True
                break
            key_text = str(key)[:128]
            normalized_key = re.sub(r"[^a-z0-9]", "", key_text.lower())
            if any(part in normalized_key for part in _SECRET_KEY_PARTS):
                continue
            result[key_text] = _bounded_value(child, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        result = [_bounded_value(item, depth + 1) for item in value[:MAX_METADATA_LIST_ITEMS]]
        if len(value) > MAX_METADATA_LIST_ITEMS:
            result.append("<truncated>")
        return result
    if isinstance(value, str):
        return value[:MAX_METADATA_STRING]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:MAX_METADATA_STRING]


def _serialized_metadata(payload, **context):
    if not isinstance(payload, dict):
        raise CloudInventoryTransientError("AWS returned an invalid resource object")
    serialized = serialize_aws(payload)
    if not isinstance(serialized, dict):
        raise CloudInventoryTransientError("AWS returned an invalid serialized resource object")
    value = dict(serialized)
    value.update(context)
    return _bounded_value(value)


def _status_value(payload):
    for key in ("Status", "State", "VaultState", "JobState", "RecoveryPointStatus"):
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip().lower().replace(" ", "_")[:64]
    return ""


def _provider_id(payload, keys, context):
    if not isinstance(payload, dict):
        raise CloudInventoryTransientError(f"AWS returned an invalid {context} object")
    for key in keys:
        value = payload.get(key)
        if value is not None and str(value).strip():
            value = str(value).strip()
            if len(value) > MAX_PROVIDER_ID:
                raise CloudInventoryTransientError(f"AWS returned an oversized {context} identifier")
            return value
    raise CloudInventoryTransientError(f"AWS returned a {context} without an identifier")


def _display_name(payload, provider_id, *keys):
    for key in keys:
        value = payload.get(key) if isinstance(payload, dict) else None
        if value is not None and str(value).strip():
            return str(value).strip()[:100]
    return str(provider_id)[:100]


def _qualified_id(region, provider_id):
    raw = f"{region}|{provider_id}"
    if len(raw) <= 100:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:56]
    return f"{region}|sha256:{digest}"[:100]


def _provider_datetime(payload, *keys):
    value = None
    for key in keys:
        if payload.get(key) is not None:
            value = payload[key]
            break
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return parse_datetime(value)
        except (TypeError, ValueError):
            return None
    return None


def _region_list(account):
    try:
        regions = get_enabled_regions(account)
    except Exception as error:
        raise CloudInventoryTransientError("Unable to discover enabled AWS Regions") from error
    if not isinstance(regions, (list, tuple)):
        raise CloudInventoryTransientError("AWS returned an invalid enabled Region collection")

    result = []
    for value in regions:
        region = str(value).strip() if value is not None else ""
        if not region or len(region) > MAX_REGION_LENGTH or not _REGION_PATTERN.fullmatch(region):
            raise CloudInventoryTransientError("AWS returned an invalid enabled Region")
        if region not in result:
            result.append(region)
    if len(result) > 100:
        raise CloudInventoryTransientError("AWS returned too many enabled Regions")
    return result


def _client(account, service, region):
    # Keep the shared helper as the sole client-construction boundary.
    return aws_client(account, service, region=region)


def _collection(client, operation, collection_key, **kwargs):
    """Consume every page from the shared paginator and validate each page."""
    stream = iter_pages(client, operation, **kwargs)
    if stream is None:
        raise CloudInventoryTransientError(f"AWS returned no {operation} pagination stream")

    if isinstance(stream, dict):
        values = require_collection(stream, collection_key, operation)
        stream = values

    result = []
    for page_or_item in stream:
        if isinstance(page_or_item, dict) and collection_key in page_or_item:
            values = require_collection(page_or_item, collection_key, operation)
            if not isinstance(values, list):
                raise CloudInventoryTransientError(f"AWS returned an invalid {operation} collection")
            result.extend(values)
        elif isinstance(page_or_item, (list, tuple)):
            result.extend(page_or_item)
        else:
            result.append(page_or_item)
        if len(result) > MAX_COLLECTION_ITEMS:
            raise CloudInventoryTransientError(f"AWS returned too many items for {operation}")
    return result


def _detail(client, operation, **kwargs):
    response = getattr(client, operation)(**kwargs)
    if not isinstance(response, dict):
        raise CloudInventoryTransientError(f"AWS returned an invalid {operation} response")
    return response


def _record_error(result, region, resource_type, error):
    try:
        provider_error = getattr(error, "__cause__", None) or error
        code = aws_error_code(provider_error)
    except Exception:
        code = type(error).__name__
    code = str(code or type(error).__name__)[:128]
    result["errors"].append({"region": region, "resource": resource_type, "code": code})
    logger.warning("AWS %s inventory incomplete in %s (%s)", resource_type, region, code)


def _upsert_asset(
    model,
    account,
    region,
    provider_id,
    asset_type,
    payload,
    *,
    name=None,
    metadata=None,
    backup_vault_name=None,
):
    serialized = dict(metadata) if metadata else _serialized_metadata(payload)
    serialized.update(
        {
            "_cloudmoo_region": region,
            "_cloudmoo_provider_id": provider_id,
            "_cloudmoo_raw_id": provider_id,
            "_cloudmoo_provider_type": asset_type,
            "_cloudmoo_resource_type": asset_type,
        }
    )
    if backup_vault_name:
        serialized["_cloudmoo_backup_vault_name"] = str(backup_vault_name)[:MAX_METADATA_STRING]

    lifecycle = payload.get("Lifecycle") if isinstance(payload.get("Lifecycle"), dict) else {}
    if not lifecycle and isinstance(payload.get("CalculatedLifecycle"), dict):
        lifecycle = payload["CalculatedLifecycle"]
    if not lifecycle and isinstance(payload.get("RecoveryPointLifecycle"), dict):
        lifecycle = payload["RecoveryPointLifecycle"]
    retention = {}
    for key in (
        "MinRetentionDays",
        "MaxRetentionDays",
        "Lifecycle",
        "CalculatedLifecycle",
        "RecoveryPointLifecycle",
    ):
        if key in payload:
            retention[key] = payload[key]
    rules = serialized.get("_cloudmoo_rules")
    if isinstance(rules, list):
        rule_lifecycles = [
            rule.get("Lifecycle")
            for rule in rules
            if isinstance(rule, dict) and isinstance(rule.get("Lifecycle"), dict)
        ]
        if rule_lifecycles:
            retention["Rules"] = rule_lifecycles

    defaults = {
        "name": (name or provider_id)[:100],
        "type": asset_type,
        "metadata": serialized,
        "region": region,
        "provider_id": provider_id,
        "provider_status": _status_value(payload),
        "lifecycle": _bounded_value(lifecycle),
        "retention": _bounded_value(retention),
        "resource_type": str(payload.get("ResourceType") or payload.get("resourceType") or asset_type)[:128],
        "provider_created_at": _provider_datetime(payload, "CreationDate", "CreatedAt", "CreationTime"),
        "provider_completed_at": _provider_datetime(payload, "CompletionDate", "CompletedAt", "CompletionTime"),
        "provider_expires_at": _provider_datetime(
            payload,
            "ExpiryDate",
            "ExpirationDate",
            "ExpirationTime",
            "DeleteAt",
        ),
    }
    asset, created = model.objects.get_or_create(
        owner=account,
        unique_id=_qualified_id(region, provider_id),
        defaults=defaults,
    )
    for field, value in defaults.items():
        setattr(asset, field, value)
    if not created and asset.monitoring == UtilAsset.Monitoring.NO_LONGER_EXISTS:
        asset.monitoring = UtilAsset.Monitoring.ACTIVE
    asset.save()
    return asset


def _reconcile_region(model, account, region, current_ids):
    """Mark only rows in a successfully enumerated Region as missing."""
    for asset in model.objects.filter(owner=account, region=region):
        if asset.unique_id in current_ids:
            continue
        if asset.monitoring != UtilAsset.Monitoring.NO_LONGER_EXISTS:
            asset.monitoring = UtilAsset.Monitoring.NO_LONGER_EXISTS
            asset.save(update_fields=["monitoring"])


def _reconcile_recovery_scope(model, account, region, vault_name, current_ids):
    for asset in model.objects.filter(owner=account, region=region):
        metadata = asset.metadata if isinstance(asset.metadata, dict) else {}
        known_vault = metadata.get("_cloudmoo_backup_vault_name") or metadata.get("BackupVaultName")
        if known_vault != vault_name or asset.unique_id in current_ids:
            continue
        if asset.monitoring != UtilAsset.Monitoring.NO_LONGER_EXISTS:
            asset.monitoring = UtilAsset.Monitoring.NO_LONGER_EXISTS
            asset.save(update_fields=["monitoring"])


def _vault_name(payload):
    value = payload.get("BackupVaultName") or payload.get("VaultName")
    if value is None and payload.get("BackupVaultArn"):
        value = str(payload["BackupVaultArn"]).rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    if value is None or not str(value).strip():
        raise CloudInventoryTransientError("AWS returned a backup vault without a name")
    return str(value).strip()[:MAX_PROVIDER_ID]


def _vault_payload(client, summary, vault_name):
    try:
        response = _detail(client, "describe_backup_vault", BackupVaultName=vault_name)
        return response, None
    except Exception as error:
        return summary, error


def _plan_enrichment(client, summary, plan_id):
    """Read plan rules and selections without fetching restore metadata."""
    metadata = dict(summary)
    child_errors = []

    try:
        detail_response = _detail(client, "get_backup_plan", BackupPlanId=plan_id)
        plan = detail_response.get("BackupPlan")
        if not isinstance(plan, dict):
            raise CloudInventoryTransientError("AWS returned an invalid backup plan detail")
        metadata["BackupPlan"] = plan
        rules = plan.get("Rules", [])
        if not isinstance(rules, list):
            raise CloudInventoryTransientError("AWS returned an invalid backup plan rule collection")
        metadata["_cloudmoo_rules"] = rules
    except Exception as error:
        child_errors.append(error)

    try:
        selection_items = _collection(
            client,
            "list_backup_selections",
            "BackupSelectionsList",
            BackupPlanId=plan_id,
        )
        selections = []
        for selection_summary in selection_items:
            selection_id = _provider_id(selection_summary, ("SelectionId", "Id"), "backup selection")
            selection = dict(selection_summary)
            try:
                detail_response = _detail(
                    client,
                    "get_backup_selection",
                    BackupPlanId=plan_id,
                    SelectionId=selection_id,
                )
                detail = detail_response.get("BackupSelection")
                if not isinstance(detail, dict):
                    raise CloudInventoryTransientError("AWS returned an invalid backup selection detail")
                selection["BackupSelection"] = detail
            except Exception as error:
                selection["_cloudmoo_detail_error"] = str(_safe_error_code(error))[:128]
            selections.append(selection)
        metadata["_cloudmoo_selections"] = selections
    except Exception as error:
        child_errors.append(error)

    safe = _serialized_metadata(metadata)
    if child_errors:
        safe["_cloudmoo_child_errors"] = [str(_safe_error_code(error))[:128] for error in child_errors]
    return safe


def _safe_error_code(error):
    try:
        provider_error = getattr(error, "__cause__", None) or error
        return aws_error_code(provider_error) or type(provider_error).__name__
    except Exception:
        return type(error).__name__


def _sync_vaults(client, account, region, result):
    items = _collection(client, "list_backup_vaults", "BackupVaultList")
    validated = [(_vault_name(item), item) for item in items]
    current_ids = set()
    for vault_name, summary in validated:
        payload, detail_error = _vault_payload(client, summary, vault_name)
        metadata = _serialized_metadata(
            payload,
            _cloudmoo_region=region,
            _cloudmoo_provider_id=vault_name,
            _cloudmoo_resource_type=AWS_BACKUP_VAULT,
        )
        if detail_error:
            metadata["_cloudmoo_detail_error"] = str(_safe_error_code(detail_error))[:128]
        asset = _upsert_asset(
            CoreAWSBackupVault,
            account,
            region,
            vault_name,
            AWS_BACKUP_VAULT,
            payload,
            name=_display_name(payload, vault_name, "BackupVaultName", "VaultName"),
            metadata=metadata,
        )
        current_ids.add(asset.unique_id)
        result["synced"][AWS_BACKUP_VAULT] += 1

        # A vault is authoritative as soon as list_backup_vaults returns it.
        # Recovery points are a child collection: a failed child call must not
        # make this already-seen vault disappear from local inventory.
        try:
            recovery_items = _collection(
                client,
                "list_recovery_points_by_backup_vault",
                "RecoveryPoints",
                BackupVaultName=vault_name,
            )
            recovery_validated = [
                (
                    _provider_id(item, ("RecoveryPointArn", "RecoveryPointId"), "recovery point"),
                    item,
                )
                for item in recovery_items
            ]
            recovery_ids = set()
            for recovery_id, recovery in recovery_validated:
                recovery_asset = _upsert_asset(
                    CoreAWSBackupRecoveryPoint,
                    account,
                    region,
                    recovery_id,
                    AWS_BACKUP_RECOVERY_POINT,
                    recovery,
                    name=_display_name(recovery, recovery_id, "RecoveryPointArn", "RecoveryPointId"),
                    backup_vault_name=vault_name,
                )
                recovery_ids.add(recovery_asset.unique_id)
                result["synced"][AWS_BACKUP_RECOVERY_POINT] += 1
            _reconcile_recovery_scope(
                CoreAWSBackupRecoveryPoint,
                account,
                region,
                vault_name,
                recovery_ids,
            )
        except Exception as error:
            _record_error(result, region, f"{AWS_BACKUP_RECOVERY_POINT}:{vault_name}", error)

    _reconcile_region(CoreAWSBackupVault, account, region, current_ids)


def _sync_plans(client, account, region, result):
    items = _collection(client, "list_backup_plans", "BackupPlansList")
    validated = [
        (_provider_id(item, ("BackupPlanId", "PlanId", "Id", "BackupPlanArn"), "backup plan"), item)
        for item in items
    ]
    current_ids = set()
    for plan_id, summary in validated:
        metadata = _plan_enrichment(client, summary, plan_id)
        asset = _upsert_asset(
            CoreAWSBackupPlan,
            account,
            region,
            plan_id,
            AWS_BACKUP_PLAN,
            summary,
            name=_display_name(summary, plan_id, "BackupPlanName", "PlanName"),
            metadata=metadata,
        )
        current_ids.add(asset.unique_id)
        result["synced"][AWS_BACKUP_PLAN] += 1
    _reconcile_region(CoreAWSBackupPlan, account, region, current_ids)


def _sync_jobs(client, account, region, result, operation, collection_key, model, asset_type, id_keys):
    items = _collection(client, operation, collection_key)
    validated = [(_provider_id(item, id_keys, asset_type), item) for item in items]
    current_ids = set()
    for provider_id, payload in validated:
        vault_name = payload.get("BackupVaultName") or payload.get("DestinationBackupVaultArn")
        asset = _upsert_asset(
            model,
            account,
            region,
            provider_id,
            asset_type,
            payload,
            name=_display_name(payload, provider_id, "BackupJobId", "CopyJobId"),
            backup_vault_name=vault_name,
        )
        current_ids.add(asset.unique_id)
        result["synced"][asset_type] += 1
    _reconcile_region(model, account, region, current_ids)


def sync_aws_backup_assets(account):
    """Synchronize AWS Backup vaults, plans, jobs, copy jobs and recovery points.

    Each collection is reconciled only after its complete paginated read has
    succeeded.  A failed Region or failed child collection therefore leaves
    the prior local rows intact instead of turning an API outage into mass
    deletion status.
    """
    regions = _region_list(account)
    result = {
        "regions": regions,
        "synced": {asset_type: 0 for asset_type in BACKUP_ASSET_TYPES},
        "errors": [],
    }

    for region in regions:
        try:
            client = _client(account, "backup", region)
        except Exception as error:
            _record_error(result, region, "backup_client", error)
            continue

        operations = (
            ("vaults", _sync_vaults, (client, account, region, result)),
            ("plans", _sync_plans, (client, account, region, result)),
            (
                "jobs",
                _sync_jobs,
                (
                    client,
                    account,
                    region,
                    result,
                    "list_backup_jobs",
                    "BackupJobs",
                    CoreAWSBackupJob,
                    AWS_BACKUP_JOB,
                    ("BackupJobId", "JobId"),
                ),
            ),
            (
                "copy_jobs",
                _sync_jobs,
                (
                    client,
                    account,
                    region,
                    result,
                    "list_copy_jobs",
                    "CopyJobs",
                    CoreAWSBackupCopyJob,
                    AWS_BACKUP_COPY_JOB,
                    ("CopyJobId", "JobId"),
                ),
            ),
        )
        for resource_name, synchronizer, args in operations:
            try:
                synchronizer(*args)
            except Exception as error:
                # No synchronizer mutates provider state.  The local rows for
                # this resource/Region are intentionally left untouched.
                _record_error(result, region, resource_name, error)

    return result


def _snapshot_metadata(payload, region, provider_id, snapshot_kind, resource_type):
    return _serialized_metadata(
        payload,
        _cloudmoo_region=region,
        _cloudmoo_provider_id=provider_id,
        _cloudmoo_raw_id=provider_id,
        _cloudmoo_provider_type=resource_type,
        _cloudmoo_snapshot_kind=snapshot_kind,
        _cloudmoo_resource_type=resource_type,
    )


def _upsert_snapshot(account, region, provider_id, payload, snapshot_kind, resource_type, name):
    metadata = _snapshot_metadata(payload, region, provider_id, snapshot_kind, resource_type)
    asset, created = CoreAWSSnapshot.objects.get_or_create(
        owner=account,
        unique_id=_qualified_id(region, provider_id),
        defaults={
            "name": name[:100],
            "monitoring": UtilAsset.Monitoring.ACTIVE,
            "type": CoreAWSSnapshot.Type.SNAPSHOT,
            "metadata": metadata,
        },
    )
    asset.name = name[:100]
    asset.type = CoreAWSSnapshot.Type.SNAPSHOT
    asset.metadata = metadata
    if not created and asset.monitoring == UtilAsset.Monitoring.NO_LONGER_EXISTS:
        asset.monitoring = UtilAsset.Monitoring.ACTIVE
    asset.save()
    return asset


def _reconcile_snapshot_scope(account, region, snapshot_kind, current_ids):
    for asset in CoreAWSSnapshot.objects.filter(owner=account):
        metadata = asset.metadata if isinstance(asset.metadata, dict) else {}
        if (
            metadata.get("_cloudmoo_region") != region
            or metadata.get("_cloudmoo_snapshot_kind") != snapshot_kind
            or asset.unique_id in current_ids
        ):
            continue
        if asset.monitoring != UtilAsset.Monitoring.NO_LONGER_EXISTS:
            asset.monitoring = UtilAsset.Monitoring.NO_LONGER_EXISTS
            asset.save(update_fields=["monitoring"])


def _sync_snapshot_collection(
    client,
    account,
    region,
    result,
    *,
    operation,
    collection_key,
    snapshot_kind,
    resource_type,
    id_keys,
    name_keys,
    request_kwargs=None,
):
    items = _collection(client, operation, collection_key, **(request_kwargs or {}))
    validated = [(_provider_id(item, id_keys, resource_type), item) for item in items]
    current_ids = set()
    for provider_id, payload in validated:
        asset = _upsert_snapshot(
            account,
            region,
            provider_id,
            payload,
            snapshot_kind,
            resource_type,
            _display_name(payload, provider_id, *name_keys),
        )
        current_ids.add(asset.unique_id)
        result["synced"] += 1
    _reconcile_snapshot_scope(account, region, snapshot_kind, current_ids)


def sync_aws_snapshots(account):
    """Reconcile EBS, RDS instance, and RDS cluster snapshots read-only.

    EBS and RDS records share ``CoreAWSSnapshot`` and the existing ``snapshot``
    asset type.  Region and source family are private metadata because this
    lane is not allowed to alter the shared model or add a migration.
    """
    regions = _region_list(account)
    result = {"regions": regions, "synced": 0, "errors": []}
    for region in regions:
        try:
            ec2 = _client(account, "ec2", region)
        except Exception as error:
            _record_error(result, region, "ec2_client", error)
            ec2 = None
        if ec2 is not None:
            try:
                _sync_snapshot_collection(
                    ec2,
                    account,
                    region,
                    result,
                    operation="describe_snapshots",
                    collection_key="Snapshots",
                    snapshot_kind="ebs",
                    resource_type="EBS",
                    id_keys=("SnapshotId",),
                    name_keys=("Description", "SnapshotId"),
                    request_kwargs={"OwnerIds": ["self"]},
                )
            except Exception as error:
                _record_error(result, region, "ebs_snapshots", error)

        try:
            rds = _client(account, "rds", region)
        except Exception as error:
            _record_error(result, region, "rds_client", error)
            rds = None
        if rds is None:
            continue
        for kwargs in (
            {
                "operation": "describe_db_snapshots",
                "collection_key": "DBSnapshots",
                "snapshot_kind": "rds_instance",
                "resource_type": "RDS_DB_SNAPSHOT",
                "id_keys": ("DBSnapshotIdentifier",),
                "name_keys": ("DBSnapshotIdentifier",),
                "error_name": "rds_instance_snapshots",
            },
            {
                "operation": "describe_db_cluster_snapshots",
                "collection_key": "DBClusterSnapshots",
                "snapshot_kind": "rds_cluster",
                "resource_type": "RDS_CLUSTER_SNAPSHOT",
                "id_keys": ("DBClusterSnapshotIdentifier",),
                "name_keys": ("DBClusterSnapshotIdentifier",),
                "error_name": "rds_cluster_snapshots",
            },
        ):
            error_name = kwargs.pop("error_name")
            try:
                _sync_snapshot_collection(rds, account, region, result, **kwargs)
            except Exception as error:
                _record_error(result, region, error_name, error)
    return result


__all__ = [
    "AWS_BACKUP_COPY_JOB",
    "AWS_BACKUP_JOB",
    "AWS_BACKUP_PLAN",
    "AWS_BACKUP_RECOVERY_POINT",
    "AWS_BACKUP_VAULT",
    "BACKUP_COPY_JOB",
    "BACKUP_JOB",
    "BACKUP_PLAN",
    "BACKUP_RECOVERY_POINT",
    "BACKUP_VAULT",
    "BACKUP_ASSET_TYPES",
    "AWS_BACKUP_ASSET_MODELS",
    "AWS_BACKUP_ASSET_TYPES",
    "CoreAWSBackupAsset",
    "CoreAWSBackupCopyJob",
    "CoreAWSBackupJob",
    "CoreAWSBackupPlan",
    "CoreAWSBackupRecoveryPoint",
    "CoreAWSBackupVault",
    "sync_aws_backup_assets",
    "sync_aws_snapshots",
]
