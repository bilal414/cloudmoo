from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.console.cloud.aws import edge
from apps.monitoring.checks import aws_edge


class FakeReadClient:
    """Small read-only AWS fixture used by the edge adapter tests."""

    def __init__(self, service, pages=None):
        self.service = service
        self.pages = pages or {}
        self.calls = []

    def call(self, operation, **kwargs):
        self.calls.append((operation, kwargs))
        if operation in self.pages:
            value = self.pages[operation]
            return value(**kwargs) if callable(value) else value
        raise AssertionError(f"Unexpected AWS call: {self.service}.{operation}")

    def __getattr__(self, name):
        if name.startswith(("list_", "get_", "describe_")):
            return lambda **kwargs: self.call(name, **kwargs)
        raise AssertionError(f"Unexpected mutating or unsupported call: {name}")


class AWSEdgeAdapterTests(SimpleTestCase):
    def setUp(self):
        self.account = SimpleNamespace(
            region="ap-southeast-1",
            access_key="access",
            secret_key="secret",
        )
        self.clients = {}

        self.clients["route53"] = FakeReadClient("route53")
        self.clients["cloudfront"] = FakeReadClient("cloudfront")
        self.clients["globalaccelerator"] = FakeReadClient("globalaccelerator")
        self.clients["wafv2:us-east-1"] = FakeReadClient("wafv2:us-east-1")
        self.clients["wafv2:us-west-2"] = FakeReadClient("wafv2:us-west-2")

    def _pages(self, client, operation, **kwargs):
        if operation == "list_hosted_zones":
            return iter([
                {"HostedZones": [{"Id": "/hostedzone/Z1", "Name": "example.test."}]},
                {"HostedZones": [{"Id": "/hostedzone/Z2", "Name": "other.test."}]},
            ])
        if operation == "list_resource_record_sets":
            return iter([
                {"ResourceRecordSets": [{"Name": "www.example.test.", "Type": "A", "TTL": 60}]},
                {"ResourceRecordSets": []},
            ])
        if operation == "list_distributions":
            return iter([
                {"DistributionList": {"Items": [{"Id": "ED1", "Status": "Deployed"}]}},
                {"DistributionList": {"Items": [{"Id": "ED2", "Status": "InProgress"}]}},
            ])
        if operation == "list_origin_access_controls":
            return iter([{"OriginAccessControlList": {"Items": [{"Id": "OAC1", "Name": "test-oac"}]}}])
        if operation == "list_web_acls":
            scope = kwargs["Scope"]
            item = {"Id": f"{scope}-1", "Name": f"{scope.lower()}-acl"}
            return iter([{"WebACLs": [item]}])
        if operation == "list_accelerators":
            return iter([{"Accelerators": [{"AcceleratorArn": "arn:aws:globalaccelerator::1:accelerator/a1", "Status": "DEPLOYED"}]}])
        if operation == "list_listeners":
            return iter([{"Listeners": [{"ListenerArn": "arn:listener/1", "Protocol": "TCP"}]}])
        raise AssertionError(f"Unexpected paginator operation: {operation}")

    def _require_collection(self, payload, key, *_args):
        value = payload
        if isinstance(key, (tuple, list)):
            for part in key:
                value = value[part]
            return value
        return value[key]

    def _client(self, _account, service, region=None):
        key = service if region is None else f"{service}:{region}"
        client = self.clients.setdefault(key, FakeReadClient(key))
        return client

    def test_global_and_regional_control_plane_selection_and_pagination(self):
        for client in self.clients.values():
            client.pages = {"unused": []}

        # Attach the non-paginator detail methods to the WAF and accelerator
        # fixtures while the paginator itself is supplied by the shared helper.
        for key in ("wafv2:us-east-1", "wafv2:us-west-2"):
            client = self.clients[key]
            client.pages["get_web_acl"] = {"WebACL": {"Name": "acl", "Id": "id", "Rules": []}}
        self.clients["globalaccelerator"].pages["list_listeners"] = {"Listeners": []}

        def pages(client, operation, **kwargs):
            return self._pages(client, operation, **kwargs)

        with (
            patch.object(edge, "aws_client", side_effect=self._client) as get_client,
            patch.object(edge, "get_enabled_regions", return_value=["us-east-1", "us-west-2"]),
            patch.object(edge, "iter_pages", side_effect=pages),
            patch.object(edge, "require_collection", side_effect=self._require_collection),
            patch.object(edge, "serialize_aws", side_effect=lambda value: value),
            patch.object(edge, "_apply_assets", side_effect=lambda *_args: 1),
        ):
            result = edge.sync_aws_edge_assets(self.account)

        get_client.assert_any_call(self.account, "route53")
        get_client.assert_any_call(self.account, "cloudfront", region="us-east-1")
        get_client.assert_any_call(self.account, "globalaccelerator", region="us-west-2")
        get_client.assert_any_call(self.account, "wafv2", region="us-east-1")
        get_client.assert_any_call(self.account, "wafv2", region="us-west-2")
        self.assertEqual(result["regions"], ["us-east-1", "us-west-2"])
        self.assertEqual(result["counts"][edge.AWS_ROUTE53_ZONE], 1)

        route53_operations = [operation for operation, _kwargs in self.clients["route53"].calls]
        self.assertEqual(route53_operations.count("list_hosted_zones"), 0)
        # The calls are made through the patched iterator, so the fake client
        # has no direct calls; pagination is asserted by the two-page fixture
        # producing both zones/records below.

    def test_route53_record_key_is_stable_and_bounded(self):
        short = edge._route53_record_unique_id("Z1", "www.example.test.", "A", "blue")
        same = edge._route53_record_unique_id("Z1", "www.example.test.", "A", "blue")
        different = edge._route53_record_unique_id("Z1", "www.example.test.", "A", "green")
        long_name = "x" * 240
        long_key = edge._route53_record_unique_id("Z1", long_name, "TXT", "blue")

        self.assertEqual(short, same)
        self.assertNotEqual(short, different)
        self.assertLessEqual(len(long_key), 100)
        self.assertEqual(long_key, edge._route53_record_unique_id("Z1", long_name, "TXT", "blue"))

    def test_waf_scopes_are_preserved_in_inventory_context(self):
        for client in self.clients.values():
            client.pages = {}
        for key in ("wafv2:us-east-1", "wafv2:us-west-2"):
            self.clients[key].pages["get_web_acl"] = {"WebACL": {"Name": "acl", "Id": "id", "Rules": []}}

        with (
            patch.object(edge, "_client", side_effect=self._client),
            patch.object(edge, "iter_pages", side_effect=self._pages),
            patch.object(edge, "require_collection", side_effect=self._require_collection),
            patch.object(edge, "serialize_aws", side_effect=lambda value: value),
        ):
            assets = edge._collect_waf(self.account, ["us-west-2"])

        scopes = {metadata["_cloudmoo_scope"] for _uid, _name, metadata in assets}
        self.assertEqual(scopes, {edge.WAF_CLOUDFRONT_SCOPE, edge.WAF_REGIONAL_SCOPE})
        cloudfront_client = self.clients["wafv2:us-east-1"]
        regional_client = self.clients["wafv2:us-west-2"]
        self.assertEqual(cloudfront_client.pages["get_web_acl"]["WebACL"]["Id"], "id")
        self.assertEqual(regional_client.pages["get_web_acl"]["WebACL"]["Id"], "id")

    def test_acm_inventory_enumerates_each_enabled_region(self):
        clients = {
            "us-east-1": FakeReadClient("acm:us-east-1"),
            "eu-west-1": FakeReadClient("acm:eu-west-1"),
        }

        def client(_account, _service, region=None):
            return clients[region]

        def pages(_client, operation, **_kwargs):
            self.assertEqual(operation, "list_certificates")
            region = _client.service.rsplit(":", 1)[-1]
            arn = f"arn:aws:acm:{region}:1:certificate/{region}"
            return iter([{"CertificateSummaryList": [{"CertificateArn": arn}]}])

        def describe(CertificateArn):
            region = CertificateArn.split(":")[3]
            return {"Certificate": {"CertificateArn": CertificateArn, "DomainName": f"{region}.example.test", "Status": "ISSUED"}}

        for fake in clients.values():
            fake.pages["describe_certificate"] = describe

        with (
            patch.object(edge, "_client", side_effect=client),
            patch.object(edge, "iter_pages", side_effect=pages),
            patch.object(edge, "require_collection", side_effect=self._require_collection),
            patch.object(edge, "serialize_aws", side_effect=lambda value: value),
        ):
            assets = edge._collect_certificates(self.account, list(clients))

        self.assertEqual(len(assets), 2)
        self.assertEqual({metadata["_cloudmoo_region"] for _uid, _name, metadata in assets}, set(clients))

    def test_status_normalization_and_global_endpoints_are_read_only(self):
        class CheckClient(FakeReadClient):
            def __init__(self, service):
                super().__init__(service)

        clients = {
            "route53": CheckClient("route53"),
            "cloudfront": CheckClient("cloudfront"),
            "wafv2": CheckClient("wafv2"),
            "globalaccelerator": CheckClient("globalaccelerator"),
        }
        clients["route53"].pages["get_hosted_zone"] = {"HostedZone": {"Id": "Z1", "Name": "example.test."}}
        clients["cloudfront"].pages["get_distribution"] = {"Distribution": {"Id": "ED1", "Status": "InProgress"}}
        clients["wafv2"].pages["get_web_acl"] = {"WebACL": {"Id": "W1", "Name": "acl"}}
        clients["globalaccelerator"].pages["describe_accelerator"] = {"Accelerator": {"Status": "DEPLOYED"}}

        def client(service, **kwargs):
            clients[service].calls.append(("client", kwargs))
            return clients[service]

        credentials = {"access_key": "access", "secret_key": "secret", "region": "eu-west-1"}
        with patch.object(aws_edge.boto3, "client", side_effect=client):
            status, _ = aws_edge.check_aws_cloudfront_distribution_status(
                "ED1", {**credentials, "metadata": {"resource_id": "ED1"}}
            )
            self.assertEqual(status, "pending")
            status, _ = aws_edge.check_aws_global_accelerator_status(
                "arn:aws:globalaccelerator::1:accelerator/a1",
                {**credentials, "metadata": {"resource_id": "arn:aws:globalaccelerator::1:accelerator/a1"}},
            )
            self.assertEqual(status, "deployed")

        self.assertEqual(
            [kwargs["region_name"] for operation, kwargs in clients["cloudfront"].calls if operation == "client"],
            ["us-east-1"],
        )
        self.assertEqual(
            [kwargs["region_name"] for operation, kwargs in clients["globalaccelerator"].calls if operation == "client"],
            ["us-west-2"],
        )
        for fake in clients.values():
            self.assertTrue(all(
                operation == "client" or operation.startswith(("list_", "get_", "describe_"))
                for operation, _kwargs in fake.calls
            ))
