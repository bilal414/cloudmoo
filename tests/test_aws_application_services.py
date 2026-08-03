from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from botocore.exceptions import ClientError
from django.test import SimpleTestCase

from apps.console.cloud.aws import application_services as inventory
from apps.console.utils.models import UtilAsset
from apps.monitoring.checks import aws_application_services as checks


def _aws_error(code):
    return ClientError(
        {"Error": {"Code": code, "Message": "password=must-not-escape"}},
        "ReadOnlyOperation",
    )


class _MemoryQuerySet:
    def __init__(self, rows):
        self.rows = rows

    def exclude(self, **kwargs):
        identifiers = set(kwargs.get("unique_id__in", []))
        return _MemoryQuerySet([row for row in self.rows if row.unique_id not in identifiers])

    def update(self, **kwargs):
        for row in self.rows:
            for key, value in kwargs.items():
                setattr(row, key, value)
        return len(self.rows)


class _MemoryManager:
    def __init__(self):
        self.rows = []

    def get_or_create(self, owner, region, unique_id, defaults):
        for row in self.rows:
            if row.owner is owner and row.region == region and row.unique_id == unique_id:
                return row, False
        row = SimpleNamespace(
            owner=owner,
            region=region,
            unique_id=unique_id,
            save=lambda: None,
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
        return iter(self.client.pages(self.operation, kwargs))


class _ReadOnlyClient:
    """Fake AWS client: unsupported/mutating methods fail immediately."""

    def __init__(self, pages=None, details=None):
        self._pages = pages or {}
        self._details = details or {}
        self.calls = []

    def pages(self, operation, kwargs):
        if callable(self._pages):
            return self._pages(operation, kwargs)
        value = self._pages.get(operation, [])
        if callable(value):
            value = value(kwargs)
        if isinstance(value, Exception):
            raise value
        return value

    def get_paginator(self, operation):
        return _Paginator(self, operation)

    def __getattr__(self, operation):
        if not operation.startswith(("get_", "describe_", "list_")):
            raise AssertionError(f"Unexpected AWS mutation or operation: {operation}")

        def call(**kwargs):
            self.calls.append((operation, kwargs))
            value = self._details.get(operation)
            if callable(value):
                value = value(kwargs)
            if isinstance(value, Exception):
                raise value
            if value is None:
                raise AssertionError(f"Missing read fixture for {operation}")
            return value

        return call


def _inventory_clients():
    rest_arn = "arn:aws:apigateway:us-east-1::/restapis/rest-1"
    v2_arn = "arn:aws:apigateway:us-east-1::/apis/v2-1"
    bus_arn = "arn:aws:events:us-east-1:123456789012:event-bus/default"
    rule_arn = f"{bus_arn}/rule-1"
    schedule_arn = "arn:aws:scheduler:us-east-1:123456789012:schedule/default/nightly"
    pipe_arn = "arn:aws:pipes:us-east-1:123456789012:pipe/pipe-1"
    topic_arn = "arn:aws:sns:us-east-1:123456789012:topic-1"
    queue_url = "https://sqs.us-east-1.amazonaws.com/123456789012/queue-1"
    queue_arn = "arn:aws:sqs:us-east-1:123456789012:queue-1"
    state_machine_arn = "arn:aws:states:us-east-1:123456789012:stateMachine:machine-1"
    stack_arn = "arn:aws:cloudformation:us-east-1:123456789012:stack/app/stack-1"

    def pages(operation, kwargs):
        if operation == "get_rest_apis":
            return [
                {"items": [{"id": "rest-1", "name": "rest-api", "SecretToken": "hidden"}]},
                {"items": []},
            ]
        if operation == "get_apis":
            return [{"Items": [{"ApiId": "v2-1", "Name": "http-api"}]}]
        if operation == "get_stages":
            stage = {"stageName": "prod", "deploymentId": "dep-1", "variables": {"token": "hidden"}}
            return [{("Items" if "ApiId" in kwargs else "item"): [stage]}]
        if operation == "list_event_buses":
            return [{"EventBuses": [{"Name": "default", "Arn": bus_arn, "Policy": "hidden"}]}]
        if operation == "list_rules":
            return [{"Rules": [{"Name": "rule-1", "Arn": rule_arn, "EventBusName": kwargs.get("EventBusName"), "EventPattern": "hidden"}]}]
        if operation == "list_schedules":
            return [{"Schedules": [{"Name": "nightly", "GroupName": "default", "Arn": schedule_arn}]}]
        if operation == "list_pipes":
            return [{"Pipes": [{"Name": "pipe-1", "Arn": pipe_arn, "RoleArn": "hidden"}]}]
        if operation == "list_topics":
            return [{"Topics": [{"TopicArn": topic_arn, "SecretToken": "hidden"}]}]
        if operation == "list_queues":
            return [{"QueueUrls": [queue_url]}]
        if operation == "list_state_machines":
            return [{"stateMachines": [{"stateMachineArn": state_machine_arn, "name": "machine-1"}]}]
        if operation == "list_work_groups":
            return [{"WorkGroups": [{"Name": "primary", "State": "ENABLED"}]}]
        if operation == "list_data_catalogs":
            return [{"DataCatalogsSummary": [{"CatalogName": "AwsDataCatalog", "Type": "GLUE"}]}]
        if operation == "list_stacks":
            return [{"StackSummaries": [{"StackId": stack_arn, "StackName": "app", "StackStatus": "CREATE_COMPLETE"}]}]
        if operation == "list_stack_resources":
            return [{"StackResourceSummaries": [{"LogicalResourceId": "Api", "PhysicalResourceId": "api-1", "ResourceType": "AWS::ApiGateway::RestApi"}]}]
        raise AssertionError(f"Missing page fixture for {operation}")

    details = {
        "get_rest_api": {"id": "rest-1", "name": "rest-api", "apiKeySource": "HEADER", "endpointConfiguration": {"types": ["REGIONAL"]}},
        "get_api": {"ApiId": "v2-1", "Name": "http-api", "ProtocolType": "HTTP", "ApiEndpoint": "https://v2-1.execute-api.us-east-1.amazonaws.com", "ApiStatus": "AVAILABLE"},
        "describe_event_bus": {"Name": "default", "Arn": bus_arn, "Description": "default"},
        "describe_rule": {"Name": "rule-1", "Arn": rule_arn, "State": "ENABLED", "EventBusName": "default"},
        "get_schedule": {"Name": "nightly", "GroupName": "default", "State": "ENABLED", "ScheduleExpression": "rate(5 minutes)", "Target": {"Input": "secret"}},
        "describe_pipe": {"Name": "pipe-1", "Arn": pipe_arn, "CurrentState": "RUNNING", "Source": "source", "Target": "target"},
        "get_topic_attributes": {"Attributes": {"TopicArn": topic_arn, "DisplayName": "topic-1", "FifoTopic": "false", "Policy": "hidden", "SecretToken": "hidden"}},
        "get_queue_attributes": {"Attributes": {"QueueArn": queue_arn, "ApproximateNumberOfMessages": "2", "RedrivePolicy": "hidden", "SecretToken": "hidden"}},
        "describe_state_machine": {"stateMachineArn": state_machine_arn, "name": "machine-1", "status": "ACTIVE", "definition": "hidden"},
        "get_work_group": {"WorkGroup": {"Name": "primary", "State": "ENABLED", "Description": "workgroup", "WorkGroupConfiguration": {"OutputLocation": "s3://bucket/results"}}},
        "get_data_catalog": {"DataCatalog": {"CatalogName": "AwsDataCatalog", "Type": "GLUE", "Status": "CREATE_COMPLETE", "Parameters": {"password": "hidden"}}},
        "describe_stacks": {"Stacks": [{"StackId": stack_arn, "StackName": "app", "StackStatus": "CREATE_COMPLETE", "Parameters": [{"ParameterValue": "hidden"}]}]},
    }
    return _ReadOnlyClient(pages=pages, details=details)


class AWSApplicationInventoryTests(SimpleTestCase):
    def setUp(self):
        self.account = SimpleNamespace(access_key="ACCESS", secret_key="SECRET", region="us-east-1")
        self.clients = {}

        def client_for(_account, service, region=None):
            key = (service, region)
            self.clients.setdefault(key, _inventory_clients())
            return self.clients[key]

        self.client_for = client_for

    def _patch_managers(self, stack):
        managers = {}
        for asset_type, model in inventory.AWS_APPLICATION_ASSET_MODELS.items():
            manager = _MemoryManager()
            managers[asset_type] = manager
            stack.enter_context(patch.object(model, "objects", manager))
        return managers

    def test_all_families_are_paginated_regional_redacted_and_read_only(self):
        with patch.object(inventory, "get_enabled_regions", return_value=["us-east-1", "eu-west-1"]), \
             patch.object(inventory, "aws_client", side_effect=self.client_for):
            with ExitStack() as stack:
                managers = self._patch_managers(stack)
                result = inventory.sync_aws_application_service_assets(self.account)

        self.assertEqual(result["regions"], ["eu-west-1", "us-east-1"])
        for asset_type in inventory.AWS_APPLICATION_ASSET_TYPES:
            self.assertEqual(result["counts"][asset_type], 2, asset_type)
            self.assertTrue(result["families"][asset_type]["us-east-1"]["reconciled"], asset_type)
            self.assertEqual(len(managers[asset_type].rows), 2)

        rest = managers[inventory.AWS_APIGATEWAY_REST_API].rows[0]
        self.assertEqual(rest.unique_id, "eu-west-1|rest-1")
        self.assertEqual(rest.metadata["_cloudmoo_raw_id"], "rest-1")
        self.assertEqual(rest.metadata["stages"][0]["stageName"], "prod")
        self.assertNotIn("variables", str(rest.metadata).lower())

        sns = managers[inventory.AWS_SNS_TOPIC].rows[0]
        self.assertNotIn("SecretToken", str(sns.metadata))
        self.assertNotIn("Policy", sns.metadata["Attributes"])
        self.assertNotIn("password", str(sns.metadata).lower())

        services = {service for service, _region in self.clients}
        self.assertEqual(
            services,
            {value["service"] for value in inventory.AWS_APPLICATION_ENDPOINTS.values()},
        )
        regions = {region for _service, region in self.clients}
        self.assertEqual(regions, {"eu-west-1", "us-east-1"})
        for client in self.clients.values():
            self.assertTrue(client.calls)
            self.assertTrue(all(operation.startswith(("get_", "describe_", "list_")) for operation, _ in client.calls))
            self.assertFalse(any(operation.startswith(("create", "delete", "put", "publish", "subscribe", "receive", "send", "start", "stop", "update")) for operation, _ in client.calls))

    def test_malformed_family_response_preserves_existing_rows_and_skips_reconciliation(self):
        clients = {}

        def client_for(_account, service, region=None):
            key = (service, region)
            if key not in clients:
                clients[key] = _inventory_clients()
            if service == "sns" and region == "us-east-1":
                clients[key]._pages = {"list_topics": [{"Topics": None}]}
            return clients[key]

        with patch.object(inventory, "get_enabled_regions", return_value=["us-east-1"]), \
             patch.object(inventory, "aws_client", side_effect=client_for):
            with ExitStack() as stack:
                managers = self._patch_managers(stack)
                old = SimpleNamespace(
                    owner=self.account,
                    region="us-east-1",
                    unique_id="us-east-1|arn:aws:sns:us-east-1:123:old",
                    monitoring=UtilAsset.Monitoring.ACTIVE,
                    save=lambda: None,
                    metadata={},
                    name="old",
                    type=inventory.AWS_SNS_TOPIC,
                )
                managers[inventory.AWS_SNS_TOPIC].rows.append(old)
                result = inventory.sync_aws_application_service_assets(self.account)

        family = result["families"][inventory.AWS_SNS_TOPIC]["us-east-1"]
        self.assertFalse(family["complete"])
        self.assertFalse(family["reconciled"])
        self.assertEqual(old.monitoring, UtilAsset.Monitoring.ACTIVE)
        self.assertEqual(len(managers[inventory.AWS_SNS_TOPIC].rows), 1)
        self.assertTrue(any(error["assetType"] == inventory.AWS_SNS_TOPIC for error in result["errors"]))

    def test_partial_child_failure_persists_seen_rows_without_marking_old_rows_missing(self):
        client = _inventory_clients()
        client._details["get_schedule"] = _aws_error("AccessDeniedException")
        manager = _MemoryManager()
        old = SimpleNamespace(
            owner=self.account,
            region="us-east-1",
            unique_id="us-east-1|arn:aws:scheduler:us-east-1:123:schedule/default/old",
            monitoring=UtilAsset.Monitoring.ACTIVE,
            save=lambda: None,
            metadata={},
            name="old",
            type=inventory.AWS_EVENTBRIDGE_SCHEDULE,
        )
        manager.rows.append(old)

        def client_for(_account, service, region=None):
            self.assertEqual((service, region), ("scheduler", "us-east-1"))
            return client

        with patch.object(inventory, "get_enabled_regions", return_value=["us-east-1"]), \
             patch.object(inventory, "aws_client", side_effect=client_for), \
             patch.object(inventory.CoreAWSEventBridgeSchedule, "objects", manager):
            records, warnings = inventory._collect_eventbridge_schedules(self.account, "us-east-1")
            count = inventory._persist_without_reconcile(
                inventory.CoreAWSEventBridgeSchedule,
                self.account,
                "us-east-1",
                records,
                inventory.AWS_EVENTBRIDGE_SCHEDULE,
            )

        self.assertEqual(count, 1)
        self.assertTrue(warnings)
        self.assertEqual(old.monitoring, UtilAsset.Monitoring.ACTIVE)
        self.assertEqual(len(manager.rows), 2)
        self.assertNotIn("must-not-escape", str(records))

    def test_raw_ids_and_model_context_are_not_credentials_in_metadata(self):
        asset = SimpleNamespace(
            owner=SimpleNamespace(access_key="ACCESS", secret_key="SECRET"),
            region="us-east-1",
            unique_id="us-east-1|arn:aws:sqs:us-east-1:123:q",
            name="q",
            type=inventory.AWS_SQS_QUEUE,
            asset_type=inventory.AWS_SQS_QUEUE,
            provider_type=inventory.AWS_SQS_QUEUE,
            metadata={
                "_cloudmoo_raw_id": "arn:aws:sqs:us-east-1:123:q",
                "_cloudmoo_queue_url": "https://sqs.us-east-1.amazonaws.com/123/q",
                "password": "[REDACTED]",
            },
        )
        context = inventory.CoreAWSApplicationServiceAsset.monitoring_credentials.fget(asset)
        self.assertEqual(context["provider_id"], "arn:aws:sqs:us-east-1:123:q")
        self.assertEqual(context["queue_url"], "https://sqs.us-east-1.amazonaws.com/123/q")
        self.assertEqual(context["metadata"]["password"], "[REDACTED]")
        self.assertNotIn("ACCESS", context["metadata"])
        self.assertIn("us-east-1", inventory.CoreAWSApplicationServiceAsset.provider_url.fget(asset))


class AWSApplicationCheckTests(SimpleTestCase):
    def setUp(self):
        self.credentials = {
            "access_key": "ACCESS",
            "secret_key": "SECRET",
            "region": "us-east-1",
        }
        self.client = _ReadOnlyClient(details={
            "get_rest_api": {"id": "rest-1", "name": "rest", "SecretToken": "hidden"},
            "get_api": {"ApiId": "v2-1", "ApiStatus": "UPDATING", "Name": "v2"},
            "describe_event_bus": {"Name": "default", "Arn": "bus-arn"},
            "describe_rule": {"Name": "rule", "State": "DISABLED", "EventBusName": "default"},
            "get_schedule": {"Name": "schedule", "State": "ENABLED", "GroupName": "default"},
            "describe_pipe": {"Name": "pipe", "CurrentState": "STOPPED"},
            "get_topic_attributes": {"Attributes": {"TopicArn": "topic-arn", "SecretToken": "hidden", "FifoTopic": "false"}},
            "get_queue_attributes": {"Attributes": {"QueueArn": "queue-arn", "SecretToken": "hidden"}},
            "describe_state_machine": {"stateMachineArn": "machine-arn", "status": "DELETING"},
            "get_work_group": {"WorkGroup": {"Name": "primary", "State": "DISABLED"}},
            "get_data_catalog": {"DataCatalog": {"CatalogName": "catalog", "Status": "CREATE_COMPLETE"}},
            "describe_stacks": {"Stacks": [{"StackName": "app", "StackStatus": "UPDATE_IN_PROGRESS", "SecretToken": "hidden"}]},
        })

    def _run(self, checker, unique_id, metadata=None):
        credentials = dict(self.credentials)
        credentials["metadata"] = metadata or {}
        with patch.object(checks, "aws_client", return_value=self.client):
            return checker(unique_id, credentials)

    def test_registry_contains_every_family_and_checks_are_normalized(self):
        expected = {
            inventory.AWS_APIGATEWAY_REST_API: (checks.check_aws_apigateway_rest_api_status, "available", "rest-1", {}),
            inventory.AWS_APIGATEWAY_V2_API: (checks.check_aws_apigateway_v2_api_status, "pending", "v2-1", {}),
            inventory.AWS_EVENTBRIDGE_BUS: (checks.check_aws_eventbridge_bus_status, "active", "bus-arn", {"Name": "default"}),
            inventory.AWS_EVENTBRIDGE_RULE: (checks.check_aws_eventbridge_rule_status, "disabled", "rule-arn", {"Name": "rule", "EventBusName": "default"}),
            inventory.AWS_EVENTBRIDGE_SCHEDULE: (checks.check_aws_eventbridge_schedule_status, "active", "schedule-arn", {"Name": "schedule", "GroupName": "default"}),
            inventory.AWS_EVENTBRIDGE_PIPE: (checks.check_aws_eventbridge_pipe_status, "disabled", "pipe-arn", {"Name": "pipe"}),
            inventory.AWS_SNS_TOPIC: (checks.check_aws_sns_topic_status, "available", "topic-arn", {}),
            inventory.AWS_SQS_QUEUE: (checks.check_aws_sqs_queue_status, "available", "queue-arn", {"_cloudmoo_queue_url": "https://sqs.us-east-1.amazonaws.com/123/q"}),
            inventory.AWS_STEPFUNCTIONS_STATE_MACHINE: (checks.check_aws_stepfunctions_state_machine_status, "deleting", "machine-arn", {}),
            inventory.AWS_ATHENA_WORKGROUP: (checks.check_aws_athena_workgroup_status, "disabled", "primary", {"Name": "primary"}),
            inventory.AWS_ATHENA_DATA_CATALOG: (checks.check_aws_athena_data_catalog_status, "active", "catalog", {"CatalogName": "catalog"}),
            inventory.AWS_CLOUDFORMATION_STACK: (checks.check_aws_cloudformation_stack_status, "pending", "stack-arn", {}),
        }
        self.assertEqual(set(checks.AWS_APPLICATION_CHECKS), set(inventory.AWS_APPLICATION_ASSET_TYPES))
        for asset_type, (checker, expected_status, provider_id, metadata) in expected.items():
            status, payload = self._run(checker, f"us-east-1|{provider_id}", metadata)
            self.assertEqual(status, expected_status, asset_type)
            self.assertIn(asset_type, payload)
            self.assertNotIn("hidden", str(payload))

        self.assertTrue(all(operation.startswith(("get_", "describe_", "list_")) for operation, _ in self.client.calls))
        self.assertFalse(any(operation.startswith(("publish", "subscribe", "receive", "delete", "start", "stop", "put", "update", "send")) for operation, _ in self.client.calls))

    def test_checks_use_service_specific_regional_endpoints(self):
        expected_services = {
            inventory.AWS_APIGATEWAY_REST_API: "apigateway",
            inventory.AWS_APIGATEWAY_V2_API: "apigatewayv2",
            inventory.AWS_EVENTBRIDGE_BUS: "events",
            inventory.AWS_EVENTBRIDGE_RULE: "events",
            inventory.AWS_EVENTBRIDGE_SCHEDULE: "scheduler",
            inventory.AWS_EVENTBRIDGE_PIPE: "pipes",
            inventory.AWS_SNS_TOPIC: "sns",
            inventory.AWS_SQS_QUEUE: "sqs",
            inventory.AWS_STEPFUNCTIONS_STATE_MACHINE: "stepfunctions",
            inventory.AWS_ATHENA_WORKGROUP: "athena",
            inventory.AWS_ATHENA_DATA_CATALOG: "athena",
            inventory.AWS_CLOUDFORMATION_STACK: "cloudformation",
        }
        for asset_type, service in expected_services.items():
            checker = checks.AWS_APPLICATION_CHECKS[asset_type]
            provider_id = "queue-arn" if asset_type == inventory.AWS_SQS_QUEUE else "resource"
            metadata = {"_cloudmoo_queue_url": "https://sqs.us-east-1.amazonaws.com/123/q"} if asset_type == inventory.AWS_SQS_QUEUE else {}
            with patch.object(checks, "aws_client", return_value=self.client) as aws_client:
                checker(f"us-east-1|{provider_id}", {**self.credentials, "metadata": metadata})
            self.assertEqual(aws_client.call_args.kwargs["region"], "us-east-1")
            self.assertEqual(aws_client.call_args.args[1], service)

    def test_provider_errors_and_malformed_responses_are_safe(self):
        denied = _ReadOnlyClient(details={"get_topic_attributes": _aws_error("AccessDeniedException")})
        with patch.object(checks, "aws_client", return_value=denied):
            status, metadata = checks.check_aws_sns_topic_status(
                "us-east-1|topic-arn",
                {**self.credentials, "metadata": {}},
            )
        self.assertEqual(status, "invalid_access_token")
        self.assertNotIn("must-not-escape", str(metadata))

        malformed = _ReadOnlyClient(details={"get_topic_attributes": {"Attributes": None}})
        with patch.object(checks, "aws_client", return_value=malformed):
            status, metadata = checks.check_aws_sns_topic_status(
                "us-east-1|topic-arn",
                {**self.credentials, "metadata": {}},
            )
        self.assertEqual(status, "error")
        self.assertEqual(metadata["errorCode"], "invalid_response")


class AWSApplicationBoundTests(SimpleTestCase):
    def test_stack_resource_bound_fails_closed(self):
        client = _ReadOnlyClient(
            pages={
                "list_stack_resources": [{"StackResourceSummaries": [{} for _ in range(inventory.MAX_STACK_RESOURCES + 1)]}],
            }
        )
        with patch.object(inventory, "iter_pages", side_effect=lambda _client, _operation, **_kwargs: iter(client.pages("list_stack_resources", {}))):
            with self.assertRaises(Exception):
                inventory._collection(
                    client,
                    "list_stack_resources",
                    "StackResourceSummaries",
                    "stack resources",
                    limit=inventory.MAX_STACK_RESOURCES,
                )
