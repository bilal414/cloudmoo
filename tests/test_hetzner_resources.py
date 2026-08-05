from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from apps.console.cloud.models import CloudInventoryTransientError
from apps.console.cloud.hetzner.resources import (
    HETZNER_RESOURCE_SPECS,
    CoreHetznerPrimaryIP,
    CoreHetznerObjectStorageBucket,
    HetznerResourceSpec,
    collect_hetzner_action_record,
    collect_hetzner_inventory,
    collect_hetzner_resource_records,
    iter_hetzner_collection,
    list_hetzner_collection,
    sync_hetzner_object_storage_assets,
    sync_hetzner_resource,
)
from apps.console.utils.models import UtilAsset


class FakeHetznerAccount:
    access_token = "test-token"

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def _make_api_call(self, endpoint, params=None):
        self.calls.append((endpoint, dict(params or {})))
        page = (params or {}).get("page", 1)
        response = self.pages[page]
        if isinstance(response, BaseException):
            raise response
        return response


class FakeQuerySet:
    def __init__(self, manager):
        self.manager = manager

    def exclude(self, **kwargs):
        self.manager.exclude_calls.append(kwargs)
        return self

    def update(self, **kwargs):
        self.manager.update_calls.append(kwargs)
        return 1


class FakeManager:
    def __init__(self, asset=None):
        self.asset = asset or SimpleNamespace(
            name="old",
            type="primary_ip",
            metadata={},
            monitoring=UtilAsset.Monitoring.NO_LONGER_EXISTS,
            save=Mock(),
        )
        self.get_or_create_calls = []
        self.exclude_calls = []
        self.update_calls = []

    def get_or_create(self, **kwargs):
        self.get_or_create_calls.append(kwargs)
        return self.asset, False

    def filter(self, **kwargs):
        return FakeQuerySet(self)


class FakeAssetModel:
    Monitoring = UtilAsset.Monitoring
    provider_type = "hetzner_test_resource"
    asset_type = "test_resource"


class HetznerResourcePaginationTests(SimpleTestCase):
    def test_pagination_follows_provider_next_page_and_keeps_page_params_bounded(self):
        account = FakeHetznerAccount(
            {
                1: {
                    "primary_ips": [{"id": 1, "ip": "203.0.113.10"}],
                    "meta": {
                        "pagination": {
                            "page": 1,
                            "per_page": 1,
                            "next_page": 2,
                            "last_page": 2,
                            "total_entries": 2,
                        }
                    },
                },
                2: {
                    "primary_ips": [{"id": 2, "ip": "203.0.113.11"}],
                    "meta": {
                        "pagination": {
                            "page": 2,
                            "per_page": 1,
                            "next_page": None,
                            "last_page": 2,
                            "total_entries": 2,
                        }
                    },
                },
            }
        )

        result = list_hetzner_collection(
            account,
            "primary_ips",
            per_page=1,
            max_pages=2,
        )

        self.assertEqual([item["id"] for item in result], [1, 2])
        self.assertEqual(
            account.calls,
            [
                ("primary_ips", {"page": 1, "per_page": 1}),
                ("primary_ips", {"page": 2, "per_page": 1}),
            ],
        )

    def test_legacy_short_page_without_meta_is_supported_but_full_pages_continue(self):
        account = FakeHetznerAccount(
            {
                1: {"locations": [{"id": 1, "name": "fsn1"}]},
                2: {"locations": []},
            }
        )

        result = list_hetzner_collection(account, "locations", per_page=2, max_pages=3)

        self.assertEqual(result, [{"id": 1, "name": "fsn1"}])
        self.assertEqual(len(account.calls), 1)

    def test_repeated_next_page_fails_before_an_unbounded_request(self):
        account = FakeHetznerAccount(
            {
                1: {
                    "networks": [{"id": 1}],
                    "meta": {"pagination": {"next_page": 1, "total_entries": 2}},
                }
            }
        )

        with self.assertRaises(CloudInventoryTransientError):
            list_hetzner_collection(account, "networks", per_page=1, max_pages=10)

        self.assertEqual(len(account.calls), 1)

    def test_page_bound_rejects_a_provider_that_keeps_returning_next_pages(self):
        account = FakeHetznerAccount(
            {
                1: {"images": [{"id": 1}], "meta": {"pagination": {"next_page": 2}}},
                2: {"images": [{"id": 2}], "meta": {"pagination": {"next_page": 3}}},
            }
        )

        with self.assertRaises(CloudInventoryTransientError):
            list_hetzner_collection(account, "images", per_page=1, max_pages=2)

        self.assertEqual(len(account.calls), 2)


