"""Read-only AWS Backup, EBS, and RDS snapshot status checks."""

from __future__ import annotations

import re

from botocore.exceptions import BotoCoreError, ClientError

from apps.console.cloud.aws.backup import _bounded_value
from apps.console.cloud.aws.backup import (
    AWS_BACKUP_COPY_JOB,
    AWS_BACKUP_JOB,
    AWS_BACKUP_PLAN,
    AWS_BACKUP_RECOVERY_POINT,
    AWS_BACKUP_VAULT,
)
from apps.console.cloud.aws.backup import (
    BACKUP_COPY_JOB,
    BACKUP_JOB,
    BACKUP_PLAN,
    BACKUP_RECOVERY_POINT,
    BACKUP_VAULT,
    aws_client,
    aws_error_code,
    iter_pages,
    require_collection,
    serialize_aws,
)


_REGION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,31}$")
_NOT_FOUND_CODES = {
    "ResourceNotFoundException",
    "ResourceNotFound",
    "InvalidSnapshot.NotFound",
    "DBSnapshotNotFound",
    "DBClusterSnapshotNotFoundFault",
    "BackupJobNotFound",
    "ResourceNotFoundFault",
}
_AUTH_CODES = {
    "AccessDenied",
    "AccessDeniedException",
    "AuthFailure",
    "ExpiredToken",
    "InvalidClientTokenId",
    "UnrecognizedClientException",
}


class _CredentialAccount:
    """Small account-shaped object for the shared discovery client helper."""

    def __init__(self, credentials):
        self.access_key = credentials.get("access_key")
        self.secret_key = credentials.get("secret_key")
        self.region = credentials.get("region") or credentials.get("resource_region")

    @property
    def access_token(self):
        return {
            "access_key": self.access_key,
            "secret_key": self.secret_key,
            "region": self.region,
        }


def _client(credentials, service, region):
    account = _CredentialAccount(credentials)
    return aws_client(account, service, region=region)


def _context(unique_id, credentials):
    if not isinstance(credentials, dict):
        raise ValueError("AWS credentials are not configured")
    region = credentials.get("resource_region") or credentials.get("region")
    if not isinstance(region, str) or not _REGION_PATTERN.fullmatch(region):
        raise ValueError("AWS credentials contain an invalid Region")
    if not credentials.get("access_key") or not credentials.get("secret_key"):
        raise ValueError("AWS credentials are incomplete")

    metadata = credentials.get("metadata") if isinstance(credentials.get("metadata"), dict) else {}
    provider_id = (
        credentials.get("provider_id")
        or credentials.get("resource_name")
        or metadata.get("_cloudmoo_raw_id")
        or metadata.get("_cloudmoo_provider_id")
    )
    if not provider_id:
        provider_id = unique_id
    provider_id = str(provider_id)

    # Inventory IDs are qualified exactly once.  ARNs remain intact after
    # splitting the first colon.
    if unique_id and isinstance(unique_id, str):
        if "|" in unique_id:
            prefix, remainder = unique_id.split("|", 1)
            if prefix == region and remainder:
                if not remainder.startswith("sha256:") or not credentials.get("provider_id"):
                    provider_id = str(remainder)
        else:
            prefix, separator, remainder = unique_id.partition(":")
            if separator and prefix == region and remainder:
                provider_id = str(remainder)
    return region, provider_id, credentials


def _serialized(payload):
    if not isinstance(payload, dict):
        raise ValueError("AWS returned an invalid resource object")
    value = serialize_aws(payload)
    if not isinstance(value, dict):
        raise ValueError("AWS returned an invalid serialized resource object")
    return _bounded_value(value)


def _collection(client, operation, collection_key, **kwargs):
    stream = iter_pages(client, operation, **kwargs)
    if stream is None:
        raise ValueError("AWS returned no pagination stream")
    if isinstance(stream, dict):
        stream = require_collection(stream, collection_key, operation)
    values = []
    for page_or_item in stream:
        if isinstance(page_or_item, dict) and collection_key in page_or_item:
            page_values = require_collection(page_or_item, collection_key, operation)
            if not isinstance(page_values, list):
                raise ValueError("AWS returned an invalid collection")
            values.extend(page_values)
        elif isinstance(page_or_item, (list, tuple)):
            values.extend(page_or_item)
        else:
            values.append(page_or_item)
        if len(values) > 10000:
            raise ValueError("AWS returned too many collection items")
    if any(not isinstance(value, dict) for value in values):
        raise ValueError("AWS returned an invalid collection item")
    return values


