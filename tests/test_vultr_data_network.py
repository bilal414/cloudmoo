from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.console.cloud.models import CloudInventoryTransientError
from apps.console.cloud.vultr.models import CoreVultrDatabase
from apps.console.cloud.vultr.resources_data_network import (
    VULTR_RESOURCE_MODELS,
    VULTR_RESOURCE_SPECS,
    CoreVultrCertificate,
    CoreVultrDNSRecord,
    CoreVultrFirewall,
    CoreVultrFirewallRule,
    CoreVultrLoadBalancer,
    CoreVultrManagedDatabase,
    CoreVultrVPC,
    collect_vultr_inventory,
    collect_vultr_resource_records,
    iter_vultr_collection,
    resource_spec,
)
from apps.console.cloud.vultr.resources_base import VultrAPIError
from apps.monitoring.checks import get_check_function
from apps.monitoring.checks.vultr_data_network import (
    check_vultr_certificate_status,
    check_vultr_firewall_rule_status,
    check_vultr_load_balancer_status,
    get_vultr_check_function,
)


def collection(key, items, next_cursor=None):
    return {key: items, "meta": {"links": {"next": next_cursor}}}


class FakeVultrClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, endpoint, params=None):
        self.calls.append((endpoint, dict(params or {})))
        response = self.responses[endpoint]
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            return response(endpoint, params)
        return response


class VultrResourceRegistryTests(SimpleTestCase):
    def test_declared_families_use_concrete_models_and_reuse_database_table_model(self):
        self.assertIs(VULTR_RESOURCE_MODELS["database"], CoreVultrDatabase)
        self.assertIs(CoreVultrManagedDatabase, CoreVultrDatabase)
        self.assertIs(VULTR_RESOURCE_SPECS["load_balancer"].model, CoreVultrLoadBalancer)
        self.assertIs(VULTR_RESOURCE_SPECS["vpc"].model, CoreVultrVPC)
        self.assertIs(VULTR_RESOURCE_SPECS["firewall"].model, CoreVultrFirewall)
        self.assertIs(VULTR_RESOURCE_SPECS["firewall_rule"].model, CoreVultrFirewallRule)
        self.assertIs(VULTR_RESOURCE_SPECS["dns_record"].model, CoreVultrDNSRecord)
        self.assertIs(VULTR_RESOURCE_SPECS["certificate"].model, CoreVultrCertificate)
        self.assertTrue(all(spec.endpoint and spec.collection_key for spec in VULTR_RESOURCE_SPECS.values()))

    def test_aliases_resolve_without_guessing_a_new_endpoint(self):
        self.assertIs(resource_spec("managed_database"), VULTR_RESOURCE_SPECS["database"])
        self.assertIs(resource_spec("tls_certificate"), VULTR_RESOURCE_SPECS["certificate"])
        self.assertIs(resource_spec("cdn_pull_zone"), VULTR_RESOURCE_SPECS["cdn_endpoint"])