class HetznerResourceSafetyTests(SimpleTestCase):
    def test_missing_collection_fails_closed_instead_of_becoming_empty(self):
        account = FakeHetznerAccount({1: {"meta": {"pagination": {"next_page": None}}}})

        with self.assertRaises(CloudInventoryTransientError):
            collect_hetzner_resource_records(account, "firewalls")

    def test_malformed_item_and_missing_identifier_fail_closed(self):
        malformed = FakeHetznerAccount({1: {"images": [None]}})
        missing_id = FakeHetznerAccount({1: {"images": [{"name": "without-id"}]}})

        with self.assertRaises(CloudInventoryTransientError):
            collect_hetzner_resource_records(malformed, "images")
        with self.assertRaises(CloudInventoryTransientError):
            collect_hetzner_resource_records(missing_id, "images")

    def test_duplicate_identifiers_are_rejected_before_reconciliation(self):
        account = FakeHetznerAccount({1: {"certificates": [{"id": 7}, {"id": 7}]}})

        with self.assertRaises(CloudInventoryTransientError):
            collect_hetzner_resource_records(account, "certificates")

    def test_sensitive_values_are_redacted_and_input_is_not_mutated(self):
        item = {
            "id": 9,
            "name": "edge-cert",
            "labels": {"environment": "production"},
            "private_key": "do-not-persist",
        }
        account = FakeHetznerAccount({1: {"certificates": [item]}})

        records = collect_hetzner_resource_records(account, "certificates")

        self.assertEqual(item["private_key"], "do-not-persist")
        self.assertEqual(records[0]["unique_id"], "9")
        self.assertEqual(records[0]["name"], "edge-cert")
        self.assertNotIn("private_key", records[0]["metadata"])
        self.assertNotIn("private_key", records[0]["raw"])

    def test_all_declared_resource_families_have_models_and_get_endpoints(self):
        expected = {
            "primary_ip",
            "floating_ip",
            "network",
            "firewall",
            "load_balancer",
            "placement_group",
            "image",
            "certificate",
            "location",
            "datacenter",
            "server_type",
            "iso",
            "ssh_key",
            "load_balancer_type",
            "zone",
            "rrset",
        }

        self.assertEqual(set(HETZNER_RESOURCE_SPECS), expected)
        self.assertIs(HETZNER_RESOURCE_SPECS["primary_ip"].model, CoreHetznerPrimaryIP)
        self.assertTrue(all(spec.endpoint and spec.collection_key for spec in HETZNER_RESOURCE_SPECS.values()))

    def test_action_collection_is_never_requested_as_unbounded_inventory(self):
        account = FakeHetznerAccount({1: {"servers": []}})

        with self.assertRaises(CloudInventoryTransientError):
            collect_hetzner_resource_records(account, "action")

        self.assertEqual(account.calls, [])

    def test_ssh_key_and_certificate_normalization_drops_key_material(self):
        account = FakeHetznerAccount({1: {
            "ssh_keys": [{
                "id": 3,
                "name": "deploy",
                "fingerprint": "aa:bb",
                "public_key": "ssh-ed25519 AAAA",
                "private_key": "never-store",
            }],
        }})

        record = collect_hetzner_resource_records(account, "ssh_key")[0]

        self.assertEqual(record["metadata"]["fingerprint"], "aa:bb")
        self.assertNotIn("public_key", record["metadata"])
        self.assertNotIn("private_key", record["metadata"])


class HetznerNestedInventoryTests(SimpleTestCase):
    class EndpointAccount:
        access_token = "test-token"

        def __init__(self, responses):
            self.responses = responses
            self.calls = []

        def _make_api_call(self, endpoint, params=None):
            self.calls.append((endpoint, dict(params or {})))
            return self.responses[endpoint]

    def test_secondary_zone_does_not_reconcile_partially_visible_rrsets(self):
        account = self.EndpointAccount({
            "zones": {"zones": [{"id": 10, "name": "example.test", "mode": "secondary"}]},
        })

        result = collect_hetzner_inventory(account, resources=["zone", "rrset"])

        self.assertIn("zone", result)
        self.assertNotIn("rrset", result)
        self.assertEqual([endpoint for endpoint, _params in account.calls], ["zones"])

    def test_primary_zone_inventory_uses_zone_scoped_rrset_collection(self):
        account = self.EndpointAccount({
            "zones": {"zones": [{"id": 10, "name": "example.test", "mode": "primary"}]},
            "zones/10/rrsets": {"rrsets": [{
                "name": "www",
                "type": "A",
                "ttl": 60,
                "records": [{"value": "192.0.2.10"}],
            }]},
        })

        result = collect_hetzner_inventory(account, resources=["zone", "rrset"])

        self.assertEqual(result["rrset"][0]["unique_id"], "10:www:A")
        self.assertEqual(result["rrset"][0]["metadata"]["_cloudmoo_zone_id"], "10")
        self.assertEqual(
            [endpoint for endpoint, _params in account.calls],
            ["zones", "zones/10/rrsets"],
        )

    def test_known_action_is_fetched_by_id_without_global_action_listing(self):
        account = self.EndpointAccount({
            "actions/42": {"action": {"id": 42, "command": "create_server", "status": "running"}},
        })

        record = collect_hetzner_action_record(account, 42)

        self.assertEqual(record["unique_id"], "42")
        self.assertEqual(record["metadata"]["status"], "running")
        self.assertEqual([endpoint for endpoint, _params in account.calls], ["actions/42"])


