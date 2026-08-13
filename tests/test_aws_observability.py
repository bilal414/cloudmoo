from unittest.mock import patch

from django.test import SimpleTestCase

from apps.console.cloud.aws import observability
from apps.console.cloud.aws.observability import (
    ASSET_TYPE_CLOUDWATCH_ALARM,
    ASSET_TYPE_CLOUDWATCH_METRIC,
    ASSET_TYPE_LOG_GROUP,
    CoreAWSCloudWatchAlarm,
    CoreAWSCloudWatchMetric,
    CoreAWSLogGroup,
    sync_aws_observability_assets,
)
from apps.monitoring.checks import aws_observability


class FakePaginator:
    def __init__(self, client, operation):
        self.client = client
        self.operation = operation

    def paginate(self, **kwargs):
        return self.client.page_factory(self.client, self.operation, **kwargs)


class FakeCloudWatchClient:
    def __init__(self, region, page_factory):
        self.region = region
        self.page_factory = page_factory
        self.operations = []

    def get_paginator(self, operation):
        return FakePaginator(self, operation)

    def __getattr__(self, name):
        if name.startswith(("put_", "delete_", "disable_", "enable_", "update_")):
            raise AssertionError(f"mutating AWS method attempted: {name}")
        raise AttributeError(name)


class FakeAsset:
    def __init__(self, owner, region, unique_id, defaults):
        self.owner = owner
        self.region = region
        self.unique_id = unique_id
        self.name = defaults.get("name")
        self.type = defaults.get("type")
        self.metadata = defaults.get("metadata")
        self.monitoring = defaults.get("monitoring")

    def save(self):
        return None


class FakeQuerySet:
    def __init__(self, rows):
        self.rows = rows

    def exclude(self, **kwargs):
        current_ids = set(kwargs.get("unique_id__in", []))
        self.rows = [row for row in self.rows if row.unique_id not in current_ids]
        return self

    def update(self, **kwargs):
        for row in self.rows:
            for key, value in kwargs.items():
                setattr(row, key, value)
        return len(self.rows)


class FakeManager:
    def __init__(self, owner, rows=()):
        self.owner = owner
        self.rows = list(rows)

    def get_or_create(self, *, owner, region, unique_id, defaults):
        for row in self.rows:
            if row.owner is owner and row.region == region and row.unique_id == unique_id:
                return row, False
        row = FakeAsset(owner, region, unique_id, defaults)
        self.rows.append(row)
        return row, True

    def filter(self, *, owner, region):
        return FakeQuerySet([
            row for row in self.rows
            if row.owner is owner and row.region == region
        ])