def _resource(response, key):
    if not isinstance(response, dict):
        raise ValueError("AWS returned an invalid response")
    value = response.get(key) if key else response
    if not isinstance(value, dict):
        raise ValueError("AWS returned an invalid resource")
    return value


def _error_code(error):
    try:
        provider_error = getattr(error, "__cause__", None) or error
        code = aws_error_code(provider_error)
    except Exception:
        code = None
    return str(code or type(error).__name__)[:128]


def _error_status(error, not_found_status="not_found"):
    code = _error_code(error)
    if code in _NOT_FOUND_CODES or any(value in str(error) for value in _NOT_FOUND_CODES):
        return not_found_status, {"error_code": code}
    if code in _AUTH_CODES:
        return "invalid_access_token", {"error_code": code}
    return "error", {"error_code": code}


def _invalid_response():
    return "error", {"error_code": "invalid_response"}


def _provider_state(resource):
    for key in ("State", "Status", "VaultState", "RecoveryPointStatus", "DBInstanceStatus"):
        value = resource.get(key)
        if value is not None and str(value).strip():
            return str(value).strip().lower().replace(" ", "_")
    return ""


def _normalize_snapshot_state(state):
    completed = {"completed", "available", "succeeded", "success"}
    pending = {"pending", "creating", "copying", "in_progress", "in-progress", "starting"}
    deleted = {"deleted", "deleting", "removed"}
    failed = {"error", "failed", "failure", "cancelled", "canceled"}
    if state in completed:
        return "completed"
    if state in pending:
        return "pending"
    if state in deleted:
        return "deleted"
    if state in failed:
        return "error"
    return "error"


def _normalize_backup_state(state):
    completed = {"completed", "available", "succeeded", "success"}
    running = {
        "running",
        "pending",
        "created",
        "creating",
        "in_progress",
        "in-progress",
        "deleting",
        "aborting",
    }
    failed = {"failed", "failure", "aborted", "cancelled", "canceled", "partial", "stopped"}
    if state in completed:
        return "completed"
    if state in running:
        return "running"
    if state == "expired":
        return "expired"
    if state in failed:
        return "failed"
    return "failed"


def _payload_result(asset_key, resource, status):
    metadata = _serialized(resource)
    payload = {asset_key: metadata}
    if asset_key.startswith("aws_"):
        payload[asset_key[4:]] = metadata
    return status, payload


def _check_backup_vault(unique_id, credentials):
    try:
        region, provider_id, context = _context(unique_id, credentials)
        client = _client(context, "backup", region)
        response = client.describe_backup_vault(BackupVaultName=provider_id)
        resource = _resource(response, None)
        state = _provider_state(resource) or "available"
        if state in {"available", "active"}:
            state = "available"
        return _payload_result(AWS_BACKUP_VAULT, resource, state)
    except (ClientError, BotoCoreError) as error:
        return _error_status(error)
    except (KeyError, TypeError, ValueError):
        return _invalid_response()
    except Exception as error:
        return _error_status(error)


def check_aws_backup_vault_status(unique_id, credentials):
    return _check_backup_vault(unique_id, credentials)


def _check_backup_plan(unique_id, credentials):
    try:
        region, provider_id, context = _context(unique_id, credentials)
        client = _client(context, "backup", region)
        response = client.get_backup_plan(BackupPlanId=provider_id)
        resource = _resource(response, "BackupPlan")
        status = _provider_state(resource) or "available"
        return _payload_result(AWS_BACKUP_PLAN, resource, status)
    except (ClientError, BotoCoreError) as error:
        return _error_status(error)
    except (KeyError, TypeError, ValueError):
        return _invalid_response()
    except Exception as error:
        return _error_status(error)


