"""Read-only AWS discovery primitives.

This module is deliberately small so later AWS adapters can share the same
client bounds, pagination checks, serialization, and error classification.
It does not persist credentials or call any AWS mutation operation.
"""

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
import re

import boto3
from botocore.exceptions import BotoCoreError, ClientError, OperationNotPageableError

from apps.console.cloud.aws.models import AWS_CLIENT_CONFIG
from apps.console.cloud.models import CloudInventoryTransientError
from apps.monitoring.metadata import redact_sensitive_metadata


__all__ = (
    "aws_client",
    "get_enabled_regions",
    "iter_pages",
    "require_collection",
    "serialize_aws",
    "aws_error_code",
    "is_transient_aws_error",
)


_ENABLED_REGION_STATUSES = frozenset({"opt-in-not-required", "opted-in"})
_MUTATING_OPERATION_VERBS = frozenset({
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
})
_TRANSIENT_ERROR_CODES = frozenset({
    "DependencyFailure",
    "DependencyTimeout",
    "InternalError",
    "InternalFailure",
    "InternalServerError",
    "PriorRequestNotComplete",
    "ProvisionedThroughputExceededException",
    "RequestLimitExceeded",
    "RequestTimeout",
    "RequestTimeoutException",
    "ServiceUnavailable",
    "ServiceUnavailableException",
    "SlowDown",
    "Throttling",
    "ThrottlingException",
    "TooManyRequestsException",
})
_SAFE_OPERATION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_SAFE_CONTEXT_RE = re.compile(r"[^A-Za-z0-9_.:/ -]+")
_REGION_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)+$")
_SENSITIVE_CONTEXT_RE = re.compile(
    r"(?i)(?:access[_-]?key|secret|token|password|credential|authorization|api[_-]?key)"
)
_ARN_RE = re.compile(r"(?i)\barn:[^\s,}]+")


def aws_client(account, service, region=None):
    """Build a bounded boto3 client for a ``CoreAWSAccount``.

    Credentials are passed directly to boto3 for the client construction and
    are never returned, logged, or included in an exception message.
    """
    if not isinstance(service, str) or not service.strip():
        raise ValueError("AWS service is required")

    return boto3.client(
        service,
        aws_access_key_id=account.access_key,
        aws_secret_access_key=account.secret_key,
        region_name=region or account.region,
        config=AWS_CLIENT_CONFIG,
    )


def get_enabled_regions(account):
    """Return enabled EC2 regions in deterministic order.

    ``DescribeRegions`` normally returns only regions enabled for the account,
    but the response can still contain opt-in metadata and malformed entries.
    Disabled, incomplete, and unusable entries are ignored; a malformed
    response itself fails closed with ``CloudInventoryTransientError``.
    """
    try:
        client = aws_client(account, "ec2", region=account.region)
        regions = []
        for page in _iter_region_pages(client):
            for region_data in require_collection(page, ("Regions",), "EC2 regions"):
                if not isinstance(region_data, dict):
                    continue

                region_name = region_data.get("RegionName")
                if not _usable_region_name(region_name):
                    continue

                opt_in_status = region_data.get("OptInStatus")
                if opt_in_status is not None:
                    if not isinstance(opt_in_status, str):
                        continue
                    if opt_in_status.lower() not in _ENABLED_REGION_STATUSES:
                        continue

                endpoint = region_data.get("Endpoint")
                if "Endpoint" in region_data and (
                    not isinstance(endpoint, str) or not endpoint.strip()
                ):
                    continue

                regions.append(region_name)

        return sorted(set(regions))
    except CloudInventoryTransientError:
        raise
    except (ClientError, BotoCoreError) as error:
        raise CloudInventoryTransientError(
            "AWS region discovery temporarily unavailable"
        ) from error
    except (AttributeError, TypeError, ValueError) as error:
        raise CloudInventoryTransientError(
            "AWS region discovery returned an invalid response"
        ) from error


def _iter_region_pages(client):
    """Yield ``DescribeRegions`` responses, following its optional token."""
    next_token = None
    seen_tokens = set()

    while True:
        request = {} if next_token is None else {"NextToken": next_token}
        page = client.describe_regions(**request)
        if not isinstance(page, dict):
            raise CloudInventoryTransientError(
                "AWS returned an invalid region discovery response"
            )
        yield page

        next_token = page.get("NextToken")
        if next_token in (None, ""):
            return
        if not isinstance(next_token, str) or not next_token.strip() or next_token in seen_tokens:
            raise CloudInventoryTransientError(
                "AWS returned an invalid region pagination response"
            )
        seen_tokens.add(next_token)


