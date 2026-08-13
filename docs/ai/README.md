# CloudMoo AI Integration Plan

Status: proposed architecture and implementation runbook. No AI integration described here exists unless it is explicitly listed under **Current reality**.

Audience: implementation agents, reviewers, and operators working across the Django backend and the CloudMoo iOS application.

Companion mobile plan: [`../../../ios_cloudmoo_com/docs/ai/README.md`](../../../ios_cloudmoo_com/docs/ai/README.md).

## 1. Outcome

CloudMoo should add AI as an evidence-backed interpretation layer over its existing monitoring system. The first feature should be an incident investigator that explains a deterministic status or configuration change. It should distinguish facts from hypotheses, cite the exact evidence used, recommend safe diagnostic checks, and abstain when the evidence is missing, stale, or contradictory.

The target flow is:

```text
Provider APIs
    |
    v
existing deterministic sync and status checks
    |
    v
PostgreSQL monitoring evidence
    |
    v
deterministic findings + sanitized EvidenceSnapshot
    |
    v
dedicated AI queue and provider adapter
    |
    v
schema-validated AIInsight with evidence references
    |
    +--> Django console
    +--> account-scoped mobile API --> iOS
```

The invariant is:

```text
durable evidence -> deterministic findings -> AI explanation or typed draft
```

AI is never part of monitoring, alerting, authorization, or provider-control correctness.

## 2. Current reality

This section describes verified code, not the proposed design.

### 2.1 Runtime and monitoring engine

- `app_cloudmoo_com/settings.py` configures Django, Django REST Framework, PostgreSQL, Celery, email, and optional Sentry.
- `app_cloudmoo_com/celery.py` imports `apps.monitoring.tasks` explicitly.
- `docker-compose.yml` runs PostgreSQL, RabbitMQ, a migration job, web, a Celery worker, and one Celery Beat process. Beat must remain a singleton because multiple Beat instances would publish duplicate scheduled checks.
- `apps/monitoring/schedules.py` stores one idempotently upserted `django-celery-beat` `PeriodicTask` per monitored asset and one per cloud.
- `apps/monitoring/tasks.py` runs provider checks, records monitoring heartbeats and transitions, synchronizes inventory, publishes notifications, recovers notification delivery, and prunes old logs.
- Status checks use generation fencing. A late provider response cannot overwrite a newer observation.
- Celery tasks use bounded time limits, late acknowledgement, worker-loss rejection, and selected retry policies.

### 2.2 Durable evidence

`apps/monitoring/models.py` provides:

- `AssetStatusLog`: status-change history, timestamp, sanitized metadata, metadata changes, and bounded error text.
- `AssetMonitoringState`: last check and success, current status/error, consecutive failures, check generation, last change, and current sanitized metadata.
- `AssetStatusEmail`: a durable notification outbox/delivery ledger with a stable delivery key, attempts, status, and timestamps.

`apps/monitoring/metadata.py` provides:

- recursive redaction of credential-like fields;
- bounded and redacted provider errors;
- provider/type-specific metadata allowlists;
- deterministic, human-readable metadata differences.

`apps/monitoring/checks/` contains explicit, bounded provider status checks. `apps/monitoring/checks/base.py` separates provider/check failures such as `error`, `invalid_access_token`, and `unsupported` from actual resource lifecycle states so they do not create false uptime incidents.

### 2.3 Inventory and tenancy

- `CoreAccount` is the tenant container. It owns clouds and has membership roles `MEMBER`, `ADMIN`, and `OWNER` in `apps/console/account/models.py`.
- `CoreMember.active_account` is mutable persisted UI state. Existing console query managers scope many objects through that field.
- `CoreCloud` in `apps/console/cloud/models.py` has a UUID, account, provider, status, and sync timestamp. Its centralized asset relation registry covers a broad multi-provider inventory.
- `UtilAsset` in `apps/console/utils/models.py` has a UUID, provider identifier, name, type, metadata, notes, monitoring state, and notification recipients.
- Provider credentials are persisted on provider-account models. They are required by deterministic adapters, but they must never be serialized to an AI snapshot or returned to iOS.

`active_account` is not an acceptable API authorization boundary. It is mutable shared state, so changing it on one device could affect another request. Every new endpoint must carry an explicit account identifier and verify membership on every request.

### 2.4 Existing user surfaces

- `apps/console/home/` renders account/cloud/asset totals.
- `apps/console/cloud/` renders cloud inventory, monitoring coverage, sync freshness, and provider validation.
- `apps/console/asset/` renders searchable assets, current state, a paginated status timeline, CSV export, monitoring controls, notification recipients, and a synchronous check-now control.
- `apps/console/notifications/` renders status-notification history from `AssetStatusEmail`.
- AWS adapters already persist bounded cost/forecast, cost anomaly, security/governance, backup, CloudWatch, and other read-only signals.

### 2.5 Current API limitation

DRF is installed and configured for session and token authentication, but `apps/api/v1/` currently exposes only the external cloud-sync webhook:

```text
POST /api/v1/webhook/cloud/sync_assets/
X-API-KEY: <one instance-wide shared secret>
```

That webhook deliberately has no user/account context. It is not a first-party mobile API and must not be reused for AI or iOS. There are no account-scoped overview, asset, timeline, notification, or AI endpoints today.

