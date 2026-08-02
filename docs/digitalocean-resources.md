# DigitalOcean resource coverage

CloudMoo’s DigitalOcean adapter is read-only. A cloud sync inventories the
resources below, creates or updates the corresponding local asset, and marks
an asset `no_longer_exists` only after a complete provider collection has been
received. Malformed or partial collections fail the sync so a temporary API
problem cannot delete the local inventory.

| DigitalOcean resource | CloudMoo asset type | Inventory source | Status check |
| --- | --- | --- | --- |
| Droplets | Server | `GET /v2/droplets` | Droplet lifecycle status |
| Managed Databases | Database | `GET /v2/databases` | Cluster status |
| Block Storage Volumes | Volume | `GET /v2/volumes` | Attached or detached |
| Droplet automatic backups | Backup | `GET /v2/droplets/{id}/backups` | Backup image status |
| Droplet and volume snapshots | Snapshot | `GET /v2/snapshots` | Available when regions are returned |
| Reserved IPv4 and IPv6 | Reserved IP | `GET /v2/reserved_ips`, `GET /v2/reserved_ipv6` | Reserved or assigned |
| Firewalls | Firewall | `GET /v2/firewalls` | Provider firewall status |
| Load Balancers | Load Balancer | `GET /v2/load_balancers` | Provider load-balancer status |
| App Platform apps | App Platform App | `GET /v2/apps` | In-progress or active deployment phase |
| Spaces buckets | Object Storage | S3-compatible `ListBuckets` | S3 `HeadBucket` |
| Container Registry | Container Registry | `GET /v2/registries` | Registry accessibility |
| Kubernetes clusters | Kubernetes Cluster | `GET /v2/kubernetes/clusters` | Cluster status state |
| Kubernetes node pools | Kubernetes Node Pool | `GET /v2/kubernetes/clusters/{id}/node_pools` | Aggregate node state |
| VPCs | VPC | `GET /v2/vpcs` | VPC definition is present |
| VPC peerings | VPC Peering | `GET /v2/vpc_peerings` | Peering lifecycle status |
| VPC NAT gateways | NAT Gateway | `GET /v2/vpc_nat_gateways` | NAT gateway lifecycle state |
| Domains | Domain | `GET /v2/domains` | Domain is present |
| DNS records | DNS Record | `GET /v2/domains/{domain}/records` | Record is present |
| CDN endpoints | CDN Endpoint | `GET /v2/cdn/endpoints` | CDN origin is present |
| TLS certificates | Certificate | `GET /v2/certificates` | Certificate state |

The endpoint and payload choices follow DigitalOcean’s [API reference](https://docs.digitalocean.com/reference/api/reference/), including the [Managed Databases API](https://docs.digitalocean.com/products/databases/postgresql/reference/api/), [Snapshots API](https://docs.digitalocean.com/products/snapshots/reference/api/), [Reserved IP API](https://docs.digitalocean.com/products/networking/reserved-ips/reference/api/), [Apps API](https://docs.digitalocean.com/reference/api/reference/apps/), [Container Registry API](https://docs.digitalocean.com/reference/api/reference/container-registries/), [Kubernetes API](https://docs.digitalocean.com/products/kubernetes/reference/api/), [VPC API](https://docs.digitalocean.com/reference/api/reference/vpcs/), [VPC peering API](https://docs.digitalocean.com/products/networking/vpc/reference/api/vpc-peerings/), [VPC NAT gateway API](https://docs.digitalocean.com/reference/api/reference/vpc-nat-gateways/), [DNS API](https://docs.digitalocean.com/products/networking/dns/reference/api/), [CDN endpoint API](https://docs.digitalocean.com/products/spaces/reference/api/cdn-endpoints/), and [certificate API](https://docs.digitalocean.com/reference/api/reference/certificates/).

Kubernetes node pools and DNS records retain their parent context in local
metadata so status checks can address the nested API path. CloudMoo never
requests or stores Kubernetes kubeconfigs, database credentials, Spaces
secrets, certificate private keys, or DNS/API credentials as asset metadata.

## Credentials

The DigitalOcean control-plane token is used for all resources backed by the
`/v2` API. It should be a read-only Personal Access Token scoped to the
intended DigitalOcean team.

Spaces is separate from the control plane and uses [S3-compatible API
credentials](https://docs.digitalocean.com/reference/api/spaces/). CloudMoo
therefore accepts optional Spaces access key, secret key, and region fields.
Without them, existing Space assets are disabled rather than marked deleted;
this avoids treating an unconfigured credential as an empty bucket inventory.
The secret is never included in asset metadata or monitoring metadata.

## Monitoring semantics

- HTTP `401`/`403` becomes `invalid_access_token`; `404` becomes `not_found`.
- Provider payloads without the required resource or status fields become an
  `error` heartbeat rather than a false healthy result.
- Spaces uses `HeadBucket`, bounded botocore timeouts, and the same normalized
  status categories as HTTP checks.
- Inventory payloads are redacted before being persisted, and status errors are
  bounded and redacted before entering the monitoring state or email path.
- IPv4 and IPv6 Reserved IPs use separate DigitalOcean endpoints and retain
  the address as the stable asset identifier.

## Automated test cases

The focused suite is `tests/test_digitalocean_resources.py` and covers:

1. Inventory of all requested DigitalOcean control-plane resource families.
2. Automatic Droplet-backup inventory and marking an old backup as gone.
3. Fail-closed behavior when Droplet backup metadata is incomplete.
4. Spaces bucket inventory with separate S3 credentials.
5. Disabling existing Spaces checks when optional credentials are absent.
6. Status checks and metadata wrappers for databases, load balancers,
   snapshots, backups, firewalls, App Platform, and Container Registry.
7. IPv4 and IPv6 Reserved IP status endpoints.
8. Spaces `HeadBucket` success and access-denied classification.
9. Kubernetes cluster/node-pool and DNS/domain contextual checks.
10. VPC, peering, NAT gateway, CDN endpoint, and certificate status checks.
11. Dynamic status-check registration for every supported DigitalOcean asset type.

Run it with:

```sh
docker compose run --rm --no-deps -e SKIP_MIGRATIONS=true web \
  python manage.py test tests.test_digitalocean_resources
```

The implementation tests use mocked provider responses. Live DigitalOcean
resource creation and lifecycle testing is documented in the [2026-08-02 live
report](digitalocean-live-integration-2026-08-02.md); the [2026-08-01 live
report](digitalocean-live-integration-2026-08-01.md) records the earlier
Droplet/Volume pass.
