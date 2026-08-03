from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from botocore.exceptions import ClientError, UnknownServiceError
from django.test import SimpleTestCase

from apps.console.cloud.aws import account_operations as inventory
from apps.console.cloud.models import CloudInventoryTransientError
from apps.console.utils.models import UtilAsset
from apps.monitoring.checks import aws_account_operations as checks


FROZEN_NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


def _aws_error(code):
    return ClientError(
        {"Error": {"Code": code, "Message": "secret=must-not-escape"}},
        "ReadOnlyOperation",
    )


class _Paginator:
    def __init__(self, client, operation):
        self.client = client
        self.operation = operation

    def paginate(self, **kwargs):
        self.client.calls.append((self.operation, kwargs))
        pages = self.client.pages.get(self.operation, [])
        if isinstance(pages, BaseException):
            raise pages
        if callable(pages):
            pages = pages(kwargs)
        return iter(pages)


class _ReadOnlyClient:
    MUTATING_PREFIXES = (
        "create", "delete", "put", "update", "start", "stop", "modify",
        "publish", "subscribe", "send", "set", "enable", "disable",
    )

    def __init__(self, *, pages=None, responses=None):
        self.pages = dict(pages or {})
        self.responses = dict(responses or {})
        self.calls = []

    def get_paginator(self, operation):
        if operation not in self.pages:
            raise AttributeError(operation)
        return _Paginator(self, operation)

    def __getattr__(self, operation):
        if operation.startswith(self.MUTATING_PREFIXES):
            raise AssertionError(f"mutating AWS operation attempted: {operation}")
        if operation not in self.responses:
            raise AttributeError(operation)

        def call(**kwargs):
            self.calls.append((operation, kwargs))
            value = self.responses[operation]
            if callable(value):
                value = value(kwargs)
            if isinstance(value, BaseException):
                raise value
            return value

        return call


class _MemoryQuerySet:
    def __init__(self, rows):
        self.rows = list(rows)

    def exclude(self, **kwargs):
        excluded = set(kwargs.get("provider_id__in", []))
        return _MemoryQuerySet([row for row in self.rows if row.provider_id not in excluded])

    def update(self, **kwargs):
        for row in self.rows:
            for key, value in kwargs.items():
                setattr(row, key, value)
        return len(self.rows)


class _MemoryManager:
    def __init__(self, owner=None, rows=()):
        self.owner = owner
        self.rows = list(rows)
        self.update_calls = []

    def get_or_create(self, *, owner, scope, provider_id, defaults):
        for row in self.rows:
            if row.owner is owner and row.scope == scope and row.provider_id == provider_id:
                return row, False
        row_defaults = {
            key: value
            for key, value in defaults.items()
            if key not in {"owner", "scope", "provider_id"}
        }
        row = SimpleNamespace(
            owner=owner,
            scope=scope,
            provider_id=provider_id,
            save=lambda: None,
            **row_defaults,
        )
        self.rows.append(row)
        return row, True

    def filter(self, *, owner, scope):
        rows = [
            row for row in self.rows
            if row.owner is owner and row.scope == scope
        ]
        queryset = _MemoryQuerySet(rows)
        original_update = queryset.update

        def update(**kwargs):
            self.update_calls.append((rows, kwargs))
            return original_update(**kwargs)

        queryset.update = update
        return queryset


def _account():
    return SimpleNamespace(
        access_key="ACCESS",
        secret_key="SECRET",
        region="eu-west-1",
    )


