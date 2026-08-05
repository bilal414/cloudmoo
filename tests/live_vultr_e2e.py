"""Disposable, ownership-safe live Vultr lifecycle test.

This is intentionally separate from the production Vultr adapter.  CloudMoo's
provider client is GET-only; this runner uses a short-lived raw HTTP client for
the explicitly requested create/action/delete operations, while all inventory
and monitoring assertions go through CloudMoo's production code.

Usage (never put the token in a file or test fixture)::

    VULTR_API_KEY='...' .venv/bin/python tests/live_vultr_e2e.py

The runner leaves a 0600 manifest in the system temporary directory.  The
manifest contains only the random marker, baseline IDs, exact owned IDs, and
cleanup state.  It never contains the API token or provider response bodies.
If the process is interrupted, rerun with ``--cleanup-ledger PATH`` using the
same environment variable to clean only the exact IDs in that manifest.
"""

from __future__ import annotations

import argparse
import hashlib
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
from typing import Any, Iterable
from urllib.parse import parse_qs, urlsplit

import requests


# Running ``python tests/live_vultr_e2e.py`` sets sys.path[0] to ``tests``;
# add the repository root explicitly so the CloudMoo Django packages are
# exercised exactly as they are when the project is run from its root.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

API_BASE = "https://api.vultr.com/v2"
REQUEST_TIMEOUT_SECONDS = 30
MAX_GET_RETRIES = 3
MAX_PAGES = 100
CREATE_DEADLINE_SECONDS = 600
ACTION_DEADLINE_SECONDS = 300
POLL_SECONDS = 5
DEFAULT_MAX_MONTHLY_COST = Decimal("10")

RUN_STARTED = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
DEFAULT_MARKER = f"cloudmoo-e2e-{RUN_STARTED}-{secrets.token_hex(8)}"


class LiveE2EError(RuntimeError):
    """A safe, credential-free live test failure."""


class AmbiguousMutation(LiveE2EError):
    """A mutation may have succeeded even though its response was unknown."""


class CleanupSafetyError(LiveE2EError):
    """The runner cannot prove that a remote object belongs to this run."""


def short_id(value: Any) -> str:
    text = str(value)
    return text if len(text) <= 12 else f"{text[:8]}..."


def safe_decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed >= 0 else None


