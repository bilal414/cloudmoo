# CloudMoo Mobile API (v1)

Account-scoped REST API for the CloudMoo iOS and Android apps. Same data and
semantics as the web console; provider credentials are never exposed.

- Base URL: `<your-cloudmoo-host>/api/v1/mobile/` (e.g. `https://demo.cloudmoo.com/api/v1/mobile/`)
- Auth: `Authorization: Token <key>` header (DRF token), obtained from `auth/login/`.
- All timestamps are ISO 8601 UTC. List endpoints paginate as
  `{count, next, previous, results}` (`?page=` / `?page_size=`, max 100).
- Errors: `{error: string}` plus optional machine-readable `code`.

## Health buckets

Every asset/status payload carries a normalized `health` value next to the
raw provider `status`:

| health | meaning |
| --- | --- |
| `healthy` | serving (`active`, `running`, `available`, ...) |
| `warning` | serving but transitional/degraded (`degraded`, `provisioning`, ...) |
| `down` | not serving (`off`, `stopped`, `terminated`, ...) |
| `paused` | monitoring disabled |
| `gone` | resource no longer exists at the provider |
| `unknown` | no data, stale check, or diagnostic status |

`uptime_30d`/`uptime_percentage` are percentages (0–100, 2 decimals) computed
from the durable status-change log; `null` when there is no data (never 0
without evidence of an outage).

## Endpoints

### `POST auth/login/`
`{email, password}` → `{token, user{name,email,role,account}, installation{name,version,environment}}`.
Error codes: `invalid_credentials` (400), `email_not_verified` (403),
`two_factor_required` (403). Throttled: 10 attempts / 15 min / IP.

### `POST auth/logout/` 🔒
Revokes the request's token. → `204`.

### `GET auth/me/` 🔒
→ `{user, installation}`.

### `GET overview/` 🔒
Dashboard snapshot:
```json
{
  "clouds": {"total": 6, "active": 6, "invalid_auth": 0, "syncing": 1},
  "assets": {"total": 5370, "active": 4481, "monitored": 889, "disabled": 3592, "gone": 0},
  "health": {"healthy": 880, "warning": 2, "down": 1, "unknown": 6},
  "incidents_last_24h": 3,
  "uptime_percentage": 99.7,
  "last_sync": "2026-08-14T13:45:00Z",
  "providers": [ /* cloud objects without counts */ ]
}
```

### `GET clouds/` 🔒
`{count, results: [cloud]}`. Cloud object:
`{uuid, name, provider, provider_name, status, health, last_synced, syncing, asset_counts{total,active,monitored,disabled,gone}}`.
Cloud `health`: `connected` (active), `attention` (invalid auth), `disconnected` (paused/suspended).

### `GET clouds/{uuid}/` 🔒
Cloud object plus `asset_counts_by_type` and `recent_sync_runs`
(`[{uuid,status,families_completed,families_total,family_errors,started_at,finished_at}]`).

### `PATCH clouds/{uuid}/` 🔒
`{name?}` renames the provider account; `{action: "pause"|"resume"}` pauses/resumes
monitoring (reconciles all asset schedules, like the console edit view).

### `POST clouds/{uuid}/sync/` 🔒
Queues a background inventory sync. → `{success, queued, message, syncing, run_uuid?}`.

### `GET assets/` 🔒
Cross-cloud inventory. Filters: `?q=` (name/identifier), `provider`, `type`,
`monitoring` (`active|disabled|no_longer_exists`), `cloud` (uuid), `health`.
Rows: `{id, uuid, key, name, identifier, type, provider, provider_name, region, status, health, monitoring, monitoring_stale, last_checked_at}`.

### `GET assets/{provider}/{type}/{id}/` 🔒
Row fields plus `metadata` (redacted), `notes`, `notification_emails`,
`monitoring_supported`, `provider_url`, `uptime_30d`, `cloud{uuid,name}`,
`created`, and `timeline{items[{timestamp,status,health,duration,metadata_changes}], has_next, has_previous, total_pages, current_page}` (`?timeline_page=`).

### `PATCH assets/{provider}/{type}/{id}/` 🔒
`{monitoring: "active"|"disabled"}` and/or `{notification_emails: [..]}` (max 50).
→ updated asset row + `notification_emails`.

### `POST assets/{provider}/{type}/{id}/check/` 🔒
Immediate status check. → `{success, status, health, timestamp, metadata_changes, error}`.

### `POST assets/{provider}/{type}/{id}/pause/` / `resume/` 🔒
→ updated asset row.

### `GET activity/` 🔒
Status-change feed. Filters: `status`, `provider`, `type`.
Rows: `{id, asset_key, provider, asset_type, status, health, timestamp, error_message, metadata_changes}`.

### `GET notifications/` 🔒
Alert-email log. Rows: `{id, asset_key, provider, asset_type, recipient, subject, status_previous, status_current, health, delivery_status, timestamp}`.

### `GET|PATCH account/` 🔒
`{name, role, monitoring_interval, log_retention_days, members_count, clouds_count}`.
`PATCH {name}` is owner-only (403 otherwise).