def _clients():
    health_arn = "arn:aws:health:us-east-1::event/AWS_EC2_OPEN"
    monitor_arn = "arn:aws:ce::123456789012:anomalymonitor/monitor-1"
    subscription_arn = "arn:aws:ce::123456789012:anomalysubscription/sub-1"

    health = _ReadOnlyClient(
        pages={
            "describe_events": [
                {
                    "events": [{
                        "arn": health_arn,
                        "service": "EC2",
                        "eventTypeCode": "AWS_EC2_OPEN",
                        "statusCode": "open",
                        "startTime": FROZEN_NOW,
                    }],
                    "nextToken": "health-page-2",
                },
                {"events": []},
            ],
        },
        responses={
            "describe_event_details": {
                "successfulSet": [{
                    "eventArn": health_arn,
                    "eventDescription": {
                        "latestDescription": "token=secret-value incident description",
                        "language": "en",
                    },
                }],
                "failedSet": [],
            },
        },
    )
    support = _ReadOnlyClient(
        responses={
            "describe_trusted_advisor_checks": {
                "checks": [{
                    "id": "check-1",
                    "name": "Low Utilization",
                    "category": "cost_optimizing",
                    "description": "safe description",
                    "metadata": ["Region", "Resource ID"],
                }],
            },
            "describe_trusted_advisor_check_result": {
                "result": {
                    "checkId": "check-1",
                    "status": "warning",
                    "resourcesSummary": {
                        "resourcesProcessed": 4,
                        "resourcesFlagged": 1,
                    },
                    "flaggedResources": [{"ResourceId": "do-not-persist"}],
                },
            },
        },
    )
    ce = _ReadOnlyClient(
        pages={
            "get_cost_and_usage": [
                {
                    "ResultsByTime": [{
                        "TimePeriod": {"Start": "2026-07-27", "End": "2026-07-28"},
                        "Total": {
                            "UnblendedCost": {"Amount": "1.23", "Unit": "USD"},
                            "UsageQuantity": {"Amount": "2", "Unit": "N/A"},
                        },
                        "Groups": [{
                            "Keys": ["Amazon Elastic Compute Cloud - Compute"],
                            "Metrics": {"UnblendedCost": {"Amount": "1.23", "Unit": "USD"}},
                        }],
                    }],
                    "NextPageToken": "cost-page-2",
                },
                {"ResultsByTime": []},
            ],
            "get_dimension_values": [{
                "DimensionValues": [{"Value": "Amazon Elastic Compute Cloud - Compute"}],
            }],
            "get_anomaly_monitors": [{
                "AnomalyMonitors": [{
                    "MonitorArn": monitor_arn,
                    "MonitorName": "service-monitor",
                    "MonitorType": "DIMENSIONAL",
                    "MonitorDimension": "SERVICE",
                    "CreationDate": "2026-01-01",
                }],
            }],
            "get_anomaly_subscriptions": [{
                "AnomalySubscriptions": [{
                    "SubscriptionArn": subscription_arn,
                    "SubscriptionName": "finops-alerts",
                    "MonitorArn": monitor_arn,
                    "Status": "ACTIVE",
                    "Subscribers": [{"Type": "EMAIL", "Address": "secret@example.com"}],
                }],
            }],
            "get_anomalies": [{
                "Anomalies": [{
                    "AnomalyId": "anomaly-1",
                    "MonitorArn": monitor_arn,
                    "AnomalyStartDate": "2026-08-01",
                    "AnomalyEndDate": "2026-08-02",
                    "TotalImpact": {"TotalImpact": "12.34", "TotalActualSpend": "20"},
                    "RootCauses": [{"Service": "Amazon EC2", "Region": "us-east-1"}],
                }],
            }],
        },
        responses={
            "get_cost_forecast": {
                "Total": {"Amount": "10.00", "Unit": "USD"},
                "PredictionIntervalLowerBound": "8.00",
                "PredictionIntervalUpperBound": "12.00",
            },
        },
    )
    return {"health": health, "support": support, "ce": ce}