class HetznerObjectStorageTests(SimpleTestCase):
    def test_bucket_inventory_is_metadata_only_and_uses_bucket_region(self):
        account = SimpleNamespace(
            access_token="cloud-token",
            object_storage_access_key="access-key",
            object_storage_secret_key="secret-key",
            object_storage_region="fsn1",
            object_storage_configured=True,
            object_storage_credentials={
                "access_key": "access-key",
                "secret_key": "secret-key",
                "region": "fsn1",
            },
        )
        manager = FakeManager()
        client = Mock()
        client.list_buckets.return_value = {
            "Buckets": [{
                "Name": "bucket-a",
                "BucketRegion": "nbg1",
                "CreationDate": datetime(2026, 8, 3, tzinfo=timezone.utc),
            }],
        }

        with patch.object(CoreHetznerObjectStorageBucket, "objects", manager), \
                patch("apps.console.cloud.hetzner.resources.boto3.client", return_value=client) as mock_client:
            count = sync_hetzner_object_storage_assets(account)

        self.assertEqual(count, 1)
        mock_client.assert_called_once()
        self.assertEqual(mock_client.call_args.kwargs["endpoint_url"], "https://fsn1.your-objectstorage.com")
        client.list_buckets.assert_called_once_with()
        self.assertEqual(manager.get_or_create_calls[0]["unique_id"], "bucket-a")
        self.assertEqual(manager.get_or_create_calls[0]["defaults"]["metadata"]["region"], "nbg1")


class HetznerReconciliationTests(SimpleTestCase):
    def test_reconciliation_updates_existing_assets_and_marks_absent_only_after_valid_input(self):
        manager = FakeManager()
        FakeAssetModel.objects = manager
        spec = HetznerResourceSpec(
            "test_resource",
            "test_resources",
            "test_resources",
            FakeAssetModel,
        )
        records = [
            {
                "unique_id": "resource-1",
                "name": "resource one",
                "metadata": {"id": 1},
            }
        ]
        account = SimpleNamespace(access_token="test-token")

        count = sync_hetzner_resource(account, spec, records=records)

        self.assertEqual(count, 1)
        self.assertEqual(manager.get_or_create_calls[0]["unique_id"], "resource-1")
        self.assertEqual(manager.exclude_calls, [{"unique_id__in": ["resource-1"]}])
        self.assertEqual(manager.update_calls, [{"monitoring": UtilAsset.Monitoring.NO_LONGER_EXISTS}])
        self.assertEqual(manager.asset.name, "resource one")
        self.assertEqual(manager.asset.monitoring, UtilAsset.Monitoring.ACTIVE)
        manager.asset.save.assert_called_once_with()

    def test_invalid_caller_records_do_not_reconcile_or_mark_existing_assets(self):
        manager = FakeManager()
        FakeAssetModel.objects = manager
        spec = HetznerResourceSpec(
            "test_resource",
            "test_resources",
            "test_resources",
            FakeAssetModel,
        )
        account = SimpleNamespace(access_token="test-token")

        with self.assertRaises(CloudInventoryTransientError):
            sync_hetzner_resource(
                account,
                spec,
                records=[{"unique_id": "resource-1", "name": "bad", "metadata": []}],
            )

        self.assertEqual(manager.get_or_create_calls, [])
        self.assertEqual(manager.update_calls, [])


class HetznerTransportTests(SimpleTestCase):
    @patch("apps.console.cloud.hetzner.resources.requests.get")
    def test_fallback_transport_uses_only_get_and_never_exposes_token_in_errors(self, mock_get):
        response = Mock()
        response.json.return_value = {"isos": [{"id": 3, "name": "rescue"}]}
        response.raise_for_status.return_value = None
        mock_get.return_value = response
        account = SimpleNamespace(access_token="secret-test-token")

        result = list_hetzner_collection(account, "isos")

        self.assertEqual(result[0]["id"], 3)
        mock_get.assert_called_once_with(
            "https://api.hetzner.cloud/v1/isos",
            headers={"Authorization": "Bearer secret-test-token"},
            params={"page": 1, "per_page": 50},
            timeout=15,
        )
        self.assertEqual(mock_get.call_args.args[0].split("/")[-1], "isos")
