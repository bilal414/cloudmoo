from django.test import SimpleTestCase, TestCase
from unittest.mock import patch

from apps.console.account.models import CoreAccount, CoreAccountMembership
from apps.console.cloud.models import CoreCloud, CoreCloudServiceProvider
from apps.console.cloud.vultr.integration import (
    get_vultr_asset_relations,
    get_vultr_resource_models,
    get_vultr_resource_specs,
    sync_vultr_inventory,
)
from apps.console.cloud.vultr.models import CoreVultrAccount
from apps.console.cloud.vultr.resources_base import VultrAPIError
from apps.console.cloud.vultr.resources_data_network import CoreVultrLoadBalancer
from apps.console.member.models import CoreMember
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


class VultrInventoryIsolationTests(TestCase):
    """A provider error in one service family must not abort the whole sync."""

    def setUp(self):
        from django.contrib.auth.models import User

        self.user = User.objects.create_user(
            username='vultr-isolation-user',
            email='vultr-isolation@example.com',
            password='testpass123',
        )
        self.account = CoreAccount.objects.create(
            name='Vultr Isolation Test',
            status=CoreAccount.Status.ACTIVE,
            owner=self.user,
        )
        member = CoreMember.objects.create(user=self.user, active_account=self.account)
        CoreAccountMembership.objects.create(
            account=self.account,
            member=member,
            role=CoreAccountMembership.Role.OWNER,
        )
        self.provider, _ = CoreCloudServiceProvider.objects.get_or_create(
            code='vultr',
            defaults={'name': 'Vultr', 'status': CoreCloudServiceProvider.Status.ACTIVE},
        )
        self.cloud = CoreCloud.objects.create(
            account=self.account,
            provider=self.provider,
            status=CoreCloud.Status.ACTIVE,
        )
        self.vultr_account = CoreVultrAccount.objects.create(
            cloud=self.cloud,
            name='Isolation Vultr Account',
            access_token='test-token',
        )

    @patch('apps.monitoring.schedules.asset_schedule_create')
    def test_family_provider_failure_is_recorded_without_aborting_sync(self, _schedule_create):
        with patch(
            'apps.console.cloud.vultr.resources_data_network.sync_vultr_resources',
            return_value={'vpc': {'created': 0}},
        ), patch(
            'apps.console.cloud.vultr.resources_platform.sync_vultr_resources',
            side_effect=VultrAPIError(
                'Vultr inventory provider is temporarily unavailable', status_code=500,
            ),
        ), patch(
            'apps.console.cloud.vultr.resources_base.VultrClient.list_collection',
            return_value=[],
        ):
            results = sync_vultr_inventory(self.vultr_account)

        self.assertEqual(results['resources_platform'], {'error': 'VultrAPIError'})
        self.assertIn('resources_compute', results)
        self.assertEqual(results['resources_data_network'], {'vpc': {'created': 0}})
