from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError
from django.test import SimpleTestCase

from apps.console.cloud.models import CloudInventoryTransientError
from apps.console.cloud.aws.discovery import (
    aws_client,
    aws_error_code,
    get_enabled_regions,
    is_transient_aws_error,
    iter_pages,
    require_collection,
    serialize_aws,
)


class _FakePaginator:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self, **kwargs):
        return iter(self.pages)


class AWSDiscoveryTestCase(SimpleTestCase):
    def setUp(self):
        self.account = SimpleNamespace(
            access_key="access-key-for-test",
            secret_key="secret-key-for-test",
            region="us-east-1",
        )

    @patch("apps.console.cloud.aws.discovery.boto3.client")
    def test_aws_client_uses_account_credentials_and_bounded_config(self, client):
        expected_client = object()
        client.return_value = expected_client

        result = aws_client(self.account, "ec2", region="eu-west-1")

        self.assertIs(result, expected_client)
        client.assert_called_once()
        args, kwargs = client.call_args
        self.assertEqual(args, ("ec2",))
        self.assertEqual(kwargs["aws_access_key_id"], self.account.access_key)
        self.assertEqual(kwargs["aws_secret_access_key"], self.account.secret_key)
        self.assertEqual(kwargs["region_name"], "eu-west-1")
        self.assertEqual(kwargs["config"].connect_timeout, 5)
        self.assertEqual(kwargs["config"].read_timeout, 15)

    def test_iter_pages_collects_multiple_pages(self):
        pages = [
            {"Items": [{"id": "one"}]},
            {"Items": [{"id": "two"}]},
        ]
        client = Mock()
        client.get_paginator.return_value = _FakePaginator(pages)

        result = list(iter_pages(client, "list_items", OwnerId="owner"))

        self.assertEqual(result, pages)
        client.get_paginator.assert_called_once_with("list_items")

    def test_iter_pages_fails_closed_for_empty_or_malformed_pages(self):
        for pages in ([], [None]):
            client = Mock()
            client.get_paginator.return_value = _FakePaginator(pages)

            with self.assertRaises(CloudInventoryTransientError):
                list(iter_pages(client, "list_items"))

    def test_mutating_operations_are_rejected_before_client_call(self):
        client = Mock()

        with self.assertRaises(ValueError):
            list(iter_pages(client, "delete_object"))

        client.get_paginator.assert_not_called()

    def test_require_collection_rejects_missing_and_malformed_collections(self):
        with self.assertRaises(CloudInventoryTransientError):
            require_collection({}, ("Items",), "resource inventory")

        with self.assertRaises(CloudInventoryTransientError):
            require_collection({"Items": None}, ("Items",), "resource inventory")

        with self.assertRaises(CloudInventoryTransientError):
            require_collection({"Items": {}}, "Items", "resource inventory")

        self.assertEqual(
            require_collection({"Outer": {"Items": [1]}}, "Outer.Items", "resource inventory"),
            [1],
        )

    @patch("apps.console.cloud.aws.discovery.aws_client")
    def test_get_enabled_regions_is_sorted_and_excludes_unusable_entries(self, aws_client_mock):
        client = Mock()
        client.describe_regions.side_effect = [
            {
                "Regions": [
                    {"RegionName": "us-west-2", "OptInStatus": "opt-in-not-required"},
                    {"RegionName": "eu-west-1", "OptInStatus": "opted-in"},
                    {"RegionName": "us-east-1", "OptInStatus": "not-opted-in"},
                    {"RegionName": "  "},
                    {"OptInStatus": "opted-in"},
                    "not-a-region",
                ],
                "NextToken": "page-2",
            },
            {
                "Regions": [
                    {"RegionName": "eu-west-1", "OptInStatus": "opted-in"},
                    {"RegionName": "ap-south-1", "OptInStatus": "opted-in"},
                    {"RegionName": "broken", "Endpoint": None},
                ]
            },
        ]
        aws_client_mock.return_value = client

        result = get_enabled_regions(self.account)

        self.assertEqual(result, ["ap-south-1", "eu-west-1", "us-west-2"])
        aws_client_mock.assert_called_once_with(
            self.account,
            "ec2",
            region="us-east-1",
        )

    def test_serialize_aws_handles_datetime_decimal_and_redaction(self):
        created_at = datetime(2026, 8, 2, 12, 34, 56, 123456, tzinfo=timezone.utc)
        value = {
            "CreatedAt": created_at,
            "Amount": Decimal("12.3400"),
            "SecretToken": "do-not-persist",
            "Nested": [{"Value": Decimal("0.000000000000000001")}],
        }

        result = serialize_aws(value)

        self.assertEqual(result["CreatedAt"], "2026-08-02T12:34:56.123456+00:00")
        self.assertEqual(result["Amount"], "12.3400")
        self.assertEqual(result["SecretToken"], "[REDACTED]")
        self.assertEqual(result["Nested"][0]["Value"], "1E-18")

    def test_aws_error_code_and_transient_classification_are_payload_safe(self):
        throttled = ClientError(
            {
                "Error": {
                    "Code": "ThrottlingException",
                    "Message": "secret-token=should-not-be-returned",
                }
            },
            "DescribeInstances",
        )
        denied = ClientError(
            {
                "Error": {
                    "Code": "AccessDeniedException",
                    "Message": "access_key=should-not-be-returned",
                }
            },
            "DescribeInstances",
        )

        self.assertEqual(aws_error_code(throttled), "ThrottlingException")
        self.assertNotIn("secret-token", aws_error_code(throttled))
        self.assertTrue(is_transient_aws_error(throttled))
        self.assertFalse(is_transient_aws_error(denied))
        self.assertFalse(is_transient_aws_error(ValueError("not an AWS error")))
