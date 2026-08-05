from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase
import requests

from apps.console.cloud.models import CloudInventoryTransientError
from apps.console.cloud.vultr.resources_base import (
    VultrReadOnlyClient,
    VultrInventoryError,
    list_vultr_collection,
    redact_vultr_metadata,
    sync_vultr_resources,
    validate_vultr_endpoint,
)
from apps.console.cloud.vultr.resources_compute import (
    CoreVultrBareMetal,
    RESOURCE_SPECS,
)
from apps.console.utils.models import UtilAsset


class _MemoryManager:
    def __init__(self, model, rows=None):
        self.model = model
        self.rows = list(rows or [])
        self.get_or_create_calls = []

    def get_or_create(self, *, owner, unique_id, defaults):
        self.get_or_create_calls.append((owner, unique_id, defaults))
        for row in self.rows:
            if row.owner is owner and row.unique_id == unique_id:
                return row, False
        row = SimpleNamespace(
            owner=owner,
            unique_id=unique_id,
            Monitoring=self.model.Monitoring,
            save=Mock(),
            **defaults,
        )
        self.rows.append(row)
        return row, True

    def filter(self, **kwargs):
        return [row for row in self.rows if all(getattr(row, key) == value for key, value in kwargs.items())]


class _FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get_json(self, endpoint, *, params=None):
        self.calls.append((endpoint, dict(params or {})))
        response = self.responses[endpoint]
        if isinstance(response, BaseException):
            raise response
        return response


