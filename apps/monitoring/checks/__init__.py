"""Status-check dispatch for provider and asset-type pairs.

AWS has several check modules because its inventory is both regional and
service-specific.  Keep the routing table here explicit so a new persisted
asset type cannot accidentally resolve to the legacy AWS checker (or to a
module with a similar-looking generated function name).
"""

import importlib


_AWS_NETWORK_TYPES = frozenset({
    "vpc",
    "subnet",
    "route_table",
    "internet_gateway",
    "nat_gateway",
    "network_acl",
    "network_interface",
    "vpc_peering",
    "transit_gateway_attachment",
    "vpn_connection",
    "flow_log",
    "auto_scaling_group",
    "launch_template",
    "ami",
    "ebs_attachment",
})

_AWS_LEGACY_TYPES = frozenset({
    "server",
    "volume",
    "database",
    "rds_database",
    "lambda",
    "dynamodb",
    "s3_bucket",
    "acm_certificate",
    "elastic_ip",
    "load_balancer",
    "security_group",
    "ecs_service",
    "ecs_task",
})

_AWS_OBSERVABILITY_PREFIXES = ("aws_cloudwatch_", "aws_log_")
_AWS_CONTAINER_PREFIXES = (
    "aws_ecr_",
    "aws_ecs_",
    "aws_eks_",
    "aws_apprunner_",
)
_AWS_EDGE_PREFIXES = (
    "aws_route53_",
    "aws_cloudfront_",
    "aws_waf_",
    "aws_global_accelerator",
)
_AWS_BACKUP_PREFIXES = ("aws_backup_",)
_AWS_DATA_SERVICE_PREFIXES = (
    "aws_rds_",
    "aws_elasticache_",
    "aws_memorydb_",
    "aws_opensearch_",
    "aws_efs_",
    "aws_fsx_",
)
_AWS_APPLICATION_PREFIXES = (
    "aws_apigateway_",
    "aws_eventbridge_",
    "aws_sns_",
    "aws_sqs_",
    "aws_stepfunctions_",
    "aws_athena_",
    "aws_cloudformation_",
)
_AWS_DELIVERY_PREFIXES = (
    "aws_elastic_beanstalk_",
    "aws_codebuild_",
    "aws_codepipeline_",
)
_AWS_SECURITY_GOVERNANCE_TYPES = frozenset({
    "aws_iam_user",
    "aws_iam_role",
    "aws_iam_policy",
    "aws_kms_key",
    "aws_kms_alias",
    "aws_cloudtrail_trail",
    "aws_config_rule",
    "aws_config_recorder",
    "aws_guardduty_detector",
    "aws_security_hub",
    "aws_inspector",
    "aws_macie",
    "aws_firewall_manager_policy",
})
_AWS_CREDENTIALS_CONFIG_TYPES = frozenset({
    "aws_secrets_manager_secret",
    "aws_ssm_parameter",
})
_AWS_ACCOUNT_OPERATIONS_TYPES = frozenset({
    "aws_health_event",
    "aws_trusted_advisor_check",
    "aws_cost_explorer_signal",
    "aws_cost_anomaly_monitor",
    "aws_cost_anomaly_subscription",
    "aws_cost_anomaly",
})


def _aws_module_for(asset_type):
    """Return the owning AWS check module for a normalized asset type."""
    if asset_type.startswith("lightsail_"):
        return "apps.monitoring.checks.aws_lightsail"
    if asset_type in _AWS_NETWORK_TYPES or (
        asset_type.startswith("aws_")
        and asset_type[4:] in _AWS_NETWORK_TYPES
    ):
        return "apps.monitoring.checks.aws_network"
    if asset_type.startswith(_AWS_OBSERVABILITY_PREFIXES):
        return "apps.monitoring.checks.aws_observability"
    if asset_type.startswith(_AWS_CONTAINER_PREFIXES):
        return "apps.monitoring.checks.aws_containers"
    if asset_type.startswith(_AWS_EDGE_PREFIXES):
        return "apps.monitoring.checks.aws_edge"
    if asset_type == "snapshot" or asset_type == "rds_snapshot":
        return "apps.monitoring.checks.aws_backup"
    if asset_type.startswith(_AWS_BACKUP_PREFIXES):
        return "apps.monitoring.checks.aws_backup"
    if asset_type.startswith(_AWS_DATA_SERVICE_PREFIXES):
        return "apps.monitoring.checks.aws_data_services"
    if asset_type.startswith(_AWS_APPLICATION_PREFIXES):
        return "apps.monitoring.checks.aws_application_services"
    if asset_type.startswith(_AWS_DELIVERY_PREFIXES):
        return "apps.monitoring.checks.aws_delivery"
    if asset_type in _AWS_SECURITY_GOVERNANCE_TYPES:
        return "apps.monitoring.checks.aws_security_governance"
    if asset_type in _AWS_CREDENTIALS_CONFIG_TYPES:
        return "apps.monitoring.checks.aws_credentials_config"
    if asset_type in _AWS_ACCOUNT_OPERATIONS_TYPES:
        return "apps.monitoring.checks.aws_account_operations"
    if asset_type in _AWS_LEGACY_TYPES:
        return "apps.monitoring.checks.aws"
    return None


