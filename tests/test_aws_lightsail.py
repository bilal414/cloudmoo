from unittest.mock import patch

from botocore.exceptions import ClientError
from django.contrib.auth.models import User
from django.test import TestCase

from apps.console.account.models import CoreAccount, CoreAccountMembership
from apps.console.cloud.aws.lightsail import (
    CoreAWSLightsailAlarm,
    CoreAWSLightsailAutoSnapshot,
    CoreAWSLightsailBucket,
    CoreAWSLightsailCertificate,
    CoreAWSLightsailContainerDeployment,
    CoreAWSLightsailContainerImage,
    CoreAWSLightsailContainerService,
    CoreAWSLightsailDNSRecord,
    CoreAWSLightsailDomain,
    CoreAWSLightsailInstance,
    CoreAWSLightsailOperation,
    _sync_auto_snapshots,
    sync_lightsail_assets,
)
from apps.console.cloud.aws.models import CoreAWSAccount
from apps.console.cloud.models import CoreCloud, CoreCloudServiceProvider, CloudInventoryTransientError
from apps.console.member.models import CoreMember
from apps.console.utils.models import UtilAsset
from apps.monitoring.checks import get_check_function
from apps.monitoring.checks.aws_lightsail import LIGHTSAIL_ASSET_TYPES


class FakeLightsailClient:
    """Deterministic Lightsail read API fixture; no mutating methods exist."""

    def __init__(self):
        self.calls = []

    def _call(self, operation, **kwargs):
        self.calls.append((operation, kwargs))
        if operation == "get_regions":
            return {"regions": [{"name": "us-east-1", "state": "available"}]}
        if operation == "get_instances":
            return {
                "instances": [{
                    "name": "cm-test-instance",
                    "state": {"name": "running", "code": 16},
                    "tags": [{"key": "Environment", "value": "test"}],
                    "publicIpAddress": "203.0.113.10",
                }],
            }
        if operation == "get_instance_port_states":
            return {"portStates": [{"fromPort": 22, "toPort": 22, "protocol": "tcp", "state": "open"}]}
        if operation == "get_auto_snapshots":
            return {"autoSnapshots": [{"date": "2026-08-01", "status": "Succeeded"}]}
        if operation == "get_domains":
            return {"domains": [{"name": "example.test", "tags": []}]}
        if operation == "get_domain":
            return {"domain": {
                "name": "example.test",
                "domainEntries": [{
                    "id": "record-1",
                    "name": "www",
                    "target": "203.0.113.20",
                    "type": "A",
                    "isAlias": False,
                }],
            }}
        if operation == "get_container_services":
            return {"containerServices": [{
                "containerServiceName": "cm-test-service",
                "state": "RUNNING",
                "tags": [],
                "currentDeployment": {"containers": {"api": {}}},
            }]}
        if operation == "get_container_service_deployments":
            return {"deployments": [{"version": 1, "state": "ACTIVE", "containers": {}}]}
        if operation == "get_container_images":
            return {"containerImages": [{"image": "nginx:latest", "digest": "sha256:test"}]}
        if operation == "get_alarms":
            return {"alarms": [{"name": "cm-test-alarm", "state": "OK", "metricName": "CPUUtilization"}]}
        if operation == "get_operations":
            return {"operations": [{"id": "operation-1", "resourceName": "cm-test-instance", "status": "Succeeded"}]}
        if operation == "get_instance":
            return {"instance": {
                "name": "cm-test-instance",
                "state": {"name": "running", "code": 16},
                "tags": [{"key": "Environment", "value": "test"}],
            }}
        if operation == "get_instance_metric_data":
            return {"metricData": [{"timestamp": "2026-08-02T12:00:00Z", "average": 12.5}]}
        if operation == "get_container_service_metric_data":
            return {"metricData": [{"timestamp": "2026-08-02T12:00:00Z", "average": 4.5}]}
        if operation == "get_container_log":
            return {"logEvents": [{
                "createdAt": "2026-08-02T12:00:00Z",
                "message": "password=do-not-persist-this",
            }]}
        if operation == "get_disks":
            return {"disks": []}
        if operation == "get_instance_snapshots":
            return {"instanceSnapshots": []}
        if operation == "get_disk_snapshots":
            return {"diskSnapshots": []}
        if operation == "get_static_ips":
            return {"staticIps": []}
        if operation == "get_relational_databases":
            return {"relationalDatabases": []}
        if operation == "get_relational_database_snapshots":
            return {"relationalDatabaseSnapshots": []}
        if operation == "get_load_balancers":
            return {"loadBalancers": []}
        if operation == "get_certificates":
            return {"certificates": [{
                "certificateName": "cm-test-certificate",
                "domainName": "example.test",
                "certificateArn": "arn:aws:acm:us-east-1:123456789012:certificate/test",
                "certificateDetail": {"status": "ISSUED"},
            }]}
        if operation == "get_buckets":
            return {"buckets": []}
        if operation == "get_distributions":
            return {"distributions": []}
        raise AssertionError(f"Unexpected non-read or unimplemented API call: {operation}")

    def __getattr__(self, name):
        if not name.startswith("get_"):
            raise AssertionError(f"Mutation or unsupported API call attempted: {name}")
        return lambda **kwargs: self._call(name, **kwargs)


class AWSLightsailIntegrationTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="lightsail-test-user",
            email="lightsail-test@example.com",
            password="test-password",
        )
        self.account = CoreAccount.objects.create(
            name="Lightsail Test Account",
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
            code="aws",
            defaults={
                "name": "Amazon Web Services",
                "status": CoreCloudServiceProvider.Status.ACTIVE,
            },
        )
        self.cloud = CoreCloud.objects.create(
            account=self.account,
            provider=self.provider,
            status=CoreCloud.Status.ACTIVE,
        )
        self.aws_account = CoreAWSAccount.objects.create(
            cloud=self.cloud,
            name="Lightsail Test AWS Account",
            access_key="TESTACCESSKEY123456789",
            secret_key="test-secret",
            region="us-east-1",
        )
        self.client = FakeLightsailClient()

    @patch.object(CoreAWSAccount, "_get_aws_client")
    def test_syncs_requested_lightsail_resources_without_mutations(self, get_client):
        get_client.return_value = self.client

        result = sync_lightsail_assets(self.aws_account)

        self.assertEqual(result["regions"], ["us-east-1"])
        self.assertEqual(CoreAWSLightsailInstance.objects.count(), 1)
        self.assertEqual(CoreAWSLightsailDomain.objects.count(), 1)
        self.assertEqual(CoreAWSLightsailDNSRecord.objects.count(), 1)
        self.assertEqual(CoreAWSLightsailContainerService.objects.count(), 1)
        self.assertEqual(CoreAWSLightsailContainerDeployment.objects.count(), 1)
        self.assertEqual(CoreAWSLightsailContainerImage.objects.count(), 1)
        self.assertEqual(CoreAWSLightsailAlarm.objects.count(), 1)
        self.assertEqual(CoreAWSLightsailOperation.objects.count(), 1)
        self.assertEqual(CoreAWSLightsailAutoSnapshot.objects.count(), 1)
        self.assertEqual(CoreAWSLightsailCertificate.objects.count(), 1)
        self.assertEqual(CoreAWSLightsailCertificate.objects.get().name, "cm-test-certificate")
        self.assertEqual(
            CoreAWSLightsailOperation.objects.get().monitoring,
            UtilAsset.Monitoring.ACTIVE,
        )
        self.assertEqual(
            CoreAWSLightsailAutoSnapshot.objects.get().monitoring,
            UtilAsset.Monitoring.ACTIVE,
        )

        instance = CoreAWSLightsailInstance.objects.get()
        self.assertEqual(instance.metadata["portStates"][0]["state"], "open")
        self.assertEqual(instance.metadata["tags"][0]["key"], "Environment")
        self.assertEqual(instance.monitoring_credentials["asset_type"], UtilAsset.Type.LIGHTSAIL_INSTANCE)
        self.assertEqual(instance.monitoring_credentials["resource_region"], "us-east-1")

        called_operations = {operation for operation, _kwargs in self.client.calls}
        self.assertTrue(called_operations)
        self.assertTrue(all(operation.startswith("get_") for operation in called_operations))

    def test_status_checker_exposes_firewall_and_metrics(self):
        instance = CoreAWSLightsailInstance.objects.create(
            owner=self.aws_account,
            unique_id="lightsail_instance:us-east-1:cm-test-instance",
            name="cm-test-instance",
            type=UtilAsset.Type.LIGHTSAIL_INSTANCE,
            metadata={
                "_cloudmoo_region": "us-east-1",
                "_cloudmoo_name": "cm-test-instance",
            },
        )

        with patch("apps.monitoring.checks.aws_lightsail.boto3.client", return_value=self.client):
            status, metadata = get_check_function("aws", UtilAsset.Type.LIGHTSAIL_INSTANCE)(
                instance.unique_id,
                instance.monitoring_credentials,
            )

        self.assertEqual(status, "running")
        self.assertEqual(metadata["lightsail_instance"]["state"]["name"], "running")
        self.assertEqual(metadata["lightsailDetails"]["portStates"][0]["fromPort"], 22)
        self.assertEqual(metadata["lightsailDetails"]["metric"]["metricName"], "CPUUtilization")

        certificate = CoreAWSLightsailCertificate.objects.create(
            owner=self.aws_account,
            unique_id="lightsail_certificate:us-east-1:cm-test-certificate",
            name="cm-test-certificate",
            type=UtilAsset.Type.LIGHTSAIL_CERTIFICATE,
            metadata={
                "_cloudmoo_region": "us-east-1",
                "_cloudmoo_name": "cm-test-certificate",
            },
        )
        with patch("apps.monitoring.checks.aws_lightsail.boto3.client", return_value=self.client):
            status, metadata = get_check_function("aws", UtilAsset.Type.LIGHTSAIL_CERTIFICATE)(
                certificate.unique_id,
                certificate.monitoring_credentials,
            )

        self.assertEqual(status, "ISSUED")
        self.assertEqual(metadata["lightsail_certificate"]["certificateName"], "cm-test-certificate")

    @patch("apps.monitoring.schedules.asset_schedule_create")
    def test_container_status_checker_exposes_bounded_redacted_logs(self, _schedule_create):
        service = CoreAWSLightsailContainerService.objects.create(
            owner=self.aws_account,
            unique_id="lightsail_container_service:us-east-1:cm-test-service",
            name="cm-test-service",
            type=UtilAsset.Type.LIGHTSAIL_CONTAINER_SERVICE,
            metadata={
                "_cloudmoo_region": "us-east-1",
                "_cloudmoo_name": "cm-test-service",
                "currentDeployment": {"containers": {"api": {}}},
            },
        )

        with patch("apps.monitoring.checks.aws_lightsail.boto3.client", return_value=self.client):
            status, metadata = get_check_function("aws", UtilAsset.Type.LIGHTSAIL_CONTAINER_SERVICE)(
                service.unique_id,
                service.monitoring_credentials,
            )

        self.assertEqual(status, "RUNNING")
        logs = metadata["lightsailDetails"]["containerLogs"]["containers"]["api"]
        self.assertEqual(logs["eventCount"], 1)
        self.assertNotIn("do-not-persist-this", logs["events"][0]["message"])
        self.assertEqual(metadata["lightsailDetails"]["metric"]["metricName"], "CPUUtilization")

    def test_all_lightsail_asset_types_resolve_to_read_only_checks(self):
        for asset_type in LIGHTSAIL_ASSET_TYPES:
            check = get_check_function("aws", asset_type)
            self.assertEqual(check.__name__, f"check_aws_{asset_type}_status")

    def test_collection_sync_fails_closed_on_incomplete_response(self):
        broken = FakeLightsailClient()
        broken._call = lambda operation, **kwargs: {"regions": []} if operation == "get_regions" else {}
        with patch.object(CoreAWSAccount, "_get_aws_client", return_value=broken):
            with self.assertRaises(CloudInventoryTransientError):
                sync_lightsail_assets(self.aws_account)

        self.assertEqual(CoreAWSLightsailInstance.objects.count(), 0)

    @patch("apps.monitoring.schedules.asset_schedule_create")
    def test_auto_snapshot_sync_keeps_successful_rows_when_one_source_fails(self, _schedule_create):
        client = type("AutoSnapshotClient", (), {})()

        def get_auto_snapshots(**kwargs):
            if kwargs["resourceName"] == "healthy-instance":
                return {"autoSnapshots": [{"date": "2026-08-01", "status": "Succeeded"}]}
            raise ClientError(
                {"Error": {"Code": "InvalidInputException", "Message": "unsupported source"}},
                "GetAutoSnapshots",
            )

        client.get_auto_snapshots = get_auto_snapshots

        with patch.object(self.aws_account, "_get_aws_client", return_value=client):
            _sync_auto_snapshots(
                self.aws_account,
                [
                    {"region": "us-east-1", "name": "healthy-instance", "resource_type": "Instance"},
                    {"region": "us-east-1", "name": "unsupported-instance", "resource_type": "Instance"},
                ],
            )

        snapshot = CoreAWSLightsailAutoSnapshot.objects.get()
        self.assertEqual(snapshot.metadata["status"], "Succeeded")
        self.assertEqual(snapshot.monitoring, UtilAsset.Monitoring.ACTIVE)
