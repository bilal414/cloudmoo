"""Disposable, ownership-safe live Vultr managed-database E2E test.

This runner deliberately keeps managed-database mutations outside CloudMoo's
production GET-only client.  It uses the raw client and manifest primitives
from ``live_vultr_e2e.py`` for a single low-cost database, then exercises
CloudMoo's read-only database collector and monitoring check.  The token is
accepted only through ``VULTR_API_KEY`` and is never written to the ledger.

The monthly cost ceiling is required explicitly because managed databases are
billable resources::

    VULTR_API_KEY='...' .venv/bin/python tests/live_vultr_database_e2e.py \
        --max-monthly-cost 20

If the process is interrupted, rerun with the printed manifest path and
``--cleanup-ledger``.  Cleanup uses only the exact ledgered database ID.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import tempfile
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = REPO_ROOT / "tests"
for path in (str(REPO_ROOT), str(TESTS_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from live_vultr_e2e import (  # noqa: E402
    AmbiguousMutation,
    CleanupSafetyError,
    LiveE2EError,
    Manifest,
    VultrHTTP,
    find_owned,
    fingerprint_ids,
    load_manifest,
    marker_matches,
    object_id,
    response_object,
    safe_decimal,
    short_id,
)


DATABASE_PROVISION_DEADLINE_SECONDS = 1800
DATABASE_CLEANUP_DEADLINE_SECONDS = 900
DATABASE_POLL_SECONDS = 10
DEFAULT_ENGINE = "pg"
DEFAULT_ENGINE_VERSION = "16"
DATABASE_READY_STATUSES = frozenset({"active", "available", "healthy", "ok", "online", "operational", "ready", "running", "started"})
DATABASE_PENDING_STATUSES = frozenset({"building", "configuring", "creating", "initializing", "migrating", "pending", "processing", "provisioning", "rebalancing", "rebuilding", "updating"})
DATABASE_FAILURE_STATUSES = frozenset({"error", "failed", "failure", "unhealthy"})


def database_status(item: dict[str, Any]) -> str:
    for field in ("status", "state", "health"):
        value = item.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip().lower().replace("-", "_").replace(" ", "_")
    return "unknown"


def database_not_found(error: LiveE2EError) -> bool:
    return "HTTP 404" in str(error)


def list_database_plans(client: VultrHTTP, region: str) -> list[dict[str, Any]]:
    """List region-specific plans with bounded cursor pagination."""

    plans: list[dict[str, Any]] = []
    cursor: str | None = None
    seen: set[str] = set()
    for _ in range(100):
        params: dict[str, Any] = {"region": region, "per_page": 500}
        if cursor:
            if cursor in seen:
                raise LiveE2EError("Database-plan pagination repeated a cursor")
            seen.add(cursor)
            params["cursor"] = cursor
        payload = client.request("GET", "databases/plans", params=params)
        page = payload.get("plans")
        if not isinstance(page, list) or any(not isinstance(item, dict) for item in page):
            raise LiveE2EError("Managed-database plan response was invalid")
        plans.extend(page)
        meta = payload.get("meta")
        links = meta.get("links") if isinstance(meta, dict) else None
        next_cursor = links.get("next") if isinstance(links, dict) else None
        if not next_cursor:
            return plans
        if isinstance(next_cursor, str) and next_cursor.startswith("http"):
            from urllib.parse import parse_qs, urlsplit

            next_cursor = parse_qs(urlsplit(next_cursor).query).get("cursor", [None])[0]
        if not isinstance(next_cursor, str) or not next_cursor.strip():
            raise LiveE2EError("Managed-database plan pagination cursor was invalid")
        cursor = next_cursor
    raise LiveE2EError("Managed-database plan pagination exceeded its bound")


def choose_database_plan(
    client: VultrHTTP,
    requested_region: str | None,
    engine: str,
    max_monthly_cost: Decimal,
    manifest: Manifest,
) -> tuple[str, str, Decimal, list[dict[str, Any]]]:
    regions = client.list_collection("regions", "regions")
    active = [item for item in regions if item.get("active") is not False and isinstance(item.get("id"), str)]
    preferred = ["ams", "ewr", "ord", "dfw", "atl"]
    region_ids = [requested_region] if requested_region else [
        next((item["id"] for item in active if item["id"] == preferred_id), None)
        for preferred_id in preferred
    ]
    region_ids.extend(item["id"] for item in active if item["id"] not in region_ids)
    region_ids = [region for region in region_ids if region]

    for region in region_ids:
        if not any(item["id"] == region for item in active):
            if requested_region:
                raise LiveE2EError("Requested managed-database region is not active")
            continue
        plans = list_database_plans(client, region)
        candidates: list[tuple[Decimal, str, dict[str, Any]]] = []
        for plan in plans:
            plan_id = plan.get("id")
            engines = plan.get("supported_engines")
            locations = plan.get("locations")
            if not isinstance(plan_id, str) or not isinstance(engines, dict) or engines.get(engine) is not True:
                continue
            if isinstance(locations, list) and locations and region.upper() not in {str(item).upper() for item in locations}:
                continue
            if plan.get("number_of_nodes") not in (None, 1):
                continue
            monthly_cost = safe_decimal(plan.get("monthly_cost"))
            if monthly_cost is None or monthly_cost <= 0 or monthly_cost > max_monthly_cost:
                continue
            candidates.append((monthly_cost, plan_id, plan))
        if candidates:
            monthly_cost, plan_id, _plan = min(candidates)
            manifest.event("database_preflight_selection", region=region, engine=engine, plan=plan_id, monthly_cost=str(monthly_cost))
            return region, plan_id, monthly_cost, plans
        if requested_region:
            raise LiveE2EError("No eligible single-node managed-database plan is under the cost ceiling")
    raise LiveE2EError("No eligible managed-database region and plan are available")


def get_database(client: VultrHTTP, database_id: str) -> dict[str, Any]:
    payload = client.request("GET", f"databases/{database_id}")
    database = response_object(payload, ("database",))
    if not database:
        raise LiveE2EError("Managed-database detail response was invalid")
    return database


def poll_database(
    client: VultrHTTP,
    database_id: str,
    label: str,
    predicate: Any,
    deadline: int,
) -> dict[str, Any]:
    end = time.monotonic() + deadline
    last_status: str | None = None
    while time.monotonic() < end:
        try:
            item = get_database(client, database_id)
        except LiveE2EError as error:
            if label.startswith("database_deleted") and database_not_found(error):
                return {}
            raise
        status = database_status(item)
        if status != last_status:
            print(json.dumps({"event": label, "status": status}, sort_keys=True), flush=True)
            last_status = status
        if label == "database_ready" and status in DATABASE_FAILURE_STATUSES:
            raise LiveE2EError(f"Managed database entered terminal failure status: {status}")
        if predicate(item):
            return item
        time.sleep(DATABASE_POLL_SECONDS)
    raise LiveE2EError(f"Timed out waiting for {label}")


def assert_database_identity(
    database: dict[str, Any],
    database_id: str,
    marker: str,
    region: str,
    plan: str,
    engine: str,
    engine_version: str,
) -> None:
    if object_id(database) != database_id or not marker_matches(database, marker):
        raise CleanupSafetyError("Managed-database ownership marker or ID verification failed")
    if str(database.get("region") or "").lower() != region.lower():
        raise CleanupSafetyError("Managed-database region changed unexpectedly")
    if str(database.get("plan") or "") != plan:
        raise CleanupSafetyError("Managed-database plan changed unexpectedly")
    if str(database.get("database_engine") or "").lower() != engine.lower():
        raise CleanupSafetyError("Managed-database engine changed unexpectedly")
    returned_version = database.get("database_engine_version")
    if returned_version is not None and str(returned_version) != engine_version:
        raise CleanupSafetyError("Managed-database engine version changed unexpectedly")


def assert_database_runtime_details(database: dict[str, Any]) -> None:
    """Require non-secret connection fields exposed for a ready cluster."""

    fields = {
        "host": ("host", "hostname"),
        "port": ("port",),
        "dbname": ("dbname", "database_name", "database"),
        "user": ("user", "username"),
    }
    for name, candidates in fields.items():
        value = next(
            (database.get(candidate) for candidate in candidates if database.get(candidate) not in (None, "")),
            None,
        )
        if value in (None, ""):
            raise LiveE2EError(f"Ready managed-database response omitted {name}")


def assert_redacted(value: Any, *, _depth: int = 0) -> None:
    """Reject credentials/connection data if a CloudMoo result exposes it."""

    if _depth > 12:
        return
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = "".join(ch for ch in str(key).lower() if ch.isalnum())
            if any(part in normalized for part in ("password", "secret", "token", "connectionstring", "privatekey", "certificate")):
                if child not in (None, "", "[REDACTED]"):
                    raise LiveE2EError("CloudMoo database metadata contained an unredacted secret field")
            assert_redacted(child, _depth=_depth + 1)
    elif isinstance(value, (list, tuple)):
        for child in value:
            assert_redacted(child, _depth=_depth + 1)


def run_cloudmoo_database_assertions(
    token: str,
    manifest: Manifest,
    database_id: str,
    region: str,
    plan: str,
    engine: str,
    engine_version: str,
) -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "app_cloudmoo_com.settings")
    import django

    django.setup()
    from apps.console.cloud.models import CloudValidationTransientError
    from apps.console.cloud.vultr.models import CoreVultrAccount
    from apps.console.cloud.vultr.resources_base import VultrReadOnlyClient
    from apps.console.cloud.vultr.resources_data_network import collect_vultr_resource_records
    from apps.monitoring.checks.vultr_data_network import check_vultr_database_status

    validated = False
    for attempt in range(3):
        try:
            validated = CoreVultrAccount(access_token=token).validate()
        except CloudValidationTransientError:
            if attempt == 2:
                raise
            time.sleep(2**attempt)
            continue
        break
    if not validated:
        raise LiveE2EError("CloudMoo Vultr account validation failed")

    client = VultrReadOnlyClient(token, timeout=30, max_retries=1)
    detail = client.get_json(f"databases/{database_id}").get("database")
    if not isinstance(detail, dict):
        raise LiveE2EError("CloudMoo managed-database detail envelope was invalid")
    assert_database_identity(detail, database_id, manifest.marker, region, plan, engine, engine_version)

    records = collect_vultr_resource_records(
        SimpleNamespace(access_token=token),
        "database",
        client=client,
    )
    owned = [record for record in records if str(record.get("unique_id")) == database_id]
    if len(owned) != 1 or not marker_matches(owned[0].get("metadata"), manifest.marker):
        raise LiveE2EError("CloudMoo managed-database inventory did not contain the exact owned record")
    assert_redacted(owned[0].get("metadata"))

    status, metadata = check_vultr_database_status(database_id, token)
    if str(status).lower() in {"error", "not_found", "invalid_access_token"}:
        raise LiveE2EError("CloudMoo managed-database monitoring check failed")
    assert_redacted(metadata)
    if token in json.dumps(metadata, default=str):
        raise LiveE2EError("CloudMoo managed-database monitoring metadata exposed the API token")
    manifest.event("cloudmoo_database_assertions_passed", status=str(status), inventory_records=len(records))


def verify_owned_database(client: VultrHTTP, manifest: Manifest, database_id: str) -> bool:
    try:
        database = get_database(client, database_id)
    except LiveE2EError as error:
        if database_not_found(error):
            return False
        raise
    selection = manifest.state.get("selection", {})
    assert_database_identity(
        database,
        database_id,
        manifest.marker,
        str(selection["region"]),
        str(selection["plan"]),
        str(selection["engine"]),
        str(selection["engine_version"]),
    )
    return True


def database_id_in_collection(client: VultrHTTP, database_id: str) -> bool:
    return any(object_id(item) == database_id for item in client.list_collection("databases", "databases"))


def deletion_confirmed(client: VultrHTTP, database_id: str) -> bool:
    """Treat a provider's transient 422 detail response as deleting only after list confirmation."""

    try:
        return not bool(get_database(client, database_id))
    except LiveE2EError as error:
        if database_not_found(error):
            return True
        if "HTTP 422" in str(error):
            return not database_id_in_collection(client, database_id)
        raise