class VultrInventoryTests(SimpleTestCase):
    def test_cursor_pagination_and_sensitive_database_fields_are_safe(self):
        client = FakeVultrClient({
            "databases": collection("databases", [{
                "id": "db-1",
                "label": "orders",
                "status": "Running",
                "password": "db-password",
                "connection_string": "postgres://secret",
                "ca_certificate": "-----BEGIN CERTIFICATE-----secret",
                "connection_details": {"host": "db.invalid", "password": "db-password"},
                "credentials": {"username": "admin", "password": "db-password"},
            }], "cursor-2"),
        })
        client.responses["databases"] = lambda _endpoint, params: (
                collection("databases", [{
                "id": "db-1",
                "label": "orders",
                "status": "Running",
                "password": "db-password",
                "connection_string": "postgres://secret",
                "credentials": {"username": "admin", "password": "db-password"},
            }], "cursor-2")
            if "cursor" not in params
            else collection("databases", [], None)
        )

        records = collect_vultr_resource_records(SimpleNamespace(access_token="token"), "database", client=client)

        self.assertEqual(records[0]["unique_id"], "db-1")
        self.assertEqual([call[0] for call in client.calls], ["databases", "databases"])
        self.assertEqual(client.calls[1][1]["cursor"], "cursor-2")
        serialized = str(records[0])
        self.assertNotIn("db-password", serialized)
        self.assertNotIn("postgres://secret", serialized)
        self.assertNotIn("connection_string", records[0]["metadata"])
        self.assertNotIn("ca_certificate", records[0]["metadata"])
        self.assertNotIn("connection_details", records[0]["metadata"])
        self.assertNotIn("credentials", records[0]["metadata"])

    def test_firewall_rules_and_dns_records_use_parent_scoped_get_collections(self):
        client = FakeVultrClient({
            "firewalls": collection("firewall_groups", [{"id": "fw-1", "description": "web"}]),
            "firewalls/fw-1/rules": collection("rules", [{
                "id": 7,
                "protocol": "tcp",
                "port": "443",
                "source": "0.0.0.0/0",
                "action": "accept",
                "private_key": "never-store",
                "future_provider_secret": "never-store",
            }]),
            "domains": collection("domains", [{"domain": "example.com", "id": "domain-1"}]),
            "domains/example.com/records": collection("records", [{
                "id": 11,
                "name": "www",
                "type": "A",
                "data": "203.0.113.10",
                "signed_url": "https://signed.invalid/?signature=secret",
            }]),
        })

        result = collect_vultr_inventory(
            SimpleNamespace(access_token="token"),
            resources=["firewall", "firewall_rule", "domain", "dns_record"],
            client=client,
        )

        self.assertEqual(result["firewall_rule"][0]["unique_id"], "fw-1:7")
        self.assertEqual(result["dns_record"][0]["unique_id"], "example.com:11")
        self.assertNotIn("private_key", result["firewall_rule"][0]["metadata"])
        self.assertNotIn("future_provider_secret", result["firewall_rule"][0]["metadata"])
        self.assertNotIn("signed_url", result["dns_record"][0]["metadata"])
        self.assertEqual(
            [endpoint for endpoint, _params in client.calls],
            ["firewalls", "domains", "firewalls/fw-1/rules", "domains/example.com/records"],
        )

    def test_missing_collection_meta_or_unknown_endpoint_fails_closed_without_extra_requests(self):
        client = FakeVultrClient({"databases": {"databases": []}})
        with self.assertRaises(CloudInventoryTransientError):
            collect_vultr_resource_records(SimpleNamespace(access_token="token"), "database", client=client)

        with self.assertRaises(CloudInventoryTransientError):
            list(iter_vultr_collection(client, "instances", "instances"))
        self.assertEqual(client.calls, [("databases", {"per_page": 100})])

    def test_total_only_meta_is_terminal_when_it_covers_the_validated_page(self):
        client = FakeVultrClient({
            "databases": {
                "databases": [{"id": "db-1", "label": "orders", "status": "Running"}],
                "meta": {"total": 1},
            },
        })

        records = collect_vultr_resource_records(
            SimpleNamespace(access_token="token"),
            "database",
            client=client,
        )

        self.assertEqual([record["unique_id"] for record in records], ["db-1"])
        self.assertEqual(client.calls, [("databases", {"per_page": 100})])

    def test_total_only_meta_fails_closed_when_the_page_is_incomplete(self):
        client = FakeVultrClient({
            "databases": {
                "databases": [{"id": "db-1", "label": "orders"}],
                "meta": {"total": 2},
            },
        })

        with self.assertRaises(CloudInventoryTransientError):
            collect_vultr_resource_records(
                SimpleNamespace(access_token="token"),
                "database",
                client=client,
            )


class VultrCheckTests(SimpleTestCase):
    @patch("apps.monitoring.checks.vultr_data_network.VultrClient.get_json")
    def test_load_balancer_detail_and_health_are_read_only_and_redacted(self, mock_get):
        mock_get.side_effect = [
            {"load_balancer": {
                "id": "lb-1",
                "label": "edge",
                "status": "active",
                "health_check": {"protocol": "https", "port": 443},
                "forwarding_rules": [{"frontend_protocol": "https", "backend_protocol": "http"}],
                "api_token": "never-return",
            }},
            {"health": {"healthy": True, "connection_string": "never-return"}},
        ]

        status, metadata = check_vultr_load_balancer_status("lb-1", "token")

        self.assertEqual(status, "available")
        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(
            [call.args[0] for call in mock_get.call_args_list],
            ["load-balancers/lb-1", "load-balancers/lb-1/health"],
        )
        self.assertNotIn("api_token", str(metadata))
        self.assertNotIn("connection_string", str(metadata))

    @patch("apps.monitoring.checks.vultr_data_network.VultrClient.get_json")
    def test_certificate_body_and_private_key_never_return_from_detail_check(self, mock_get):
        mock_get.return_value = {"certificate": {
            "id": "cert-1",
            "name": "example.com",
            "state": "issued",
            "certificate": "-----BEGIN CERTIFICATE-----secret",
            "private_key": "-----BEGIN PRIVATE KEY-----secret",
            "signed_url": "https://signed.invalid/?signature=secret",
        }}

        status, metadata = check_vultr_certificate_status("cert-1", {"access_token": "token"})

        self.assertEqual(status, "available")
        self.assertNotIn("BEGIN CERTIFICATE", str(metadata))
        self.assertNotIn("BEGIN PRIVATE KEY", str(metadata))
        self.assertNotIn("signed.invalid", str(metadata))

    @patch("apps.monitoring.checks.vultr_data_network.VultrClient.get_json")
    def test_child_check_requires_parent_context_and_maps_http_errors(self, mock_get):
        status, metadata = check_vultr_firewall_rule_status("7", "token")
        self.assertEqual(status, "error")
        mock_get.assert_not_called()

        mock_get.side_effect = VultrAPIError("forbidden", status_code=403)
        status, metadata = check_vultr_certificate_status("cert-1", "token")
        self.assertEqual(status, "invalid_access_token")
        self.assertNotIn("forbidden", str(metadata))

    def test_dispatch_registry_exposes_canonical_and_alias_functions(self):
        self.assertIs(get_vultr_check_function("managed_database"), get_vultr_check_function("database"))
        self.assertIs(get_vultr_check_function("tls_certificate"), get_vultr_check_function("certificate"))
        self.assertIs(get_check_function("vultr", "load_balancer"), check_vultr_load_balancer_status)
