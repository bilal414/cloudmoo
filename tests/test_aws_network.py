from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from botocore.exceptions import ClientError
from django.test import SimpleTestCase

from apps.console.cloud.aws.network import (
    AWS_NETWORK_COLLECTION_SPECS,
    CoreAWSSubnet,
    CoreAWSVPC,
    sync_aws_network_assets,
)
from apps.monitoring.checks.aws_network import (
    check_aws_instance_health_status,
    check_aws_vpc_status,
)


class _MemoryQuerySet:
    def __init__(self, rows):
        self.rows = rows

    def exclude(self, **kwargs):
        values = set(kwargs.get("unique_id__in", []))
        return _MemoryQuerySet([row for row in self.rows if row.unique_id not in values])

    def update(self, **kwargs):
        for row in self.rows:
            for key, value in kwargs.items():
                setattr(row, key, value)
        return len(self.rows)


class _MemoryManager:
    def __init__(self, model):
        self.model = model
        self.rows = []

    def get_or_create(self, owner, unique_id, defaults):
        for row in self.rows:
            if row.owner is owner and row.unique_id == unique_id:
                return row, False

        row = SimpleNamespace(
            owner=owner,
            unique_id=unique_id,
            Monitoring=self.model.Monitoring,
            save=lambda *args, **kwargs: None,
            **defaults,
        )
        self.rows.append(row)
        return row, True

    def filter(self, **kwargs):
        rows = self.rows
        for key, value in kwargs.items():
            rows = [row for row in rows if getattr(row, key) == value]
        return _MemoryQuerySet(rows)


class _Paginator:
    def __init__(self, client, operation):
        self.client = client
        self.operation = operation

    def paginate(self, **kwargs):
        self.client.calls.append((self.operation, kwargs))
        failure = self.client.failures.get(self.operation)
        if failure:
            raise failure
        return iter(self.client.pages.get(self.operation, []))


class _ReadOnlyInventoryClient:
    def __init__(self, pages=None, failures=None):
        self.pages = pages or {}
        self.failures = failures or {}
        self.calls = []

    def get_paginator(self, operation):
        return _Paginator(self, operation)

    def __getattr__(self, name):
        raise AssertionError(f"Unexpected boto3 operation referenced: {name}")


class _ReadOnlyStatusClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def describe_vpcs(self, **kwargs):
        self.calls.append(("describe_vpcs", kwargs))
        return self.response

    def describe_instance_status(self, **kwargs):
        self.calls.append(("describe_instance_status", kwargs))
        return self.response

    def __getattr__(self, name):
        raise AssertionError(f"Unexpected boto3 operation referenced: {name}")


def _empty_pages():
    response_keys = {
        "describe_vpcs": "Vpcs",
        "describe_subnets": "Subnets",
        "describe_route_tables": "RouteTables",
        "describe_internet_gateways": "InternetGateways",
        "describe_nat_gateways": "NatGateways",
        "describe_network_acls": "NetworkAcls",
        "describe_network_interfaces": "NetworkInterfaces",
        "describe_vpc_peering_connections": "VpcPeeringConnections",
        "describe_transit_gateway_attachments": "TransitGatewayAttachments",
        "describe_vpn_connections": "VpnConnections",
        "describe_flow_logs": "FlowLogs",
        "describe_auto_scaling_groups": "AutoScalingGroups",
        "describe_launch_templates": "LaunchTemplates",
        "describe_images": "Images",
        "describe_volumes": "Volumes",
    }
    return {operation: [{key: []}] for operation, key in response_keys.items()}


