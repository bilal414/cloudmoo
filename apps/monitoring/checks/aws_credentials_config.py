"""Read-only status checks for AWS credential/configuration metadata assets."""

from __future__ import annotations

from collections.abc import Mapping
import re
from types import SimpleNamespace

from botocore.exceptions import BotoCoreError, ClientError

from apps.console.cloud.aws.credentials_config import (
    AWS_SECRETS_MANAGER_SECRET,
    AWS_SSM_PARAMETER,
    _bounded_metadata,
    _parameter_metadata,
    _safe_error_code,
    _secret_metadata,
)
from apps.console.cloud.aws.discovery import (
    aws_client,
    iter_pages,
    require_collection,
    serialize_aws,
)
from apps.console.cloud.models import CloudInventoryTransientError
from apps.monitoring.metadata import redact_sensitive_metadata


_REGION_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)+$")
MAX_CHECK_PAGES = 100
MAX_CHECK_ITEMS = 5_000
MAX_CHECK_METADATA_ITEMS = 80
MAX_CHECK_METADATA_LIST_ITEMS = 50
MAX_CHECK_METADATA_DEPTH = 5
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
    }
)


class _ProviderNotFound(Exception):
    """The provider answered successfully but did not return the asset."""


class _CredentialAccount:
    """Account-shaped object accepted by the shared AWS client helper."""

    def __init__(self, credentials, region):
        self.access_key = credentials.get("access_key") or credentials.get("aws_access_key_id")
        self.secret_key = credentials.get("secret_key") or credentials.get("aws_secret_access_key")
        self.region = region


def _context(unique_id, credentials):
    if not isinstance(credentials, Mapping):
        raise ValueError("AWS monitoring credentials are invalid")
    if not isinstance(unique_id, str) or not unique_id.strip():
        raise ValueError("AWS resource identifier is invalid")

    metadata = credentials.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    region_values = {
        str(value).strip()
        for value in (
            credentials.get("resource_region"),
            credentials.get("region"),
            metadata.get("_cloudmoo_region"),
        )
        if value
    }
    identity_region = ""
    identity_id = ""
    if isinstance(unique_id, str) and "|" in unique_id:
        identity_region, identity_id = unique_id.split("|", 1)
        identity_region = identity_region.strip()
        identity_id = identity_id.strip()
    elif isinstance(unique_id, str):
        identity_id = unique_id.strip()
    if identity_region:
        region_values.add(identity_region)
    if len(region_values) != 1:
        raise ValueError("AWS resource Region context is inconsistent")
    region = next(iter(region_values))
    if len(region) > 64 or _REGION_RE.fullmatch(region) is None:
        raise ValueError("AWS resource Region is invalid")

    account = _CredentialAccount(credentials, region)
    if not account.access_key or not account.secret_key:
        raise ValueError("AWS monitoring credentials are incomplete")

    provider_id = (
        credentials.get("provider_id")
        or metadata.get("_cloudmoo_provider_id")
        or metadata.get("provider_id")
        or metadata.get("_cloudmoo_raw_id")
        or identity_id
    )
    resource_name = (
        credentials.get("resource_name")
        or metadata.get("_cloudmoo_resource_name")
        or metadata.get("name")
        or provider_id
    )
    if not isinstance(provider_id, str) or not provider_id.strip():
        raise ValueError("AWS monitoring credentials are missing the provider identifier")
    if not isinstance(resource_name, str) or not resource_name.strip():
        raise ValueError("AWS monitoring credentials are missing the resource name")
    return region, provider_id.strip(), resource_name.strip(), metadata


def _client(credentials, service, region):
    return aws_client(_CredentialAccount(credentials, region), service, region=region)


def _collection(client, context, **kwargs):
    items = []
    for page_number, page in enumerate(
        iter_pages(client, "describe_parameters", **kwargs),
        start=1,
    ):
        if page_number > MAX_CHECK_PAGES:
            raise CloudInventoryTransientError(
                f"AWS {context} pagination exceeded the safety bound"
            )
        page_items = require_collection(page, "Parameters", context)
        if len(items) + len(page_items) > MAX_CHECK_ITEMS:
            raise CloudInventoryTransientError(
                f"AWS {context} inventory exceeded the safety bound"
            )
        for item in page_items:
            if not isinstance(item, Mapping):
                raise CloudInventoryTransientError(f"AWS returned an invalid {context} item")
            items.append(item)
    return items


def _find_parameter(parameters, provider_id, resource_name):
    expected = {provider_id, resource_name}
    for parameter in parameters:
        if parameter.get("Name") in expected or parameter.get("ARN") in expected:
            return parameter
    raise _ProviderNotFound()


def _safe_check_metadata(value):
    serialized = serialize_aws(value)
    if not isinstance(serialized, dict):
        raise CloudInventoryTransientError("AWS returned invalid status metadata")
    return _bounded_metadata(
        redact_sensitive_metadata(serialized),
        depth=0,
    )


def _error_result(error):
    code = _safe_error_code(error)
    normalized = code.lower().replace("_", "")
    if code in _NOT_FOUND_CODES or "notfound" in normalized:
        return "not_found", {"error_code": code}
    if code in _AUTH_CODES or any(auth.lower() in normalized for auth in _AUTH_CODES):
        return "invalid_access_token", {"error_code": code}
    return "error", {"error_code": code}