class AWSAccountOperationsInventoryTests(SimpleTestCase):
    def setUp(self):
        self.account = _account()
        self.clients = _clients()
        self.managers = {
            asset_type: _MemoryManager(self.account)
            for asset_type in inventory.AWS_ACCOUNT_OPERATIONS_ASSET_TYPES
        }
        self.manager_patches = [
            patch.object(model, "objects", self.managers[asset_type])
            for asset_type, model in inventory.AWS_ACCOUNT_OPERATIONS_ASSET_MODELS.items()
        ]
        for patcher in self.manager_patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.manager_patches):
            patcher.stop()

    def client_for(self, _account, service, region=None):
        self.assertEqual(region, inventory.AWS_ACCOUNT_OPERATIONS_CONTROL_PLANE_REGION)
        return self.clients[service]

    def test_sync_covers_every_family_once_at_account_control_plane(self):
        with patch.object(inventory, "aws_client", side_effect=self.client_for):
            result = inventory.sync_aws_account_operations_assets(
                self.account,
                regions=["eu-west-1", "ap-southeast-1"],
                now=FROZEN_NOW,
            )

        self.assertEqual(result["scope"], inventory.AWS_ACCOUNT_SCOPE)
        self.assertEqual(result["regions"], ["us-east-1"])
        self.assertEqual(result["counts"][inventory.AWS_HEALTH_EVENT], 1)
        self.assertEqual(result["counts"][inventory.AWS_TRUSTED_ADVISOR_CHECK], 1)
        self.assertEqual(result["counts"][inventory.AWS_COST_EXPLORER_SIGNAL], 1)
        self.assertEqual(result["counts"][inventory.AWS_COST_ANOMALY_MONITOR], 1)
        self.assertEqual(result["counts"][inventory.AWS_COST_ANOMALY_SUBSCRIPTION], 1)
        self.assertEqual(result["counts"][inventory.AWS_COST_ANOMALY], 1)
        self.assertFalse(result["errors"], result)

        health_call = self.clients["health"].calls[0]
        self.assertEqual(
            health_call[1]["filter"]["startTimes"][0]["from"],
            datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc),
        )
        cost_usage_call = next(
            kwargs for operation, kwargs in self.clients["ce"].calls
            if operation == "get_cost_and_usage"
        )
        self.assertEqual(cost_usage_call["TimePeriod"], {"Start": "2026-07-27", "End": "2026-08-03"})
        anomaly_call = next(
            kwargs for operation, kwargs in self.clients["ce"].calls
            if operation == "get_anomalies"
        )
        self.assertEqual(
            anomaly_call["DateInterval"],
            {"StartDate": "2026-07-04", "EndDate": "2026-08-03"},
        )

        subscription = self.managers[inventory.AWS_COST_ANOMALY_SUBSCRIPTION].rows[0]
        self.assertNotIn("secret@example.com", str(subscription.metadata))
        health = self.managers[inventory.AWS_HEALTH_EVENT].rows[0]
        self.assertNotIn("token=secret-value", str(health.metadata))
        self.assertNotIn("ResourceId", str(self.managers[inventory.AWS_TRUSTED_ADVISOR_CHECK].rows[0].metadata))

        allowed = inventory.READ_ONLY_AWS_OPERATIONS
        for client in self.clients.values():
            self.assertTrue(client.calls)
            self.assertTrue(all(operation in allowed for operation, _kwargs in client.calls))

    def test_permission_failure_is_provider_error_and_does_not_reconcile(self):
        old = SimpleNamespace(
            owner=self.account,
            scope=inventory.AWS_ACCOUNT_SCOPE,
            provider_id="check-old",
            monitoring=UtilAsset.Monitoring.ACTIVE,
        )
        self.managers[inventory.AWS_TRUSTED_ADVISOR_CHECK].rows.append(old)
        self.clients["support"] = _ReadOnlyClient(
            responses={
                "describe_trusted_advisor_checks": _aws_error("SubscriptionRequiredException"),
            }
        )

        with patch.object(inventory, "aws_client", side_effect=self.client_for):
            result = inventory.sync_aws_account_operations_assets(self.account, now=FROZEN_NOW)

        error = next(
            item for item in result["errors"]
            if item["assetType"] == inventory.AWS_TRUSTED_ADVISOR_CHECK
        )
        self.assertEqual(error["errorKind"], "provider_error")
        self.assertEqual(error["errorCode"], "SubscriptionRequiredException")
        self.assertEqual(old.monitoring, UtilAsset.Monitoring.ACTIVE)
        self.assertNotIn("secret=must-not-escape", str(result))

    def test_malformed_collection_preserves_existing_assets(self):
        old = SimpleNamespace(
            owner=self.account,
            scope=inventory.AWS_ACCOUNT_SCOPE,
            provider_id="old-health",
            monitoring=UtilAsset.Monitoring.ACTIVE,
        )
        self.managers[inventory.AWS_HEALTH_EVENT].rows.append(old)
        self.clients["health"] = _ReadOnlyClient(
            pages={"describe_events": [{"events": None}]},
            responses={},
        )

        with patch.object(inventory, "aws_client", side_effect=self.client_for):
            result = inventory.sync_aws_account_operations_assets(self.account, now=FROZEN_NOW)

        self.assertTrue(any(item["assetType"] == inventory.AWS_HEALTH_EVENT for item in result["errors"]))
        self.assertEqual(old.monitoring, UtilAsset.Monitoring.ACTIVE)
        self.assertEqual(self.managers[inventory.AWS_HEALTH_EVENT].update_calls, [])

    def test_mutating_operations_are_rejected_explicitly(self):
        with self.assertRaises(ValueError):
            inventory.assert_read_only_aws_operation("create_anomaly_monitor")
        with self.assertRaises(ValueError):
            inventory.assert_read_only_aws_operation("put_cost_allocation_tags")
        with self.assertRaises(ValueError):
            inventory.assert_read_only_aws_operation("get_unknown_signal")

    def test_unsupported_service_is_reported_as_provider_error(self):
        def unavailable(_account, _service, region=None):
            self.assertEqual(region, "us-east-1")
            raise UnknownServiceError(service_name="health", known_service_names=[])

        with patch.object(inventory, "aws_client", side_effect=unavailable):
            result = inventory.sync_aws_account_operations_assets(self.account, now=FROZEN_NOW)

        self.assertTrue(result["errors"])
        self.assertTrue(all(item["errorKind"] == "provider_error" for item in result["errors"]))
        self.assertTrue(all(item["errorCode"] == "UnknownServiceError" for item in result["errors"]))

    def test_health_details_and_anomaly_metadata_stay_bounded(self):
        health_arn = "arn:aws:health:us-east-1::event/BOUND"
        health = _ReadOnlyClient(
            pages={"describe_events": [{"events": [{"arn": health_arn, "statusCode": "open"}]}]},
            responses={
                "describe_event_details": {
                    "successfulSet": [
                        {"eventArn": health_arn, "eventDescription": {"latestDescription": "bounded"}}
                    ] * (inventory.HEALTH_DETAIL_BATCH_SIZE + 1),
                    "failedSet": [],
                }
            },
        )
        with patch.object(inventory, "aws_client", return_value=health):
            with self.assertRaises(CloudInventoryTransientError):
                inventory.collect_aws_health_events(self.account, now=FROZEN_NOW)

        item = {
            "AnomalyId": "anomaly-bound",
            "RootCauses": [{"Service": "EC2", "Region": "us-east-1"}] * 200,
        }
        metadata = inventory._safe_anomaly(item)
        self.assertEqual(metadata["rootCauseCount"], 200)
        self.assertLessEqual(len(metadata["rootCauses"]), inventory.MAX_ANOMALY_ROOT_CAUSES)


