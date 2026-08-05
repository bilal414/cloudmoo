# Vultr resource coverage

CloudMoo's Vultr inventory and monitoring lanes are read-only. Every
authenticated inventory/monitoring request goes through the shared GET-only
client and a fixed endpoint allowlist; the legacy account-credential
validation path is also GET-only. The adapter never creates, updates, starts, stops,
attaches, detaches, deletes, or otherwise mutates a Vultr resource.

The existing `CoreVultrAccount` continues to store the configured account
credential as it did before this expansion. The expanded inventory metadata,
check results, logs, and error messages never copy or return that bearer
token. At runtime, credentials are held only long enough to make an allowed
request.

## Inventory and monitoring coverage

### Compute, backups, and storage

| Surface | GET path | CloudMoo asset/check | Boundary |
| --- | --- | --- | --- |
| Instances | `/v2/instances` | Existing Vultr server asset | Existing legacy sync/check path, now using the shared transport for inventory. |
| Block volumes | `/v2/blocks` | Existing Vultr volume asset | Existing legacy sync/check path, with fail-closed missing-resource reconciliation. |
| Managed databases | `/v2/databases` | Existing database asset | Safe database projection; credentials, connection strings, and certificates are removed. |
| Bare-metal servers | `/v2/bare-metals` | `vultr_bare_metal` | Inventory plus lifecycle/health status check. |
| Block snapshots | `/v2/blocks/snapshots` | `vultr_block_snapshot` | Inventory plus snapshot state check. |
| Instance backups | `/v2/backups` | `backup` | Inventory plus backup state check. |
| Compute plans | `/v2/plans` | `vultr_compute_plan` | Reference inventory; monitoring is disabled by default. |
| Bandwidth | Resource/account bandwidth GET surfaces | `vultr_bandwidth_metric` | Returned as a bounded metric/status result; no unbounded time-series ingestion. |

VFS and storage-gateway internals are represented as explicit unsupported
surfaces. No endpoint is guessed for them, so they cannot accidentally trigger
an arbitrary provider request or be reconciled as an empty collection.

### Data, network, DNS, and edge

| Surface | GET path | CloudMoo asset/check | Boundary |
| --- | --- | --- | --- |
| Load balancers | `/v2/load-balancers` | `load_balancer` | Inventory and status; listener/certificate secrets are projected away. |
| VPCs | `/v2/vpc2` | `vpc` | Inventory and lifecycle/status check. |
| NAT gateways | `/v2/nat-gateways` | `nat_gateway` | Inventory and lifecycle/status check. |
| Firewalls | `/v2/firewalls` | `firewall` | Inventory and status. |
| Firewall rules | Firewall-scoped GET | `vultr_firewall_rule` | Nested rule summaries only; no write/action endpoint. |
| Reserved IPs | `/v2/reserved-ips` | `reserved_ip` | Inventory and assignment/status check. |
| DNS zones | `/v2/domains` | `domain` | Inventory and status. |
| DNS records | Domain-scoped GET | `dns_record` | Nested record summaries; record data is bounded and redacted. |
| CDN zones | `/v2/cdn` | `cdn_endpoint` | Pull and push zone inventory/status; signed URLs are discarded. |
| TLS certificates | `/v2/ssl/certificates` | `certificate` | Lifecycle/expiry metadata only; certificate bodies/private keys are discarded. |

### Platform and delivery services