def object_id(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in ("id", "uuid", "rule_id"):
        candidate = value.get(key)
        if isinstance(candidate, (str, int)) and str(candidate).strip():
            return str(candidate).strip()
    return None


def response_object(payload: Any, keys: Iterable[str]) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return payload if object_id(payload) else None


def marker_matches(value: Any, marker: str) -> bool:
    """Match only supported ownership fields, never arbitrary raw bodies."""

    if not isinstance(value, dict):
        return False
    for key in ("label", "hostname", "description", "name", "notes"):
        if marker in str(value.get(key) or ""):
            return True
    tags = value.get("tags")
    if isinstance(tags, list) and any(marker in str(tag) for tag in tags):
        return True
    return False


def fingerprint_ids(ids: Iterable[str]) -> str:
    joined = "\n".join(sorted(str(item) for item in ids))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


class Manifest:
    """Crash-resilient ownership ledger with no credential or raw payload."""

    def __init__(self, path: Path, state: dict[str, Any] | None = None) -> None:
        self.path = path
        self.state = state or {
            "schema": 1,
            "marker": DEFAULT_MARKER,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "baseline": {},
            "resources": {},
            "events": [],
            "status": "running",
        }

    @property
    def marker(self) -> str:
        return str(self.state["marker"])

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            os.chmod(temporary, 0o600)
            json.dump(self.state, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
        os.chmod(self.path, 0o600)

    def event(self, name: str, **fields: Any) -> None:
        safe_fields = {key: value for key, value in fields.items() if key not in {"body", "headers", "token"}}
        self.state["events"].append({"at": datetime.now(timezone.utc).isoformat(), "event": name, **safe_fields})
        self.save()

    def own(self, kind: str, resource_id: str, *, parent_id: str | None = None) -> None:
        self.state["resources"][kind] = {
            "id": str(resource_id),
            "parent_id": parent_id,
            "state": "present",
        }
        self.event("owned_resource_recorded", resource_type=kind, resource_id=short_id(resource_id), parent_id=short_id(parent_id) if parent_id else None)

    def mark_deleted(self, kind: str) -> None:
        resource = self.state["resources"].get(kind)
        if resource:
            resource["state"] = "absent"
        self.event("owned_resource_deleted", resource_type=kind, resource_id=short_id(resource.get("id")) if resource else None)


class VultrHTTP:
    def __init__(self, token: str, manifest: Manifest) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )
        self.manifest = manifest

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        method = method.upper()
        safe_get = method == "GET"
        for attempt in range(MAX_GET_RETRIES + 1 if safe_get else 1):
            try:
                response = self.session.request(
                    method,
                    f"{API_BASE}/{path.lstrip('/')}",
                    params=params,
                    json=payload,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            except requests.RequestException as error:
                if safe_get and attempt < MAX_GET_RETRIES:
                    time.sleep(min(2**attempt, 8))
                    continue
                if safe_get:
                    raise LiveE2EError(f"GET {path} failed") from error
                raise AmbiguousMutation(f"{method} {path} outcome is unknown") from error

            status = response.status_code
            self.manifest.event("http", method=method, path=path, status=status)
            if safe_get and status in (429, 500, 502, 503, 504) and attempt < MAX_GET_RETRIES:
                retry_after = safe_decimal(response.headers.get("Retry-After"))
                time.sleep(min(float(retry_after or Decimal(2**attempt)), 15))
                continue
            if safe_get and status in (429, 500, 502, 503, 504):
                raise LiveE2EError(f"GET {path} returned HTTP {status}")
            if status < 200 or status >= 300:
                if not safe_get and (status >= 500 or status == 429):
                    raise AmbiguousMutation(f"{method} {path} outcome is unknown (HTTP {status})")
                raise LiveE2EError(f"{method} {path} returned HTTP {status}")
            if status == 204 or not response.content:
                return {}
            try:
                parsed = response.json()
            except ValueError as error:
                raise LiveE2EError(f"{method} {path} returned malformed JSON") from error
            if not isinstance(parsed, dict):
                raise LiveE2EError(f"{method} {path} returned an invalid envelope")
            return parsed
        raise LiveE2EError(f"GET {path} failed")

    def list_collection(self, path: str, key: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(MAX_PAGES):
            params: dict[str, Any] = {"per_page": 500}
            if cursor:
                if cursor in seen_cursors:
                    raise LiveE2EError(f"GET {path} returned a repeated cursor")
                seen_cursors.add(cursor)
                params["cursor"] = cursor
            payload = self.request("GET", path, params=params)
            page = payload.get(key)
            if not isinstance(page, list) or any(not isinstance(item, dict) for item in page):
                raise LiveE2EError(f"GET {path} returned an invalid {key} collection")
            items.extend(page)
            meta = payload.get("meta")
            links = meta.get("links") if isinstance(meta, dict) else None
            next_cursor = links.get("next") if isinstance(links, dict) else None
            if not next_cursor:
                return items
            if isinstance(next_cursor, str) and next_cursor.startswith("http"):
                next_cursor = parse_qs(urlsplit(next_cursor).query).get("cursor", [None])[0]
            if not isinstance(next_cursor, str) or not next_cursor.strip():
                raise LiveE2EError(f"GET {path} returned an invalid pagination cursor")
            cursor = next_cursor
        raise LiveE2EError(f"GET {path} exceeded the page limit")


def choose_resources(client: VultrHTTP, args: argparse.Namespace, manifest: Manifest) -> tuple[str, str, int, Decimal]:
    account = client.request("GET", "account")
    if not isinstance(account, dict):
        raise LiveE2EError("Account preflight returned an invalid response")
    try:
        client.request("GET", "account/limits")
    except LiveE2EError as error:
        # This endpoint is recommended by older Vultr API references but is
        # not exposed by every current account.  A 404 is an explicit,
        # non-mutating capability result; other failures remain fail-closed.
        if "HTTP 404" not in str(error):
            raise
        manifest.event("optional_preflight_unavailable", endpoint="account/limits")
    regions = client.list_collection("regions", "regions")
    plans = client.list_collection("plans", "plans")
    os_images = client.list_collection("os", "os")
    instances = client.list_collection("instances", "instances")
    firewalls = client.list_collection("firewalls", "firewall_groups")

    baseline = {
        "instances": sorted(str(item.get("id")) for item in instances if object_id(item)),
        "firewall_groups": sorted(str(item.get("id")) for item in firewalls if object_id(item)),
    }
    for kind, records in (("instance", instances), ("firewall_group", firewalls)):
        if any(marker_matches(item, manifest.marker) for item in records):
            raise LiveE2EError(f"Ownership marker collision in existing {kind} resources")
    manifest.state["baseline"] = {
        key: {"ids": values, "count": len(values), "fingerprint": fingerprint_ids(values)}
        for key, values in baseline.items()
    }
    manifest.event("preflight_complete", instances=len(instances), firewall_groups=len(firewalls), regions=len(regions), plans=len(plans), os_images=len(os_images))

    active_regions = [item for item in regions if item.get("active") is not False and isinstance(item.get("id"), str)]
    preferred = ["ams", "ewr", "ord", "dfw", "atl"]
    region = next((item["id"] for preferred_id in preferred for item in active_regions if item["id"] == preferred_id), None)
    region = region or active_regions[0]["id"] if active_regions else None
    if not region:
        raise LiveE2EError("No active Vultr region is available")
    if args.region:
        region = args.region
        if not any(item["id"] == region for item in active_regions):
            raise LiveE2EError("Requested region is not active")

    availability = client.request("GET", f"regions/{region}/availability", params={"type": "vc2"})
    available_plans = availability.get("available_plans")
    if not isinstance(available_plans, list) or any(not isinstance(item, str) for item in available_plans):
        raise LiveE2EError("Region availability returned an invalid plan list")
    available = set(available_plans)
    max_monthly = Decimal(str(args.max_monthly_cost))
    if args.plan:
        plan_id = args.plan
        matching = next((item for item in plans if item.get("id") == plan_id), None)
        monthly = safe_decimal(matching.get("monthly_cost")) if matching else None
        if monthly is None and matching:
            hourly = safe_decimal(matching.get("hourly_cost"))
            monthly = hourly * Decimal(730) if hourly is not None else None
        if plan_id not in available or monthly is None or monthly > max_monthly:
            raise LiveE2EError("Requested plan is unavailable or over the configured cost ceiling")
    else:
        selected = [
            (safe_decimal(item.get("monthly_cost")) or (safe_decimal(item.get("hourly_cost")) or Decimal(0)) * Decimal(730), str(item.get("id")))
            for item in plans
            if isinstance(item.get("id"), str) and item.get("id", "").startswith("vc2-") and item.get("id") in available and item.get("active") is not False
        ]
        selected = [(cost, plan_id) for cost, plan_id in selected if cost > 0 and cost <= max_monthly]
        if not selected:
            raise LiveE2EError("No active shared-CPU plan is available under the configured cost ceiling")
        monthly, plan_id = min(selected)

    if args.os_id:
        os_id = int(args.os_id)
        os_image = next((item for item in os_images if item.get("id") == os_id), None)
    else:
        os_image = next(
            (
                item
                for item in os_images
                if item.get("active") is not False
                and "ubuntu" in str(item.get("name", "")).lower()
                and "arm" not in str(item.get("name", "")).lower()
                and str(item.get("arch", "")).lower() not in {"arm", "arm64", "aarch64"}
            ),
            None,
        )
    if not isinstance(os_image, dict) or not isinstance(os_image.get("id"), int):
        raise LiveE2EError("No active x64 Ubuntu OS image is available")
    if args.plan:
        monthly = safe_decimal(next(item for item in plans if item.get("id") == args.plan).get("monthly_cost")) or Decimal(0)
    return region, plan_id, int(os_image["id"]), monthly


def find_owned(records: list[dict[str, Any]], marker: str, baseline_ids: set[str]) -> dict[str, Any]:
    matches = [item for item in records if marker_matches(item, marker) and object_id(item) not in baseline_ids]
    if len(matches) != 1:
        raise AmbiguousMutation(f"Expected exactly one new resource for marker; found {len(matches)}")
    return matches[0]


def poll_until(label: str, fetch: Any, predicate: Any, *, deadline: int) -> dict[str, Any]:
    end = time.monotonic() + deadline
    last_status: str | None = None
    while time.monotonic() < end:
        item = fetch()
        status = str(item.get("power_status") or item.get("server_status") or item.get("status") or "unknown")
        if status != last_status:
            print(json.dumps({"event": label, "status": status}, sort_keys=True), flush=True)
            last_status = status
        if predicate(item):
            return item
        time.sleep(POLL_SECONDS)
    raise LiveE2EError(f"Timed out waiting for {label}")


def get_instance(client: VultrHTTP, instance_id: str) -> dict[str, Any]:
    payload = client.request("GET", f"instances/{instance_id}")
    item = response_object(payload, ("instance",))
    if not item:
        raise LiveE2EError("Instance detail response was invalid")
    return item


def get_firewall(client: VultrHTTP, firewall_id: str) -> dict[str, Any]:
    payload = client.request("GET", f"firewalls/{firewall_id}")
    item = response_object(payload, ("firewall_group", "firewall"))
    if not item:
        raise LiveE2EError("Firewall detail response was invalid")
    return item


def get_rules(client: VultrHTTP, firewall_id: str) -> list[dict[str, Any]]:
    payload = client.request("GET", f"firewalls/{firewall_id}/rules")
    if "firewall_rules" in payload:
        rules = payload.get("firewall_rules")
    else:
        rules = payload.get("rules")
    if not isinstance(rules, list) or any(not isinstance(item, dict) for item in rules):
        raise LiveE2EError("Firewall rules response was invalid")
    return rules


def verify_owned_detail(client: VultrHTTP, kind: str, resource: dict[str, Any], marker: str) -> bool:
    resource_id = str(resource["id"])
    if kind == "instance":
        try:
            item = get_instance(client, resource_id)
        except LiveE2EError as error:
            if "HTTP 404" in str(error):
                return False
            raise
    elif kind == "firewall_group":
        try:
            item = get_firewall(client, resource_id)
        except LiveE2EError as error:
            if "HTTP 404" in str(error):
                return False
            raise
    else:
        parent_id = str(resource["parent_id"])
        rules = get_rules(client, parent_id)
        item = next((rule for rule in rules if object_id(rule) == resource_id), None)
        if item is None:
            return False
    if object_id(item) != resource_id or not marker_matches(item, marker):
        raise CleanupSafetyError(f"Refusing to delete an unowned {kind} resource")
    return True


def delete_owned(client: VultrHTTP, manifest: Manifest, kind: str, path: str) -> None:
    resource = manifest.state["resources"].get(kind)
    if not resource or resource.get("state") == "absent":
        return
    resource_id = str(resource["id"])
    if not verify_owned_detail(client, kind, resource, manifest.marker):
        manifest.mark_deleted(kind)
        return
    if kind == "instance":
        # Vultr may reject DELETE with 409 while the instance is running or
        # while its control-plane lock is settling.  Stop only this exact
        # ledgered ID, then wait for an unlocked stopped state.
        current = get_instance(client, resource_id)
        power_status = str(current.get("power_status") or current.get("server_status") or current.get("status") or "").lower()
        if power_status not in {"stopped", "off", "powered_off", "inactive"}:
            client.request("POST", "instances/halt", payload={"instance_ids": [resource_id]})
            poll_until(
                "cleanup_instance_stopped",
                lambda: get_instance(client, resource_id),
                lambda item: str(item.get("power_status") or item.get("status") or "").lower() in {"stopped", "off", "powered_off", "inactive"},
                deadline=ACTION_DEADLINE_SECONDS,
            )
    try:
        client.request("DELETE", path)
    except AmbiguousMutation:
        # Resolve the exact ID before deciding whether any retry is safe.
        if verify_owned_detail(client, kind, resource, manifest.marker):
            client.request("DELETE", path)
    except LiveE2EError as error:
        if kind != "instance" or "HTTP 409" not in str(error):
            raise
        # A 409 after the exact instance is stopped means the provider lock
        # is still settling.  Poll the same ID and retry one exact DELETE.
        poll_until(
            "cleanup_instance_unlocked",
            lambda: get_instance(client, resource_id),
            lambda item: str(item.get("server_status") or "").lower() not in {"locked", "busy", "processing"},
            deadline=ACTION_DEADLINE_SECONDS,
        )
        client.request("DELETE", path)
    manifest.event("delete_requested", resource_type=kind, resource_id=short_id(resource_id))
    end = time.monotonic() + ACTION_DEADLINE_SECONDS
    while time.monotonic() < end:
        if not verify_owned_detail(client, kind, resource, manifest.marker):
            manifest.mark_deleted(kind)
            return
        time.sleep(POLL_SECONDS)
    raise LiveE2EError(f"Timed out deleting owned {kind} {short_id(resource_id)}")


def cleanup(client: VultrHTTP, manifest: Manifest) -> None:
    # Reverse dependency order.  Only IDs already written to the manifest may
    # reach a DELETE endpoint, and every object is revalidated by marker first.
    delete_owned(
        client,
        manifest,
        "firewall_rule",
        f"firewalls/{manifest.state['resources'].get('firewall_group', {}).get('id')}/rules/{manifest.state['resources'].get('firewall_rule', {}).get('id')}",
    )
    delete_owned(
        client,
        manifest,
        "instance",
        f"instances/{manifest.state['resources'].get('instance', {}).get('id')}",
    )
    delete_owned(
        client,
        manifest,
        "firewall_group",
        f"firewalls/{manifest.state['resources'].get('firewall_group', {}).get('id')}",
    )

    baseline = manifest.state.get("baseline", {})
    current_instances = client.list_collection("instances", "instances")
    current_firewalls = client.list_collection("firewalls", "firewall_groups")
    current_instance_ids = sorted(str(item["id"]) for item in current_instances if object_id(item))
    current_firewall_ids = sorted(str(item["id"]) for item in current_firewalls if object_id(item))
    owned_ids = {
        str(resource.get("id"))
        for resource in manifest.state.get("resources", {}).values()
        if resource.get("id")
    }
    if owned_ids.intersection(current_instance_ids + current_firewall_ids):
        raise CleanupSafetyError("An owned resource ID is still listed after cleanup")
    if any(marker_matches(item, manifest.marker) for item in current_instances + current_firewalls):
        raise CleanupSafetyError("An ownership marker is still listed after cleanup")

    baseline_instance_ids = set(baseline.get("instances", {}).get("ids", []))
    baseline_firewall_ids = set(baseline.get("firewall_groups", {}).get("ids", []))
    instance_drift = (len(baseline_instance_ids - set(current_instance_ids)), len(set(current_instance_ids) - baseline_instance_ids))
    firewall_drift = (len(baseline_firewall_ids - set(current_firewall_ids)), len(set(current_firewall_ids) - baseline_firewall_ids))
    if instance_drift != (0, 0) or firewall_drift != (0, 0):
        # Concurrent/provider-side changes to unrelated resources are audited
        # but never acted upon.  Ownership and marker checks above remain the
        # authoritative cleanup gate for this run.
        manifest.event("unowned_baseline_drift", instance_removed=instance_drift[0], instance_added=instance_drift[1], firewall_removed=firewall_drift[0], firewall_added=firewall_drift[1])
    manifest.event("cleanup_verified", instances=len(current_instance_ids), firewall_groups=len(current_firewall_ids))


def run_cloudmoo_assertions(token: str, manifest: Manifest, instance_id: str, firewall_id: str) -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "app_cloudmoo_com.settings")
    import django

    django.setup()
    from apps.console.cloud.models import CloudValidationTransientError
    from apps.console.cloud.vultr.models import CoreVultrAccount
    from apps.console.cloud.vultr.resources_base import VultrReadOnlyClient
    from apps.console.cloud.vultr.resources_data_network import collect_vultr_inventory
    from apps.monitoring.checks.vultr import check_vultr_server_status

    # This object is never saved.  It verifies the existing account validation
    # path without putting the live token in the database.
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
    client = VultrReadOnlyClient(token, timeout=REQUEST_TIMEOUT_SECONDS, max_retries=1)
    detail = client.get_json(f"instances/{instance_id}").get("instance")
    if not isinstance(detail, dict) or str(detail.get("id")) != instance_id or not marker_matches(detail, manifest.marker):
        raise LiveE2EError("CloudMoo instance detail assertion failed")
    status, metadata = check_vultr_server_status(instance_id, token)
    if str(status).lower() in {"error", "not_found", "invalid_access_token"} or not isinstance(metadata, dict):
        raise LiveE2EError("CloudMoo Vultr status check failed")
    inventory = collect_vultr_inventory(
        SimpleNamespace(access_token=token),
        resources=["firewall", "firewall_rule"],
        client=client,
    )
    firewall_ids = {str(item["unique_id"]) for item in inventory.get("firewall", [])}
    rule_ids = {str(item["unique_id"]) for item in inventory.get("firewall_rule", [])}
    if firewall_id not in firewall_ids or not rule_ids:
        raise LiveE2EError("CloudMoo Vultr firewall inventory assertion failed")
    manifest.event("cloudmoo_assertions_passed", status=str(status), firewall_records=len(firewall_ids), firewall_rule_records=len(rule_ids))


def load_manifest(path: Path) -> Manifest:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise LiveE2EError("Unable to read the cleanup manifest") from error
    if not isinstance(state, dict) or state.get("schema") != 1 or not state.get("marker"):
        raise LiveE2EError("Cleanup manifest is invalid")
    return Manifest(path, state)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cleanup-ledger", type=Path, help="clean exact IDs from a previous interrupted run")
    parser.add_argument("--ledger", type=Path, help="manifest path; defaults to the system temporary directory")
    parser.add_argument("--region", help="optional region override after active-region validation")
    parser.add_argument("--plan", help="optional plan override after availability and cost validation")
    parser.add_argument("--os-id", type=int, help="optional OS ID override after /os validation")
    parser.add_argument("--max-monthly-cost", default=str(DEFAULT_MAX_MONTHLY_COST), help="maximum monthly plan price")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    token = os.environ.get("VULTR_API_KEY", "").strip()
    if not token:
        print("VULTR_API_KEY is required through the environment", file=sys.stderr)
        return 2
    if args.cleanup_ledger:
        manifest = load_manifest(args.cleanup_ledger)
    else:
        ledger_path = args.ledger or Path(tempfile.gettempdir()) / f"cloudmoo-vultr-e2e-{manifest_marker_for_path()}.json"
        manifest = Manifest(ledger_path)
        manifest.save()

    client = VultrHTTP(token, manifest)
    failure: BaseException | None = None
    try:
        if args.cleanup_ledger:
            print(json.dumps({"event": "cleanup_resume", "marker": manifest.marker, "ledger": str(manifest.path)}), flush=True)
            cleanup(client, manifest)
            manifest.state["status"] = "passed"
            manifest.save()
            print(json.dumps({"event": "cleanup_passed", "ledger": str(manifest.path)}), flush=True)
            return 0

        print(json.dumps({"event": "live_test_started", "marker": manifest.marker, "ledger": str(manifest.path)}), flush=True)
        region, plan, os_id, monthly = choose_resources(client, args, manifest)
        manifest.state["selection"] = {"region": region, "plan": plan, "os_id": os_id, "monthly_cost": str(monthly)}
        manifest.save()
        print(json.dumps({"event": "selection", "region": region, "plan": plan, "os_id": os_id, "monthly_cost": str(monthly)}), flush=True)

        firewall_payload = {"description": f"{manifest.marker}-firewall"}
        try:
            firewall_response = client.request("POST", "firewalls", payload=firewall_payload)
        except AmbiguousMutation:
            groups = client.list_collection("firewalls", "firewall_groups")
            firewall = find_owned(groups, manifest.marker, set(manifest.state["baseline"]["firewall_groups"]["ids"]))
        else:
            firewall = response_object(firewall_response, ("firewall_group", "firewall"))
            if not firewall or not object_id(firewall):
                groups = client.list_collection("firewalls", "firewall_groups")
                firewall = find_owned(groups, manifest.marker, set(manifest.state["baseline"]["firewall_groups"]["ids"]))
        firewall_id = object_id(firewall)
        if not firewall_id or firewall_id in manifest.state["baseline"]["firewall_groups"]["ids"]:
            raise CleanupSafetyError("Created firewall group could not be proven to be owned")
        manifest.own("firewall_group", firewall_id)
        firewall = get_firewall(client, firewall_id)
        if not marker_matches(firewall, manifest.marker):
            raise CleanupSafetyError("Created firewall group marker verification failed")
        print(json.dumps({"event": "firewall_group_created", "id": short_id(firewall_id)}), flush=True)

        # Vultr's current API requires the subnet/netmask pair.  ``source``
        # may remain empty so the API derives the CIDR from those fields.
        rule_payload = {
            "ip_type": "v4",
            "protocol": "tcp",
            "port": "22",
            "subnet": "0.0.0.0",
            "subnet_size": 0,
            "source": "",
            "notes": f"{manifest.marker}-rule",
        }
        try:
            rule_response = client.request("POST", f"firewalls/{firewall_id}/rules", payload=rule_payload)
        except AmbiguousMutation:
            rules = get_rules(client, firewall_id)
            rule = find_owned(rules, manifest.marker, set())
        else:
            rule = response_object(rule_response, ("rule", "firewall_rule"))
            if not rule or not object_id(rule):
                rules = get_rules(client, firewall_id)
                rule = find_owned(rules, manifest.marker, set())
        rule_id = object_id(rule)
        if not rule_id:
            raise CleanupSafetyError("Created firewall rule could not be proven to be owned")
        manifest.own("firewall_rule", rule_id, parent_id=firewall_id)
        if not marker_matches(rule, manifest.marker):
            rules = get_rules(client, firewall_id)
            rule = next((item for item in rules if marker_matches(item, manifest.marker)), None)
            rule_id = object_id(rule)
        if not rule_id or not marker_matches(rule, manifest.marker):
            raise CleanupSafetyError("Created firewall rule could not be proven to be owned")
        print(json.dumps({"event": "firewall_rule_created", "id": short_id(rule_id), "parent_id": short_id(firewall_id)}), flush=True)

        instance_payload = {
            "region": region,
            "plan": plan,
            "os_id": os_id,
            "label": f"{manifest.marker}-instance",
            "hostname": f"{manifest.marker}-instance",
            "tags": [manifest.marker],
        }
        try:
            instance_response = client.request("POST", "instances", payload=instance_payload)
        except AmbiguousMutation:
            instances = client.list_collection("instances", "instances")
            instance = find_owned(instances, manifest.marker, set(manifest.state["baseline"]["instances"]["ids"]))
        else:
            instance = response_object(instance_response, ("instance",))
            if not instance or not object_id(instance):
                instances = client.list_collection("instances", "instances")
                instance = find_owned(instances, manifest.marker, set(manifest.state["baseline"]["instances"]["ids"]))
        instance_id = object_id(instance)
        if not instance_id or instance_id in manifest.state["baseline"]["instances"]["ids"]:
            raise CleanupSafetyError("Created instance could not be proven to be owned")
        manifest.own("instance", instance_id)
        instance = get_instance(client, instance_id)
        if not marker_matches(instance, manifest.marker):
            raise CleanupSafetyError("Created instance marker verification failed")
        print(json.dumps({"event": "instance_created", "id": short_id(instance_id)}), flush=True)

        poll_until("instance_ready", lambda: get_instance(client, instance_id), lambda item: str(item.get("power_status") or item.get("server_status") or item.get("status")).lower() in {"running", "active", "available"}, deadline=CREATE_DEADLINE_SECONDS)
        run_cloudmoo_assertions(token, manifest, instance_id, firewall_id)

        client.request("POST", "instances/halt", payload={"instance_ids": [instance_id]})
        poll_until("instance_stopped", lambda: get_instance(client, instance_id), lambda item: str(item.get("power_status") or item.get("server_status") or item.get("status")).lower() in {"stopped", "off", "powered_off", "inactive"}, deadline=ACTION_DEADLINE_SECONDS)
        run_cloudmoo_assertions(token, manifest, instance_id, firewall_id)
        client.request("POST", "instances/start", payload={"instance_ids": [instance_id]})
        poll_until("instance_started", lambda: get_instance(client, instance_id), lambda item: str(item.get("power_status") or item.get("server_status") or item.get("status")).lower() in {"running", "active", "available"}, deadline=ACTION_DEADLINE_SECONDS)
        run_cloudmoo_assertions(token, manifest, instance_id, firewall_id)
        manifest.event("lifecycle_passed", instance_id=short_id(instance_id))
    except BaseException as error:
        failure = error
        manifest.state["failure"] = str(error)[:240]
        manifest.event("test_failed", error_type=type(error).__name__)
        print(json.dumps({"event": "live_test_failed", "error_type": type(error).__name__, "message": str(error)[:240]}), file=sys.stderr, flush=True)
    finally:
        try:
            cleanup(client, manifest)
        except BaseException as error:
            manifest.state["cleanup_failure"] = str(error)[:240]
            manifest.event("cleanup_failed", error_type=type(error).__name__)
            print(json.dumps({"event": "cleanup_failed", "error_type": type(error).__name__, "message": str(error)[:240], "ledger": str(manifest.path)}), file=sys.stderr, flush=True)
            failure = failure or error
        manifest.state["status"] = "passed" if failure is None else "failed"
        manifest.save()

    if failure is not None:
        print(json.dumps({"event": "live_test_failed_final", "ledger": str(manifest.path), "status": manifest.state["status"]}), file=sys.stderr, flush=True)
        return 130 if isinstance(failure, KeyboardInterrupt) else 1
    print(json.dumps({"event": "live_test_passed", "ledger": str(manifest.path)}), flush=True)
    return 0


def manifest_marker_for_path() -> str:
    return f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"


if __name__ == "__main__":
    raise SystemExit(main())