def _check_names(provider, asset_type):
    """Yield the generated and compatibility names used by AWS check modules."""
    names = [f"check_{provider}_{asset_type}_status"]
    if asset_type.startswith("aws_"):
        names.append(f"check_{provider}_{asset_type[4:]}_status")
    return tuple(dict.fromkeys(names))


def _lookup_check(provider_module, provider, asset_type):
    for name in _check_names(provider, asset_type):
        check = getattr(provider_module, name, None)
        if callable(check):
            return check

    # Service modules also expose explicit registries.  These are the safe
    # fallback for a generated name that changes while the asset type remains
    # owned by the same module.
    for registry_name in (
        "AWS_OBSERVABILITY_CHECKS",
        "AWS_CONTAINER_STATUS_CHECKS",
        "AWS_EDGE_CHECKS",
        "AWS_BACKUP_STATUS_CHECKS",
        "AWS_DATA_SERVICE_STATUS_CHECKS",
        "AWS_DATA_SERVICE_CHECKS",
        "AWS_APPLICATION_CHECKS",
        "AWS_APPLICATION_STATUS_CHECKS",
        "AWS_DELIVERY_STATUS_CHECKS",
        "AWS_DELIVERY_CHECKS",
        "AWS_DELIVERY_CHECK_REGISTRY",
        "AWS_SECURITY_GOVERNANCE_CHECKS",
        "AWS_SECURITY_GOVERNANCE_STATUS_CHECKS",
        "AWS_SECURITY_GOVERNANCE_CHECK_REGISTRY",
        "CHECK_REGISTRATION",
        "AWS_CREDENTIALS_CONFIG_CHECKS",
        "AWS_CREDENTIALS_CONFIG_STATUS_CHECKS",
        "AWS_CREDENTIALS_CONFIG_CHECK_REGISTRY",
        "AWS_CREDENTIALS_CONFIG_CHECK_FUNCTIONS",
        "AWS_CREDENTIALS_CONFIG_ASSET_TYPE_CHECKS",
        "AWS_CREDENTIALS_CONFIG_CHECK_MAP",
        "AWS_CREDENTIAL_CONFIG_CHECKS",
        "AWS_ACCOUNT_OPERATIONS_CHECKS",
        "AWS_ACCOUNT_OPERATIONS_STATUS_CHECKS",
        "AWS_ACCOUNT_OPERATIONS_CHECK_REGISTRY",
        "AWS_ACCOUNT_OPERATIONS_CHECK_FUNCTIONS",
    ):
        registry = getattr(provider_module, registry_name, None)
        if isinstance(registry, dict):
            check = registry.get(asset_type)
            if callable(check):
                return check
    return None


def get_check_function(provider, asset_type):
    """Resolve a status check function for a provider and persisted asset type.

    Non-AWS providers retain the historical convention.  AWS routes through an
    explicit service map and only falls back to a module registry owned by the
    selected service.  Unsupported values consistently raise ``ValueError``.
    """
    provider_name = str(provider or "").strip().lower()
    normalized_type = str(asset_type or "").strip().lower()

    if provider_name == "aws":
        module_name = _aws_module_for(normalized_type)
        if module_name is None:
            raise ValueError(
                f"Unsupported provider or asset type: {provider_name}, {normalized_type}"
            )
    else:
        module_name = f"apps.monitoring.checks.{provider_name}"

    try:
        provider_module = importlib.import_module(module_name)
        if provider_name == "aws":
            check = _lookup_check(provider_module, provider_name, normalized_type)
        else:
            check = getattr(
                provider_module,
                f"check_{provider_name}_{normalized_type}_status",
                None,
            )
        if callable(check):
            return check
    except (ImportError, AttributeError) as error:
        raise ValueError(
            f"Unsupported provider or asset type: {provider_name}, {normalized_type}"
        ) from error

    raise ValueError(
        f"Unsupported provider or asset type: {provider_name}, {normalized_type}"
    )
