# DigitalOcean live integration test report

Date: 2026-08-02 (provider timestamps are UTC)

## Scope and safety controls

- The DigitalOcean control-plane token was read from `_docs/digitalocean.txt`
  in memory only and was not printed, persisted, or committed.
- The token was verified immediately before mutation against exactly one
  `Personal` team (`0ba41777-3fbc-4093-9193-0f2709d2948a`) and its `Personal`
  project (`9a10e395-1a6a-470c-adaf-8897ff5576ae`).
- Existing resources were inventoried read-only. Every mutating request used a
  fresh test prefix, and cleanup used only IDs returned by that run's create
  calls.
- Temporary resources were deleted in reverse dependency order. Each delete
  was followed by a provider `404` check; a final inventory found no test
  prefixes.
- The CloudMoo live-inventory fixture used a temporary local user/account and
  patched schedule calls. It was removed after each run.

The API paths and lifecycle expectations follow DigitalOcean's [Kubernetes
API](https://docs.digitalocean.com/products/kubernetes/reference/api/), [VPC
API](https://docs.digitalocean.com/reference/api/reference/vpcs/), [VPC
peering API](https://docs.digitalocean.com/reference/api/reference/vpc-peerings/),
[VPC NAT gateway API](https://docs.digitalocean.com/reference/api/reference/vpc-nat-gateways/),
[DNS records API](https://docs.digitalocean.com/products/networking/dns/reference/api/domain-records/),
[CDN endpoint API](https://docs.digitalocean.com/products/spaces/reference/api/cdn-endpoints/),
and [certificate API](https://docs.digitalocean.com/reference/api/reference/certificates/).

## Live test matrix

| Case | Resource and operation | Observed result | Cleanup |
| --- | --- | --- | --- |
| DO-LIVE-NEW-01 | Personal team/project guard | PASS — account `200`; exactly one Personal team/project | Read-only |
| DO-LIVE-NEW-02 | VPC create, detail, status | PASS — two VPCs returned `201`; CloudMoo status `available` | Both returned `404` after delete |
| DO-LIVE-NEW-03 | VPC peering lifecycle | PASS — `PROVISIONING → ACTIVE`; status check `ACTIVE` | Delete accepted `202`; final `404` |
| DO-LIVE-NEW-04 | VPC NAT gateway lifecycle | PASS — `NEW → ACTIVE`; status check `ACTIVE` | Delete accepted `204`; final `404` |
| DO-LIVE-NEW-05 | Kubernetes options and cluster | PASS — options exposed `nyc3`, `1.36.3-do.0`, `s-1vcpu-2gb`; cluster `provisioning → running` | Delete accepted `204`; final `404` |
| DO-LIVE-NEW-06 | Kubernetes node pool | PASS — live list response used `node_pools`; node status `running` | Removed with cluster |
| DO-LIVE-NEW-07 | Domain and DNS record | PASS — valid disposable `.com` zone created; A record was `present`, updated from `203.0.113.10` to `203.0.113.11` | Record and domain both returned `404` |
| DO-LIVE-NEW-08 | Let's Encrypt certificate | EXPECTED BLOCK — provider rejected the undelegated test zone with `422` (`no NS records found`) | No certificate created |
| DO-LIVE-NEW-09 | Custom certificate | PASS — ephemeral self-signed custom certificate created `201`, reported `verified`, status check returned `verified` | Delete `204`; final `404` |
| DO-LIVE-NEW-10 | Spaces credential and bucket | BLOCKED — CSV access-key ID returned `InvalidAccessKeyId` when paired with the newly supplied secret; no bucket was created or modified | None required |
| DO-LIVE-NEW-11 | CDN origin `bakrameter` | SAFE NO-OP — DigitalOcean returned `409 cdn already enabled`; an existing endpoint was not touched and no duplicate was created | None required |
| DO-LIVE-NEW-12 | CloudMoo live inventory | PASS — rebuilt adapter imported live Kubernetes, node-pool, VPC, peering, NAT, domain, DNS, CDN, and certificate collections; nested node-pool/DNS context was preserved | Temporary local rows removed |
| DO-LIVE-NEW-13 | Empty NAT inventory | PASS — live provider returned `vpc_nat_gateways: null` with `meta.total: 0`; adapter now treats that exact shape as empty | Read-only |

## Resource audit

### Run 1 — defect discovery

Prefix: `cloudmoo-live-do-20260802-gg2wifgh`

- VPCs: `a14d3680-d706-452b-9d69-2231cfeb2ed6`,
  `04c2b982-7032-4f3a-9e7c-03794a926e8b`.
- VPC peering: `5a7f6196-1ff2-427b-a69e-e5012f47db48`.
- NAT gateway: `9c05504c-d3ea-4645-8872-26d1ec42744d`.
- Kubernetes cluster: `12e434c9-9986-4d99-b25f-176199497093`.
- Kubernetes node pool: `0f591137-1a95-4cfa-a000-c007afa71e4e`.

This run found two live adapter mismatches: VPC `region` is a string in the
live response, and the nested DOKS node-pool list omits the normal pagination
metadata. All created resources were still deleted successfully.

### Run 2 — corrected adapter and full networking/DNS pass

Prefix: `cloudmoo-live-do-20260802-2cw5h8x3`

- VPCs: `15637c68-c286-482e-92d1-8d43d89af088`,
  `862f2e07-74a3-4f02-a22c-54caff4daab3`.
- VPC peering: `5a3909ee-d16c-48ff-b435-ae6965920ba0`.
- NAT gateway: `8008b751-cb66-4096-909a-8558251cdc1e`.
- Kubernetes cluster: `a3ad2610-8352-4f63-8ce0-67a5ee7bd19c`.
- Disposable domain: `cloudmoo-live-do-20260802-2cw5h8x3.com`.
- DNS record: `1827734586`.

All resources in this run were created in the Personal team, exercised, and
deleted. The final provider inventory contained 10 pre-existing VPCs, one
pre-existing CDN endpoint, and no resources matching either test prefix.

### Certificate follow-up

The custom certificate test created ID
`1a2e1c75-fa68-4eb8-813a-18cb50e44786`, verified its `verified` state through
CloudMoo's status checker, and deleted it. It is absent from the final
certificate inventory.

## Fixes made from live evidence

1. VPC status checks now accept both provider representations of `region`:
   the live string slug and the dictionary shape used by older fixtures.
2. DOKS node-pool inventory explicitly allows the documented unpaginated
   nested collection while retaining fail-closed behavior for ordinary
   paginated collections.
3. NAT gateway inventory accepts a `null` collection only when DigitalOcean
   also reports `meta.total == 0`; malformed non-empty responses still fail
   closed.
4. Regression coverage now includes the live VPC shape, unpaginated node-pool
   response, and empty NAT response.

## Verification

- Full Django suite against the rebuilt image: **118 passed, 7 skipped**.
- Focused connection/resource suite against the rebuilt image: **24 passed**.
- Live CloudMoo adapter inventory against the rebuilt image: **passed**.
- Django system checks: **passed**.
- Migration drift check: **passed** (`No changes detected`).
- `git diff --check`: **passed**.
- Live zero-NAT inventory against the rebuilt image: **passed**.
- Final read-only provider audit: no matching test prefixes in VPCs, peerings,
  NAT gateways, Kubernetes clusters, domains, certificates, or CDN origins.

## Limitations and next action

- Spaces object-storage testing is not complete because the available access
  key ID is invalid. A valid active access-key ID paired with the supplied
  secret is required before creating a test bucket. The existing `bakrameter`
  bucket/endpoint was not modified.
- Let's Encrypt issuance requires a domain delegated to DigitalOcean's name
  servers. The disposable test zone was intentionally not delegated; the
  rejection was recorded rather than changing registrar DNS.
