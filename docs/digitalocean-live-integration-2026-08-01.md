# DigitalOcean live integration test report

Date: 2026-08-01 (API timestamps are UTC)

## Scope and safety controls

- Credential: the supplied local token file was read without printing or storing the token. It was not committed.
- API team guard: the token resolved to the DigitalOcean team `Personal` (`0ba41777-3fbc-4093-9193-0f2709d2948a`).
- Project guard: the default project was `Personal` (`9a10e395-1a6a-470c-adaf-8897ff5576ae`).
- Existing resources were listed and fingerprinted before mutation. The test harness mutated and deleted only IDs returned by its own create calls.
- Status-change notification enqueueing was mocked during live checks; no test email was sent.
- Cleanup was executed in a `finally` path and verified by a final resource inventory.

The API behavior used here follows DigitalOcean's [API reference](https://docs.digitalocean.com/reference/api/reference/), [account endpoint](https://docs.digitalocean.com/reference/api/reference/account/), and [Droplet actions](https://docs.digitalocean.com/products/droplets/reference/api/droplet-actions/).

## Test matrix — successful final run

Final run prefix: `cloudmoo-live-do-20260801-210724-7ebf67`

Created resources:

- Droplet `589280440`, `nyc3`, `s-1vcpu-512mb-10gb`, Ubuntu `ubuntu-24-04-x64`.
- Volume `0f90aa00-8ded-11f1-8ea0-8e56643b6766`, 1 GB, `nyc3`.

| ID | Test case | Expected result | Observed result |
|---|---|---|---|
| DO-LIVE-01 | Validate team/project scope and baseline inventory | Mutate only Personal; record existing resources | PASS — Personal team/project verified; baseline was recorded |
| DO-LIVE-02 | Discover available image, region, and smallest plan | Required capabilities are available | PASS — Ubuntu 24.04, `nyc3`, `$0.00595/hour` plan |
| DO-LIVE-03 | Create a Droplet | New Droplet becomes `active` | PASS — Droplet `589280440` became active |
| DO-LIVE-04 | Create a Volume | New 1-GB volume is available | PASS — Volume `0f90aa00-8ded-11f1-8ea0-8e56643b6766` created |
| DO-LIVE-05 | Attach Volume | Volume reports the test Droplet ID | PASS — `droplet_ids=[589280440]` |
| DO-LIVE-06 | CloudMoo credential validation and inventory sync | Cloud validates and imports the test assets | PASS — validation true, sync successful, local server/volume rows created |
| DO-LIVE-07 | Check active status | Provider adapter and CloudMoo state report healthy status | PASS — Droplet `active`; Volume `attached`; heartbeat state active |
| DO-LIVE-08 | Power off Droplet | Provider and CloudMoo report `off`; status transition is logged | PASS — direct status `off`, snapshot `off`, log sequence `active -> off`, notification enqueue intercepted |
| DO-LIVE-09 | Power on Droplet | Provider and CloudMoo report recovery to `active` | PASS — direct status `active`, snapshot `active`, log sequence `active -> off -> active` |
| DO-LIVE-10 | Detach Volume | Provider and CloudMoo report `detached` | PASS — direct status `detached`, snapshot `detached`, notification enqueue intercepted |
| DO-LIVE-11 | Delete Volume and resync | CloudMoo marks the local asset `no_longer_exists` | PASS — volume marked `no_longer_exists`; Droplet remained active |
| DO-LIVE-12 | Delete Droplet and resync | CloudMoo marks the local asset `no_longer_exists` | PASS — Droplet marked `no_longer_exists`; cloud remained active |
| DO-LIVE-13 | Local cleanup | Temporary user/account/token-backed model rows are removed | PASS — temporary user count returned to zero |
| DO-LIVE-14 | External cleanup verification | Created IDs absent after cleanup | PASS — final Personal inventory had no test Droplet or test Volume |

## Temporary resource audit

All resources below were created by this test effort and were deleted by the harness. The failed attempts are retained here for traceability.

| Droplet ID | Volume ID | Outcome | Cleanup |
|---:|---|---|---|
| `589278006` | none | Volume create rejected because the initial payload used `size` | Droplet deleted by emergency cleanup |
| `589278380` | `31cc28de-8deb-11f1-b168-0685d84720d7` | Exposed valid one-page pagination with `links: {}` | Both deleted by emergency cleanup |
| `589278737` | `ba24cb06-8deb-11f1-b168-0685d84720d7` | Used an image that did not yet contain the paginator fix | Both deleted by emergency cleanup |
| `589279576` | `392c9649-8dec-11f1-b168-0685d84720d7` | Temporary fixture used an invalid `CoreMember` import | Both deleted by emergency cleanup |
| `589280089` | `b54fc8a0-8dec-11f1-8ea0-8e56643b6766` | Temporary fixture still used the invalid import | Both deleted by emergency cleanup |
| `589280440` | `0f90aa00-8ded-11f1-8ea0-8e56643b6766` | Final run passed all lifecycle checks | Both deleted normally; final inventory confirmed absent |

## Existing-resource integrity observation

At the start of the guarded runs, the Personal team contained resources from a separate BackupSheep E2E run:

- Droplet `589276938`, `backupsheep-e2e-20260801-server`.
- Volume `3c6f4936-8de9-11f1-b2b3-8a6f1f710ee0`, `backupsheep-e2e-20260801-volume`, attached to that Droplet.

The CloudMoo harness never sent a mutating request for either baseline ID. During the final run, the baseline Droplet disappeared independently; a read-only action-history audit found DigitalOcean destroy action `3324569641` for Droplet `589276938`, started at `21:13:03Z` and completed at `21:13:07Z`, plus earlier snapshot actions. The final inventory therefore showed zero Droplets and zero Volumes. This is recorded as an external concurrent change, not a pass for baseline preservation; the resources were not restored or modified by this test.

## Defect found and fixed

DigitalOcean returned valid one-page collections shaped as:

```json
{"droplets": [...], "links": {}, "meta": {"total": 1}}
```

CloudMoo previously required `links.pages` to be a dictionary and incorrectly raised `CloudInventoryTransientError`. The paginator now treats absent pagination links as terminal only when `meta.total` exactly equals the number of collected items; an incomplete or malformed response still fails closed. Regression coverage was added for both the valid terminal response and the incomplete response.

## Regression verification

- Focused DigitalOcean tests against the rebuilt image: 9 passed.
- Full Django suite after the fix: 103 passed, 7 skipped.
- `makemigrations --check --dry-run`: no changes detected.
- Rebuilt Docker application image and recreated web/worker/beat services so the live test executed the patched code.
- Web readiness after rebuild: database check healthy.

## Conclusion

CloudMoo successfully validated, inventoried, monitored, transitioned, and cleaned up resources created by this test in the Personal team. The live run also found and fixed a production-relevant DigitalOcean pagination compatibility defect. The unrelated BackupSheep baseline was not mutated by this test, but it was concurrently destroyed by another process and should be investigated separately if it was expected to remain.