class VultrTransportTests(SimpleTestCase):
    @patch("apps.console.cloud.vultr.resources_base.requests.get")
    def test_client_is_allowlisted_get_only_and_bounded(self, mock_get):
        response = Mock(status_code=200)
        response.json.return_value = {"instances": [], "meta": {"links": {"next": None}}}
        mock_get.return_value = response

        client = VultrReadOnlyClient("fake-token", timeout=7, max_retries=0, retry_backoff=0)
        self.assertEqual(client.get_json("instances"), {"instances": [], "meta": {"links": {"next": None}}})
        mock_get.assert_called_once_with(
            "https://api.vultr.com/v2/instances",
            headers={
                "Authorization": "Bearer fake-token",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            params={},
            timeout=7,
        )
        with self.assertRaises(VultrInventoryError):
            client.request("POST", "instances")
        with self.assertRaises(VultrInventoryError):
            validate_vultr_endpoint("https://attacker.invalid/v2/instances")
        with self.assertRaises(VultrInventoryError):
            validate_vultr_endpoint("instances/../account")
        for endpoint in (
            "load-balancers/lb-1/health",
            "firewalls/fw-1/rules/7",
            "domains/example.com/records/11",
            "instances/i-1/actions/action-1",
            "kubernetes/clusters/cluster-1/actions/action-1",
        ):
            self.assertEqual(validate_vultr_endpoint(endpoint), endpoint)
        with self.assertRaises(VultrInventoryError):
            client.get_json("instances", params={"unexpected": "value"})
        with self.assertRaises(VultrInventoryError):
            client.get_json("instances", params={"cursor": "bad?cursor"})

    @patch("apps.console.cloud.vultr.resources_base.time.sleep")
    @patch("apps.console.cloud.vultr.resources_base.requests.get")
    def test_retry_handles_rate_limit_and_server_error_without_returning_body(self, mock_get, mock_sleep):
        first = Mock(status_code=429)
        first.json.return_value = {"secret": "not returned"}
        second = Mock(status_code=503)
        third = Mock(status_code=200)
        third.json.return_value = {"instances": [], "meta": {"links": {"next": None}}}
        mock_get.side_effect = [first, second, third]

        payload = VultrReadOnlyClient("fake-token", retry_backoff=1).get_json("instances")

        self.assertEqual(payload["instances"], [])
        self.assertEqual(mock_get.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("apps.console.cloud.vultr.resources_base.time.sleep")
    @patch("apps.console.cloud.vultr.resources_base.requests.get")
    def test_retry_handles_transport_failures_with_same_bounded_budget(self, mock_get, mock_sleep):
        response = Mock(status_code=200)
        response.json.return_value = {"instances": [], "meta": {"links": {"next": None}}}
        mock_get.side_effect = [requests.Timeout("provider timeout"), response]

        payload = VultrReadOnlyClient("fake-token", retry_backoff=1).get_json("instances")

        self.assertEqual(payload["instances"], [])
        self.assertEqual(mock_get.call_count, 2)
        mock_sleep.assert_called_once_with(1)

    def test_cursor_pagination_rejects_loops_and_malformed_pages(self):
        looping = _FakeClient({
            "bare-metals": {
                "bare_metals": [{"id": "bm-1"}],
                "meta": {"links": {"next": "cursor-1"}},
            },
        })
        with self.assertRaises(CloudInventoryTransientError):
            list_vultr_collection(looping, "bare-metals", "bare_metals", client=looping)

        malformed = _FakeClient({"bare-metals": {"meta": {"links": {"next": None}}}})
        with self.assertRaises(CloudInventoryTransientError):
            list_vultr_collection(malformed, "bare-metals", "bare_metals", client=malformed)

    def test_total_only_meta_is_terminal_when_the_validated_page_is_complete(self):
        complete = _FakeClient({
            "bare-metals": {
                "bare_metals": [{"id": "bm-1"}],
                "meta": {"total": 1},
            },
        })

        records = list_vultr_collection(complete, "bare-metals", "bare_metals", client=complete)

        self.assertEqual(records, [{"id": "bm-1"}])
        self.assertEqual(complete.calls, [("bare-metals", {"per_page": 100})])

    def test_total_only_meta_rejects_an_incomplete_page(self):
        incomplete = _FakeClient({
            "bare-metals": {
                "bare_metals": [{"id": "bm-1"}],
                "meta": {"total": 2},
            },
        })

        with self.assertRaises(CloudInventoryTransientError):
            list_vultr_collection(incomplete, "bare-metals", "bare_metals", client=incomplete)

    def test_recursive_redaction_covers_nested_credentials_and_signed_links(self):
        value = {
            "safe": {"name": "node"},
            "nested": [{"kubeconfig": "secret", "user_data": "cloud-init"}],
            "vnc_url": "https://console.invalid/vnc?token=secret",
            "download": "https://objects.invalid/a?X-Amz-Signature=secret",
        }

        redacted = redact_vultr_metadata(value)

        self.assertEqual(redacted["safe"]["name"], "node")
        self.assertEqual(redacted["nested"][0]["kubeconfig"], "[REDACTED]")
        self.assertEqual(redacted["nested"][0]["user_data"], "[REDACTED]")
        self.assertEqual(redacted["vnc_url"], "[REDACTED]")
        self.assertEqual(redacted["download"], "[REDACTED]")
        self.assertEqual(value["nested"][0]["kubeconfig"], "secret")


class VultrReconciliationTests(SimpleTestCase):
    def test_all_reads_complete_before_any_family_is_reconciled(self):
        account = object()
        bare_metal_manager = _MemoryManager(CoreVultrBareMetal)
        block_snapshot_manager = _MemoryManager(RESOURCE_SPECS["block_snapshot"].model)
        client = _FakeClient({
            "bare-metals": {
                "bare_metals": [{"id": "bm-1", "label": "one"}],
                "meta": {"links": {"next": None}},
            },
            "blocks/snapshots": {
                "meta": {"links": {"next": None}},
            },
        })

        with patch.object(CoreVultrBareMetal, "objects", bare_metal_manager), \
                patch.object(RESOURCE_SPECS["block_snapshot"].model, "objects", block_snapshot_manager):
            with self.assertRaises(CloudInventoryTransientError):
                sync_vultr_resources(account, ["bare_metal", "block_snapshot"], client=client)

        self.assertEqual(bare_metal_manager.get_or_create_calls, [])
        self.assertEqual(block_snapshot_manager.get_or_create_calls, [])

    def test_valid_collection_marks_missing_assets_with_save_not_bulk_update(self):
        old = SimpleNamespace(
            owner=None,
            unique_id="bm-old",
            name="old",
            type="vultr_bare_metal",
            metadata={},
            monitoring=UtilAsset.Monitoring.ACTIVE,
            Monitoring=UtilAsset.Monitoring,
            save=Mock(),
        )
        manager = _MemoryManager(CoreVultrBareMetal, [old])
        account = object()
        old.owner = account

        with patch.object(CoreVultrBareMetal, "objects", manager):
            from apps.console.cloud.vultr.resources_base import sync_vultr_resource

            count = sync_vultr_resource(
                account,
                "bare_metal",
                records=[{"unique_id": "bm-new", "name": "new", "metadata": {}}],
            )

        self.assertEqual(count, 1)
        self.assertEqual(old.monitoring, UtilAsset.Monitoring.NO_LONGER_EXISTS)
        old.save.assert_called_once_with()
