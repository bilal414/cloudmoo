import importlib


def get_check_function(provider, asset_type):
    """
    Resolve the status check function for a provider/asset type pair.

    Returns the ``check_<provider>_<asset_type>_status`` callable from
    ``apps.monitoring.checks.<provider>``. Raises ValueError when the
    provider or asset type is not supported.
    """
    try:
        provider_module = importlib.import_module(f'apps.monitoring.checks.{provider}')
        return getattr(provider_module, f'check_{provider}_{asset_type}_status')
    except (ImportError, AttributeError) as e:
        raise ValueError(f'Unsupported provider or asset type: {provider}, {asset_type}') from e