def delete_owned_database(client: VultrHTTP, manifest: Manifest) -> None:
    resource = manifest.state.get("resources", {}).get("database")
    if not resource or resource.get("state") == "absent":
        return
    database_id = str(resource["id"])
    path = f"databases/{database_id}"
    delete_event_exists = any(
        event.get("event") == "database_delete_requested"
        for event in manifest.state.get("events", [])
        if isinstance(event, dict)
    )
    if not delete_event_exists:
        try:
            if not verify_owned_database(client, manifest, database_id):
                manifest.mark_deleted("database")
                return
        except LiveE2EError as error:
            if "HTTP 422" in str(error) and not database_id_in_collection(client, database_id):
                manifest.mark_deleted("database")
                return
            raise
        manifest.state["resources"]["database"]["state"] = "delete_requested"
        manifest.save()
        try:
            client.request("DELETE", path)
        except AmbiguousMutation:
            # Reconfirm the exact ID and marker before the single permitted
            # retry.  If the provider already accepted the request, the
            # subsequent deletion poll will resolve it without another DELETE.
            if verify_owned_database(client, manifest, database_id):
                client.request("DELETE", path)
        except LiveE2EError as error:
            if "HTTP 409" not in str(error):
                raise
            # A provider-side operation can briefly lock a cluster.  Resolve
            # the exact owned ID, wait for a non-pending state, then retry once.
            poll_database(
                client,
                database_id,
                "database_delete_unlocked",
                lambda item: database_status(item) not in DATABASE_PENDING_STATUSES,
                DATABASE_CLEANUP_DEADLINE_SECONDS,
            )
            client.request("DELETE", path)
        manifest.event("database_delete_requested", resource_id=short_id(database_id))
    else:
        manifest.event("database_delete_resume", resource_id=short_id(database_id))
    end = time.monotonic() + DATABASE_CLEANUP_DEADLINE_SECONDS
    while time.monotonic() < end:
        if deletion_confirmed(client, database_id):
            manifest.mark_deleted("database")
            return
        time.sleep(DATABASE_POLL_SECONDS)
    raise LiveE2EError(f"Timed out deleting owned managed database {short_id(database_id)}")


