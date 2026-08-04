"""Credential-free, fully mocked tests for Vultr account operations."""

from __future__ import annotations

import importlib
import sys
import types
from unittest.mock import Mock, patch

from django.test import SimpleTestCase


# The account-operation worker is intentionally delivered alongside a base
# worker.  This small test-only compatibility module lets this focused suite
# run on the isolated branch before that sibling file is merged; it is never
# used by production code.
try:
    importlib.import_module("apps.console.cloud.vultr.resources_base")
except ModuleNotFoundError as error:
    if error.name != "apps.console.cloud.vultr.resources_base":
        raise

    base_module = types.ModuleType("apps.console.cloud.vultr.resources_base")

    class _TestResourceSpec:
        def __init__(
            self,
            key,
            endpoint,
            collection_key,
            asset_type,
            identifier_fields=("id",),
            name_fields=("name",),
            **kwargs,
        ):
            self.key = key
            self.endpoint = endpoint
            self.collection_key = collection_key
            self.asset_type = asset_type
            self.identifier_fields = tuple(identifier_fields)
            self.name_fields = tuple(name_fields)

    base_module.VultrClient = type("VultrClient", (), {})
    base_module.VultrResourceSpec = _TestResourceSpec
    sys.modules[base_module.__name__] = base_module


from apps.console.cloud.vultr import resources_account_operations as resources
from apps.monitoring.checks import vultr_account_operations as checks


TOKEN = "vultr-test-token-that-must-not-escape"


class FakeVultrClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, endpoint, params=None):
        params = dict(params or {})
        self.calls.append((endpoint, params))
        value = self.responses[endpoint]
        if callable(value):
            value = value(params)
        if isinstance(value, BaseException):
            raise value
        return value