class AWSNetworkAdapterTestCase(SimpleTestCase):
    def setUp(self):
        self.account = SimpleNamespace(
            access_key="access-key",
            secret_key="secret-key",
            region="us-east-1",
        )

    def _patch_managers(self, stack):
        managers = {}
        for spec in AWS_NETWORK_COLLECTION_SPECS:
            manager = _MemoryManager(spec["model"])
            managers[spec["provider_type"]] = manager
            stack.enter_context(patch.object(spec["model"], "objects", manager))
        return managers

    def test_sync_is_regional_paginated_redacted_and_read_only(self):
        pages = _empty_pages()
        pages["describe_vpcs"] = [
            {
                "Vpcs": [
                    {
                        "VpcId": "vpc-1",
                        "Tags": [{"Key": "Name", "Value": "primary"}],
                        "CreatedAt": datetime(2026, 8, 2, tzinfo=timezone.utc),
                        "SecretToken": "must-not-persist",
                    }
                ]
            },
            {"Vpcs": [{"VpcId": "vpc-2"}]},
        ]
        pages["describe_volumes"] = [
            {
                "Volumes": [
                    {
                        "VolumeId": "vol-1",
                        "Attachments": [
                            {
                                "AttachmentId": "attach-1",
                                "InstanceId": "i-1",
                                "Device": "/dev/sda1",
                                "State": "attached",
                            }
                        ],
                    }
                ]
            }
        ]
        ec2 = _ReadOnlyInventoryClient(pages=pages)
        autoscaling = _ReadOnlyInventoryClient(pages=pages)

        def client_for(_account, service, region=None):
            self.assertEqual(region, "us-east-1")
            return autoscaling if service == "autoscaling" else ec2

        with patch(
            "apps.console.cloud.aws.network.get_enabled_regions",
            return_value=["us-east-1"],
        ), patch("apps.console.cloud.aws.network.aws_client", side_effect=client_for):
            from contextlib import ExitStack

            with ExitStack() as stack:
                managers = self._patch_managers(stack)
                summary = sync_aws_network_assets(self.account)

        self.assertEqual(summary["regions"], ["us-east-1"])
        self.assertEqual(summary["families"]["aws_vpc"]["us-east-1"]["count"], 2)
        self.assertEqual(
            summary["families"]["aws_ebs_attachment"]["us-east-1"]["count"],
            1,
        )
        self.assertEqual(len(managers["aws_vpc"].rows), 2)
        vpc = managers["aws_vpc"].rows[0]
        self.assertEqual(vpc.unique_id, "us-east-1|vpc-1")
        self.assertEqual(vpc.type, "vpc")
        self.assertEqual(vpc.metadata["CreatedAt"], "2026-08-02T00:00:00+00:00")
        self.assertEqual(vpc.metadata["SecretToken"], "[REDACTED]")
        self.assertEqual(vpc.metadata["_cloudmoo_raw_id"], "vpc-1")

        calls = ec2.calls + autoscaling.calls
        self.assertTrue(calls)
        self.assertTrue(all(operation.startswith("describe_") for operation, _ in calls))
        self.assertFalse(summary["errors"])

    def test_failed_region_family_is_not_reconciled(self):
        pages = _empty_pages()
        us_ec2 = _ReadOnlyInventoryClient(pages=pages)
        eu_ec2 = _ReadOnlyInventoryClient(
            pages=pages,
            failures={
                "describe_subnets": ClientError(
                    {"Error": {"Code": "RequestLimitExceeded"}},
                    "DescribeSubnets",
                )
            },
        )
        autoscaling = _ReadOnlyInventoryClient(pages=pages)

        def client_for(_account, service, region=None):
            # The fake failure is deliberately only for the second region's
            # subnet family; all other family/region scopes remain valid.
            if service == "autoscaling":
                return autoscaling
            return eu_ec2 if region == "eu-west-1" else us_ec2

        with patch(
            "apps.console.cloud.aws.network.get_enabled_regions",
            return_value=["us-east-1", "eu-west-1"],
        ), patch("apps.console.cloud.aws.network.aws_client", side_effect=client_for):
            from contextlib import ExitStack

            with ExitStack() as stack:
                managers = self._patch_managers(stack)
                existing = SimpleNamespace(
                    owner=self.account,
                    unique_id="eu-west-1|subnet-old",
                    region="eu-west-1",
                    monitoring=CoreAWSSubnet.Monitoring.ACTIVE,
                )
                managers["aws_subnet"].rows.append(existing)
                summary = sync_aws_network_assets(self.account)

        result = summary["families"]["aws_subnet"]["eu-west-1"]
        self.assertFalse(result["complete"])
        self.assertFalse(result["reconciled"])
        self.assertEqual(result["error"]["kind"], "incomplete_inventory")
        self.assertEqual(existing.monitoring, CoreAWSSubnet.Monitoring.ACTIVE)
        self.assertEqual(summary["families"]["aws_subnet"]["us-east-1"]["count"], 0)

    @patch("apps.monitoring.checks.aws_network.boto3.client")
    def test_status_uses_regional_raw_id_and_sanitized_metadata(self, boto_client):
        client = _ReadOnlyStatusClient(
            {
                "Vpcs": [
                    {
                        "VpcId": "vpc-123",
                        "State": "available",
                        "SecretToken": "must-not-return",
                    }
                ]
            }
        )
        boto_client.return_value = client

        status, metadata = check_aws_vpc_status(
            "eu-west-1|vpc-123",
            {
                "access_key": "access-key",
                "secret_key": "secret-key",
                "region": "eu-west-1",
            },
        )

        self.assertEqual(status, "available")
        self.assertEqual(client.calls[0][1]["VpcIds"], ["vpc-123"])
        self.assertEqual(metadata["vpc"]["SecretToken"], "[REDACTED]")
        self.assertNotIn("secret-key", str(metadata))

    @patch("apps.monitoring.checks.aws_network.boto3.client")
    def test_instance_health_uses_only_describe_instance_status(self, boto_client):
        client = _ReadOnlyStatusClient(
            {
                "InstanceStatuses": [
                    {
                        "InstanceId": "i-123",
                        "SystemStatus": {"Status": "ok"},
                        "InstanceStatus": {"Status": "ok"},
                    }
                ]
            }
        )
        boto_client.return_value = client

        status, metadata = check_aws_instance_health_status(
            "us-east-1|i-123",
            {
                "access_key": "access-key",
                "secret_key": "secret-key",
                "region": "us-east-1",
            },
        )

        self.assertEqual(status, "ok")
        self.assertEqual(client.calls[0][0], "describe_instance_status")
        self.assertTrue(client.calls[0][1]["IncludeAllInstances"])
        self.assertEqual(metadata["instance_health"]["InstanceId"], "i-123")
