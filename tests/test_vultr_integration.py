from django.test import SimpleTestCase

from apps.console.cloud.vultr.integration import (
    get_vultr_asset_relations,
    get_vultr_resource_models,
    get_vultr_resource_specs,
)
from apps.console.cloud.vultr.resources_data_network import CoreVultrLoadBalancer
from apps.monitoring.checks import get_check_function


class VultrIntegrationRegistryTests(SimpleTestCase):
    def test_all_family_registries_are_merged_without_account_response_models(self):
        models = get_vultr_resource_models()
        specs = get_vultr_resource_specs()

        self.assertIs(models["load_balancer"], CoreVultrLoadBalancer)
        for key in (
            "bare_metal",
            "block_snapshot",
            "kubernetes_cluster",
            "object_storage",
            "registry_artifact",
            "load_balancer",
            "dns_record",
        ):
            self.assertIn(key, models)
            self.assertIn(key, specs)

        self.assertNotIn("account_profile", models)
        self.assertEqual(specs["account_profile"].model, "vultr_account_profile")
        self.assertFalse(specs["bandwidth_metric"].supported)

    def test_dynamic_asset_relations_cover_legacy_and_expanded_models(self):
        relations = set(get_vultr_asset_relations())

        self.assertIn(("databases", "database"), relations)
        self.assertIn(("corevultrloadbalancer_assets", "load_balancer"), relations)
        self.assertIn(("corevultrkubernetescluster_assets", "kubernetes_cluster"), relations)

    def test_shared_status_dispatch_reaches_each_vultr_family(self):
        for asset_type in (
            "vultr_bare_metal",
            "load_balancer",
            "kubernetes_cluster",
            "vultr_account_log",
            "vultr_status_incident",
        ):
            self.assertTrue(callable(get_check_function("vultr", asset_type)))

        with self.assertRaises(ValueError):
            get_check_function("vultr", "not_a_vultr_asset")
