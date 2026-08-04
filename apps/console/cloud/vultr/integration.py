"""Cross-cutting registration for the read-only Vultr resource modules.

The Vultr adapter is split by service family so a failure in one API surface
does not require duplicating the client, redaction, or reconciliation policy.
This module is intentionally small: family modules own their models, endpoint
specifications, and provider-specific sync logic; this module discovers those
registries and connects them to CloudMoo's common asset and monitoring paths.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Mapping
from typing import Any

from apps.console.cloud.models import CloudInventoryTransientError


logger = logging.getLogger(__name__)

VULTR_RESOURCE_MODULES = (
    "apps.console.cloud.vultr.resources_compute",
    "apps.console.cloud.vultr.resources_data_network",
    "apps.console.cloud.vultr.resources_platform",
    "apps.console.cloud.vultr.resources_account_operations",
)


def _modules():
    """Load all Vultr family modules in deterministic order."""
    return tuple(importlib.import_module(name) for name in VULTR_RESOURCE_MODULES)


def _registry(module: Any, *names: str) -> Mapping[str, Any]:
    for name in names:
        value = getattr(module, name, None)
        if isinstance(value, Mapping):
            return value
    return {}


def get_vultr_resource_models() -> dict[str, type]:
    """Return the merged ``asset_type -> Django model`` registry."""
    models: dict[str, type] = {}
    for module in _modules():
        for key, model in _registry(module, "RESOURCE_MODELS", "VULTR_RESOURCE_MODELS").items():
            if key in models and models[key] is not model:
                raise RuntimeError(f"Duplicate Vultr asset model registration: {key}")
            models[str(key)] = model
    return models


def get_vultr_resource_specs() -> dict[str, Any]:
    """Return the merged endpoint/spec registry used by inventory sync."""
    specs: dict[str, Any] = {}
    for module in _modules():
        for key, spec in _registry(module, "RESOURCE_SPECS", "VULTR_RESOURCE_SPECS").items():
            if key in specs and specs[key] is not spec:
                raise RuntimeError(f"Duplicate Vultr resource specification: {key}")
            specs[str(key)] = spec
    return specs


def get_vultr_resource_checks() -> dict[str, Any]:
    """Return the merged status-check registry exposed by family modules."""
    checks: dict[str, Any] = {}
    for module in _modules():
        for key, check in _registry(
            module,
            "RESOURCE_CHECKS",
            "VULTR_RESOURCE_CHECKS",
            "CHECK_REGISTRY",
            "VULTR_COMPUTE_CHECKS",
            "VULTR_ACCOUNT_OPERATIONS_CHECKS",
            "VULTR_STATUS_CHECKS",
        ).items():
            if key in checks and checks[key] is not check:
                raise RuntimeError(f"Duplicate Vultr status-check registration: {key}")
            checks[str(key)] = check
    return checks


def get_vultr_asset_relations() -> tuple[tuple[str, str], ...]:
    """Return resolved account reverse relations for ``CoreCloud``.

    Family models use Django's ``%(class)s_assets`` related-name pattern.  It
    is resolved only after the concrete model is loaded, so deriving it from
    the owner field avoids a second hand-maintained list that can drift from
    the model registry.
    """
    relations: list[tuple[str, str]] = []
    specs = get_vultr_resource_specs()
    models = get_vultr_resource_models()
    for key, model in models.items():
        try:
            relation_name = model._meta.get_field("owner").remote_field.related_name
        except (LookupError, AttributeError):
            logger.warning("Skipping Vultr model without an owner relation: %s", model)
            continue
        spec = specs.get(key)
        asset_type = getattr(spec, "asset_type", None) or getattr(model, "asset_type", None) or key
        relation = (str(relation_name), str(asset_type))
        if relation not in relations:
            relations.append(relation)
    return tuple(relations)


def _legacy_models():
    # Import lazily to avoid a circular import while Django is constructing
    # the Vultr model registry.
    from apps.console.cloud.vultr.models import (
        CoreVultrDatabase,
        CoreVultrServer,
        CoreVultrVolume,
    )

    return CoreVultrServer, CoreVultrVolume, CoreVultrDatabase


def sync_vultr_inventory(account) -> dict[str, Any]:
    """Synchronize all registered Vultr families with fail-closed semantics.

    A family may expose a specialized ``sync_resources(account)`` function
    (needed for nested resources such as Kubernetes node pools or DNS
    records).  Otherwise its endpoint specs are fetched by the shared client
    and reconciled through the common helper.  Existing legacy server,
    volume, and database tables remain owned by ``models.py`` and are not
    duplicated here.
    """
    results: dict[str, Any] = {}
    legacy_models = _legacy_models()
    specs = get_vultr_resource_specs()

    client = None
    for module in _modules():
        family_specs = _registry(module, "RESOURCE_SPECS", "VULTR_RESOURCE_SPECS")
        persistent_specs = {
            key: spec
            for key, spec in family_specs.items()
            if isinstance(getattr(spec, "model", None), type)
            and getattr(getattr(spec, "model", None), "_meta", None) is not None
            and getattr(spec, "supported", True)
        }
        # Account/governance helpers intentionally expose response specs but
        # no Django inventory model. They are monitored on demand and are not
        # part of the asset reconciliation pass.
        if not persistent_specs:
            results[module.__name__.rsplit(".", 1)[-1]] = {"skipped": "non_model_surfaces"}
            continue

        family_sync = getattr(module, "sync_vultr_resources", None)
        if callable(family_sync):
            selected_keys = [key for key, spec in persistent_specs.items() if spec.model not in legacy_models]
            if selected_keys:
                try:
                    result = family_sync(account, resources=selected_keys)
                except TypeError:
                    # A family implementation may expose a positional-only
                    # resource selector; the selected set remains explicit.
                    result = family_sync(account, selected_keys)
            else:
                result = {"skipped": "legacy_models_only"}
            results[module.__name__.rsplit(".", 1)[-1]] = result
            continue

        if client is None:
            from apps.console.cloud.vultr.resources_base import VultrClient

            client = VultrClient(account.access_token)

        family_result: dict[str, int] = {}
        for key, spec in persistent_specs.items():
            model = getattr(spec, "model", None)
            if model in legacy_models:
                continue
            endpoint = getattr(spec, "endpoint", None)
            if not endpoint:
                raise CloudInventoryTransientError(f"Vultr resource endpoint is missing: {key}")
            records = client.list_collection(spec)
            from apps.console.cloud.vultr.resources_base import reconcile_collection

            family_result[str(key)] = reconcile_collection(account, spec, records, client)
        results[module.__name__.rsplit(".", 1)[-1]] = family_result

    return results


RESOURCE_MODELS = get_vultr_resource_models
RESOURCE_SPECS = get_vultr_resource_specs
RESOURCE_CHECKS = get_vultr_resource_checks
