from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.console.cloud.models import CloudInventoryTransientError
from apps.console.cloud.vultr.resources_platform import (
    RESOURCE_MODELS,
    RESOURCE_SPECS,
    collect_vultr_resource_records,
)
from apps.monitoring.checks.vultr_platform import (
    check_vultr_container_registry_status,
    check_vultr_kubernetes_cluster_status,
)


class FakeVultrClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, endpoint, params=None):
        self.calls.append((endpoint, dict(params or {})))
        return self.responses[endpoint]


class VultrPlatformInventoryTests(SimpleTestCase):
    account = SimpleNamespace(access_token="control-plane-token")

    def test_kubernetes_inventory_drops_kubeconfig_and_keeps_health(self):
        client = FakeVultrClient(
            {
                "kubernetes/clusters": {
                    "vke_clusters": [
                        {
                            "id": "cluster-1",
                            "label": "production",
                            "status": "Running",
                            "health": "healthy",
                            "kubeconfig": "never-persist-this",
                            "node_pools": [{"id": "pool-1"}],
                        }
                    ]
                }
            }
        )

        records = collect_vultr_resource_records(
            self.account,
            "kubernetes_cluster",
            client=client,
        )

        self.assertEqual(records[0]["unique_id"], "cluster-1")
        self.assertEqual(records[0]["metadata"]["status"], "Running")
        self.assertEqual(records[0]["metadata"]["_cloudmoo_node_pool_count"], 1)
        self.assertNotIn("kubeconfig", records[0]["metadata"])
        self.assertNotIn("never-persist-this", str(records[0]))
        self.assertEqual([call[0] for call in client.calls], ["kubernetes/clusters"])

    def test_registry_inventory_keeps_pull_summary_and_drops_credentials(self):
        client = FakeVultrClient(
            {
                "registry": {
                    "registries": [
                        {
                            "id": "registry-1",
                            "name": "images",
                            "status": "active",
                            "username": "registry-user",
                            "password": "registry-password",
                        }
                    ]
                },
                "registry/registry-1/repositories": {
                    "repositories": [
                        {
                            "id": "repository-1",
                            "name": "api",
                            "status": "ready",
                            "pull_count": 42,
                            "credentials": {"token": "registry-secret"},
                        }
                    ]
                },
            }
        )

        records = collect_vultr_resource_records(
            self.account,
            "registry_repository",
            client=client,
        )

        self.assertEqual(records[0]["metadata"]["pull_count"], 42)
        self.assertEqual(records[0]["metadata"]["status"], "ready")
        self.assertNotIn("credentials", records[0]["metadata"])
        self.assertNotIn("registry-secret", str(records[0]))
        self.assertNotIn("registry-password", str(records[0]))

    def test_unsupported_collection_shape_fails_closed(self):
        client = FakeVultrClient({"kubernetes/clusters": {"vke_clusters": {}}})

        with self.assertRaises(CloudInventoryTransientError):
            collect_vultr_resource_records(
                self.account,
                "kubernetes_cluster",
                client=client,
            )

    def test_resource_registries_do_not_include_app_platform(self):
        self.assertIn("kubernetes_cluster", RESOURCE_MODELS)
        self.assertIn("registry_artifact", RESOURCE_SPECS)
        self.assertNotIn("app", RESOURCE_SPECS)
        self.assertNotIn("app_platform", RESOURCE_SPECS)


class VultrPlatformCheckTests(SimpleTestCase):
    @patch("apps.monitoring.checks.vultr_platform.VultrClient.get_json")
    def test_cluster_check_returns_health_status_without_kubeconfig(self, mock_get):
        mock_get.return_value = {
            "vke_cluster": {
                "id": "cluster-1",
                "status": "Running",
                "health": "healthy",
                "kubeconfig": "never-persist-this",
            }
        }

        status, metadata = check_vultr_kubernetes_cluster_status("cluster-1", "token")

        self.assertEqual(status, "running")
        self.assertNotIn("kubeconfig", str(metadata))
        mock_get.assert_called_once()
        self.assertEqual(mock_get.call_args.args[0], "kubernetes/clusters/cluster-1")

    @patch("apps.monitoring.checks.vultr_platform.VultrClient.get_json")
    def test_registry_check_returns_safe_summary(self, mock_get):
        mock_get.return_value = {
            "registry": {
                "id": "registry-1",
                "status": "active",
                "pull_count": 9,
                "password": "never-return-this",
            }
        }

        status, metadata = check_vultr_container_registry_status("registry-1", "token")

        self.assertEqual(status, "active")
        self.assertEqual(metadata["registry"]["pull_count"], 9)
        self.assertNotIn("password", str(metadata))
        self.assertNotIn("never-return-this", str(metadata))