class AWSAccountOperationsCheckerTests(SimpleTestCase):
    def setUp(self):
        self.account = _account()
        self.clients = _clients()

    def credentials(self, asset_type, provider_id, metadata=None):
        return {
            "access_key": "ACCESS",
            "secret_key": "SECRET",
            "region": "us-east-1",
            "control_plane_region": "us-east-1",
            "scope": inventory.AWS_ACCOUNT_SCOPE,
            "provider_id": provider_id,
            "asset_type": asset_type,
            "metadata": metadata or {},
        }

    def client_for(self, _account, service, region=None):
        self.assertEqual(region, "us-east-1")
        return self.clients[service]

    def test_checkers_return_normalized_status_and_redacted_payloads(self):
        health_id = "arn:aws:health:us-east-1::event/AWS_EC2_OPEN"
        monitor_id = "arn:aws:ce::123456789012:anomalymonitor/monitor-1"
        subscription_id = "arn:aws:ce::123456789012:anomalysubscription/sub-1"
        with patch.object(checks, "aws_client", side_effect=self.client_for):
            health = checks.check_aws_health_event_status(
                "account|aws_health_event|" + health_id,
                self.credentials(inventory.AWS_HEALTH_EVENT, health_id),
            )
            advisor = checks.check_aws_trusted_advisor_check_status(
                "account|aws_trusted_advisor_check|check-1",
                self.credentials(inventory.AWS_TRUSTED_ADVISOR_CHECK, "check-1"),
            )
            cost = checks.check_aws_cost_explorer_signal_status(
                "account|aws_cost_explorer_signal|usage-cost:2026-07-27:2026-08-03:daily:service",
                self.credentials(
                    inventory.AWS_COST_EXPLORER_SIGNAL,
                    "usage-cost:2026-07-27:2026-08-03:daily:service",
                    {"time_period": {"Start": "2026-07-27", "End": "2026-08-03"}},
                ),
            )
            monitor = checks.check_aws_cost_anomaly_monitor_status(
                "account|aws_cost_anomaly_monitor|" + monitor_id,
                self.credentials(inventory.AWS_COST_ANOMALY_MONITOR, monitor_id),
            )
            subscription = checks.check_aws_cost_anomaly_subscription_status(
                "account|aws_cost_anomaly_subscription|" + subscription_id,
                self.credentials(inventory.AWS_COST_ANOMALY_SUBSCRIPTION, subscription_id),
            )
            anomaly = checks.check_aws_cost_anomaly_status(
                "account|aws_cost_anomaly|anomaly-1",
                self.credentials(inventory.AWS_COST_ANOMALY, "anomaly-1"),
            )

        self.assertEqual(health[0], "degraded")
        self.assertEqual(advisor[0], "degraded")
        self.assertEqual(cost[0], "available")
        self.assertEqual(monitor[0], "available")
        self.assertEqual(subscription[0], "available")
        self.assertEqual(anomaly[0], "degraded")
        self.assertNotIn("secret@example.com", str(subscription))
        self.assertNotIn("token=secret-value", str(health))
        self.assertTrue(all(
            operation in inventory.READ_ONLY_AWS_OPERATIONS
            for client in self.clients.values()
            for operation, _kwargs in client.calls
        ))

    def test_checker_permission_error_is_normalized_without_provider_text(self):
        self.clients["support"] = _ReadOnlyClient(
            responses={
                "describe_trusted_advisor_check_result": _aws_error("AccessDeniedException"),
            }
        )
        with patch.object(checks, "aws_client", side_effect=self.client_for):
            status, payload = checks.check_aws_trusted_advisor_check_status(
                "account|aws_trusted_advisor_check|check-1",
                self.credentials(inventory.AWS_TRUSTED_ADVISOR_CHECK, "check-1"),
            )
        self.assertEqual(status, "invalid_access_token")
        self.assertEqual(payload[inventory.AWS_TRUSTED_ADVISOR_CHECK]["errorCode"], "AccessDeniedException")
        self.assertNotIn("secret=must-not-escape", str(payload))

    def test_checker_malformed_response_is_error(self):
        self.clients["ce"] = _ReadOnlyClient(
            pages={"get_anomaly_monitors": [{"AnomalyMonitors": None}]},
            responses={},
        )
        with patch.object(checks, "aws_client", side_effect=self.client_for):
            status, payload = checks.check_aws_cost_anomaly_monitor_status(
                "account|aws_cost_anomaly_monitor|monitor-1",
                self.credentials(inventory.AWS_COST_ANOMALY_MONITOR, "monitor-1"),
            )
        self.assertEqual(status, "error")
        self.assertIn("errorCode", payload[inventory.AWS_COST_ANOMALY_MONITOR])


