# Vultr live E2E test

## Latest run

- Date: 2026-08-04 (UTC)
- Result: **PASS**
- Run marker: `cloudmoo-e2e-20260804T095149Z-8914a4e7d4082b9c`
- Region: `ams`
- Plan: `vc2-1c-1gb` (`$5/month` at test time)
- OS: Ubuntu 22.04 LTS x64 (`1743`)
- Resources created: one firewall group, one firewall rule, and one shared-CPU instance
- Resources remaining after cleanup: zero of the three owned resources

The API token was supplied only through `VULTR_API_KEY` at process launch. It
was not stored in the repository, the test manifest, CloudMoo's database, or
the test report. The temporary 0600 manifest recorded the exact returned IDs,
baseline IDs, lifecycle events, and cleanup state without raw provider
responses.

## Test cases

| ID | Test case | Result |
| --- | --- | --- |
| V-E2E-01 | Authenticate with `GET /account`; validate credentials through `CoreVultrAccount.validate()` without saving an account model. | Pass |
| V-E2E-02 | Read account limits capability. The account returned HTTP 404 for the optional `/account/limits` endpoint; the runner recorded this as an unsupported optional capability and continued. | Pass / capability recorded |
| V-E2E-03 | Discover active regions, current plans, region availability, and OS images. Select the lowest-priced active `vc2` plan available in the selected region under the `$10/month` ceiling. | Pass |
| V-E2E-04 | Create a uniquely marked firewall group and verify its returned ID, marker, and detail response. | Pass (`201`) |
| V-E2E-05 | Create a uniquely marked TCP/22 firewall rule using the required `subnet`/`subnet_size` fields; verify the exact parent and rule ID. | Pass (`201`) |
| V-E2E-06 | Create a uniquely marked instance; poll the exact ID until it reaches `running`. | Pass (`202`, then `running`) |
| V-E2E-07 | Run CloudMoo's read-only Vultr account validation, instance detail/status check, and firewall/firewall-rule inventory collector against the live resources. | Pass |
| V-E2E-08 | Halt the exact instance ID and poll until `stopped`; rerun CloudMoo monitoring assertions. | Pass (`204`, then `stopped`) |
| V-E2E-09 | Start the exact instance ID and poll until `running`; rerun CloudMoo monitoring assertions. | Pass (`204`, then `running`) |
| V-E2E-10 | Stop if necessary, delete only the ledgered rule, instance, and group in reverse dependency order, and verify each exact ID returns HTTP 404. | Pass (all `204`, then `404`) |
| V-E2E-11 | Re-list instances and firewall groups and verify no ownership marker or owned ID remains. | Pass (zero owned resources) |

## Safety and cleanup evidence

The production Vultr adapter remains GET-only. The live runner uses a separate
raw HTTP client only for the explicitly requested lifecycle operations. Every
create response is immediately written to the manifest; ambiguous mutations
are resolved by the exact ownership marker and baseline before any retry.
Deletion revalidates the exact ID, parent relationship, and marker. An
instance that is still running or control-plane locked is stopped and polled
before deletion; no broad or prefix-based delete is used.

During an intermediate run, an unrelated instance with a `bs-vultr-e2e-*`
marker appeared/disappeared outside this test. The baseline/drift guard never
deleted or changed it. The final run completed with no owned resources left.

## Provider/API compatibility fixes found live

1. Vultr's current firewall-rule response envelope is `firewall_rules`, while
   older fixtures used `rules`. CloudMoo now accepts this explicit alias while
   still failing closed for omitted or malformed collections.
2. Current Vultr firewall-rule creation requires `subnet` and `subnet_size`;
   the runner now sends those fields and leaves `source` empty so Vultr derives
   the CIDR from the subnet/netmask pair.
3. Vultr can return HTTP 409 when an instance is stopped but its control-plane
   lock is settling. Cleanup now waits on the exact instance ID and retries
   only that exact deletion after the lock clears.

## Re-running safely

From the repository root, use a secret environment variable and let the runner
select current capacity dynamically:

```sh
VULTR_API_KEY="$VULTR_API_KEY" .venv/bin/python tests/live_vultr_e2e.py
```

If a process is interrupted, use the manifest path printed by the runner to
resume exact-ID cleanup:

```sh
VULTR_API_KEY="$VULTR_API_KEY" .venv/bin/python tests/live_vultr_e2e.py \
  --cleanup-ledger /path/to/cloudmoo-vultr-e2e-<run>.json
```

The lifecycle request shapes follow Vultr's [Cloud Compute provisioning guide](https://docs.vultr.com/products/compute/instances/cloud-compute/provisioning),
[stop guide](https://docs.vultr.com/products/compute/instances/cloud-compute/management/stop-instance),
[firewall-rule guide](https://docs.vultr.com/products/network/firewall-groups/management/rules),
and [firewall deletion guide](https://docs.vultr.com/products/network/firewall-groups/management/delete).