### 2.6 Current test posture

`tests/` contains extensive mocked provider and monitoring tests, including status generation fencing, stale monitoring state, metadata redaction, idempotent notification delivery, retention, schedules, and webhook authentication. Live-provider scripts and integration tests are separate and may require credentials.

The prior Docker-based architecture verification passed the then-current test suite, but that historical result is not proof that the current worktree passes. Agents must run current verification after implementation.

## 3. Proposed plan

Everything below is proposed.

## 4. Invariants and no-go boundaries

AI must not:

1. Decide asset health, cloud credential validity, resource existence, monitoring freshness, compliance, or recoverability.
2. Run provider inventory reconciliation or delete/mark resources as missing.
3. create, modify, disable, or delete monitoring schedules.
4. Suppress, delay, rewrite, deduplicate, or route the authoritative deterministic alert.
5. Calculate authoritative cost totals, percentages, security scores, or uptime.
6. Receive provider credentials, API tokens, authorization headers, email recipients, session data, TOTP material, or password/reset data.
7. Call a cloud provider API directly.
8. Select tenant scope or authorize a user.
9. Generate or execute raw SQL.
10. Mutate a cloud, asset, monitoring setting, recipient list, or provider resource.
11. Treat provider text, names, descriptions, tags, or logs as trusted instructions.
12. claim a root cause, secure/compliant state, or recoverable backup solely from model inference.

An AI outage must have no effect on cloud sync, status checks, monitoring history, deterministic findings, or notification delivery.

## 5. Prerequisites

Complete these before the first AI feature is exposed:

1. Add a stable public opaque ID to `CoreAccount`. UUID is appropriate internally, but API contracts call it `account_id` and clients must treat it as an opaque string.
2. Build an account-scoped first-party API and membership permission class. Do not use `active_account` for tenant authorization.
3. Define role-to-scope policy for first-party and AI endpoints.
4. Add first-party mobile authentication with revocation and rotation. Do not use the instance-wide webhook secret. Decide between short-lived JWT access tokens plus rotating refresh tokens and opaque server-side device tokens.
5. Fix or explicitly guard any console view that lacks normal login enforcement before copying its access pattern into DRF.
6. Add a deterministic evidence builder that never serializes provider-account models.
7. Add an AI-specific egress allowlist and redaction test corpus.
8. Decide supported provider modes and whether external AI egress is opt-in per instance, per account, or both.
9. Add a dedicated AI Celery queue and worker so model latency cannot consume monitoring-worker capacity.

## 6. Canonical API contract

All endpoint paths use trailing slashes. All identifiers are opaque strings. Every account-scoped endpoint verifies that the authenticated member belongs to the path account. Responses must never infer the account from `CoreMember.active_account`.

### 6.1 First-party foundation

```text
GET   /api/v1/me/
GET   /api/v1/accounts/
GET   /api/v1/accounts/{account_id}/overview/
GET   /api/v1/accounts/{account_id}/clouds/
GET   /api/v1/accounts/{account_id}/assets/
GET   /api/v1/accounts/{account_id}/assets/{asset_id}/
GET   /api/v1/accounts/{account_id}/assets/{asset_id}/timeline/
GET   /api/v1/accounts/{account_id}/activity/
GET   /api/v1/accounts/{account_id}/notifications/
POST  /api/v1/accounts/{account_id}/clouds/{cloud_id}/sync/
POST  /api/v1/accounts/{account_id}/assets/{asset_id}/check/
PATCH /api/v1/accounts/{account_id}/assets/{asset_id}/monitoring/
```

The `sync`, `check`, and `monitoring` endpoints remain deterministic. They are not AI endpoints.

### 6.2 AI capabilities

```text
GET /api/v1/accounts/{account_id}/ai/capabilities/
```

Example response:

```json
{
  "data": {
    "provider_mode": "external_byok",
    "features": {
      "incident_investigator": {
        "enabled": true,
        "minimum_role": "member"
      },
      "daily_brief": {
        "enabled": false,
        "reason": "not_released"
      },
      "resource_query": {
        "enabled": false,
        "reason": "not_released"
      },
      "action_draft": {
        "enabled": false,
        "reason": "not_released"
      }
    },
    "data_egress": "sanitized_evidence_only"
  }
}
```

### 6.3 Create an insight

```text
POST /api/v1/accounts/{account_id}/ai/insights/
Idempotency-Key: <client-generated opaque key>
```

The header is authoritative. `idempotency_key` in the JSON body may be accepted temporarily for client compatibility, but if both are supplied they must match.

Example request:

```json
{
  "feature": "incident_investigator",
  "subject": {
    "type": "asset",
    "id": "ast_01J..."
  },
  "idempotency_key": "ios-01J..."
}
```

The client must not submit evidence, raw provider metadata, logs, prompt text, account scope, or model settings. The server resolves the subject inside the path account and builds evidence itself.

Example `202 Accepted` response:

```json
{
  "data": {
    "id": "ins_01J...",
    "feature": "incident_investigator",
    "lifecycle": "queued",
    "freshness": "fresh",
    "subject": {
      "type": "asset",
      "id": "ast_01J...",
      "name": "api-production"
    },
    "created_at": "2026-08-12T18:45:00Z",
    "updated_at": "2026-08-12T18:45:00Z",
    "expires_at": null,
    "poll_after_seconds": 2
  }
}
```

