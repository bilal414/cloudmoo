# Amazon Lightsail resource coverage

CloudMoo inventories Lightsail through a dedicated, read-only adapter. The
adapter uses Lightsail `Get*` APIs only; it does not create, update, attach,
detach, start, stop, or delete AWS resources. Inventory writes are limited to
CloudMoo's local database and monitoring schedules.

The implementation follows the [Lightsail API reference](https://docs.aws.amazon.com/lightsail/2016-11-28/api-reference/),
including [regions](https://docs.aws.amazon.com/lightsail/2016-11-28/api-reference/API_GetRegions.html),
[distributions](https://docs.aws.amazon.com/cli/latest/reference/lightsail/get-distributions.html),
[domains](https://docs.aws.amazon.com/lightsail/2016-11-28/api-reference/API_GetDomains.html),
and [container logs](https://docs.aws.amazon.com/lightsail/2016-11-28/api-reference/API_GetContainerLog.html).

## Coverage

| Lightsail capability | CloudMoo coverage |
| --- | --- |
| Instances | Inventory, state, tags, port/firewall states, CPU metric |
| Disks | Inventory and state |
| Instance and disk snapshots | Inventory and state |
| Static IPs | Inventory and attached/available state |
| Managed databases and snapshots | Inventory and state; database CPU metric |
| Load balancers | Inventory, state, request metric, attached TLS certificates |
| TLS certificates | Inventory, certificate details, status, SANs, and tags |
| Buckets/object storage | Inventory, state, versioning, tags, and object-count metric |
| CDN distributions | Inventory, enabled/status state, tags, and request metric |
| DNS zones and records | Domains plus expanded domain entries/records |
| Container services | Inventory, deployment state, CPU metric, bounded log window |
| Container deployments and images | Inventory and deployment/image metadata |
| Alarms | Inventory and alarm state/threshold/metric metadata |
| Operations | Inventory and operation status/details |
| Auto-snapshots | Instance/disk auto-snapshot inventory and status |

Regional collections are queried in every available Lightsail region returned
by `GetRegions`. The global-control-plane collections (distributions and DNS
domains) are queried through `us-east-1`. A malformed or incomplete collection
response fails closed and does not mark previously known assets as missing.

## Read-only IAM surface

The adapter and status checker require only Lightsail read operations: the
resource collection/detail calls listed above, `GetInstancePortStates`, the
resource metric calls, `GetLoadBalancerTlsCertificates`,
`GetContainerServiceDeployments`, `GetContainerImages`, `GetContainerLog`,
`GetAutoSnapshots`, `GetAlarms`, `GetOperations`, and `GetOperation`. A
least-privilege policy should grant only the corresponding `lightsail:Get...`
actions. No `Create*`, `Update*`, `Delete*`, `Attach*`, `Detach*`, `Start*`,
`Stop*`, `Allocate*`, or `Release*` Lightsail actions are needed.

## Monitoring and data safety

Primary resource types receive the normal CloudMoo status schedule. Deployment
and image rows are inventory records and are intentionally not scheduled as
independent health checks. Container-service checks fetch at most three pages
and 100 events per container from the last 15 minutes; log messages are bounded
and passed through the shared sensitive-value redaction before persistence.

Provider metadata is redacted at the model boundary. In particular, container
environment configuration and credential-like log values are not retained in
status history.

## Automated test cases

The focused suite is `tests/test_aws_lightsail.py`:

1. Inventories the requested resource families, DNS records, deployments,
   images, alarms, operations, and auto-snapshots.
2. Verifies instance port states, tags, regional monitoring context, and
   read-only API usage.
3. Verifies status checks for instance firewall/port state and CPU metrics.
4. Verifies container-service status, CPU metrics, bounded logs, and log
   redaction.
5. Verifies every Lightsail asset type resolves to a status checker.
6. Verifies incomplete inventory responses fail closed.

Run the focused suite with:

```bash
docker compose run --rm --no-deps -e SKIP_MIGRATIONS=true web \
  python manage.py test tests.test_aws_lightsail
```

The test fixture exposes only `get_*` methods, so an attempted provider
mutation fails the test immediately.

## Live read-only verification

On 2026-08-02, the adapter was run against the AWS account using a temporary
local CloudMoo account record. The run queried all 19 available Lightsail
regions, made 261 Lightsail API calls, and observed only `Get*` operations.
No AWS resource was created, changed, or deleted. The temporary local account,
its inventory rows, and its schedules were removed afterward and verified
absent. A few existing auto-snapshot sources returned the provider's
`InvalidInputException`; the sync logged those partial-detail failures without
marking previously known snapshot rows as missing.