def check_aws_backup_plan_status(unique_id, credentials):
    return _check_backup_plan(unique_id, credentials)


def _check_recovery_point(unique_id, credentials):
    try:
        region, provider_id, context = _context(unique_id, credentials)
        metadata = context.get("metadata") if isinstance(context.get("metadata"), dict) else {}
        vault_name = (
            context.get("backup_vault_name")
            or metadata.get("_cloudmoo_backup_vault_name")
            or metadata.get("BackupVaultName")
        )
        if not vault_name:
            raise ValueError("recovery point is missing its backup vault")
        client = _client(context, "backup", region)
        response = client.describe_recovery_point(
            BackupVaultName=str(vault_name),
            RecoveryPointArn=provider_id,
        )
        resource = _resource(response, None)
        state = _provider_state(resource)
        if not state:
            return _invalid_response()
        return _payload_result(AWS_BACKUP_RECOVERY_POINT, resource, _normalize_backup_state(state))
    except (ClientError, BotoCoreError) as error:
        return _error_status(error)
    except (KeyError, TypeError, ValueError):
        return _invalid_response()
    except Exception as error:
        return _error_status(error)


def check_aws_backup_recovery_point_status(unique_id, credentials):
    return _check_recovery_point(unique_id, credentials)


def _check_job(unique_id, credentials, *, operation, response_key, id_parameter, asset_key):
    try:
        region, provider_id, context = _context(unique_id, credentials)
        client = _client(context, "backup", region)
        response = getattr(client, operation)(**{id_parameter: provider_id})
        resource = _resource(response, response_key)
        state = _provider_state(resource)
        if not state:
            return _invalid_response()
        return _payload_result(asset_key, resource, _normalize_backup_state(state))
    except (ClientError, BotoCoreError) as error:
        return _error_status(error)
    except (KeyError, TypeError, ValueError):
        return _invalid_response()
    except Exception as error:
        return _error_status(error)


def check_aws_backup_job_status(unique_id, credentials):
    return _check_job(
        unique_id,
        credentials,
        operation="describe_backup_job",
        response_key="BackupJob",
        id_parameter="BackupJobId",
        asset_key=AWS_BACKUP_JOB,
    )


def check_aws_backup_copy_job_status(unique_id, credentials):
    return _check_job(
        unique_id,
        credentials,
        operation="describe_copy_job",
        response_key="CopyJob",
        id_parameter="CopyJobId",
        asset_key=AWS_BACKUP_COPY_JOB,
    )


def _snapshot_kind(credentials):
    metadata = credentials.get("metadata") if isinstance(credentials.get("metadata"), dict) else {}
    return (
        credentials.get("snapshot_kind")
        or credentials.get("snapshot_type")
        or metadata.get("_cloudmoo_snapshot_kind")
        or metadata.get("snapshot_kind")
        or "ebs"
    )


def _check_snapshot(unique_id, credentials, forced_kind=None):
    try:
        region, provider_id, context = _context(unique_id, credentials)
        kind = forced_kind or _snapshot_kind(context)
        if kind in {"rds_instance", "db", "instance", "rds_db", "rds"}:
            service = "rds"
            operation = "describe_db_snapshots"
            collection_key = "DBSnapshots"
            id_parameter = "DBSnapshotIdentifier"
        elif kind in {"rds_cluster", "cluster", "aurora"}:
            service = "rds"
            operation = "describe_db_cluster_snapshots"
            collection_key = "DBClusterSnapshots"
            id_parameter = "DBClusterSnapshotIdentifier"
        else:
            service = "ec2"
            operation = "describe_snapshots"
            collection_key = "Snapshots"
            id_parameter = "SnapshotIds"

        client = _client(context, service, region)
        kwargs = {id_parameter: [provider_id]} if id_parameter == "SnapshotIds" else {id_parameter: provider_id}
        resources = _collection(client, operation, collection_key, **kwargs)
        matching = []
        id_keys = (
            ("SnapshotId",)
            if kind == "ebs"
            else (("DBClusterSnapshotIdentifier",) if service == "rds" and "cluster" in collection_key.lower() else ("DBSnapshotIdentifier",))
        )
        for resource in resources:
            if any(resource.get(key) == provider_id for key in id_keys):
                matching.append(resource)
        if not matching:
            return ("deleted", {"error_code": "SnapshotNotFound"})
        resource = matching[0]
        state = _provider_state(resource)
        if not state:
            return _invalid_response()
        return _payload_result("snapshot", resource, _normalize_snapshot_state(state))
    except (ClientError, BotoCoreError) as error:
        return _error_status(error, not_found_status="deleted")
    except (KeyError, TypeError, ValueError):
        return _invalid_response()
    except Exception as error:
        return _error_status(error, not_found_status="deleted")


