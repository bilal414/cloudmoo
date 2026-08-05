from unittest.mock import patch

from django.test import SimpleTestCase

from apps.console.cloud.vultr.resources_compute import (
    CoreVultrBareMetal,
    CoreVultrBlockSnapshot,
    CoreVultrInstanceBackup,
    CoreVultrStorageGateway,
    CoreVultrVFS,
    RESOURCE_MODELS,
    RESOURCE_SPECS,
)
from apps.monitoring.checks.vultr_compute import (
    CHECK_REGISTRY,
    VULTR_COMPUTE_CHECKS,
    check_vultr_bare_metal_bandwidth_status,
    check_vultr_bare_metal_status,
    check_vultr_block_snapshot_status,
    check_vultr_instance_backup_status,
    get_vultr_compute_check_function,
)


class VultrComputeRegistryTests(SimpleTestCase):
    def test_registry_exposes_supported_and_explicitly_unsupported_families(self):
        self.assertIs(RESOURCE_MODELS["bare_metal"], CoreVultrBareMetal)
        self.assertIs(RESOURCE_MODELS["block_snapshot"], CoreVultrBlockSnapshot)
        self.assertIs(RESOURCE_MODELS["instance_backup"], CoreVultrInstanceBackup)
        self.assertIs(RESOURCE_MODELS["vfs"], CoreVultrVFS)
        self.assertIs(RESOURCE_MODELS["storage_gateway"], CoreVultrStorageGateway)
        self.assertEqual(RESOURCE_SPECS["bare_metal"].collection_key, "bare_metals")
        self.assertEqual(RESOURCE_SPECS["block_snapshot"].endpoint, "blocks/snapshots")
        self.assertFalse(RESOURCE_SPECS["vfs"].supported)
        self.assertFalse(RESOURCE_SPECS["storage_gateway"].supported)
        self.assertIs(CHECK_REGISTRY, VULTR_COMPUTE_CHECKS)
        self.assertIs(get_vultr_compute_check_function("vultr_bare_metal"), check_vultr_bare_metal_status)

    @patch("apps.monitoring.checks.vultr_compute.VultrReadOnlyClient.get_json")
    def test_status_checks_use_detail_get_and_redact_response(self, get_json):
        get_json.return_value = {
            "bare_metal": {
                "id": "bm-1",
                "status": "active",
                "user_data": "must-not-return",
                "vnc_url": "https://console.invalid/vnc?token=secret",
            }
        }

        status, metadata = check_vultr_bare_metal_status("bm-1", {"access_token": "fake-token"})

        self.assertEqual(status, "available")
        self.assertEqual(get_json.call_args.args[0], "bare-metals/bm-1")
        self.assertNotIn("must-not-return", repr(metadata))
        self.assertNotIn("token=secret", repr(metadata))

    @patch("apps.monitoring.checks.vultr_compute.VultrReadOnlyClient.get_json")
    def test_snapshot_and_backup_checks_are_read_only(self, get_json):
        get_json.side_effect = [
            {"snapshot": {"id": "snap-1", "status": "completed"}},
            {"backup": {"id": "backup-1", "status": "completed"}},
        ]

        snapshot_status, _ = check_vultr_block_snapshot_status("snap-1", "fake-token")
        backup_status, _ = check_vultr_instance_backup_status("backup-1", "fake-token")

        self.assertEqual(snapshot_status, "available")
        self.assertEqual(backup_status, "available")
        self.assertEqual(
            [call.args[0] for call in get_json.call_args_list],
            ["blocks/snapshots/snap-1", "backups/backup-1"],
        )

    @patch("apps.monitoring.checks.vultr_compute.VultrReadOnlyClient.get_json")
    def test_bandwidth_handler_returns_only_redacted_metrics(self, get_json):
        get_json.return_value = {
            "bandwidth": {
                "incoming_bytes": 10,
                "outgoing_bytes": 20,
                "signed_url": "https://objects.invalid/a?signature=secret",
            }
        }

        status, metadata = check_vultr_bare_metal_bandwidth_status(
            "bm-1",
            {"access_token": "fake-token"},
        )

        self.assertEqual(status, "available")
        self.assertEqual(get_json.call_args.args[0], "bare-metals/bm-1/bandwidth")
        self.assertNotIn("signature=secret", repr(metadata))
        self.assertNotIn("fake-token", repr(metadata))

    def test_unsupported_vfs_has_no_client_path(self):
        with patch("apps.monitoring.checks.vultr_compute.VultrReadOnlyClient") as client:
            status, metadata = VULTR_COMPUTE_CHECKS["vfs"]("vfs-1", {"access_token": "fake-token"})

        self.assertEqual(status, "unsupported")
        self.assertEqual(metadata["errorCode"], "unsupported")
        client.assert_not_called()