def cleanup(client: VultrHTTP, manifest: Manifest) -> None:
    delete_owned_database(client, manifest)
    baseline = manifest.state.get("baseline", {}).get("databases", {})
    current = client.list_collection("databases", "databases")
    current_ids = sorted(str(item["id"]) for item in current if object_id(item))
    owned_ids = {
        str(item.get("id"))
        for item in manifest.state.get("resources", {}).values()
        if item.get("id")
    }
    if owned_ids.intersection(current_ids):
        raise CleanupSafetyError("Owned managed-database ID remains after cleanup")
    if any(marker_matches(item, manifest.marker) for item in current):
        raise CleanupSafetyError("Managed-database ownership marker remains after cleanup")
    baseline_ids = set(baseline.get("ids", []))
    if not baseline_ids.issubset(set(current_ids)):
        raise CleanupSafetyError("A baseline managed-database ID disappeared during the test")
    removed = len(baseline_ids - set(current_ids))
    added = len(set(current_ids) - baseline_ids)
    if removed or added:
        manifest.event("unowned_database_baseline_drift", removed=removed, added=added)
    manifest.event("database_cleanup_verified", remaining=len(current_ids))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-monthly-cost", required=True, help="explicit maximum monthly database plan cost")
    parser.add_argument("--cleanup-ledger", type=Path)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--region")
    parser.add_argument("--engine", default=DEFAULT_ENGINE)
    parser.add_argument("--engine-version", default=DEFAULT_ENGINE_VERSION)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    token = os.environ.get("VULTR_API_KEY", "").strip()
    if not token:
        print("VULTR_API_KEY is required through the environment", file=sys.stderr)
        return 2
    try:
        max_monthly_cost = Decimal(str(args.max_monthly_cost))
    except (InvalidOperation, ValueError):
        print("--max-monthly-cost must be a finite positive number", file=sys.stderr)
        return 2
    if not max_monthly_cost.is_finite() or max_monthly_cost <= 0:
        print("--max-monthly-cost must be a finite positive number", file=sys.stderr)
        return 2

    if args.cleanup_ledger:
        manifest = load_manifest(args.cleanup_ledger)
        if not manifest.marker.startswith("cloudmoo-db-e2e-"):
            print("cleanup manifest marker is not a managed-database E2E marker", file=sys.stderr)
            return 2
    else:
        marker = f"cloudmoo-db-e2e-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(8)}"
        path = args.ledger or Path(tempfile.gettempdir()) / f"cloudmoo-vultr-db-e2e-{marker.rsplit('-', 1)[-1]}.json"
        manifest = Manifest(path)
        manifest.state["marker"] = marker
        manifest.save()

    client = VultrHTTP(token, manifest)
    failure: BaseException | None = None
    try:
        if args.cleanup_ledger:
            print(json.dumps({"event": "database_cleanup_resume", "marker": manifest.marker, "ledger": str(manifest.path)}), flush=True)
            cleanup(client, manifest)
            manifest.state["status"] = "passed"
            manifest.save()
            print(json.dumps({"event": "database_cleanup_passed", "ledger": str(manifest.path)}), flush=True)
            return 0

        print(json.dumps({"event": "database_live_test_started", "marker": manifest.marker, "ledger": str(manifest.path)}), flush=True)
        account = client.request("GET", "account")
        if not isinstance(account, dict):
            raise LiveE2EError("Account preflight returned an invalid response")
        try:
            client.request("GET", "account/limits")
        except LiveE2EError as error:
            if "HTTP 404" not in str(error):
                raise
            manifest.event("optional_preflight_unavailable", endpoint="account/limits")

        databases = client.list_collection("databases", "databases")
        baseline_ids = sorted(str(item["id"]) for item in databases if object_id(item))
        if any(marker_matches(item, manifest.marker) for item in databases):
            raise LiveE2EError("Managed-database ownership marker collision detected")
        manifest.state["baseline"] = {
            "databases": {
                "ids": baseline_ids,
                "count": len(baseline_ids),
                "fingerprint": fingerprint_ids(baseline_ids),
            }
        }
        manifest.event("database_preflight_complete", existing_databases=len(databases))

        engine = str(args.engine).strip().lower()
        engine_version = str(args.engine_version).strip()
        if engine != "pg" or not engine_version:
            raise LiveE2EError("This disposable test currently permits only PostgreSQL with a non-empty version")
        region, plan, monthly_cost, _plans = choose_database_plan(client, args.region, engine, max_monthly_cost, manifest)
        manifest.state["selection"] = {
            "region": region,
            "engine": engine,
            "engine_version": engine_version,
            "plan": plan,
            "monthly_cost": str(monthly_cost),
            "max_monthly_cost": str(max_monthly_cost),
        }
        manifest.save()
        print(json.dumps({"event": "database_selection", "region": region, "engine": engine, "engine_version": engine_version, "plan": plan, "monthly_cost": str(monthly_cost)}), flush=True)

        payload = {
            "database_engine": engine,
            "database_engine_version": engine_version,
            "plan": plan,
            "region": region,
            "label": f"{manifest.marker}-database",
        }
        try:
            response = client.request("POST", "databases", payload=payload)
        except AmbiguousMutation:
            databases = client.list_collection("databases", "databases")
            database = find_owned(databases, manifest.marker, set(baseline_ids))
        else:
            database = response_object(response, ("database",))
            if not database or not object_id(database):
                databases = client.list_collection("databases", "databases")
                database = find_owned(databases, manifest.marker, set(baseline_ids))
        database_id = object_id(database)
        if not database_id or database_id in baseline_ids:
            raise CleanupSafetyError("Created managed-database ID could not be proven new")
        # Persist the exact returned ID before any further verification.
        manifest.own("database", database_id)
        manifest.state["resources"]["database"].update({"label": f"{manifest.marker}-database", "ownership_verified": False})
        manifest.save()
        database = get_database(client, database_id)
        assert_database_identity(database, database_id, manifest.marker, region, plan, engine, engine_version)
        manifest.state["resources"]["database"]["ownership_verified"] = True
        manifest.state["resources"]["database"]["state"] = "present"
        manifest.save()
        print(json.dumps({"event": "database_created", "id": short_id(database_id)}), flush=True)

        database = poll_database(
            client,
            database_id,
            "database_ready",
            lambda item: database_status(item) in DATABASE_READY_STATUSES,
            DATABASE_PROVISION_DEADLINE_SECONDS,
        )
        assert_database_identity(database, database_id, manifest.marker, region, plan, engine, engine_version)
        assert_database_runtime_details(database)
        manifest.state["resources"]["database"]["state"] = "ready"
        manifest.save()
        run_cloudmoo_database_assertions(token, manifest, database_id, region, plan, engine, engine_version)
        manifest.event("database_lifecycle_passed", database_id=short_id(database_id))
    except BaseException as error:
        failure = error
        manifest.state["failure"] = str(error)[:240]
        manifest.event("database_test_failed", error_type=type(error).__name__)
        print(json.dumps({"event": "database_live_test_failed", "error_type": type(error).__name__, "message": str(error)[:240]}), file=sys.stderr, flush=True)
    finally:
        try:
            cleanup(client, manifest)
        except BaseException as error:
            manifest.state["cleanup_failure"] = str(error)[:240]
            manifest.event("database_cleanup_failed", error_type=type(error).__name__)
            print(json.dumps({"event": "database_cleanup_failed", "error_type": type(error).__name__, "message": str(error)[:240], "ledger": str(manifest.path)}), file=sys.stderr, flush=True)
            failure = failure or error
        manifest.state["status"] = "passed" if failure is None else "failed"
        manifest.save()

    if failure is not None:
        print(json.dumps({"event": "database_live_test_failed_final", "ledger": str(manifest.path), "status": manifest.state["status"]}), file=sys.stderr, flush=True)
        return 130 if isinstance(failure, KeyboardInterrupt) else 1
    print(json.dumps({"event": "database_live_test_passed", "ledger": str(manifest.path)}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