The only public lifecycle values are:

```text
queued | running | ready | abstained | failed | expired
```

Freshness is independent:

```text
fresh | stale
```

Do not expose a Celery task ID or separate public job ID. The insight ID is the polling resource.

### 6.4 Read an insight

```text
GET /api/v1/accounts/{account_id}/ai/insights/{insight_id}/
```

Example ready response:

```json
{
  "data": {
    "id": "ins_01J...",
    "feature": "incident_investigator",
    "lifecycle": "ready",
    "freshness": "fresh",
    "subject": {
      "type": "asset",
      "id": "ast_01J...",
      "name": "api-production"
    },
    "result": {
      "summary": "The instance changed from running to stopped after its latest successful check.",
      "known_facts": [
        {
          "text": "The latest deterministic status is stopped.",
          "evidence_refs": ["ev_state_latest"]
        }
      ],
      "hypotheses": [
        {
          "text": "A deployment or operator action may have stopped the instance.",
          "confidence": "low",
          "evidence_refs": ["ev_transition_01"]
        }
      ],
      "recommended_checks": [
        {
          "text": "Review the provider activity log for the transition window.",
          "kind": "read_only"
        }
      ],
      "limitations": [
        "CloudMoo does not currently ingest the provider activity log for this asset."
      ]
    },
    "evidence": [
      {
        "ref": "ev_state_latest",
        "kind": "monitoring_state",
        "label": "Latest monitoring state",
        "observed_at": "2026-08-12T18:44:31Z"
      },
      {
        "ref": "ev_transition_01",
        "kind": "status_transition",
        "label": "running to stopped",
        "observed_at": "2026-08-12T18:44:31Z"
      }
    ],
    "source_observed_at": "2026-08-12T18:44:31Z",
    "generated_at": "2026-08-12T18:45:04Z",
    "expires_at": "2026-08-13T18:45:04Z"
  }
}
```

Example abstention response:

```json
{
  "data": {
    "id": "ins_01J...",
    "feature": "incident_investigator",
    "lifecycle": "abstained",
    "freshness": "stale",
    "result": null,
    "abstention": {
      "code": "evidence_stale",
      "message": "CloudMoo has not checked this asset recently enough to produce a reliable explanation."
    }
  }
}
```

Stable public failure categories should be safe and non-provider-specific, for example `temporarily_unavailable`, `budget_exceeded`, `invalid_output`, and `feature_disabled`. Do not return model/provider exception text.

### 6.5 Feedback

```text
POST /api/v1/accounts/{account_id}/ai/insights/{insight_id}/feedback/
```

Example request:

```json
{
  "rating": "helpful",
  "reason_codes": ["clear", "evidence_useful"],
  "comment": "The suggested provider activity check was the right next step."
}
```

Feedback is immutable or append-only. It must not modify the stored model output.

### 6.6 Later endpoints

```text
GET  /api/v1/accounts/{account_id}/ai/briefs/latest/
POST /api/v1/accounts/{account_id}/ai/resource-queries/
POST /api/v1/accounts/{account_id}/ai/action-drafts/
```

Natural-language resource queries must compile to a small allowlisted filter DSL that Django validates and executes. Never execute model-authored SQL.

Action drafts are typed, inert records. They are not executable provider operations.

### 6.7 Response conventions

- Use an envelope: `{ "data": ... }` for success and `{ "error": { "code": ..., "message": ..., "request_id": ... } }` for failure.
- Use UTC RFC 3339 timestamps.
- Return opaque identifiers as strings.
- Cursor-paginate collections. Do not expose database offsets as stable contracts.
- Return `404`, rather than revealing cross-account existence, when the caller cannot access a subject or insight.
- Echo a request/correlation ID in headers and errors.

## 7. Proposed persistence model

Names are canonical for this plan. Place models in a dedicated backend module such as `apps/ai/`; do not mix provider transport code with AI orchestration.

### 7.1 `EvidenceSnapshot`

Purpose: immutable, server-built, sanitized input to one or more deterministic findings and AI insights.

Fields:

```text
id                      opaque public ID / UUID, unique
account                 FK CoreAccount, indexed
subject_type            constrained enum, indexed
subject_id              opaque subject ID, indexed
schema_version          positive integer
facts                   JSON, validated against a versioned internal schema
evidence_manifest       JSON list of ref/kind/label/observed_at
facts_hash              SHA-256 of canonical JSON
source_observed_at      timestamp
freshness               fresh|stale
redaction_version       string
created_at              timestamp
expires_at              timestamp, indexed
```

Constraints/indexes:

- Unique `(account, subject_type, subject_id, schema_version, facts_hash)` to reuse identical snapshots.
- Index `(account, subject_type, subject_id, -created_at)`.
- Index `(expires_at)` for pruning.
- Store no prompt, credential, recipient, or raw provider object.

### 7.2 `DeterministicFinding`

Purpose: reproducible rule output used in briefs and as model evidence.

Fields:

```text
id
account                 FK, indexed
snapshot                FK EvidenceSnapshot
rule_id                 stable string
rule_version            string
severity                info|warning|critical
state                   open|resolved
title                   bounded text
facts                   JSON, deterministic output only
evidence_refs           JSON array of manifest refs
fingerprint             SHA-256 stable finding identity
detected_at
resolved_at             nullable
created_at
```