class VultrAccountOperationsTests(SimpleTestCase):
    def test_account_profile_is_get_only_and_redacts_sensitive_fields(self):
        client = FakeVultrClient(
            {
                "account": {
                    "account": {
                        "id": "acct-1",
                        "name": "Operations",
                        "email": "private@example.test",
                        "api_key": TOKEN,
                        "balance": "12.50",
                    }
                }
            }
        )

        result = resources.get_vultr_account_profile(client)

        self.assertEqual(result["status"], "complete")
        record = result["records"][0]
        self.assertEqual(record["id"], "acct-1")
        self.assertNotIn("email", repr(result))
        self.assertNotIn(TOKEN, repr(result))
        self.assertEqual(client.calls, [("account", {})])

    def test_account_logs_are_bounded_and_drop_headers_urls_queries_and_tokens(self):
        def response(params):
            if params.get("cursor") == "cursor-2":
                return {
                    "logs": [
                        {
                            "id": "log-2",
                            "action": "read",
                            "description": f"token={TOKEN}",
                            "headers": {"Authorization": f"Bearer {TOKEN}"},
                            "url": f"https://example.test/a?token={TOKEN}",
                            "query": {"api_key": TOKEN},
                        }
                    ],
                    "meta": {"links": {"next": None}},
                }
            return {
                "logs": [
                    {
                        "id": "log-1",
                        "action": "list",
                        "description": f"authorization={TOKEN}",
                        "request_headers": {"X-Api-Key": TOKEN},
                        "shortlink": f"https://status.test/?signature={TOKEN}",
                    }
                ],
                "meta": {"links": {"next": "cursor-2"}},
            }

        client = FakeVultrClient({"account/log": response})

        result = resources.list_vultr_account_logs(client, per_page=1, max_pages=5)

        self.assertEqual(result["status"], "complete")
        self.assertEqual([item["id"] for item in result["records"]], ["log-1", "log-2"])
        self.assertNotIn(TOKEN, repr(result))
        self.assertNotIn("headers", repr(result))
        self.assertNotIn("shortlink", repr(result))
        self.assertEqual(client.calls[0], ("account/log", {"per_page": 1}))
        self.assertEqual(client.calls[1], ("account/log", {"per_page": 1, "cursor": "cursor-2"}))

    def test_log_pagination_returns_partial_at_the_page_bound(self):
        def response(params):
            cursor = params.get("cursor") or "first"
            return {
                "logs": [{"id": cursor, "action": "read"}],
                "meta": {"links": {"next": f"cursor-{len(self._calls_seen(client)) + 1}"}},
            }

        client = FakeVultrClient({"account/log": response})

        result = resources.list_vultr_account_logs(client, per_page=1, max_pages=2)

        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["partial"])
        self.assertEqual(len(client.calls), 2)
        self.assertLessEqual(max(call[1]["per_page"] for call in client.calls), 100)

    @staticmethod
    def _calls_seen(client):
        return client.calls

    @patch("apps.monitoring.checks.vultr_account_operations.requests.get")
    def test_status_json_is_unauthenticated_and_separate_from_control_plane(self, mock_get):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "page": {"name": "Vultr", "url": "https://status.vultr.com/?token=should-not-return"},
            "components": [{"id": "api", "name": "API", "status": "operational"}],
            "incidents": [
                {
                    "id": "inc-1",
                    "name": "API issue",
                    "status": "investigating",
                    "impact": "major",
                    "shortlink": "https://status.vultr.com/incidents/signed?sig=secret",
                }
            ],
            "scheduled_maintenances": [],
        }
        mock_get.return_value = response

        status, payload = checks.check_vultr_status_incident_status(credentials=None)

        self.assertEqual(status, "degraded")
        self.assertEqual(payload[resources.VULTR_STATUS_INCIDENT]["controlPlane"], "vultr_status_json")
        self.assertNotIn(TOKEN, repr(payload))
        self.assertNotIn("shortlink", repr(payload))
        kwargs = mock_get.call_args.kwargs
        self.assertNotIn("Authorization", kwargs["headers"])
        self.assertEqual(mock_get.call_args.args[0], resources.VULTR_STATUS_JSON_URL)

    def test_unsupported_api_key_metadata_is_explicit_and_does_not_call_provider(self):
        result = resources.get_vultr_api_key_metadata()

        self.assertEqual(result["status"], "unsupported")
        self.assertEqual(result["records"], [])
        self.assertIn("no documented", result["reason"])

        status, payload = checks.check_vultr_api_key_metadata_status("api-key", {"access_token": TOKEN})
        self.assertEqual(status, "unsupported")
        self.assertEqual(payload[resources.VULTR_API_KEY_METADATA]["status"], "unsupported")
        self.assertNotIn(TOKEN, repr(payload))

    @patch("apps.console.cloud.vultr.resources_account_operations.VultrClient")
    def test_missing_credentials_are_credential_free_and_do_not_construct_client(self, mock_client):
        status, payload = checks.check_vultr_account_status("account", {})

        self.assertEqual(status, "credentials_unavailable")
        self.assertEqual(payload[resources.VULTR_ACCOUNT_PROFILE]["errorCode"], "credentials_unavailable")
        self.assertNotIn(TOKEN, repr(payload))
        mock_client.assert_not_called()

    def test_iam_records_never_return_api_key_or_private_contact_data(self):
        client = FakeVultrClient(
            {
                "account/users": {
                    "users": [
                        {
                            "id": "user-1",
                            "name": "operator",
                            "role": "admin",
                            "email": "private@example.test",
                            "api_key": TOKEN,
                            "permissions": ["read"],
                        }
                    ],
                    "meta": {"links": {"next": None}},
                }
            }
        )

        result = resources.list_vultr_iam_users(client)

        self.assertEqual(result["status"], "complete")
        self.assertNotIn("email", repr(result))
        self.assertNotIn(TOKEN, repr(result))
        self.assertNotIn("api_key", repr(result))


if __name__ == "__main__":  # pragma: no cover
    import unittest

    unittest.main()
