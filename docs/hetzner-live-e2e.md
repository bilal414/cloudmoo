# Hetzner Cloud live E2E test

## Final run

- Date: 2026-08-04 (UTC)
- Result: **PASS**
- Run marker: `cloudmoo-hetzner-e2e-20260804T142036Z-2901604e2a904062`
- Location: `fsn1`
- Server type: `cx23` (`$6.49/month` gross at test time)
- Image: Ubuntu 24.04 x86
- Public networking: IPv6 enabled, IPv4 disabled
- Resources created: one Server
- Resources remaining after cleanup: zero owned Servers
- Independent post-run Server list: zero Servers

The token was read from the local credential file only into the
`HETZNER_API_TOKEN` process environment. It was not written to the repository,
ledger, CloudMoo database, or output. The ledger was mode `0600` and contained
only IDs, labels, actions, statuses, fingerprints, and sanitized events.

## Test cases

| ID | Test case | Result |
| --- | --- | --- |
| HZ-E2E-01 | Read-only baseline of the Project's Servers; record exact IDs, count, and fingerprint; reject marker collisions. | Pass; baseline was empty |
| HZ-E2E-02 | Discover Locations, Server Types/prices, and current x86 Ubuntu system images; enforce a `$10/month` ceiling. | Pass |
| HZ-E2E-03 | Create one uniquely named/labeled Server with no SSH key, no user data, no Volume, no Network, no Firewall, IPv6-only public networking, and `start_after_create=false`. | Pass |
| HZ-E2E-04 | Poll the returned create Action by exact top-level Action ID and verify the exact Server settles at `off`. | Pass |
| HZ-E2E-05 | Run CloudMoo account validation, legacy Server inventory, exact detail status, check dispatch, and expanded Location inventory against the owned Server/selected Location. | Pass |
| HZ-E2E-06 | Power on the exact Server, poll the exact Action, verify provider `running` and CloudMoo `available`. | Pass |
| HZ-E2E-07 | Query the exact Server CPU metrics window; CloudMoo returned `unknown` because the new Server had no points yet, which is a valid bounded metrics result. | Pass / no-data state |
| HZ-E2E-08 | Power off the exact Server, verify provider `off` and CloudMoo `stopped`, then power it on again and verify `available`. | Pass |
| HZ-E2E-09 | Delete only the ledgered Server ID, poll the delete Action when returned, and verify the exact ID is absent. | Pass |
| HZ-E2E-10 | Re-list Servers; verify no owned ID/marker remains, the baseline is preserved, and no raw response or secret entered the ledger. | Pass |

CloudMoo assertion phases recorded by the final ledger were:
`stopped → available (metrics: unknown) → stopped → available`.

## Compatibility findings

Three initial create attempts were rejected with HTTP 422 before Hetzner
created any resource:

1. The current `POST /servers` schema requires the `server_type`, `image`, and
   `location` request fields as strings. The runner initially sent catalog IDs
   and was corrected to send the selected names while retaining IDs for
   verification.
2. Hetzner rejected the cheapest `cpx11` catalog entry as unsupported in
   `fsn1` despite the catalog advertising that location. The runner now
   prefers the current `cx23` type when it is available under the explicit
   budget, then uses bounded catalog fallbacks.
3. The final request used Ubuntu 24.04, IPv6-only networking, and the
   deterministic initial-off lifecycle and passed.

No resource was created by the rejected attempts; each cleanup pass verified
that the Project still had zero Servers.

## Safety boundary

CloudMoo’s normal Hetzner validation, inventory, and monitoring paths remain
GET-only. The live runner uses a separate mutation client only for the one
explicitly marked Server and its exact power/action/delete endpoints. It does
not enumerate arbitrary Actions, use existing SSH keys, attach existing
resources, create Volumes/Networks/Firewalls/IPs, touch DNS or certificates,
or run `sync_assets()` against the local database.

Ambiguous mutations are resolved only through the exact ledgered ID and
ownership marker. Cleanup refuses to broaden a target set, and final
verification requires all baseline IDs to remain present.

## Re-running safely

From the repository root:

```sh
HETZNER_API_TOKEN="$HETZNER_API_TOKEN" .venv/bin/python tests/live_hetzner_e2e.py \
  --max-monthly-cost 10
```

If interrupted, use the manifest path printed by the runner to resume cleanup:

```sh
HETZNER_API_TOKEN="$HETZNER_API_TOKEN" .venv/bin/python tests/live_hetzner_e2e.py \
  --max-monthly-cost 10 \
  --cleanup-ledger /path/to/cloudmoo-hetzner-e2e-<run>.json
```

The request and action lifecycle follow Hetzner’s [Cloud API
reference](https://docs.hetzner.cloud/reference/cloud), [machine-readable API
specification](https://docs.hetzner.cloud/cloud.spec.json), and [API
changelog](https://docs.hetzner.cloud/changelog).