Constraints/indexes:

- Unique `(account, fingerprint, state)` or an equivalent transition-safe constraint.
- Index `(account, state, severity, -detected_at)`.
- A rule result must be reproducible from its referenced snapshot and rule version.

### 7.3 `AIInsight`

Purpose: public async resource and immutable provenance for a generated explanation.

Fields:

```text
id                      opaque public ID / UUID, unique
account                 FK, indexed
requested_by            FK User/CoreMember, nullable for scheduled work
feature                 enum, initially incident_investigator
subject_type
subject_id
snapshot                FK EvidenceSnapshot
lifecycle               queued|running|ready|abstained|failed|expired
freshness               fresh|stale
idempotency_key         bounded string
request_hash            SHA-256 of normalized feature + subject + options
result                   JSON, nullable, versioned schema
result_schema_version   integer
abstention_code         nullable constrained string
failure_code            nullable constrained string
model_provider          internal operator field
model_name              internal operator field
model_version           nullable
prompt_version          string
input_facts_hash        copied/verified snapshot hash
attempt_count           integer
queued_at
started_at              nullable
generated_at            nullable
failed_at               nullable
expires_at              nullable, indexed
created_at
updated_at
```

Constraints/indexes:

- Unique `(account, requested_by, idempotency_key)` for user-triggered requests. If scheduled requests are allowed, use a separate deterministic schedule key rather than a nullable uniqueness assumption.
- A reused idempotency key with a different `request_hash` returns `409 idempotency_conflict`.
- Index `(account, id)` and `(account, lifecycle, -created_at)`.
- Index `(lifecycle, queued_at)` for recovery.
- Optional deduplication index/key over `(account, feature, subject_type, subject_id, input_facts_hash, prompt_version, model_name)`.
- Only one lifecycle transition function may update state; enforce legal transitions and compare-and-swap the prior state.
- Public responses may expose model provenance only if the product explicitly decides to do so. Operators still need it internally.

### 7.4 `AIUsageLedger`

Purpose: append-only cost, latency, and provider-call accounting.

Fields:

```text
id
account                 FK, indexed
insight                 FK AIInsight, indexed
attempt_number
provider
model
operation               generation|embedding (embedding is not initially needed)
input_units
output_units
cached_input_units
estimated_cost_micros
currency                default USD
latency_ms
provider_request_id_hash nullable
outcome                 success|abstained|timeout|provider_error|invalid_output
created_at
```

Constraints/indexes:

- Unique `(insight, attempt_number, operation)`.
- Index `(account, -created_at)` for budgets.
- Never store raw provider responses, prompts, or secrets in the ledger.

### 7.5 `AIInsightFeedback`

Fields:

```text
id
account                 FK
insight                 FK
member                  FK
rating                  helpful|not_helpful
reason_codes            JSON allowlisted strings
comment                 bounded optional text
created_at
```

Index `(account, insight, -created_at)`. Apply content length limits and normal tenant checks.

### 7.6 `AIActionDraft` (later)

Fields should include `account`, `requested_by`, `snapshot`, `action_type`, `typed_parameters`, `validation_state`, `evidence_refs`, provenance, expiry, and timestamps. A draft contains no provider credential and cannot execute itself. Provider mutation requires a separate future system with RBAC, validation, preview, confirmation, audit, and idempotency.

## 8. Lifecycle and idempotency

Legal insight transitions:

```text
queued -> running
queued -> failed
queued -> expired
running -> ready
running -> abstained
running -> failed
running -> expired
ready -> expired
abstained -> expired
failed -> expired
```

An insight is immutable after `ready` or `abstained`, except for the transition to `expired`. A retry creates another internal attempt on the same insight only while the public lifecycle remains recoverable; it must not produce two final results.

Creation flow:

1. Authenticate and authorize the path account.
2. Validate feature, subject type, and subject membership in that account.
3. Normalize request and calculate `request_hash`.
4. Lock/find `(account, requested_by, idempotency_key)`.
5. Return the existing insight for an identical hash; return `409` for a different hash.
6. Build or reuse a sanitized snapshot inside the server boundary.
7. Persist the queued insight transactionally.
8. Publish the Celery task with `transaction.on_commit`.
9. A recovery task finds old `queued` or stale `running` rows and safely republishes/retries them.

The AI worker receives only `insight.id`, loads the authorized snapshot server-side, and uses a compare-and-swap lifecycle update. Celery result state is not authoritative.

## 9. Evidence and redaction pipeline

### 9.1 Allowed first-release evidence

For an asset incident:

- asset public ID, bounded display name, provider, type, and region when explicitly safe;
- deterministic monitoring state and freshness;
- recent non-diagnostic status transitions;
- deterministic metadata changes;
- cloud status and last sync time;
- applicable deterministic findings;
- bounded provider incident/health facts already normalized by an adapter.

For later briefs:

- counts computed by Django;
- stale-monitoring findings;
- invalid-auth cloud findings;
- failed/dead notification-delivery findings;
- normalized AWS cost anomaly facts, with authoritative values computed by code;
- normalized AWS security/governance and backup-job facts.

### 9.2 Denied by default