def iter_pages(client, operation_name, **kwargs):
    """Yield validated pages from a boto3 read-only paginator.

    A paginator that yields no pages, a non-mapping page, or a malformed
    non-pageable response is treated as incomplete inventory.  This prevents a
    later adapter from interpreting a provider protocol failure as an empty
    inventory.
    """
    _assert_read_only_operation(operation_name)

    try:
        try:
            paginator = client.get_paginator(operation_name)
            pages = paginator.paginate(**kwargs)
        except (AttributeError, OperationNotPageableError):
            operation = getattr(client, operation_name)
            pages = (operation(**kwargs),)

        if pages is None or isinstance(pages, (str, bytes, dict)):
            raise CloudInventoryTransientError(
                "AWS returned an invalid paginated inventory response"
            )

        yielded_page = False
        for page in pages:
            if not isinstance(page, dict):
                raise CloudInventoryTransientError(
                    "AWS returned an invalid paginated inventory response"
                )
            yielded_page = True
            yield page

        if not yielded_page:
            raise CloudInventoryTransientError(
                "AWS returned an incomplete paginated inventory response"
            )
    except CloudInventoryTransientError:
        raise
    except (ClientError, BotoCoreError) as error:
        raise CloudInventoryTransientError(
            "AWS inventory request temporarily unavailable"
        ) from error
    except Exception as error:
        raise CloudInventoryTransientError(
            "AWS returned an invalid paginated inventory response"
        ) from error


def require_collection(payload, path, context):
    """Return a required AWS list collection or fail closed.

    ``path`` may be a dotted string or a sequence of mapping keys.  A missing
    path, ``None``, or any non-list value is not treated as an empty result.
    """
    keys = _path_keys(path)
    current = payload
    for key in keys:
        if not isinstance(current, Mapping) or key not in current:
            raise CloudInventoryTransientError(
                f"AWS returned an incomplete {_safe_context(context)} response"
            )
        current = current[key]

    if not isinstance(current, list):
        raise CloudInventoryTransientError(
            f"AWS returned an invalid {_safe_context(context)} collection"
        )
    return current


def serialize_aws(value):
    """Make AWS values JSON-safe and apply CloudMoo metadata redaction.

    ``Decimal`` values are represented as strings to preserve precision rather
    than silently converting billing or capacity data to binary floats.
    """
    serialized = _serialize_aws_value(value)
    return redact_sensitive_metadata(serialized)


def aws_error_code(error):
    """Return only a bounded AWS error code, never the provider payload."""
    if isinstance(error, ClientError):
        response = getattr(error, "response", None)
        if isinstance(response, dict):
            details = response.get("Error")
            if isinstance(details, dict):
                code = details.get("Code")
                if isinstance(code, str) and code:
                    return code[:128]
        return "ClientError"

    return type(error).__name__


def is_transient_aws_error(error):
    """Classify retryable AWS failures without inspecting secret-bearing text."""
    if isinstance(error, (BotoCoreError, TimeoutError, ConnectionError)):
        return True

    if not isinstance(error, ClientError):
        return False

    code = aws_error_code(error)
    normalized_code = code.lower()
    if code in _TRANSIENT_ERROR_CODES or normalized_code.startswith((
        "throttl",
        "requestlimit",
        "serviceunavailable",
    )):
        return True

    response = getattr(error, "response", None)
    if isinstance(response, dict):
        details = response.get("Error")
        if isinstance(details, dict):
            status = details.get("HTTPStatusCode")
            if status is None:
                metadata = response.get("ResponseMetadata")
                if isinstance(metadata, dict):
                    status = metadata.get("HTTPStatusCode")
            if isinstance(status, int) and (status == 429 or status >= 500):
                return True

    return False


def _assert_read_only_operation(operation_name):
    if not isinstance(operation_name, str) or not _SAFE_OPERATION_RE.fullmatch(operation_name):
        raise ValueError("AWS operation is invalid")

    normalized_operation = operation_name.lower()
    if any(normalized_operation.startswith(mutation) for mutation in _MUTATING_OPERATION_VERBS):
        raise ValueError("AWS mutation operations are not supported")


def _path_keys(path):
    if isinstance(path, str):
        keys = tuple(path.split("."))
    else:
        try:
            keys = tuple(path)
        except TypeError as error:
            raise CloudInventoryTransientError(
                "AWS returned an incomplete inventory response"
            ) from error

    if not keys or any(key in (None, "") for key in keys):
        raise CloudInventoryTransientError(
            "AWS returned an incomplete inventory response"
        )
    return keys


def _safe_context(context):
    if not isinstance(context, str):
        return "inventory"

    label = " ".join(context.split())
    if not label or _SENSITIVE_CONTEXT_RE.search(label) or _ARN_RE.search(label):
        return "inventory"

    label = _SAFE_CONTEXT_RE.sub("", label).strip()
    return label[:80] or "inventory"


def _serialize_aws_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {
            _serialize_aws_value(key): _serialize_aws_value(child)
            for key, child in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_serialize_aws_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        serialized_items = [_serialize_aws_value(item) for item in value]
        return sorted(serialized_items, key=repr)
    return value


def _usable_region_name(value):
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and len(value) <= 64
        and _REGION_NAME_RE.fullmatch(value) is not None
    )