| Surface | GET path | CloudMoo asset/check | Boundary |
| --- | --- | --- | --- |
| Kubernetes clusters | `/v2/kubernetes/clusters` | `kubernetes_cluster` | Cluster metadata and health; kubeconfig/credentials are never requested or stored. |
| Kubernetes node pools | Cluster-scoped GET | `kubernetes_node_pool` | Pool and bounded node-state summaries; no node credentials. |
| Object Storage control plane | `/v2/object-storage` | `object_storage` | Control-plane metadata only; bucket/object contents are never read. |
| Object Storage clusters/tiers | `/v2/object-storage/clusters`, `/v2/object-storage/tiers` | `storage_cluster`, `storage_tier` | Capacity/status metadata only. |
| Container registries | `/v2/registry` | `container_registry` | Registry metadata only; no login material. |
| Registry repositories | Registry-scoped GET | `registry_repository` | Repository metadata/counts only. |
| Registry artifacts | Repository-scoped GET | `registry_artifact` | Digest/tag/manifest summary only; no image pull. |
| Inference endpoints | `/v2/inference` | `vultr_inference` | Endpoint/model/status metadata; no data-plane requests. |
| Plans and regions | `/v2/plans`, `/v2/regions` | `plan`, `region` | Reference inventory; monitoring is disabled by default. |

Vultr does not expose a separate AWS-style App Platform product. App
Platform-specific resources are therefore not fabricated or mapped to an
unrelated service.

## Account, governance, operations, and provider health

| Surface | GET path | Result | Boundary |
| --- | --- | --- | --- |
| Account/profile | `/v2/account` | `vultr_account_profile` | Safe identity and billing-summary fields; contact/payment details omitted. |
| Plan and limits | `/v2/account/plan`, `/v2/account/limits` | `vultr_account_plan`, `vultr_account_limits` | Capacity and quota posture only. |
| Account bandwidth | `/v2/account/bandwidth` | `vultr_bandwidth_metric` | Bounded usage fields only. |
| Billing transactions | `/v2/account/transactions` | `vultr_billing_transaction` | Bounded amount/date/type summaries; no payment method/profile data. |
| Activity log | `/v2/account/log` | `vultr_account_log` | Bounded cursor pagination; raw headers, cookies, URLs, query strings, and sensitive descriptions are removed. |
| IAM users | `/v2/account/users` | `vultr_iam_user` | Role/status/posture and timestamps; no email, credentials, or key material. |
| BGP sessions | `/v2/bgp` | `vultr_bgp_session` | Read only when available to the account/API version. |
| Scoped operation status | Known parent resource + action ID | shared `action` result | Unscoped account-wide action listing is unsupported. |
| Vultr public status | `https://status.vultr.com/status.json` | external health result | Unauthenticated provider health only; it is never treated as account/resource state. |

API-key metadata, support-ticket details, detailed cost/customer data, and
unscoped action history are explicit unsupported results. The adapter does not
attempt to retrieve key material, private support records, or undocumented
cost surfaces.

## Safety and reconciliation guarantees

- Authenticated calls use GET only, with a 15-second timeout and at most two
  retries for 429/5xx/transport failures. Backoff is bounded and injectable in
  tests.
- Endpoint paths are fixed or built from strictly validated, URL-encoded
  resource identifiers. Query parameters are limited to bounded `per_page`
  and `cursor` pagination values.
- Collection reads use bounded page size, page count, record count, and cursor
  depth. Repeated cursors, malformed pages, missing pagination metadata, and
  partial reads fail closed.
- A collection is reconciled only after all pages and every item pass
  validation. Provider failures never mark local assets as missing; missing
  assets are saved individually so monitoring schedules are preserved.
- Provider records are allowlisted and recursively redacted before they reach
  models, checks, logs, or the UI. Tokens, passwords, credentials, private
  keys, kubeconfigs, certificate bodies, signed URLs, object contents, and
  raw request/response material are excluded.
- Optional or version-dependent endpoints report `unsupported`, `error`, or
  `partial` explicitly. A 404 is never silently treated as an empty
  collection.

## Verification

The focused Vultr suite covers the GET-only client, endpoint allowlisting,
retry behavior, pagination bounds, redaction, family collection/sync logic,
status checks, account-operation handling, and shared registration/dispatch.
The complete Django suite also runs against the Docker PostgreSQL service.

```text
docker compose run --rm --no-deps -e DB_HOST=db -e SKIP_MIGRATIONS=true web \
  python manage.py test --verbosity 1
```

At implementation time this completed with 298 tests passing and 7 existing
skips. No live Vultr credentials or lifecycle mutations are required for the
test suite.