- provider-account rows and every credential field;
- authorization headers and request/response dumps;
- notification recipient addresses and email bodies;
- user email/profile/session/authentication data;
- reset, verification, TOTP, reCAPTCHA, Django, webhook, SMTP, database, broker, Sentry, or model-provider secrets;
- raw provider metadata not selected by an AI-specific allowlist;
- raw logs, environment variables, secrets-manager values, SSM values, policies, or configuration blobs;
- URLs containing query strings or signed credentials;
- arbitrary notes or comments until separately classified and sanitized.

### 9.3 Pipeline

1. Resolve the subject inside the authorized account.
2. Read deterministic evidence from PostgreSQL.
3. Project through a feature-specific allowlist; do not start from model serialization and subtract fields.
4. Normalize types, timestamps, units, and bounded cardinality.
5. Apply recursive key/value redaction as a second defense.
6. Drop or tokenize untrusted free text. Treat all remaining provider-controlled text as quoted data, never instructions.
7. Build a manifest and stable evidence references.
8. Canonicalize JSON and compute `facts_hash`.
9. Evaluate freshness and deterministic abstention conditions before calling a model.
10. Store the immutable snapshot.
11. Send only the snapshot schema and fixed system/developer prompt to the provider.
12. Parse strict structured output, reject unknown fields, validate every evidence reference, and reject claims without references.
13. Persist either a valid result or a safe abstention/failure; never render partial output.

Do not assume the existing `redact_sensitive_metadata` function alone defines a safe external-AI boundary. It is a useful second defense, while the AI-specific projection must be allowlist-first.

## 10. Provider modes and configuration

Supported modes should be:

```text
disabled              no generation and no AI egress; default
local_openai_compatible locally operated endpoint; no managed egress
external_byok         instance operator supplies an external provider key
managed_proxy         future hosted offering, not part of the self-hosted MVP
```

Configuration belongs in environment variables or `CLOUDMOO_SECRETS`, following `app_cloudmoo_com/settings.py`. Provider secrets must not be embedded in iOS or stored in ordinary model JSON.

Proposed names (final names are an open decision):

```text
AI_ENABLED=false
AI_PROVIDER_MODE=disabled
AI_BASE_URL=
AI_API_KEY=
AI_MODEL=
AI_REQUEST_TIMEOUT_SECONDS=30
AI_MAX_ATTEMPTS=3
AI_MAX_INPUT_UNITS_PER_REQUEST=
AI_MAX_OUTPUT_UNITS_PER_REQUEST=
AI_DAILY_ACCOUNT_BUDGET_MICROS=
AI_DAILY_INSTANCE_BUDGET_MICROS=
AI_RETENTION_DAYS=30
AI_QUEUE=ai
AI_EGRESS_ALLOWLIST=
```

Settings validation must fail closed when AI is enabled with an inconsistent configuration. Do not log resolved secret values.

## 11. Queue isolation and failure handling

- Route all generation to a dedicated `ai` queue and worker process with low, explicit concurrency.
- Do not add model calls to `run_status_check`, `_record_monitoring_observation`, `run_cloud_sync`, email outbox transactions, or provider adapters.
- The monitoring transition may schedule snapshot/insight work only after its transaction commits. The existing deterministic alert proceeds independently.
- Use hard provider timeouts lower than the Celery soft time limit.
- Bound input facts, output units, attempts, and cost before publishing.
- Use exponential retry only for explicitly transient provider errors. Do not retry invalid output indefinitely.
- Recover stale queued/running rows from the database. Do not trust Celery result backend state.
- When the model/provider is unavailable, show a stable failure or deterministic fallback; never substitute an unvalidated free-text result.
- Deployments without an AI worker remain fully functional with capabilities disabled.

## 12. RBAC and authorization

Proposed scopes:

```text
core:read
cloud:sync
asset:check
asset:monitoring_write
ai:read
ai:generate
ai:feedback
ai:draft_actions       later
```

Baseline role mapping:

| Role | Read account evidence | Generate/read insight | Submit feedback | Sync/check | Change monitoring | Draft action |
| --- | --- | --- | --- | --- | --- | --- |
| MEMBER | yes | yes | yes | product decision | no | no |
| ADMIN | yes | yes | yes | yes | yes | later, if enabled |
| OWNER | yes | yes | yes | yes | yes | later, if enabled |

Final permission mapping is an open product decision, especially whether `MEMBER` may trigger provider reads through sync/check. Regardless of mapping:

- the path account is authoritative;
- subject, snapshot, insight, ledger, and feedback must all match the same account;
- list querysets are account-filtered before object lookup;
- inaccessible and nonexistent opaque IDs produce indistinguishable `404` responses;
- superuser access is explicit and audited rather than inherited accidentally through a broad manager;
- capabilities report the current member’s enabled features, not merely instance configuration.

## 13. Observability, privacy, and cost ledger

Record metrics without prompt/evidence contents:

- insight requests by feature/lifecycle/provider mode;
- queue wait, generation latency, and total latency histograms;
- snapshot size and structured-output validation failures;
- abstentions by stable reason;
- retries/timeouts/provider failures;
- input/output units and estimated cost from `AIUsageLedger`;
- cache/deduplication and idempotency reuse;
- feedback outcome;
- AI worker saturation and stale queued/running counts;
- monitoring check latency and notification delivery latency, so non-regression can be proven.