def _check_secret(unique_id, credentials):
    region, provider_id, resource_name, _metadata = _context(unique_id, credentials)
    client = _client(credentials, "secretsmanager", region)
    response = client.describe_secret(SecretId=provider_id)
    if not isinstance(response, Mapping) or not response:
        raise CloudInventoryTransientError("AWS returned an invalid secret response")
    name = response.get("Name") or resource_name
    if not isinstance(name, str) or not name.strip():
        raise CloudInventoryTransientError("AWS returned a secret without a name")
    metadata = _secret_metadata(
        {},
        response,
        {},
        region,
        provider_id,
        name.strip(),
    )
    return metadata.get("normalized_status", "unknown"), {
        AWS_SECRETS_MANAGER_SECRET: _safe_check_metadata(metadata)
    }


def _check_parameter(unique_id, credentials):
    region, provider_id, resource_name, _metadata = _context(unique_id, credentials)
    client = _client(credentials, "ssm", region)
    parameters = _collection(client, "SSM parameters")
    try:
        parameter = _find_parameter(parameters, provider_id, resource_name)
    except _ProviderNotFound:
        raise
    name = parameter.get("Name") or resource_name
    if not isinstance(name, str) or not name.strip():
        raise CloudInventoryTransientError("AWS returned a parameter without a name")
    metadata = _parameter_metadata(
        parameter,
        {},
        region,
        name.strip(),
        name.strip(),
    )
    return metadata.get("normalized_status", "unknown"), {
        AWS_SSM_PARAMETER: _safe_check_metadata(metadata)
    }


def _checked(checker, unique_id, credentials):
    try:
        return checker(unique_id, credentials)
    except _ProviderNotFound:
        return "not_found", {"error_code": "not_found"}
    except (ClientError, BotoCoreError) as error:
        return _error_result(error)
    except (CloudInventoryTransientError, ValueError, TypeError) as error:
        return _error_result(error)
    except Exception as error:
        # Do not return provider exception text; it can contain request or
        # configuration material.  The bounded class name is enough to debug.
        return _error_result(error)


def check_aws_secrets_manager_secret_status(unique_id, credentials):
    return _checked(_check_secret, unique_id, credentials)


def check_aws_ssm_parameter_status(unique_id, credentials):
    return _checked(_check_parameter, unique_id, credentials)


check_aws_secrets_manager_status = check_aws_secrets_manager_secret_status
check_aws_ssm_parameter_store_status = check_aws_ssm_parameter_status
check_aws_ssm_parameter_store_parameter_status = check_aws_ssm_parameter_status


AWS_CREDENTIALS_CONFIG_CHECKS = {
    AWS_SECRETS_MANAGER_SECRET: check_aws_secrets_manager_secret_status,
    AWS_SSM_PARAMETER: check_aws_ssm_parameter_status,
}
AWS_CREDENTIALS_CONFIG_STATUS_CHECKS = AWS_CREDENTIALS_CONFIG_CHECKS
AWS_CREDENTIALS_CONFIG_CHECK_REGISTRY = AWS_CREDENTIALS_CONFIG_CHECKS
AWS_CREDENTIALS_CONFIG_CHECK_FUNCTIONS = AWS_CREDENTIALS_CONFIG_CHECKS
AWS_CREDENTIALS_CONFIG_ASSET_TYPE_CHECKS = AWS_CREDENTIALS_CONFIG_CHECKS
AWS_CREDENTIALS_CONFIG_CHECK_MAP = AWS_CREDENTIALS_CONFIG_CHECKS
AWS_CREDENTIAL_CONFIG_CHECKS = AWS_CREDENTIALS_CONFIG_CHECKS
CHECK_REGISTRATION = AWS_CREDENTIALS_CONFIG_CHECKS


def check_aws_credentials_config_asset_status(asset_type, unique_id, credentials):
    checker = AWS_CREDENTIALS_CONFIG_CHECKS.get(asset_type)
    if checker is None:
        return "error", {"error_code": "unsupported_asset_type"}
    return checker(unique_id, credentials)


check_aws_asset_status = check_aws_credentials_config_asset_status

# Generated names used by the generic monitoring dispatcher once the parent
# integration lane registers this module.
check_aws_aws_secrets_manager_secret_status = check_aws_secrets_manager_secret_status
check_aws_aws_ssm_parameter_status = check_aws_ssm_parameter_status


__all__ = [
    "AWS_CREDENTIALS_CONFIG_CHECKS",
    "AWS_CREDENTIALS_CONFIG_STATUS_CHECKS",
    "AWS_CREDENTIALS_CONFIG_CHECK_REGISTRY",
    "AWS_CREDENTIALS_CONFIG_CHECK_FUNCTIONS",
    "AWS_CREDENTIALS_CONFIG_ASSET_TYPE_CHECKS",
    "AWS_CREDENTIALS_CONFIG_CHECK_MAP",
    "AWS_CREDENTIAL_CONFIG_CHECKS",
    "CHECK_REGISTRATION",
    "check_aws_credentials_config_asset_status",
    "check_aws_asset_status",
    "check_aws_secrets_manager_secret_status",
    "check_aws_secrets_manager_status",
    "check_aws_ssm_parameter_status",
    "check_aws_ssm_parameter_store_status",
    "check_aws_ssm_parameter_store_parameter_status",
    "check_aws_aws_secrets_manager_secret_status",
    "check_aws_aws_ssm_parameter_status",
]