def check_aws_snapshot_status(unique_id, credentials):
    return _check_snapshot(unique_id, credentials)


def check_aws_rds_snapshot_status(unique_id, credentials):
    kind = _snapshot_kind(credentials) if isinstance(credentials, dict) else "rds_instance"
    forced_kind = "rds_cluster" if kind in {"rds_cluster", "cluster", "aurora"} else "rds_instance"
    return _check_snapshot(unique_id, credentials, forced_kind=forced_kind)


AWS_BACKUP_STATUS_CHECKS = {
    AWS_BACKUP_VAULT: check_aws_backup_vault_status,
    AWS_BACKUP_PLAN: check_aws_backup_plan_status,
    AWS_BACKUP_RECOVERY_POINT: check_aws_backup_recovery_point_status,
    AWS_BACKUP_JOB: check_aws_backup_job_status,
    AWS_BACKUP_COPY_JOB: check_aws_backup_copy_job_status,
    BACKUP_VAULT: check_aws_backup_vault_status,
    BACKUP_PLAN: check_aws_backup_plan_status,
    BACKUP_RECOVERY_POINT: check_aws_backup_recovery_point_status,
    BACKUP_JOB: check_aws_backup_job_status,
    BACKUP_COPY_JOB: check_aws_backup_copy_job_status,
    "snapshot": check_aws_snapshot_status,
    "rds_snapshot": check_aws_rds_snapshot_status,
}
AWS_BACKUP_ASSET_TYPES = tuple(AWS_BACKUP_STATUS_CHECKS)

# Explicit aliases make the integration boundary discoverable without
# changing the global asset-type registry in this lane.
AWS_BACKUP_CHECKS = AWS_BACKUP_STATUS_CHECKS
AWS_BACKUP_CHECK_FUNCTIONS = AWS_BACKUP_STATUS_CHECKS
AWS_BACKUP_ASSET_TYPE_CHECKS = AWS_BACKUP_STATUS_CHECKS
AWS_BACKUP_CHECK_MAP = AWS_BACKUP_STATUS_CHECKS

check_aws_aws_backup_vault_status = check_aws_backup_vault_status
check_aws_aws_backup_plan_status = check_aws_backup_plan_status
check_aws_aws_backup_recovery_point_status = check_aws_backup_recovery_point_status
check_aws_aws_backup_job_status = check_aws_backup_job_status
check_aws_aws_backup_copy_job_status = check_aws_backup_copy_job_status


__all__ = [
    "AWS_BACKUP_CHECKS",
    "AWS_BACKUP_CHECK_FUNCTIONS",
    "AWS_BACKUP_ASSET_TYPES",
    "AWS_BACKUP_ASSET_TYPE_CHECKS",
    "AWS_BACKUP_CHECK_MAP",
    "AWS_BACKUP_STATUS_CHECKS",
    "check_aws_aws_backup_copy_job_status",
    "check_aws_aws_backup_job_status",
    "check_aws_aws_backup_plan_status",
    "check_aws_aws_backup_recovery_point_status",
    "check_aws_aws_backup_vault_status",
    "check_aws_backup_copy_job_status",
    "check_aws_backup_job_status",
    "check_aws_backup_plan_status",
    "check_aws_backup_recovery_point_status",
    "check_aws_backup_vault_status",
    "check_aws_rds_snapshot_status",
    "check_aws_snapshot_status",
]