class AWSAccountOperationsSymbolTests(SimpleTestCase):
    def test_public_integration_symbols_are_explicit(self):
        self.assertEqual(
            tuple(inventory.AWS_ACCOUNT_OPERATIONS_ASSET_MODELS),
            inventory.AWS_ACCOUNT_OPERATIONS_ASSET_TYPES,
        )
        self.assertEqual(
            set(inventory.AWS_ACCOUNT_OPERATIONS_ENDPOINTS),
            set(inventory.AWS_ACCOUNT_OPERATIONS_ASSET_TYPES),
        )
        self.assertTrue(all(
            endpoint["scope"] == inventory.AWS_ACCOUNT_SCOPE
            and endpoint["region"] == "us-east-1"
            for endpoint in inventory.AWS_ACCOUNT_OPERATIONS_ENDPOINTS.values()
        ))

    def test_trusted_advisor_summary_fallback_is_supported(self):
        client = _ReadOnlyClient(
            responses={
                "describe_trusted_advisor_checks": {
                    "checks": [{"id": "check-summary", "name": "Summary check"}],
                },
                "describe_trusted_advisor_check_summaries": {
                    "summaries": [{"checkId": "check-summary", "status": "ok"}],
                },
            }
        )
        account = _account()
        with patch.object(inventory, "aws_client", return_value=client):
            records = inventory.collect_aws_trusted_advisor_checks(account)
        self.assertEqual(records[0]["provider_id"], "check-summary")
        self.assertEqual(records[0]["metadata"]["normalized_status"], "available")
        self.assertEqual(
            [operation for operation, _kwargs in client.calls],
            [
                "describe_trusted_advisor_checks",
                "describe_trusted_advisor_check_summaries",
            ],
        )