Logs should contain request ID, account ID hash/opaque ID, insight ID, feature, lifecycle transition, prompt/model version, facts hash, attempt, latency, unit counts, and safe error code. Do not log prompts, facts JSON, raw outputs, names, recipient addresses, or provider request bodies.

Budgets must be enforced deterministically before the provider call using the ledger plus conservative requested-unit reservation. Reconcile the reservation to actual usage afterward. A budget error cannot consume an unbounded retry loop.

Add retention/pruning for snapshots, insights, feedback, and ledger rows. Account log-retention policy can cap evidence availability, but product and legal policy must decide whether AI provenance can be retained for a shorter or longer interval. Expired insights must no longer expose result content to clients.

## 14. Evaluation and test corpus

### 14.1 Deterministic tests

- account isolation for every collection/detail/create/feedback endpoint;
- account-switch concurrency showing `active_account` cannot change API scope;
- role/scope matrix and superuser audit path;
- subject/account mismatch and insight/account mismatch;
- idempotent create, replay, concurrent create, and hash conflict;
- legal/illegal lifecycle transitions and stale-worker compare-and-swap;
- lost broker publish and queued/running recovery;
- provider timeout, invalid JSON, invalid schema, unknown fields, oversized output, and missing evidence references;
- snapshot canonicalization and stable facts hash;
- allowlist projection and recursive redaction;
- retention and expiry;
- budget reservation/reconciliation;
- AI queue outage with monitoring and email paths still passing.

### 14.2 Prompt/evidence adversarial corpus

Create synthetic fixtures containing secrets and prompt injection in:

- asset/provider names;
- tags and descriptions;
- metadata changes;
- provider errors;
- container/log messages;
- URLs and query strings;
- nested environment/configuration fields;
- recipient/user data accidentally attached to an object.

Expected result: denied fields never enter a snapshot; retained untrusted strings cannot change instructions or cause disclosure.

### 14.3 Incident investigator golden cases

Build cases from existing monitoring semantics:

1. first observation with no prior incident;
2. running to stopped with fresh evidence;
3. configuration-only change;
4. repeated diagnostic errors with no false outage;
5. stale monitoring heartbeat;
6. invalid provider authentication;
7. out-of-order result discarded by generation fencing;
8. contradictory state/log evidence;
9. missing timeline;
10. unsupported provider/type;
11. notification delivery failure that is not an asset outage;
12. cost anomaly with authoritative numeric facts;
13. backup job/recovery-point presence without recovery proof;
14. security finding without a compliance conclusion;
15. cross-resource root-cause evidence absent.

Score factual claims, evidence-reference validity, unsupported claims, safe uncertainty, abstention, recommended-check safety, latency, and cost. Human reviewers should grade operational usefulness separately from factual correctness.

### 14.4 Hard release gates

- Zero credential, recipient, or cross-account leakage in deterministic and adversarial tests.
- 100% of factual model claims carry at least one valid evidence reference.
- 100% of returned evidence references exist in the snapshot manifest.
- Invalid or partial structured output is never rendered.
- At least 95% abstention on the intentionally missing, stale, contradictory, and unsupported evaluation subset.
- Zero false `healthy`, `secure`, `compliant`, `root cause confirmed`, or `recoverable` claims in the golden set.
- No model or AI worker receives a provider credential.
- No direct provider mutation path exists.
- Duplicate delivery produces one final insight for the same idempotent request.
- AI-provider outage does not degrade deterministic monitoring correctness or notification delivery.
- Monitoring/status-check and alert-delivery latency remain within an agreed non-regression budget under AI load.
- Account budgets and instance kill switch work in integration tests.

## 15. Phased delivery, dependencies, and exit gates

### Phase 0: First-party API and tenant boundary

Dependencies: none.

Deliver:

- stable account opaque IDs;
- mobile authentication and revocation;
- explicit account permission classes;
- `/me/`, accounts, overview, clouds, assets, timeline, activity, notifications;
- deterministic cloud sync, asset check, and monitoring endpoints;
- pagination, error envelope, request IDs, and authorization tests.

Exit gate: iOS can replace its mock data path for read-only overview/assets/timeline without using `active_account`, the webhook key, or provider credentials. Every endpoint passes cross-account tests.

### Phase 1: Evidence and deterministic findings

Dependencies: Phase 0 tenant boundary and public IDs.

Deliver:

- `EvidenceSnapshot` and `DeterministicFinding` models;
- versioned feature-specific snapshot schemas;
- allowlist-first projection, redaction, canonical hash, freshness, and retention;
- first deterministic rules;
- admin/operator inspection that shows provenance but never secrets.

Exit gate: snapshot fixtures are reproducible, fully referenced, bounded, and pass the secret/prompt-injection corpus. No model call is required.

### Phase 2: Incident investigator MVP

Dependencies: Phase 1, provider-mode decision, dedicated AI queue, budget policy.

Deliver:

- `AIInsight`, `AIUsageLedger`, and feedback;
- provider adapter and configuration validation;
- capabilities, create, read, and feedback APIs;
- strict output schema and evidence-reference validation;
- iOS/web “Explain this change” UI with queued/running/ready/abstained/failed/expired states;
- kill switch, timeouts, retry/recovery, metrics, and pruning.

Exit gate: all hard release gates pass in a staging environment, including provider outage, queue outage, duplicate request, and monitoring non-regression tests.