class AWSObservabilityInventoryTest(SimpleTestCase):
    def setUp(self):
        self.account = object()
        self.clients = {}
        self.operations = []

    def pages(self, client, operation, **_kwargs):
        self.operations.append(operation)
        if client.region == "us-west-2":
            raise RuntimeError("regional provider outage")
        if operation == "describe_alarms":
            yield {
                "MetricAlarms": [{
                    "AlarmArn": "arn:aws:cloudwatch:us-east-1:123:alarm:cpu",
                    "AlarmName": "cpu",
                    "StateValue": "ALARM",
                    "Namespace": "AWS/EC2",
                    "MetricName": "CPUUtilization",
                    "Dimensions": [
                        {"Name": "InstanceId", "Value": "i-123"},
                        {"Name": "SecretToken", "Value": "never-store"},
                    ],
                    "AlarmActions": ["arn:aws:sns:us-east-1:123:topic"],
                }],
                "CompositeAlarms": [],
            }
            yield {
                "MetricAlarms": [],
                "CompositeAlarms": [{
                    "AlarmArn": "arn:aws:cloudwatch:us-east-1:123:alarm:service",
                    "AlarmName": "service",
                    "StateValue": "OK",
                    "AlarmRule": "ALARM(cpu)",
                    "OKActions": ["arn:aws:sns:us-east-1:123:topic"],
                }],
            }
        elif operation == "list_metrics":
            yield {
                "Metrics": [{
                    "Namespace": "AWS/EC2",
                    "MetricName": "CPUUtilization",
                    "Dimensions": [{"Name": "InstanceId", "Value": "i-123"}],
                }],
            }
            yield {
                "Metrics": [{
                    "Namespace": "Custom",
                    "MetricName": "Requests",
                    "Dimensions": [],
                }],
            }
        elif operation == "describe_log_groups":
            yield {
                "logGroups": [{
                    "logGroupName": "/service/api",
                    "retentionInDays": 30,
                    "metricFilterCount": 2,
                    "storedBytes": 123,
                    "kmsKeyId": "arn:aws:kms:us-east-1:123:key/example",
                }],
            }
        else:
            raise AssertionError(f"unexpected operation: {operation}")

    @staticmethod
    def require_collection(payload, key, _context):
        return payload[key]

    @staticmethod
    def serialize(value):
        return value

    def client_factory(self, _account, _service, *, region):
        self.clients.setdefault(region, FakeCloudWatchClient(region, self.pages))
        return self.clients[region]

    def test_inventory_is_paginated_redacted_and_read_only(self):
        alarm_manager = FakeManager(self.account)
        metric_manager = FakeManager(self.account)
        log_manager = FakeManager(self.account)
        with patch.object(CoreAWSCloudWatchAlarm, "objects", alarm_manager), \
             patch.object(CoreAWSCloudWatchMetric, "objects", metric_manager), \
             patch.object(CoreAWSLogGroup, "objects", log_manager), \
             patch.object(observability, "get_enabled_regions", return_value=["us-east-1", "us-west-2"]), \
             patch.object(observability, "aws_client", new=self.client_factory):
            result = sync_aws_observability_assets(self.account)

        self.assertEqual(result["regions"], ["us-east-1", "us-west-2"])
        self.assertEqual(result["counts"][ASSET_TYPE_CLOUDWATCH_ALARM], 2, result)
        self.assertEqual(result["counts"][ASSET_TYPE_CLOUDWATCH_METRIC], 2)
        self.assertEqual(result["counts"][ASSET_TYPE_LOG_GROUP], 1)
        self.assertEqual(len(result["errors"]), 3)
        self.assertEqual(self.operations.count("list_metrics"), 2)
        # CloudWatch metric series are inventory-only: no per-minute checks.
        self.assertTrue(all(row.monitoring == "disabled" for row in metric_manager.rows))
        self.assertTrue(all(row.monitoring == "active" for row in alarm_manager.rows))
        self.assertTrue(all(row.monitoring == "active" for row in log_manager.rows))
        self.assertEqual(self.operations.count("describe_alarms"), 2)
        self.assertEqual(self.operations.count("describe_log_groups"), 2)

        alarm = alarm_manager.rows[0]
        self.assertNotIn("AlarmActions", alarm.metadata)
        self.assertNotIn("OKActions", alarm.metadata)
        self.assertEqual(alarm.metadata["Dimensions"], [{"Name": "InstanceId", "Value": "i-123"}])
        self.assertTrue(all(
            not operation.startswith(("put_", "delete_", "disable_", "enable_", "update_"))
            for operation in self.operations
        ))

    def test_failed_region_keeps_previous_assets_present(self):
        active = "active"
        previous_alarm = FakeAsset(
            self.account,
            "us-west-2",
            "old-alarm",
            {"name": "old", "type": ASSET_TYPE_CLOUDWATCH_ALARM, "metadata": {}, "monitoring": active},
        )
        previous_metric = FakeAsset(
            self.account,
            "us-west-2",
            "old-metric",
            {"name": "old", "type": ASSET_TYPE_CLOUDWATCH_METRIC, "metadata": {}, "monitoring": active},
        )
        previous_log = FakeAsset(
            self.account,
            "us-west-2",
            "old-log",
            {"name": "old", "type": ASSET_TYPE_LOG_GROUP, "metadata": {}, "monitoring": active},
        )
        managers = (
            FakeManager(self.account, [previous_alarm]),
            FakeManager(self.account, [previous_metric]),
            FakeManager(self.account, [previous_log]),
        )
        with patch.object(CoreAWSCloudWatchAlarm, "objects", managers[0]), \
             patch.object(CoreAWSCloudWatchMetric, "objects", managers[1]), \
             patch.object(CoreAWSLogGroup, "objects", managers[2]), \
             patch.object(observability, "get_enabled_regions", return_value=["us-west-2"]), \
             patch.object(observability, "aws_client", new=self.client_factory):
            result = sync_aws_observability_assets(self.account)

        self.assertEqual(len(result["errors"]), 3)
        self.assertEqual(previous_alarm.monitoring, active)
        self.assertEqual(previous_metric.monitoring, active)
        self.assertEqual(previous_log.monitoring, active)


