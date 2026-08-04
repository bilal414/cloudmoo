# Vultr managed-database live E2E test

## Final run

- Date: 2026-08-04 (UTC)
- Result: **PASS**
- Run marker: `cloudmoo-db-e2e-20260804T123346Z-0bf98d8e4c85083b`
- Region: `ams`
- Engine/version: PostgreSQL 16 (`pg`)
- Plan: `vultr-dbaas-hobbyist-cc-1-25-1` (`$15/month` at test time)
- Resources created by the test: one managed database per attempted run
- Resources remaining after cleanup: zero owned databases
- Independent post-run list: zero managed databases

The API token was supplied only through `VULTR_API_KEY` at process launch. It
was not written to the repository, the temporary 0600 ledger, CloudMoo, or
the test report. The ledger stores only the marker, baseline IDs, selected
plan, exact returned ID, lifecycle events, and cleanup state.

## Test cases

| ID | Test case | Result |
| --- | --- | --- |
| DB-E2E-01 | Authenticate with `GET /account` and validate the token through `CoreVultrAccount.validate()` without saving a CloudMoo account. | Pass |
| DB-E2E-02 | Probe optional `GET /account/limits`; record an explicit 404 capability result without treating it as a failure. | Pass / capability recorded |
| DB-E2E-03 | List the existing managed databases, record the baseline fingerprint, and reject marker collisions or baseline-ID reuse. | Pass; baseline was empty |
| DB-E2E-04 | Discover active regions and region-specific managed-database plans; select the cheapest eligible single-node PostgreSQL plan under the explicit `$20/month` ceiling. | Pass; selected `$15/month` plan |
| DB-E2E-05 | Create one uniquely labelled PostgreSQL 16 database using only engine, version, plan, region, and label. Record the exact returned ID before further reads. | Pass |
| DB-E2E-06 | Poll only the owned database ID until Vultr reports `running`; require matching ID, marker, region, plan, engine/version, host, port, user, and default database fields. | Pass |
| DB-E2E-07 | Exercise CloudMoo’s read-only account validation and exact managed-database detail path. | Pass |
| DB-E2E-08 | Exercise CloudMoo normalized database inventory and verify exactly one owned record with a non-empty name and matching marker metadata. | Pass |
| DB-E2E-09 | Exercise `check_vultr_database_status()` and verify the normalized status is healthy/available and monitoring metadata does not expose the API token or database credentials. | Pass |
| DB-E2E-10 | Delete only the ledgered database ID, then verify the ID is absent from the collection. A transient provider `422` after the successful `204` delete is resolved by exact collection verification, never by deleting another resource. | Pass |
| DB-E2E-11 | Re-list managed databases and verify no owned ID or marker remains, all baseline IDs remain, the ledger is `0600`, and all cleanup events are recorded without raw responses. | Pass |

## Earlier attempts and fixes

Three independent disposable live attempts were made. Every attempt created a
fresh marker and completed cleanup:

1. The first attempt reached `running`, then found that Vultr’s current
   database collection returned `meta.total` without `meta.links`. CloudMoo’s
   strict paginator rejected the valid terminal response. Cleanup still
   succeeded after the provider temporarily returned `422` for the deleting
   database detail endpoint.
2. The second attempt reached `running` and exposed a redaction gap for
   certificate/connection-shaped fields in the managed-database payload. It
   was cleaned up successfully.
3. The final attempt passed all CloudMoo assertions and cleanup.

The fixes are intentionally fail-closed:

- The Vultr collection readers accept a links-free page only when an integer
  `meta.total` is present and the validated item count covers that total. A
  page that could be incomplete still raises a transient inventory error.
- Managed-database normalization drops certificate, connection, credential,
  secret, and token field variants before metadata reaches CloudMoo checks or
  persistence.
- Cleanup treats a post-delete `422` as deletion-in-progress only after a
  second exact-ID collection read proves that the ID is absent. It never
  broadens a delete by label, prefix, or account sweep.

## Safety boundary

CloudMoo’s production Vultr client remains GET-only. The live runner uses a
separate short-lived mutation client only for the explicitly requested
managed-database create and delete operations. It does not create or change
VPCs, replicas, logical databases, users, connection pools, trusted sources,
backups, or any unrelated resource. `CoreVultrAccount.sync_assets()` is not
called because it would sweep the whole account; the test calls the targeted
database collector and status checker instead.

## Re-running safely

From the repository root:

```sh
VULTR_API_KEY="$VULTR_API_KEY" .venv/bin/python tests/live_vultr_database_e2e.py \
  --max-monthly-cost 20
```

If a process is interrupted, use the manifest path printed by the runner to
resume exact-ID cleanup:

```sh
VULTR_API_KEY="$VULTR_API_KEY" .venv/bin/python tests/live_vultr_database_e2e.py \
  --max-monthly-cost 20 \
  --cleanup-ledger /path/to/cloudmoo-vultr-db-e2e-<run>.json
```

The request and lifecycle shapes follow Vultr’s [PostgreSQL provisioning
guide](https://docs.vultr.com/products/storage/databases/postgresql/provisioning),
[managed-database connection-details guide](https://docs.vultr.com/products/storage/databases/postgresql/management/connection/connection-details),
and [managed-database deletion reference](https://docs.vultr.com/reference/vultr-cli/database/delete).