### Phase 3: Account brief and AWS explainers

Dependencies: stable Phase 2 telemetry and reviewed finding taxonomy.

Deliver:

- deterministic finding selection and ranking inputs;
- on-demand/latest daily brief;
- AWS cost anomaly, security posture, and backup-state explanation cards;
- notification preference for an optional AI follow-up, separate from deterministic alerts.

Exit gate: authoritative numbers are code-computed; briefs cite evidence; no alert is delayed; cost per active account stays within the approved budget.

### Phase 4: Natural-language resource query

Dependencies: complete account-scoped inventory API and agreed filter schema.

Deliver:

- allowlisted filter DSL;
- model-to-DSL compiler;
- deterministic validator/executor and explainable query display;
- bounded result pagination.

Exit gate: no SQL reaches the model or comes from the model; adversarial queries cannot escape account scope or allowed fields/operators.

### Phase 5: Correlation and inert action drafts

Dependencies: explicit resource-edge/topology facts, incident lifecycle, mature RBAC/audit.

Deliver:

- normalized resource dependencies;
- incident grouping/acknowledgement/severity lifecycle;
- evidence-ranked hypotheses;
- typed, non-executable action drafts.

Exit gate: no draft executes itself; every parameter is validated; root-cause language remains probabilistic unless deterministic evidence proves it. Provider execution is a separate future project.

## 16. Rollout and backout

Roll out in this order:

1. Ship migrations and APIs with `AI_ENABLED=false`.
2. Ship evidence generation in shadow mode with no external egress.
3. Inspect snapshot safety/cardinality and run the full corpus.
4. Enable AI for staff/admin accounts on a staging instance.
5. Enable a small explicit account allowlist in production.
6. Monitor leakage tests, validation failures, abstention, latency, cost, and monitoring non-regression.
7. Expand only after the release gates remain green over an agreed observation window.

Backout controls:

- instance kill switch: set `AI_ENABLED=false`;
- account/feature capability flag to stop new requests;
- stop or scale the `ai` worker independently;
- preserve deterministic APIs and monitoring services;
- mark queued/running insights failed or expired with a safe code;
- retain or prune provenance according to policy;
- never roll back database migrations destructively during an incident;
- do not delete or rewrite deterministic monitoring evidence when disabling AI.

## 17. Backend file map

Current files agents should understand before editing:

| Area | Stable paths/symbols |
| --- | --- |
| Settings | `app_cloudmoo_com/settings.py`, `REST_FRAMEWORK`, Celery broker/Beat settings |
| Celery | `app_cloudmoo_com/celery.py`, `apps.monitoring.tasks` import |
| Root URLs | `app_cloudmoo_com/urls.py`, `apps/api/urls.py`, `apps/api/v1/urls.py` |
| Existing webhook | `apps/api/v1/webhook/` |
| Accounts/RBAC | `apps/console/account/models.py`, `CoreAccount`, `CoreAccountMembership` |
| Member context | `apps/console/member/models.py`, `CoreMember.active_account` |
| Clouds | `apps/console/cloud/models.py`, `CoreCloud`, provider-account adapters |
| Assets | `apps/console/utils/models.py`, `UtilAsset`, `AssetManager` |
| Monitoring state | `apps/monitoring/models.py` |
| Monitoring orchestration | `apps/monitoring/tasks.py` |
| Schedules | `apps/monitoring/schedules.py` |
| Metadata safety | `apps/monitoring/metadata.py` |
| Provider checks | `apps/monitoring/checks/` |
| Notifications | `apps/monitoring/email.py`, `apps/console/notifications/` |
| Current console | `apps/console/home/`, `apps/console/cloud/`, `apps/console/asset/` |
| Tests | `tests/test_monitoring.py`, provider test modules, live E2E scripts |
| Deployment | `docker-compose.yml`, `Dockerfile`, `render.yaml`, `heroku.yml`, `deploy/` |

Proposed paths:

```text
apps/ai/
  apps.py
  models.py
  schemas.py
  evidence.py
  findings.py
  redaction.py
  providers/
  services.py
  tasks.py
  budgets.py
  metrics.py

apps/api/v1/first_party/
  authentication.py
  permissions.py
  serializers.py
  pagination.py
  views.py
  urls.py

apps/api/v1/ai/
  serializers.py
  permissions.py
  views.py
  urls.py

tests/ai/
  fixtures/
  test_evidence.py
  test_redaction.py
  test_insight_lifecycle.py
  test_api_tenancy.py
  test_idempotency.py
  test_provider_failures.py
  test_budgets.py
  test_evals.py
```

The final structure may consolidate small modules, but the trust boundaries must remain visible.

## 18. Ordered agent work packages

Each work package should be a focused PR or an explicitly reviewed stack. Do not parallel-edit the central settings, URL, or model files without coordination.

### WP-01: Contract and security baseline

- Confirm opaque ID format, auth mechanism, error envelope, pagination, role mapping, provider modes, and budgets.
- Add an ADR with resolved decisions.
- Dependency: none.
- Done when backend and iOS contract fixtures agree exactly.

### WP-02: Account IDs and first-party authentication

- Add account public ID migration.
- Implement device authentication/revocation and account membership permission.
- Add tenant-isolation tests.
- Dependency: WP-01.