class AWSObservabilityChecksTest(SimpleTestCase):
    @staticmethod
    def require_collection(payload, key, _context):
        return payload[key]

    @staticmethod
    def serialize(value):
        return value

    def setUp(self):
        self.client = FakeCloudWatchClient("us-east-1", lambda *_args, **_kwargs: iter(()))

    def test_alarm_states_include_composite_alarms(self):
        for state in ("OK", "ALARM", "INSUFFICIENT_DATA"):
            def pages(_client, operation, **_kwargs):
                self.assertEqual(operation, "describe_alarms")
                yield {
                    "MetricAlarms": [],
                    "CompositeAlarms": [{
                        "AlarmName": "service",
                        "AlarmArn": "arn:aws:cloudwatch:us-east-1:123:alarm:service",
                        "StateValue": state,
                        "AlarmRule": "ALARM(child)",
                        "AlarmActions": ["sns-target-must-not-appear"],
                    }],
                }

            with patch.object(aws_observability, "aws_client", return_value=self.client), \
                 patch.object(aws_observability, "iter_pages", side_effect=pages), \
                 patch.object(aws_observability, "require_collection", side_effect=self.require_collection), \
                 patch.object(aws_observability, "serialize_aws", side_effect=self.serialize), \
                 patch.object(observability, "serialize_aws", side_effect=self.serialize):
                result = aws_observability.check_aws_aws_cloudwatch_alarm_status(
                    "aws_cloudwatch_alarm:us-east-1:service",
                    {
                        "access_key": "test-access-key",
                        "secret_key": "test-secret-key",
                        "region": "us-east-1",
                        "metadata": {"AlarmName": "service"},
                    },
                )
            self.assertEqual(result[0], state)
            self.assertNotIn("AlarmActions", result[1][ASSET_TYPE_CLOUDWATCH_ALARM])

    def test_metric_no_data_uses_bounded_read_only_window(self):
        calls = []

        def pages(_client, operation, **kwargs):
            calls.append((operation, kwargs))
            yield {"MetricDataResults": [{"Id": "cloudmoo_metric", "Values": [], "Timestamps": []}]}

        self.client.get_metric_data = lambda **_kwargs: None
        with patch.object(aws_observability, "aws_client", return_value=self.client), \
             patch.object(aws_observability, "iter_pages", side_effect=pages), \
             patch.object(aws_observability, "require_collection", side_effect=self.require_collection), \
             patch.object(aws_observability, "serialize_aws", side_effect=self.serialize):
            status, metadata = aws_observability.check_aws_aws_cloudwatch_metric_status(
                "metric-id",
                {
                    "access_key": "test-access-key",
                    "secret_key": "test-secret-key",
                    "region": "us-east-1",
                    "metadata": {
                        "Namespace": "AWS/EC2",
                        "MetricName": "CPUUtilization",
                        "Dimensions": [],
                    },
                },
            )

        self.assertEqual(status, "no_data")
        self.assertEqual(calls[0][0], "get_metric_data")
        self.assertEqual(calls[0][1]["MaxDatapoints"], 100)
        self.assertEqual(metadata[ASSET_TYPE_CLOUDWATCH_METRIC]["datapoints"], [])

    def test_log_group_reports_configuration_without_log_messages(self):
        called = []

        def pages(_client, operation, **kwargs):
            called.append((operation, kwargs))
            yield {
                "logGroups": [{
                    "logGroupName": "/service/api",
                    "retentionInDays": 7,
                    "metricFilterCount": 1,
                }],
            }

        with patch.object(aws_observability, "aws_client", return_value=self.client), \
             patch.object(aws_observability, "iter_pages", side_effect=pages), \
             patch.object(aws_observability, "require_collection", side_effect=self.require_collection), \
             patch.object(aws_observability, "serialize_aws", side_effect=self.serialize), \
             patch.object(observability, "serialize_aws", side_effect=self.serialize):
            status, metadata = aws_observability.check_aws_aws_log_group_status(
                "aws_log_group:us-east-1:/service/api",
                {
                    "access_key": "test-access-key",
                    "secret_key": "test-secret-key",
                    "region": "us-east-1",
                    "metadata": {"logGroupName": "/service/api"},
                },
            )

        self.assertEqual(status, "available")
        self.assertEqual(metadata[ASSET_TYPE_LOG_GROUP]["retentionInDays"], 7)
        self.assertEqual(metadata[ASSET_TYPE_LOG_GROUP]["metricFilterCount"], 1)
        self.assertEqual([operation for operation, _kwargs in called], ["describe_log_groups"])

    def test_malformed_response_and_provider_errors_are_normalized_and_redacted(self):
        def malformed(_client, _operation, **_kwargs):
            yield {"MetricAlarms": []}

        with patch.object(aws_observability, "aws_client", return_value=self.client), \
             patch.object(aws_observability, "iter_pages", side_effect=malformed), \
             patch.object(aws_observability, "require_collection", side_effect=self.require_collection), \
             patch.object(aws_observability, "aws_error_code", return_value="AccessDeniedException"):
            status, metadata = aws_observability.check_aws_aws_cloudwatch_alarm_status(
                "alarm-id",
                {
                    "access_key": "test-access-key",
                    "secret_key": "test-secret-key",
                    "region": "us-east-1",
                    "metadata": {"AlarmName": "alarm"},
                },
            )
        self.assertEqual(status, "error")
        self.assertNotIn("password=super-secret", str(metadata))

        def denied(_client, _operation, **_kwargs):
            raise RuntimeError("password=super-secret")

        with patch.object(aws_observability, "aws_client", return_value=self.client), \
             patch.object(aws_observability, "iter_pages", side_effect=denied), \
             patch.object(aws_observability, "aws_error_code", return_value="AccessDeniedException"), \
             patch.object(observability, "aws_error_code", return_value="AccessDeniedException"):
            status, metadata = aws_observability.check_aws_aws_log_group_status(
                "log-id",
                {
                    "access_key": "test-access-key",
                    "secret_key": "test-secret-key",
                    "region": "us-east-1",
                    "metadata": {"logGroupName": "/service/api"},
                },
            )
        self.assertEqual(status, "invalid_access_token")
        self.assertNotIn("super-secret", str(metadata))
