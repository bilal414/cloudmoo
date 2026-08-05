"""Ownership-safe live Hetzner Cloud server E2E test.

Normal CloudMoo Hetzner inventory and monitoring are GET-only.  This runner
uses a separate short-lived HTTP client for one explicitly marked test Server,
then exercises the production validation, legacy Server inventory, detail
status, metrics, and expanded reference-data paths.

The token is accepted only through ``HETZNER_API_TOKEN`` and never enters the
ledger or logs.  A monthly cost ceiling is mandatory::

    HETZNER_API_TOKEN='...' .venv/bin/python tests/live_hetzner_e2e.py \
        --max-monthly-cost 10

If interrupted, rerun with the printed manifest path and
``--cleanup-ledger``.  Cleanup uses only the exact Server ID already recorded
in that manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

import requests


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


API_BASE = "https://api.hetzner.cloud/v1"
REQUEST_TIMEOUT_SECONDS = 30
MAX_GET_RETRIES = 3
MAX_PAGES = 100
POLL_SECONDS = 5
PROVISION_DEADLINE_SECONDS = 900
ACTION_DEADLINE_SECONDS = 300
CLEANUP_DEADLINE_SECONDS = 600
DEFAULT_OWNER_LABEL = "cloudmoo-e2e"
DEFAULT_MAX_MONTHLY_COST = Decimal("10")
RUN_PREFIX = "cloudmoo-hetzner-e2e-"

SERVER_READY_STATUSES = frozenset({"running"})
SERVER_STOPPED_STATUSES = frozenset({"off", "stopped", "inactive"})
ACTION_SUCCESS_STATUSES = frozenset({"success", "succeeded", "complete", "completed"})
ACTION_FAILURE_STATUSES = frozenset({"error", "failed", "failure"})


class LiveE2EError(RuntimeError):
    """Credential-free, bounded live-test failure."""


class AmbiguousMutation(LiveE2EError):
    """A mutation may have succeeded despite an unknown client outcome."""


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
    candidate = value.get("id")
    if isinstance(candidate, (str, int)) and not isinstance(candidate, bool) and str(candidate).strip():
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


def fingerprint_ids(ids: Iterable[str]) -> str:
    joined = "\n".join(sorted(str(item) for item in ids))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def marker_matches(value: Any, marker: str) -> bool:
    if not isinstance(value, dict):
        return False
    if marker in str(value.get("name") or ""):
        return True
    labels = value.get("labels")
    return isinstance(labels, dict) and any(marker in str(item) for item in labels.values())


def safe_error_summary(response: requests.Response) -> str:
    """Extract only provider error codes and field names, never messages/bodies."""

    try:
        payload = response.json()
    except ValueError:
        return ""
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return ""
    code = error.get("code")
    details = error.get("details")
    fields = details.get("fields") if isinstance(details, dict) else None
    field_names = sorted(
        str(item.get("name"))
        for item in fields
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    ) if isinstance(fields, list) else []
    parts = []
    if isinstance(code, str) and code:
        parts.append(f"code={code}")
    if field_names:
        parts.append(f"fields={','.join(field_names)}")
    message = error.get("message")
    if isinstance(message, str) and message:
        # Keep only a short diagnostic sentence; never persist a raw provider
        # body or characters that could carry an injected log line.
        safe_message = re.sub(r"[^A-Za-z0-9_.:/ -]", "", message)[:180].strip()
        if safe_message:
            parts.append(f"message={safe_message}")
    return " ".join(parts)


class Manifest:
    """Atomic 0600 ledger containing no credentials or raw provider bodies."""

    def __init__(self, path: Path, state: dict[str, Any] | None = None) -> None:
        self.path = path
        self.state = state or {
            "schema": 1,
            "marker": f"{RUN_PREFIX}{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(8)}",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "baseline": {},
            "selection": {},
            "resources": {},
            "actions": [],
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
        safe_fields = {
            key: value
            for key, value in fields.items()
            if key not in {"body", "headers", "token", "password", "root_password"}
        }
        self.state["events"].append({"at": datetime.now(timezone.utc).isoformat(), "event": name, **safe_fields})
        self.save()

    def own(self, kind: str, resource_id: str) -> None:
        self.state["resources"][kind] = {
            "id": str(resource_id),
            "state": "present",
            "ownership_verified": False,
        }
        self.event("owned_resource_recorded", resource_type=kind, resource_id=short_id(resource_id))

    def record_action(self, action_id: str, command: str, resource_id: str) -> None:
        self.state["actions"].append({
            "id": str(action_id),
            "command": command,
            "resource_id": str(resource_id),
            "state": "running",
        })
        self.event("owned_action_recorded", action_id=short_id(action_id), command=command, resource_id=short_id(resource_id))

    def action_state(self, action_id: str, state: str) -> None:
        for action in self.state["actions"]:
            if str(action.get("id")) == str(action_id):
                action["state"] = state
                break
        self.save()

    def mark_deleted(self, kind: str) -> None:
        resource = self.state["resources"].get(kind)
        if resource:
            resource["state"] = "absent"
        self.event("owned_resource_deleted", resource_type=kind, resource_id=short_id(resource.get("id")) if resource else None)


def load_manifest(path: Path) -> Manifest:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise LiveE2EError("Hetzner cleanup ledger could not be read") from error
    if not isinstance(data, dict) or data.get("schema") != 1 or not str(data.get("marker", "")).startswith(RUN_PREFIX):
        raise LiveE2EError("Hetzner cleanup ledger is invalid")
    return Manifest(path, data)


class HetznerHTTP:
    def __init__(self, token: str, manifest: Manifest) -> None:
        if not isinstance(token, str) or not token.strip():
            raise LiveE2EError("Hetzner API token is unavailable")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
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
        is_get = method == "GET"
        attempts = MAX_GET_RETRIES + 1 if is_get else 1
        for attempt in range(attempts):
            try:
                response = self.session.request(
                    method,
                    f"{API_BASE}/{path.lstrip('/')}",
                    params=params,
                    json=payload,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            except requests.RequestException as error:
                if is_get and attempt < MAX_GET_RETRIES:
                    time.sleep(min(2**attempt, 8))
                    continue
                if is_get:
                    raise LiveE2EError(f"GET {path} failed") from error
                raise AmbiguousMutation(f"{method} {path} outcome is unknown") from error

            status = response.status_code
            self.manifest.event("http", method=method, path=path, status=status)
            if is_get and status in {429, 500, 502, 503, 504} and attempt < MAX_GET_RETRIES:
                retry_after = safe_decimal(response.headers.get("Retry-After"))
                time.sleep(min(float(retry_after or Decimal(2**attempt)), 15))
                continue
            if is_get and status in {429, 500, 502, 503, 504}:
                raise LiveE2EError(f"GET {path} returned HTTP {status}")
            if status < 200 or status >= 300:
                if not is_get and (status >= 500 or status == 429):
                    raise AmbiguousMutation(f"{method} {path} outcome is unknown (HTTP {status})")
                summary = safe_error_summary(response)
                suffix = f" ({summary})" if summary else ""
                raise LiveE2EError(f"{method} {path} returned HTTP {status}{suffix}")
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

    def list_collection(
        self,
        path: str,
        key: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        seen_pages: set[int] = set()
        for _ in range(MAX_PAGES):
            if page in seen_pages:
                raise LiveE2EError(f"GET {path} returned a repeated page")
            seen_pages.add(page)
            page_params = dict(params or {})
            page_params.update({"page": page, "per_page": 50})
            payload = self.request("GET", path, params=page_params)
            page_items = payload.get(key)
            if not isinstance(page_items, list) or any(not isinstance(item, dict) for item in page_items):
                raise LiveE2EError(f"GET {path} returned an invalid {key} collection")
            items.extend(page_items)
            meta = payload.get("meta")
            if not isinstance(meta, dict):
                if len(page_items) < 50:
                    return items
                page += 1
                continue
            pagination = meta.get("pagination")
            if not isinstance(pagination, dict):
                raise LiveE2EError(f"GET {path} returned an invalid pagination response")
            next_page = pagination.get("next_page")
            if next_page in (None, ""):
                total = pagination.get("total_entries")
                if isinstance(total, int) and total > len(items):
                    raise LiveE2EError(f"GET {path} returned an incomplete pagination response")
                return items
            if isinstance(next_page, bool) or not isinstance(next_page, int) or next_page <= page:
                raise LiveE2EError(f"GET {path} returned an invalid pagination sequence")
            page = next_page
        raise LiveE2EError(f"GET {path} exceeded the page limit")


def action_from_response(payload: dict[str, Any]) -> dict[str, Any] | None:
    action = payload.get("action")
    return action if isinstance(action, dict) else None


def get_server(client: HetznerHTTP, server_id: str) -> dict[str, Any]:
    payload = client.request("GET", f"servers/{server_id}")
    server = response_object(payload, ("server",))
    if not server:
        raise LiveE2EError("Hetzner Server detail response was invalid")
    return server


def server_status(server: dict[str, Any]) -> str:
    value = server.get("status")
    return str(value).strip().lower().replace("-", "_").replace(" ", "_") if value else "unknown"


def assert_server_identity(server: dict[str, Any], server_id: str, manifest: Manifest) -> None:
    if object_id(server) != server_id or not marker_matches(server, manifest.marker):
        raise CleanupSafetyError("Hetzner Server ID or ownership marker verification failed")
    labels = server.get("labels")
    if not isinstance(labels, dict) or labels.get("cloudmoo.com/test-run") != manifest.marker:
        raise CleanupSafetyError("Hetzner Server test-run label verification failed")


def wait_server(
    client: HetznerHTTP,
    manifest: Manifest,
    server_id: str,
    predicate: Any,
    label: str,
    deadline: int,
) -> dict[str, Any]:
    end = time.monotonic() + deadline
    last: str | None = None
    while time.monotonic() < end:
        server = get_server(client, server_id)
        assert_server_identity(server, server_id, manifest)
        current = server_status(server)
        if current != last:
            print(json.dumps({"event": label, "status": current}, sort_keys=True), flush=True)
            last = current
        if predicate(server):
            return server
        time.sleep(POLL_SECONDS)
    raise LiveE2EError(f"Timed out waiting for {label}")


def get_action(client: HetznerHTTP, action_id: str) -> dict[str, Any]:
    payload = client.request("GET", f"actions/{action_id}")
    action = response_object(payload, ("action",))
    if not action or object_id(action) != str(action_id):
        raise LiveE2EError("Hetzner action detail response was invalid")
    return action


def wait_action(client: HetznerHTTP, manifest: Manifest, action_id: str, deadline: int) -> dict[str, Any]:
    end = time.monotonic() + deadline
    last: str | None = None
    while time.monotonic() < end:
        action = get_action(client, action_id)
        status = str(action.get("status") or "unknown").lower()
        if status != last:
            print(json.dumps({"event": "action_status", "action_id": short_id(action_id), "status": status}, sort_keys=True), flush=True)
            last = status
        if status in ACTION_SUCCESS_STATUSES:
            manifest.action_state(action_id, "success")
            return action
        if status in ACTION_FAILURE_STATUSES:
            manifest.action_state(action_id, "error")
            raise LiveE2EError("Hetzner action entered an error state")
        time.sleep(POLL_SECONDS)
    raise LiveE2EError(f"Timed out waiting for action {short_id(action_id)}")


def verify_owned_server(client: HetznerHTTP, manifest: Manifest, server_id: str) -> bool:
    try:
        server = get_server(client, server_id)
    except LiveE2EError as error:
        if "HTTP 404" in str(error):
            return False
        raise
    assert_server_identity(server, server_id, manifest)
    return True


def find_owned_server(records: list[dict[str, Any]], marker: str, baseline_ids: set[str]) -> dict[str, Any]:
    matches = [item for item in records if marker_matches(item, marker) and object_id(item) not in baseline_ids]
    if len(matches) != 1:
        raise AmbiguousMutation(f"Expected exactly one new Hetzner Server for marker; found {len(matches)}")
    return matches[0]


def select_server_configuration(
    client: HetznerHTTP,
    requested_location: str | None,
    max_monthly_cost: Decimal,
    manifest: Manifest,
) -> dict[str, Any]:
    locations = client.list_collection("locations", "locations")
    usable_locations = [item for item in locations if isinstance(item.get("id"), int) and isinstance(item.get("name"), str)]
    preferred = ["fsn1", "nbg1", "hel1", "ash", "hil", "sin"]
    if requested_location:
        location = next((item for item in usable_locations if item["name"] == requested_location), None)
        if location is None:
            raise LiveE2EError("Requested Hetzner location was not found")
    else:
        location = next((item for name in preferred for item in usable_locations if item["name"] == name), None)
        location = location or (usable_locations[0] if usable_locations else None)
    if location is None:
        raise LiveE2EError("No Hetzner location is available")

    server_types = client.list_collection("server_types", "server_types")
    candidates: list[tuple[Decimal, Decimal, int, dict[str, Any], dict[str, Any]]] = []
    for server_type in server_types:
        type_id = server_type.get("id")
        if isinstance(type_id, bool) or not isinstance(type_id, int) or server_type.get("deprecated") is True:
            continue
        supported_locations = server_type.get("locations")
        if isinstance(supported_locations, list) and supported_locations:
            supported_names = {
                item.get("name") if isinstance(item, dict) else str(item)
                for item in supported_locations
            }
            if location["name"] not in supported_names:
                continue
        prices = server_type.get("prices")
        if not isinstance(prices, list):
            continue
        price = next((item for item in prices if isinstance(item, dict) and item.get("location") == location["name"]), None)
        if not isinstance(price, dict):
            continue
        hourly = safe_decimal((price.get("price_hourly") or {}).get("gross"))
        monthly = safe_decimal((price.get("price_monthly") or {}).get("gross"))
        if hourly is None or monthly is None or monthly <= 0 or monthly > max_monthly_cost:
            continue
        candidates.append((hourly, monthly, type_id, server_type, price))
    if not candidates:
        raise LiveE2EError("No eligible Hetzner Server type is below the cost ceiling")
    # Hetzner's current catalog may advertise legacy shared types in a
    # location even when the create endpoint rejects them as unsupported.
    # Prefer the current general-purpose CX type when available, then use
    # the lowest-cost catalog candidate as a bounded fallback.
    preferred_types = {"cx23": 0, "cx22": 1, "cpx11": 2, "cpx21": 3}
    _, monthly, type_id, server_type, price = min(
        candidates,
        key=lambda item: (preferred_types.get(str(item[3].get("name")), 100), item[0], item[1], item[2]),
    )

    images = client.list_collection("images", "images", params={"type": "system"})
    ubuntu = [
        item for item in images
        if item.get("type") == "system"
        and item.get("status") in (None, "available")
        and item.get("deprecated") is not True
        and str(item.get("architecture") or "").lower() == "x86"
        and "ubuntu" in str(item.get("name") or "").lower()
    ]
    if not ubuntu:
        raise LiveE2EError("No current x86 Ubuntu system image is available")
    image = min(
        ubuntu,
        key=lambda item: (
            0 if str(item.get("name") or "").lower() == "ubuntu-24.04" else 1,
            str(item.get("name") or ""),
            -int(item.get("id") or 0),
        ),
    )
    selection = {
        "location_id": location["id"],
        "location_name": location["name"],
        "server_type_id": type_id,
        "server_type_name": server_type.get("name"),
        "hourly_gross": str(price.get("price_hourly", {}).get("gross")),
        "monthly_gross": str(monthly),
        "image_id": image.get("id"),
        "image_name": image.get("name"),
    }
    manifest.event("preflight_selection", **selection)
    return selection


def assert_no_secret_values(value: Any, token: str, *, depth: int = 0) -> None:
    if depth > 12:
        return
    if isinstance(value, str):
        if token and token in value:
            raise LiveE2EError("CloudMoo Hetzner metadata exposed the API token")
        return
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = "".join(character for character in str(key).lower() if character.isalnum())
            if any(part in normalized for part in ("password", "secret", "token", "privatekey", "credential", "authorization", "kubeconfig")):
                if child not in (None, "", "[REDACTED]"):
                    raise LiveE2EError("CloudMoo Hetzner metadata contained an unredacted secret field")
            assert_no_secret_values(child, token, depth=depth + 1)
    elif isinstance(value, (list, tuple)):
        for child in value:
            assert_no_secret_values(child, token, depth=depth + 1)


def run_cloudmoo_assertions(
    token: str,
    manifest: Manifest,
    server_id: str,
    selection: dict[str, Any],
    *,
    phase: str,
    check_metrics: bool = False,
) -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "app_cloudmoo_com.settings")
    import django

    django.setup()
    from apps.console.cloud.models import CloudValidationTransientError
    from apps.console.cloud.hetzner.models import CoreHetznerAccount
    from apps.console.cloud.hetzner.resources import collect_hetzner_resource_records
    from apps.monitoring.checks.hetzner_resources import (
        check_hetzner_server_metrics_status,
        check_hetzner_server_status,
        get_hetzner_check_function,
    )

    validated = False
    for attempt in range(3):
        try:
            validated = CoreHetznerAccount(access_token=token).validate()
        except CloudValidationTransientError:
            if attempt == 2:
                raise
            time.sleep(2**attempt)
            continue
        break
    if not validated:
        raise LiveE2EError("CloudMoo Hetzner account validation failed")

    account = CoreHetznerAccount(access_token=token)
    servers = account._paginate_api_call("servers", "servers")
    owned = [item for item in servers if object_id(item) == server_id]
    if len(owned) != 1 or not marker_matches(owned[0], manifest.marker):
        raise LiveE2EError("CloudMoo legacy Hetzner Server inventory missed the owned Server")
    assert_no_secret_values(owned[0], token)

    status, metadata = check_hetzner_server_status(server_id, token)
    expected = "available" if phase == "running" else "stopped"
    if str(status).lower() != expected:
        raise LiveE2EError(f"CloudMoo Hetzner Server status was {status}, expected {expected}")
    assert_no_secret_values(metadata, token)
    if get_hetzner_check_function("servers") is not check_hetzner_server_status:
        raise LiveE2EError("CloudMoo Hetzner Server check dispatcher resolved an unexpected function")

    locations = collect_hetzner_resource_records(SimpleNamespace(access_token=token), "location")
    location_id = str(selection["location_id"])
    if not any(str(record.get("unique_id")) == location_id for record in locations):
        raise LiveE2EError("CloudMoo expanded Hetzner location inventory missed the selected location")
    for record in locations:
        assert_no_secret_values(record.get("metadata"), token)

    metrics_status = None
    if check_metrics:
        end = datetime.now(timezone.utc).replace(microsecond=0)
        start = end - timedelta(minutes=15)
        metrics_status, metrics_metadata = check_hetzner_server_metrics_status(
            server_id,
            {
                "access_token": token,
                "metrics": {
                    "type": "cpu",
                    "start": start.isoformat().replace("+00:00", "Z"),
                    "end": end.isoformat().replace("+00:00", "Z"),
                },
            },
        )
        if str(metrics_status).lower() in {"error", "not_found", "invalid_access_token"}:
            raise LiveE2EError("CloudMoo Hetzner Server metrics check failed")
        assert_no_secret_values(metrics_metadata, token)
    manifest.event("cloudmoo_server_assertions_passed", phase=phase, status=str(status), metrics_status=str(metrics_status) if metrics_status is not None else None, inventory_records=len(servers), location_records=len(locations))


def invoke_server_action(
    client: HetznerHTTP,
    manifest: Manifest,
    server_id: str,
    command: str,
    desired: set[str],
) -> None:
    if not verify_owned_server(client, manifest, server_id):
        raise CleanupSafetyError("Owned Hetzner Server disappeared before action")
    path = f"servers/{server_id}/actions/{command}"
    try:
        response = client.request("POST", path, payload={})
    except AmbiguousMutation:
        manifest.event("action_ambiguous", command=command, resource_id=short_id(server_id))
        try:
            wait_server(client, manifest, server_id, lambda item: server_status(item) in desired, f"server_{command}_resolve", ACTION_DEADLINE_SECONDS)
            return
        except LiveE2EError:
            if not verify_owned_server(client, manifest, server_id):
                raise CleanupSafetyError("Owned Hetzner Server disappeared after ambiguous action")
            response = client.request("POST", path, payload={})
    action = action_from_response(response)
    if action:
        action_id = object_id(action)
        if not action_id:
            raise LiveE2EError("Hetzner action response omitted its ID")
        manifest.record_action(action_id, command, server_id)
        manifest.save()
        wait_action(client, manifest, action_id, ACTION_DEADLINE_SECONDS)
    wait_server(client, manifest, server_id, lambda item: server_status(item) in desired, f"server_{command}", ACTION_DEADLINE_SECONDS)


def delete_owned_server(client: HetznerHTTP, manifest: Manifest) -> None:
    resource = manifest.state.get("resources", {}).get("server")
    if not resource or resource.get("state") == "absent":
        return
    server_id = str(resource["id"])
    if not verify_owned_server(client, manifest, server_id):
        manifest.mark_deleted("server")
        return
    server = get_server(client, server_id)
    if server_status(server) not in SERVER_STOPPED_STATUSES:
        invoke_server_action(client, manifest, server_id, "poweroff", set(SERVER_STOPPED_STATUSES))
    manifest.state["resources"]["server"]["state"] = "delete_requested"
    manifest.save()
    try:
        response = client.request("DELETE", f"servers/{server_id}")
    except AmbiguousMutation:
        if verify_owned_server(client, manifest, server_id):
            response = client.request("DELETE", f"servers/{server_id}")
        else:
            manifest.mark_deleted("server")
            return
    action = action_from_response(response)
    if action:
        action_id = object_id(action)
        if not action_id:
            raise LiveE2EError("Hetzner delete response contained an invalid action")
        manifest.record_action(action_id, "delete", server_id)
        wait_action(client, manifest, action_id, ACTION_DEADLINE_SECONDS)
    manifest.event("server_delete_requested", resource_id=short_id(server_id))
    end = time.monotonic() + CLEANUP_DEADLINE_SECONDS
    while time.monotonic() < end:
        try:
            get_server(client, server_id)
        except LiveE2EError as error:
            if "HTTP 404" in str(error):
                manifest.mark_deleted("server")
                return
            raise
        time.sleep(POLL_SECONDS)
    raise LiveE2EError(f"Timed out deleting owned Hetzner Server {short_id(server_id)}")


def cleanup(client: HetznerHTTP, manifest: Manifest) -> None:
    delete_owned_server(client, manifest)
    baseline = manifest.state.get("baseline", {}).get("servers", {})
    current = client.list_collection("servers", "servers")
    current_ids = {str(item["id"]) for item in current if object_id(item)}
    owned_id = str(manifest.state.get("resources", {}).get("server", {}).get("id") or "")
    if owned_id and owned_id in current_ids:
        raise CleanupSafetyError("Owned Hetzner Server ID remains after cleanup")
    if any(marker_matches(item, manifest.marker) for item in current):
        raise CleanupSafetyError("Hetzner ownership marker remains after cleanup")
    baseline_ids = set(baseline.get("ids", []))
    if not baseline_ids.issubset(current_ids):
        raise CleanupSafetyError("A baseline Hetzner Server disappeared during the test")
    added = len(current_ids - baseline_ids)
    if added:
        manifest.event("unowned_server_baseline_drift", added=added)
    manifest.event("server_cleanup_verified", remaining=len(current_ids))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-monthly-cost", required=True, help="explicit maximum monthly gross Server price")
    parser.add_argument("--cleanup-ledger", type=Path)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--location")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    token = os.environ.get("HETZNER_API_TOKEN", "").strip()
    if not token:
        print("HETZNER_API_TOKEN is required through the environment", file=sys.stderr)
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
    else:
        marker = f"{RUN_PREFIX}{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(8)}"
        path = args.ledger or Path(tempfile.gettempdir()) / f"cloudmoo-hetzner-e2e-{marker.rsplit('-', 1)[-1]}.json"
        manifest = Manifest(path)
        manifest.state["marker"] = marker
        manifest.save()

    client = HetznerHTTP(token, manifest)
    failure: BaseException | None = None
    try:
        if args.cleanup_ledger:
            print(json.dumps({"event": "hetzner_cleanup_resume", "marker": manifest.marker, "ledger": str(manifest.path)}), flush=True)
            cleanup(client, manifest)
            manifest.state["status"] = "passed"
            manifest.save()
            print(json.dumps({"event": "hetzner_cleanup_passed", "ledger": str(manifest.path)}), flush=True)
            return 0

        print(json.dumps({"event": "hetzner_live_test_started", "marker": manifest.marker, "ledger": str(manifest.path)}), flush=True)
        servers = client.list_collection("servers", "servers")
        baseline_ids = sorted(str(item["id"]) for item in servers if object_id(item))
        if any(marker_matches(item, manifest.marker) for item in servers):
            raise LiveE2EError("Hetzner Server ownership marker collision detected")
        manifest.state["baseline"] = {
            "servers": {
                "ids": baseline_ids,
                "count": len(baseline_ids),
                "fingerprint": fingerprint_ids(baseline_ids),
            }
        }
        manifest.event("server_preflight_complete", existing_servers=len(servers))
        selection = select_server_configuration(client, args.location, max_monthly_cost, manifest)
        selection["max_monthly_cost"] = str(max_monthly_cost)
        manifest.state["selection"] = selection
        manifest.save()
        print(json.dumps({"event": "server_selection", **selection}, sort_keys=True), flush=True)

        payload = {
            "name": f"{manifest.marker}-server",
            # The current API schema declares these three create fields as
            # strings.  Names are unambiguous because the preflight selected
            # them from the exact current Project catalogs; IDs remain in the
            # ledger for post-create identity verification.
            "server_type": selection["server_type_name"],
            "image": selection["image_name"],
            "location": selection["location_name"],
            "labels": {
                "cloudmoo.com/owner": DEFAULT_OWNER_LABEL,
                "cloudmoo.com/test-run": manifest.marker,
            },
            "start_after_create": False,
            "public_net": {
                "enable_ipv4": False,
                "enable_ipv6": True,
            },
        }
        try:
            response = client.request("POST", "servers", payload=payload)
        except AmbiguousMutation:
            servers = client.list_collection("servers", "servers")
            server = find_owned_server(servers, manifest.marker, set(baseline_ids))
        else:
            server = response_object(response, ("server",))
            if not server or not object_id(server):
                servers = client.list_collection("servers", "servers")
                server = find_owned_server(servers, manifest.marker, set(baseline_ids))
        server_id = object_id(server)
        if not server_id or server_id in baseline_ids:
            raise CleanupSafetyError("Created Hetzner Server ID could not be proven new")
        manifest.own("server", server_id)
        manifest.save()
        server = get_server(client, server_id)
        assert_server_identity(server, server_id, manifest)
        manifest.state["resources"]["server"].update({"name": server.get("name"), "ownership_verified": True})
        manifest.save()
        print(json.dumps({"event": "server_created", "id": short_id(server_id)}), flush=True)

        action = action_from_response(response) if "response" in locals() else None
        if action:
            action_id = object_id(action)
            if not action_id:
                raise LiveE2EError("Hetzner create response contained an invalid action")
            manifest.record_action(action_id, "create", server_id)
            wait_action(client, manifest, action_id, ACTION_DEADLINE_SECONDS)
        wait_server(client, manifest, server_id, lambda item: server_status(item) in SERVER_STOPPED_STATUSES, "server_created_off", PROVISION_DEADLINE_SECONDS)
        manifest.state["resources"]["server"]["state"] = "stopped"
        manifest.save()
        run_cloudmoo_assertions(token, manifest, server_id, selection, phase="stopped")

        invoke_server_action(client, manifest, server_id, "poweron", set(SERVER_READY_STATUSES))
        manifest.state["resources"]["server"]["state"] = "running"
        manifest.save()
        run_cloudmoo_assertions(token, manifest, server_id, selection, phase="running", check_metrics=True)

        invoke_server_action(client, manifest, server_id, "poweroff", set(SERVER_STOPPED_STATUSES))
        manifest.state["resources"]["server"]["state"] = "stopped"
        manifest.save()
        run_cloudmoo_assertions(token, manifest, server_id, selection, phase="stopped")

        invoke_server_action(client, manifest, server_id, "poweron", set(SERVER_READY_STATUSES))
        manifest.state["resources"]["server"]["state"] = "running"
        manifest.save()
        run_cloudmoo_assertions(token, manifest, server_id, selection, phase="running")
        manifest.event("server_lifecycle_passed", server_id=short_id(server_id))
    except BaseException as error:
        failure = error
        manifest.state["failure"] = str(error)[:240]
        manifest.event("server_test_failed", error_type=type(error).__name__)
        print(json.dumps({"event": "hetzner_live_test_failed", "error_type": type(error).__name__, "message": str(error)[:240]}), file=sys.stderr, flush=True)
    finally:
        try:
            cleanup(client, manifest)
        except BaseException as error:
            manifest.state["cleanup_failure"] = str(error)[:240]
            manifest.event("server_cleanup_failed", error_type=type(error).__name__)
            print(json.dumps({"event": "hetzner_cleanup_failed", "error_type": type(error).__name__, "message": str(error)[:240], "ledger": str(manifest.path)}), file=sys.stderr, flush=True)
            failure = failure or error
        manifest.state["status"] = "passed" if failure is None else "failed"
        manifest.save()

    if failure is not None:
        print(json.dumps({"event": "hetzner_live_test_failed_final", "ledger": str(manifest.path), "status": manifest.state["status"]}), file=sys.stderr, flush=True)
        return 130 if isinstance(failure, KeyboardInterrupt) else 1
    print(json.dumps({"event": "hetzner_live_test_passed", "ledger": str(manifest.path)}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