### WP-03: Read-only first-party API

- Implement `/me/`, accounts, overview, clouds, assets, asset detail/timeline, activity, and notifications.
- Add cursor pagination and request IDs.
- Dependency: WP-02.

### WP-04: Deterministic control API

- Implement cloud sync, asset check, and monitoring update with explicit role checks.
- Keep all operations outside AI.
- Dependency: WP-02; may proceed alongside WP-03 after shared contracts stabilize.

### WP-05: Evidence schema and persistence

- Add `EvidenceSnapshot`, canonical hashing, freshness, manifests, and pruning.
- Add sanitized snapshot inspection for operators.
- Dependency: WP-02.

### WP-06: Redaction and adversarial corpus

- Build feature allowlists and leakage/prompt-injection fixtures.
- Prove credentials, recipients, and user data never enter snapshots.
- Dependency: WP-05; begin fixture preparation earlier without editing shared files.

### WP-07: Deterministic finding engine

- Add rules and `DeterministicFinding` with reproducibility tests.
- Dependency: WP-05.

### WP-08: Insight persistence and lifecycle

- Add `AIInsight`, usage ledger, feedback, legal transitions, idempotency, recovery, and pruning.
- No provider calls yet.
- Dependency: WP-05 and WP-01.

### WP-09: Provider adapter and isolated queue

- Implement disabled/local/BYOK modes, configuration validation, dedicated route/worker, timeouts, budgets, metrics, and safe failures.
- Dependency: WP-08 and WP-06.

### WP-10: AI API

- Implement capabilities, insight create/read, and feedback using the canonical contract.
- Add account, RBAC, idempotency, and error-contract tests.
- Dependency: WP-08 and WP-09.

### WP-11: Evaluation harness

- Implement golden/adversarial cases and automated gates.
- Emit machine-readable results tied to prompt/model/schema versions.
- Dependency: WP-06 through WP-10.

### WP-12: Web and iOS incident investigator

- Web: add an evidence-backed explanation panel to asset detail.
- iOS: follow the companion plan and consume the same fixtures.
- Show lifecycle, freshness, evidence, limitations, and feedback. Never imply an AI result is authoritative state.
- Dependency: WP-03, WP-10, WP-11.

### WP-13: Staged rollout and operational rehearsal

- Shadow snapshots, staff allowlist, failure injection, budget/load tests, monitoring non-regression, kill-switch/backout rehearsal.
- Dependency: all MVP packages.

Agents should stop and request review if a package would cross a no-go boundary, require provider mutation, expose a new data category to an external model, or change the canonical public contract.

## 19. Definition of Done for the MVP

The incident investigator MVP is done only when:

- the first-party account-scoped API is live and iOS no longer needs mock data for the affected read surfaces;
- authentication is revocable and no first-party endpoint uses the webhook key;
- every endpoint enforces path-account membership and the approved RBAC matrix;
- snapshot construction is allowlist-first, bounded, versioned, hashed, retained, and independently testable;
- provider credentials and recipient/user data cannot enter AI evidence;
- the public API matches this document and the iOS companion plan, including trailing slashes, opaque IDs, lifecycle values, and freshness;
- create/read/feedback are idempotent and recover safely from broker/worker failure;
- the AI worker is isolated from monitoring workers;
- output is strict structured data, every factual claim cites valid evidence, and invalid output is discarded;
- all hard evaluation gates pass for the pinned model/prompt/schema version;
- AI budgets, telemetry, pruning, kill switch, staged rollout, and backout are verified;
- AI/provider outage leaves sync, checks, timelines, deterministic findings, and notifications correct;
- current unit/integration tests pass in the supported Docker environment, with live-provider checks reported separately;
- operator and user documentation clearly state that AI explains evidence and may abstain; it does not establish health, security, compliance, root cause, or recoverability.

## 20. Open decisions

Resolve and record these before or during WP-01:

1. Public opaque ID format and migration/backfill strategy for `CoreAccount`.
2. Mobile authentication design: JWT/rotating refresh versus opaque device sessions, token lifetime, revocation, and 2FA relationship.
3. Whether a member may trigger deterministic cloud sync or asset check, or whether those require admin.
4. Whether external AI egress requires both instance opt-in and account opt-in.
5. Initial local OpenAI-compatible providers/models that CloudMoo will support and test.
6. Default external provider/model and whether model provenance is shown to users.
7. Snapshot, insight, feedback, and usage-ledger retention policy.
8. Daily account and instance budget defaults and operator override UI.
9. Whether on-demand insights are cached across users within an account when facts/prompt/model are identical.
10. Whether the first release is generated only on demand or optionally pre-generated after a committed transition.
11. Whether raw/bounded logs are permanently denied to external providers or can be enabled in a separately consented future feature.
12. The deterministic finding taxonomy, severity mapping, and which AWS signals are eligible for briefs.
13. Product language for uncertainty, abstention, and limitations.
14. Feedback retention and whether comments may be used for evaluation/model improvement.
15. Approved monitoring and alert latency non-regression thresholds under AI load.
16. Whether hosted/managed AI is ever offered and, if so, its isolation, data-processing, regional, and deletion commitments.

Until resolved, choose the privacy-preserving and least-authoritative behavior: disabled external egress, no raw logs, no mutation, conservative budgets, and abstention.
