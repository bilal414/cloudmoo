# Hetzner Cloud resource coverage

This document is the coverage contract for CloudMoo's Hetzner integration. It
records the provider surface, the resources CloudMoo will inventory and
monitor, and the safety boundary for any later live lifecycle tests.

This commit is documentation-only. It does not change application code, read
the local Hetzner credential file, call a Hetzner resource endpoint, or create,
change, or delete a provider resource.

The source of truth is Hetzner's [Cloud API
reference](https://docs.hetzner.cloud/reference/cloud) and its
[machine-readable Cloud API specification](https://docs.hetzner.cloud/cloud.spec.json),
reviewed on 2026-08-03. The API endpoint described by the documentation is
`https://api.hetzner.cloud/v1`.

## Current CloudMoo state

The repository currently has a legacy Hetzner adapter that inventories:

- Cloud Servers in `apps/console/cloud/hetzner/models.py`;
- Volumes in the same module; and
- Server and Volume detail checks in `apps/monitoring/checks/hetzner.py`.

The existing adapter is not evidence that the rest of the provider surface is
supported. The integration scope below expands and hardens this lane. A
resource is not considered integrated until its provider adapter, persistence,
status-check dispatch, UI/admin registration, tests, and read-only safety
guards are all complete.

## Authentication and API boundary

Hetzner API tokens are bound to one Project. The official [API token
guide](https://docs.hetzner.com/cloud/api/getting-started/generating-api-token/)
defines a **Read** token as GET-only and a **Read & Write** token as capable of
GET, POST, PUT, and DELETE requests. Normal CloudMoo inventory and monitoring
must use a Read token and must never rely on a write-capable token.

The [API usage guide](https://docs.hetzner.com/cloud/api/getting-started/using-api/)
defines the request semantics:

- `GET` reads a resource, collection, metric, catalog, or action;
- `POST` creates a resource or starts/configures an action;
- `PUT` changes an existing resource; and
- `DELETE` removes an existing resource.

CloudMoo writes only to its own database. Any write-capable token used for a
controlled live test belongs to a dedicated test Project and is never used by
the production sync path.

## Complete Cloud API surface

The following table covers the resource and reference groups present in the
official Cloud API specification. The write column describes provider
mutations, not CloudMoo behavior.

| API group | Read operations | Mutating operations | CloudMoo disposition |
| --- | --- | --- | --- |
| Actions | `GET /actions`, `GET /actions/{id}` | Actions are started by resource `POST` endpoints | P0 operation tracking for actions initiated by a controlled test; no unbounded historical event feed |
| Certificates | `GET /certificates`, `GET /certificates/{id}`, certificate actions | `POST`, `PUT`, `DELETE`, managed-certificate retry action | P0 inventory, expiry/status monitoring, and ownership-safe relationships |
| Datacenters | `GET /datacenters`, `GET /datacenters/{id}` | None | P1 reference context; not an independently monitored asset |
| Firewalls | `GET /firewalls`, `GET /firewalls/{id}` | `POST`, `PUT`, `DELETE`, apply/remove/set-rules actions | P0 inventory, rules, attached-resource relationships, and action failure detection |
| Floating IPs | `GET /floating_ips`, `GET /floating_ips/{id}` | `POST`, `PUT`, `DELETE`, assign/unassign/DNS/protection actions | P0 inventory and assignment/protection monitoring |
| Images | `GET /images`, `GET /images/{id}` | Image update/delete; server image-creation action | P0 system, app, snapshot, and backup-image inventory; no Volume snapshot claim |
| ISOs | `GET /isos`, `GET /isos/{id}` | None in the Cloud API resource group | P1 server-creation reference data; not a monitored asset |
| Load Balancer types | `GET /load_balancer_types`, `GET /load_balancer_types/{id}` | None | P1 pricing/capacity reference data |
| Load Balancers | `GET /load_balancers`, `GET /load_balancers/{id}`, `GET /load_balancers/{id}/metrics` | `POST`, `PUT`, `DELETE`, service/target/network/type/interface/protection actions | P0 inventory, service/target state, metrics, and action failure detection |
| Locations | `GET /locations`, `GET /locations/{id}` | None | P1 reference context and placement validation |
| Networks | `GET /networks`, `GET /networks/{id}` | `POST`, `PUT`, `DELETE`, subnet/route/IP-range/protection actions | P0 private-network topology and configuration monitoring |
| Placement Groups | `GET /placement_groups`, `GET /placement_groups/{id}` | `POST`, `PUT`, `DELETE` | P1 server-placement relationships; not an independent health signal |
| Pricing | `GET /pricing` | None | P1 optional reference/cost context; no billing ledger or cost alerting |
| Primary IPs | `GET /primary_ips`, `GET /primary_ips/{id}` | `POST`, `PUT`, `DELETE`, assign/unassign/DNS/protection actions | P0 inventory and assignment/protection monitoring |
| Server types | `GET /server_types`, `GET /server_types/{id}` | None | P1 server-capacity reference data |
| Servers | `GET /servers`, `GET /servers/{id}`, `GET /servers/{id}/metrics` | `POST`, `PUT`, `DELETE`, power, rescue, rebuild, image, network, type, IP, protection, and console actions | Existing coverage hardened as P0 inventory, failure detection, and metrics |
| SSH keys | `GET /ssh_keys`, `GET /ssh_keys/{id}` | `POST`, `PUT`, `DELETE` | P1 metadata/audit inventory; retain fingerprint and labels only, never private material |
| Volumes | `GET /volumes`, `GET /volumes/{id}` | `POST`, `PUT`, `DELETE`, attach/detach/resize/protection actions | Existing coverage hardened as P0 inventory and attachment monitoring |
| DNS zones | `GET /zones`, `GET /zones/{id_or_name}`, `GET /zones/{id_or_name}/zonefile` | `POST`, `PUT`, `DELETE`, primary-nameserver/TTL/import/protection actions | P0 zone configuration inventory; no unapproved DNS writes |
| DNS RRsets | `GET /zones/{id_or_name}/rrsets`, `GET /zones/{id_or_name}/rrsets/{rr_name}/{rr_type}` | `POST`, `PUT`, `DELETE`, record/TTL/protection actions | P0 nested record-set inventory; names and parent-zone identity are required |

The Cloud API also exposes collection filters, sorting, label selectors, and
reference metadata. Those are query aids and must not be persisted as separate
health assets unless a future design explicitly requires it.

The API's resource-specific `GET /<resource>/{id}/actions/{action_id}` forms
are deprecated in the [Cloud API
changelog](https://docs.hetzner.cloud/changelog). New code should use the
top-level action endpoint, or the supported collection form, for action
status.

## Integration priorities for this change

“Integrated” below means the target scope for the Hetzner implementation
initiative, not a claim that this documentation-only commit already ships the
adapter.

| Priority | CloudMoo integration | Read-only monitoring signal | Mutation boundary |
| --- | --- | --- | --- |
| P0 | Servers and Volumes | Provider lifecycle/status fields, attachments, labels, protection, and Server metrics | Normal sync uses only collection/detail/metric GETs; lifecycle actions are test-only and ID-allowlisted |
| P0 | Images: system, app, snapshot, and backup images | Image type/status, source server relationship, creation metadata, and protection | Snapshot creation/deletion is test-only; CloudMoo never enables backups or deletes customer images |
| P0 | Primary IPs and Floating IPs | Assignment, address family, assignee, location, protection, and presence | Allocation, assignment, unassignment, and release are test-only on resources created by that run |
| P0 | Firewalls | Rules, applied resources, labels, protection, and action errors | No rule, attachment, or protection changes during normal sync |
| P0 | Networks, subnets, and routes | Private-network topology, IP ranges, zones, attached resources, and action errors | No network, subnet, route, or IP-range changes during normal sync |
| P0 | Load Balancers | State, type, services, targets, network attachments, protection, and Load Balancer metrics | No service, target, network, type, interface, or protection changes during normal sync |
| P0 | DNS zones and RRsets | Zone mode, nameservers, TTL, RRset/record configuration, and zone-file readability | No zone, RRset, record, import, nameserver, or TTL writes during normal sync |
| P0 | TLS certificates | Managed/uploaded type, status, domains/SANs, expiration, and issuance action status | No certificate request, replacement, retry, or deletion during normal sync |
| P0 | Bounded action status | `running`, `success`, `error`, progress, and bounded error details for known action IDs | Never enumerate or mutate arbitrary customer actions; never poll deprecated resource-action paths |
| P1 | Placement Groups and SSH keys | Membership/configuration, labels, protection, and key fingerprint metadata | No placement or key changes during normal sync; private key material is never accepted or stored |
| P1 | Datacenters, Locations, Server Types, Load Balancer Types, ISOs, and Pricing | Reference data used to enrich assets and validate placement/capacity | Reference GETs only; these are not scheduled health assets |

### Identity and persistence requirements

Every persisted asset must be identified by the Hetzner Project, resource
kind, stable numeric ID, and parent context where applicable. Names are labels,
not identity. DNS RRsets additionally require the exact zone and record-set
name/type; action records require the action ID and related resource IDs.

Provider payloads must be allowlisted and bounded before persistence. Never
persist API tokens, private keys, passwords, kubeconfigs, certificate private
keys, or arbitrary request/response bodies. Public SSH-key material should be
reduced to a fingerprint and safe metadata. Error messages and action details
must be length-bounded and passed through the shared redaction path.

## Read-only versus mutating operations

### Normal CloudMoo operation

Inventory, reconciliation, detail views, and scheduled status checks may use
only:

- `GET` collection and detail endpoints for the P0/P1 resources;
- `GET /servers/{id}/metrics` and `GET /load_balancers/{id}/metrics`;
- `GET /actions/{id}` for action IDs already associated with a CloudMoo test
  record; and
- `GET` reference/catalog endpoints needed to enrich an asset.

There must be no `POST`, `PUT`, or `DELETE` call in the normal sync or
monitoring path. A static method guard and mocked provider client should fail
tests if a mutating method is attempted.

### Controlled live-test operation

Live lifecycle tests may use a separate Read & Write token only after the
preconditions in the protocol below are satisfied. Each mutating endpoint must
be explicitly allowlisted, and every target ID must have been returned by a
successful create operation in the same run. A write-capable token is not
permission to discover or modify unrelated Project resources.

## Monitoring semantics and limitations

The Cloud API exposes time-series metrics for Servers and Load Balancers. It
does not provide an equivalent metrics endpoint for every resource family.
CloudMoo therefore must distinguish the following signals:

- **Direct state:** provider lifecycle/status fields for Servers, Volumes,
  Images, Load Balancers, and Certificates.
- **Configuration posture:** presence, protection, labels, assignments, rules,
  routes, services, targets, RRsets, or certificate expiration for resources
  without a health state.
- **Operation state:** an asynchronous Action can be `running`, `success`, or
  `error`; a failed action is not silently converted into a healthy resource.
- **Provider/API failure:** authentication failures, throttling, malformed
  responses, timeouts, and 5xx responses are distinct errors and must not be
  recorded as a provider resource being deleted or healthy.

DNS, Firewalls, Networks, IPs, SSH keys, Placement Groups, catalogs, and
reference data do not become healthy merely because a GET request succeeded.
CloudMoo should report the resource's observable configuration and availability
of the API response. External reachability, application health, Kubernetes
pod health, database query health, and object-level storage health require a
separate probe or product connector and are outside this Cloud API monitor.

## Pagination, rate limiting, and fail-closed reconciliation

The official [pagination and rate-limiting
documentation](https://docs.hetzner.cloud/reference/cloud#description/pagination)
specifies `page`, `per_page`, pagination metadata, `Link` headers, and the
`RateLimit-Limit`, `RateLimit-Remaining`, and `RateLimit-Reset` headers. The
default limit is documented as 3,600 requests per hour per Project, subject to
change by Hetzner.

The adapter must follow these rules:

1. Request an explicit `per_page` no greater than the documented maximum of 50
   unless the endpoint documents another limit.
2. Require the expected collection key and validate that it is a list.
3. Follow the response's pagination metadata or `Link` header. Do not decide
   that a collection is complete solely because the current page contains
   fewer items than `per_page`.
4. Validate page progression, stop only at a documented terminal page, and
   bound the maximum page count for one sync.
5. Treat a missing collection, malformed JSON, inconsistent pagination, a
   truncated page, timeout, 429, or 5xx response as an incomplete inventory.
6. Reconcile `no_longer_exists` only after every page for that resource family
   succeeds. A failed page must preserve all existing local assets.
7. Honor rate-limit headers and use bounded exponential backoff. Action polling
   must be slow enough not to exhaust the Project quota and must stop at a
   deadline.
8. Redact and bound provider error details before they enter logs, status
   history, email notifications, or the local database.

The same fail-closed rule applies to nested collections such as network
subnets/routes and DNS RRsets. An omitted nested collection is not equivalent
to an empty collection.

## Resources outside the Cloud API

The Cloud API reference is not a universal Hetzner product API. Hetzner's
[API overview](https://docs.hetzner.cloud/) separates the Cloud API from other
product APIs.

| Product or capability | Official access surface | Status for this change |
| --- | --- | --- |
| Object Storage | S3-compatible API and Object Storage endpoints; see the [Object Storage overview](https://docs.hetzner.com/storage/object-storage/overview/) | Not a Cloud API resource. Do not invent `/v1/buckets` inventory. A future connector may use separate S3 credentials and must define safe bucket/object metadata boundaries. |
| Storage Boxes | `api.hetzner.com`, Console, SFTP/SCP/SMB/WebDAV and related protocols; see the [Storage Box overview](https://docs.hetzner.com/storage/storage-box/general/) | Separate product and credential surface. Not included in the Cloud API adapter or this change. |
| Dedicated Servers and vSwitches | Robot Web Service; see the [official API overview](https://docs.hetzner.cloud/) | Separate API and product model. Not included in this Cloud API adapter. |
| Managed databases | Hetzner managed database/konsoleH documentation, such as [database connection details](https://docs.hetzner.com/managed/databases/general/connection-details-database/) | No managed-database resource appears in the Cloud API specification. Do not claim Cloud API database inventory or create a fake `database` endpoint. A future product-specific connector would need separate scope and credentials. |
| PaaS/application hosting | Hetzner Cloud Apps are server images and deployment recipes; see the [Cloud Apps overview](https://docs.hetzner.com/cloud/apps/overview/) | `images?type=app` is image catalog data, not a managed PaaS control plane. PaaS deployment, application logs, and release state are out of scope. |
| Managed Kubernetes | No Kubernetes resource is present in the [Cloud API specification](https://docs.hetzner.cloud/cloud.spec.json). Kubernetes on Hetzner Cloud is commonly assembled from Cloud Servers, Networks, Volumes, and Load Balancers; see the [official Kubernetes tutorial](https://community.hetzner.com/tutorials/install-kubernetes-cluster/) | No managed-cluster or Kubernetes-object integration in this change. CloudMoo may monitor the underlying Hetzner resources, but it must not label them as a Kubernetes cluster or inspect a kubeconfig without a separate connector. |

These products must not be treated as empty Cloud API collections. Missing
support is a deliberate boundary, not evidence that the customer's account
contains no resources.

## Live-test protocol

This protocol is a future opt-in procedure for validating the implementation.
It is not executed by this documentation change. It permits only resources
created by the current test run to be mutated or deleted.

### Preconditions

1. Use a dedicated Hetzner Cloud Project created for testing, with a dedicated
   Read & Write token. The token must not be reused by normal CloudMoo sync.
2. Generate a run identifier such as
   `cm-e2e-20260803T120000Z-<random>`. Keep it short enough for provider name
   limits and use it in every resource name and supported label.
3. Use a label such as `cloudmoo.com/test-run=<run-id>` and an owner label such
   as `cloudmoo.com/owner=cloudmoo-e2e`. Do not use the reserved `hetzner.cloud/`
   label prefix.
4. Before any write, make read-only collection calls and verify that the exact
   run ID and name prefix do not already exist. If a collision is found, abort;
   never reuse or rename a pre-existing resource.
5. Select the smallest suitable server, volume, Load Balancer, and location
   permitted by the Project. Record an estimated cost and a maximum runtime
   before starting. Skip optional DNS/certificate tests unless the test domain
   is owned exclusively for this run.

### Ownership ledger

The harness must create an in-memory or protected local ledger containing, for
each successful create:

- resource kind, Project, exact returned numeric ID, name, run label, and
  parent IDs;
- the action ID returned by the API, if any; and
- the exact endpoint and intended cleanup operation.

Only IDs in this ledger may be passed to a mutating endpoint. A name prefix or
label alone is never sufficient authorization to delete or change a resource.
If a response does not contain the expected ID, label, or parent relationship,
stop and fail closed.

### Suggested test cases

| Case | Resources created by the run | Checks | Required write boundary |
| --- | --- | --- | --- |
| T1: baseline and inventory | None, then only test resources | Read-only auth, project scope, complete collection/pagination, stable IDs, labels, and no collision | No writes before the ledger is initialized |
| T2: Server and Volume lifecycle | One labeled Server and one labeled Volume | Create, inventory, detail status, attachment relationship, Server metrics, and cleanup | Only returned Server/Volume IDs; never use an existing SSH key, Server, or Volume as a target |
| T3: status transition | The test Server | Start/stop or another explicitly approved action, action polling, state transition, timeout, and recovery | Only the test Server ID; no broad action enumeration |
| T4: private network and firewall | One Network, its Subnet, one Firewall, and test Server | Attach/detach, apply/remove, rule/config inventory, action errors, and cleanup | Only test Network/Subnet/Firewall/Server IDs |
| T5: IP assignment | One Primary IP and/or Floating IP plus the test Server | Assignment, unassignment, address and assignee state, protection metadata, and cleanup | Only IPs created in this run and the test Server |
| T6: Load Balancer | One Load Balancer and the test Server as target | Service/target state, metrics, action polling, target removal, and cleanup | Never add an existing Project Server as a target |
| T7: snapshot/backup image | One test Server and an image created from it | Image type/source relationship, action status, inventory, and image deletion | Only the image and source Server IDs in the ledger; no Volume snapshot assumption |
| T8: DNS | A disposable test zone and RRset, only if the Project/domain is owned for testing | Zone mode, nameservers, RRset, zone file, TTL, and cleanup | No production zone or record may be used; no external delegation change unless separately approved |
| T9: certificate | A disposable test certificate/domain, only as an explicit opt-in | Managed/uploaded state, SANs, expiration, issuance action, and cleanup | Never request, retry, replace, or delete a certificate for an existing domain |
| T10: reference data | None | Locations, datacenters, types, ISOs, pricing, and labels are readable and bounded | GET only |

T8 and T9 are optional because DNS delegation and certificate issuance can
affect systems outside the Cloud API Project and may take an unpredictable
amount of time. A test must not use a production domain merely to exercise
these endpoints.

### Action polling

For every mutating request that returns an Action:

1. Record the Action ID immediately in the ownership ledger.
2. Poll the supported top-level action endpoint at a bounded interval.
3. Stop only at `success` or `error`, or at a hard deadline.
4. After success, GET the target resource and verify the expected state before
   continuing.
5. On error, record a redacted, bounded diagnostic and begin safe cleanup; do
   not retry an unknown mutation against another resource.

### Cleanup and final verification

Cleanup runs in reverse dependency order and waits for each Action to settle:

1. Remove RRsets and delete the disposable DNS zone, if T8 ran.
2. Remove certificate test associations and delete the test certificate, if
   T9 ran.
3. Remove Load Balancer targets/services and delete the test Load Balancer.
4. Unassign Primary/Floating IPs and delete only the test IPs.
5. Remove firewall attachments and delete only the test Firewall.
6. Detach the test Volume and delete only the test Volume.
7. Delete test Images created by the run.
8. Detach/delete test Network subnets and routes, then delete only the test
   Network.
9. Delete only the test Server, Placement Group, or SSH key if that run
   created them.

Before each deletion, GET the exact ID and verify its ledger entry, name/label,
Project, and parent relationship. If any check fails, stop cleanup rather than
expanding the target set. After cleanup, perform read-only GETs for the ledger
IDs and record 404/not-found confirmation. If a resource remains, report its
exact owned ID for manual follow-up; never run a bulk delete or a prefix-only
cleanup.

Test logs may include method, endpoint family, resource kind, owned ID, action
state, and timing, but must never include tokens, authorization headers, full
provider payloads, private keys, passwords, certificate private material, or
unredacted live API output.

## Explicit non-goals

- No provider mutations from normal CloudMoo sync, monitoring, inventory, UI,
  or admin code.
- No changes to any pre-existing Hetzner resource, including resources that
  happen to share a name or label with a test run.
- No account-wide discovery across Projects; a Cloud API token is Project
  scoped.
- No volume backups or snapshots: Hetzner's [Volume
  documentation](https://docs.hetzner.com/cloud/volumes/overview/) states that
  Volumes do not have provider Backups or Snapshots, and Server backups do not
  include attached Volumes.
- No Object Storage buckets/objects, Storage Boxes, Dedicated Servers,
  vSwitches, managed databases, PaaS deployments, managed Kubernetes clusters,
  Kubernetes objects, or application logs in the Cloud API adapter.
- No claim that a successful API GET proves end-user reachability, application
  health, database query health, Kubernetes health, or object durability.
- No arbitrary action-history ingestion, aggressive action polling, or use of
  deprecated resource-specific action endpoints.
- No storage of secrets, kubeconfigs, private keys, certificate private keys,
  or unbounded provider payloads.
- No billing system or cost-optimization engine; the optional Pricing endpoint
  is reference context only.

## Definition of done for the implementation

The subsequent code change is complete only when it has:

1. Provider adapters and model relations for every P0 resource in the
   priority table, with explicit type/dispatch registration.
2. Read-only operation guards proving normal sync and monitoring never issue
   POST, PUT, or DELETE requests.
3. Fixture coverage for valid pagination, full terminal pages, malformed
   pages, missing nested collections, rate limits, timeouts, provider errors,
   action failures, redaction, and fail-closed reconciliation.
4. Status checks that distinguish provider state, configuration posture,
   action state, missing resources, and API errors.
5. No claims or tests for resources outside the Cloud API unless a separate
   product-specific connector and credential boundary has been approved.
6. A live-test report, when authorized, containing only owned test-resource
   identifiers and sanitized outcomes, with final verification that every
   created resource was cleaned up or explicitly handed off for manual
   follow-up.
