"""Focused, fully mocked tests for the read-only Hetzner monitoring checks."""

from unittest import TestCase
from unittest.mock import Mock, patch

import requests

from apps.monitoring.checks import hetzner_resources as checks


def response_for(payload=None, status_code=200, json_error=None):
    response = Mock()
    response.status_code = status_code
    if status_code >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(response=response)
    else:
        response.raise_for_status.return_value = None
    if json_error is not None:
        response.json.side_effect = json_error
    else:
        response.json.return_value = payload
    return response


class HetznerResourceChecksTestCase(TestCase):
    token = "test-token-that-must-not-appear-in-results"

    def assert_generic_error(self, result, status="error"):
        current_status, payload = result
        self.assertEqual(current_status, status)
        self.assertIsInstance(payload, dict)
        self.assertNotIn(self.token, repr(result))
        self.assertLess(len(repr(payload)), 4096)

    @patch("apps.monitoring.checks.hetzner_resources.requests.get")
    def test_registry_and_healthy_server_use_explicit_read_only_contract(self, mock_get):
        mock_get.return_value = response_for({"server": {"id": 101, "status": "running"}})

        status, metadata = checks.check_hetzner_server_status("101", self.token)

        self.assertEqual(status, "available")
        self.assertEqual(metadata["server"]["status"], "running")
        self.assertIs(checks.HETZNER_RESOURCE_CHECKS["server"], checks.check_hetzner_server_status)
        self.assertIs(checks.get_hetzner_check_function("servers"), checks.check_hetzner_server_status)
        mock_get.assert_called_once_with(
            "https://api.hetzner.cloud/v1/servers/101",
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
            timeout=15,
        )

    @patch("apps.monitoring.checks.hetzner_resources.requests.get")
    def test_generic_resource_check_supports_volume_and_url_escaping(self, mock_get):
        mock_get.return_value = response_for({"volume": {"status": "available"}})

        status, metadata = checks.check_hetzner_resource_status("volume", "id/with space", self.token)

        self.assertEqual(status, "available")
        self.assertEqual(metadata["volume"]["status"], "available")
        self.assertEqual(
            mock_get.call_args.args[0],
            "https://api.hetzner.cloud/v1/volumes/id%2Fwith%20space",
        )

    @patch("apps.monitoring.checks.hetzner_resources.requests.get")
    def test_degraded_provider_state_is_not_treated_as_transport_error(self, mock_get):
        mock_get.return_value = response_for({"load_balancer": {"status": "error"}})

        status, metadata = checks.check_hetzner_load_balancer_status("lb-1", self.token)

        self.assertEqual(status, "degraded")
        self.assertEqual(metadata["load_balancer"]["status"], "error")

    @patch("apps.monitoring.checks.hetzner_resources.requests.get")
    def test_unknown_provider_state_is_explicit(self, mock_get):
        mock_get.return_value = response_for({"server": {"status": "vendor-future-state"}})

        status, metadata = checks.check_hetzner_server_status("101", self.token)

        self.assertEqual(status, "unknown")
        self.assertEqual(metadata["server"]["status"], "vendor-future-state")

    @patch("apps.monitoring.checks.hetzner_resources.requests.get")
    def test_resource_without_lifecycle_status_is_available_when_present(self, mock_get):
        mock_get.return_value = response_for({"firewall": {"id": 7, "rules": []}})

        status, metadata = checks.check_hetzner_firewall_status("7", self.token)

        self.assertEqual(status, "available")
        self.assertEqual(metadata["firewall"]["rules"], [])

    @patch("apps.monitoring.checks.hetzner_resources.requests.get")
    def test_network_and_catalog_resources_use_presence_semantics(self, mock_get):
        mock_get.return_value = response_for({"network": {"id": 7, "subnets": [], "routes": []}})
        self.assertEqual(checks.check_hetzner_network_status("7", self.token)[0], "available")

        mock_get.return_value = response_for({"location": {"id": 8, "name": "fsn1"}})
        self.assertEqual(checks.check_hetzner_location_status("8", self.token)[0], "available")

    @patch("apps.monitoring.checks.hetzner_resources.requests.get")
    def test_load_balancer_target_health_is_reported(self, mock_get):
        mock_get.return_value = response_for({
            "load_balancer": {
                "id": 9,
                "targets": [{"health_status": "healthy"}, {"health_status": "unhealthy"}],
            },
        })

        status, _metadata = checks.check_hetzner_load_balancer_status("9", self.token)

        self.assertEqual(status, "degraded")

    @patch("apps.monitoring.checks.hetzner_resources.requests.get")
    def test_blocked_and_assigned_ip_states_are_derived(self, mock_get):
        mock_get.return_value = response_for({"primary_ip": {"blocked": True}})
        self.assertEqual(checks.check_hetzner_primary_ip_status("1", self.token)[0], "degraded")

        mock_get.return_value = response_for({"floating_ip": {"blocked": False, "assignee_id": 42}})
        self.assertEqual(checks.check_hetzner_floating_ip_status("2", self.token)[0], "assigned")

    @patch("apps.monitoring.checks.hetzner_resources.requests.get")
    def test_not_found_uses_shared_http_classifier_without_provider_text(self, mock_get):
        mock_get.return_value = response_for(status_code=404)

        status, payload = checks.check_hetzner_server_status("missing", self.token)

        self.assertEqual(status, "not_found")
        self.assertEqual(payload, {
            "error": "Hetzner resource was not found",
            "errorCode": "not_found",
        })

    @patch("apps.monitoring.checks.hetzner_resources.requests.get")
    def test_authentication_errors_are_normalized(self, mock_get):
        for status_code in (401, 403):
            with self.subTest(status_code=status_code):
                mock_get.return_value = response_for(status_code=status_code)
                status, payload = checks.check_hetzner_volume_status("1", self.token)
                self.assertEqual(status, "invalid_access_token")
                self.assertEqual(payload["errorCode"], "invalid_access_token")
                self.assertNotIn(str(status_code), repr(payload))

    @patch("apps.monitoring.checks.hetzner_resources.requests.get")
    def test_network_error_is_bounded_and_does_not_leak_exception_text(self, mock_get):
        mock_get.side_effect = requests.ConnectionError(f"authorization={self.token}")

        result = checks.check_hetzner_server_status("1", self.token)

        self.assert_generic_error(result)
        mock_get.assert_called_once()
        self.assertEqual(mock_get.call_args.kwargs["timeout"], checks.REQUEST_TIMEOUT_SECONDS)

    @patch("apps.monitoring.checks.hetzner_resources.requests.get")
    def test_malformed_json_and_response_shape_fail_closed(self, mock_get):
        mock_get.return_value = response_for(json_error=ValueError(f"secret={self.token}"))
        self.assert_generic_error(checks.check_hetzner_server_status("1", self.token))

        mock_get.return_value = response_for({"unexpected": []})
        self.assert_generic_error(checks.check_hetzner_server_status("1", self.token))

        mock_get.return_value = response_for([])
        self.assert_generic_error(checks.check_hetzner_server_status("1", self.token))

    @patch("apps.monitoring.checks.hetzner_resources.requests.get")
    def test_metrics_healthy_uses_explicit_context_and_redacts_metadata(self, mock_get):
        mock_get.return_value = response_for({
            "metrics": {
                "start": "2026-08-03T00:00:00+00:00",
                "end": "2026-08-03T00:15:00+00:00",
                "step": 60,
                "time_series": {
                    "cpu": {"values": [[1, "0.25"], [2, "0.5"]]},
                },
                "secret_token": self.token,
            },
        })
        credentials = {
            "access_token": self.token,
            "metrics": {
                "type": "cpu",
                "start": "2026-08-03T00:00:00+00:00",
                "end": "2026-08-03T00:15:00+00:00",
            },
        }

        status, metadata = checks.check_hetzner_server_metrics_status("101", credentials)

        self.assertEqual(status, "available")
        self.assertEqual(metadata["metrics"]["time_series"]["cpu"]["values"][0], [1, "0.25"])
        self.assertEqual(metadata["metrics"]["secret_token"], "[REDACTED]")
        self.assertNotIn(self.token, repr(metadata))
        self.assertEqual(mock_get.call_args.kwargs["params"], credentials["metrics"])

    @patch("apps.monitoring.checks.hetzner_resources.requests.get")
    def test_metrics_with_no_points_is_unknown(self, mock_get):
        mock_get.return_value = response_for({
            "metrics": {"time_series": {"cpu": {"values": []}}},
        })

        status, _metadata = checks.check_hetzner_server_metrics_status("101", self.token)

        self.assertEqual(status, "unknown")

    @patch("apps.monitoring.checks.hetzner_resources.requests.get")
    def test_metrics_malformed_series_is_error(self, mock_get):
        mock_get.return_value = response_for({
            "metrics": {"time_series": {"cpu": {"values": [[1]]}}},
        })

        self.assert_generic_error(checks.check_hetzner_server_metrics_status("101", self.token))

    @patch("apps.monitoring.checks.hetzner_resources.requests.get")
    def test_metrics_window_rejects_invalid_or_unbounded_ranges(self, mock_get):
        credentials = {
            "access_token": self.token,
            "metrics": {
                "type": "cpu",
                "start": "2026-01-01T00:00:00Z",
                "end": "2026-02-01T00:00:00Z",
            },
        }

        self.assert_generic_error(checks.check_hetzner_server_metrics_status("101", credentials))
        mock_get.assert_not_called()

    @patch("apps.monitoring.checks.hetzner_resources.requests.get")
    def test_action_status_distinguishes_pending_and_failed(self, mock_get):
        mock_get.return_value = response_for({"action": {"status": "running", "progress": 40}})
        self.assertEqual(checks.check_hetzner_action_status("action-1", self.token)[0], "pending")

        mock_get.return_value = response_for({"action": {"status": "error"}})
        self.assertEqual(checks.check_hetzner_action_status("action-1", self.token)[0], "degraded")

    @patch("apps.monitoring.checks.hetzner_resources.requests.get")
    def test_rrset_status_uses_parent_zone_context(self, mock_get):
        mock_get.return_value = response_for({
            "rrset": {"name": "www", "type": "A", "records": [{"value": "192.0.2.10"}]},
        })
        credentials = {
            "access_token": self.token,
            "zone_id": "10",
            "rr_name": "www",
            "rr_type": "A",
        }

        status, _metadata = checks.check_hetzner_rrset_status("10:www:A", credentials)

        self.assertEqual(status, "available")
        self.assertEqual(
            mock_get.call_args.args[0],
            "https://api.hetzner.cloud/v1/zones/10/rrsets/www/A",
        )

    @patch("apps.monitoring.checks.hetzner_resources.requests.get")
    def test_load_balancer_metrics_use_bounded_explicit_query(self, mock_get):
        mock_get.return_value = response_for({
            "metrics": {"time_series": {"open_connections": {"values": [[1, "2"]]}}},
        })
        credentials = {
            "access_token": self.token,
            "metrics": {
                "type": "open_connections",
                "start": "2026-08-03T00:00:00Z",
                "end": "2026-08-03T00:15:00Z",
            },
        }

        status, _metadata = checks.check_hetzner_load_balancer_metrics_status("9", credentials)

        self.assertEqual(status, "available")
        self.assertEqual(mock_get.call_args.kwargs["params"], credentials["metrics"])

    @patch("apps.monitoring.checks.hetzner_resources.boto3.client")
    def test_object_storage_monitor_uses_head_bucket_only(self, mock_client):
        client = mock_client.return_value
        credentials = {
            "access_key": "s3-access",
            "secret_key": "s3-secret",
            "region": "nbg1",
            "bucket": "bucket-a",
        }

        status, metadata = checks.check_hetzner_object_storage_status("bucket-a", credentials)

        self.assertEqual(status, "available")
        self.assertEqual(metadata["bucket"]["name"], "bucket-a")
        client.head_bucket.assert_called_once_with(Bucket="bucket-a")
        self.assertFalse(client.list_objects_v2.called)

    def test_invalid_credentials_and_unsupported_types_do_not_call_provider(self):
        with patch("apps.monitoring.checks.hetzner_resources.requests.get") as mock_get:
            self.assertEqual(checks.check_hetzner_server_status("1", {})[0], "invalid_access_token")
            self.assertEqual(checks.check_hetzner_server_status("1", "")[0], "invalid_access_token")
            self.assertEqual(checks.check_hetzner_resource_status("unsupported", "1", self.token)[0], "error")
            self.assertEqual(checks.check_hetzner_resource_status("server", "", self.token)[0], "error")
            mock_get.assert_not_called()

    def test_get_check_function_rejects_unsupported_types(self):
        with self.assertRaises(ValueError):
            checks.get_hetzner_check_function("database")

    def test_registry_resolves_nested_and_object_storage_types(self):
        self.assertIs(checks.get_hetzner_check_function("server"), checks.check_hetzner_server_status)
        self.assertIs(checks.get_hetzner_check_function("rrsets"), checks.check_hetzner_rrset_status)
        self.assertIs(
            checks.get_hetzner_check_function("object-storage"),
            checks.check_hetzner_object_storage_status,
        )
