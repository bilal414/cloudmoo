# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

CloudMoo is an open-source, self-hosted multi-cloud infrastructure monitoring platform that tracks assets (servers, volumes, databases) across cloud providers including DigitalOcean, AWS, Vultr, Hetzner, Linode, and UpCloud. The application uses Django with PostgreSQL and a built-in Celery-based monitoring engine (Celery Beat + worker + RabbitMQ).

## Essential Commands

### Development Server
```bash
python manage.py runserver
```

### Database Operations
```bash
python manage.py makemigrations
python manage.py migrate
python manage.py createcachetable   # required on fresh installs (DB cache)
python manage.py dbshell
```

### Testing
```bash
python manage.py test
```

### Static Files
```bash
python manage.py collectstatic
```

### Custom Management Commands
```bash
# Create database scheduler entries (PeriodicTasks) for all cloud assets
python manage.py create_all_cloud_schedules --confirm

# Remove all database scheduler entries
python manage.py remove_all_cloud_schedules --confirm
```

### Celery (monitoring engine)
```bash
celery -A app_cloudmoo_com worker --loglevel=info
celery -A app_cloudmoo_com beat --loglevel=info --scheduler django_celery_beat.schedulers:DatabaseScheduler
```

### Docker Development
```bash
docker compose up --build   # starts PostgreSQL, RabbitMQ, and the web/worker/beat services
```

## Architecture Overview

### Multi-App Django Structure
- **apps/console/**: Main web interface with account management, cloud integrations, and asset monitoring
- **apps/api/**: REST API with v1 endpoints, an internal webhook, and token authentication
- **apps/management/**: Management commands

### Core Models Hierarchy
- **CoreAccount**: Team-based accounts with plan-based limits
- **CoreMember**: Extended user profiles with verification
- **CorePlan**: Limits configuration (a single "Self-Hosted" plan by default; adjust via Django admin)
- **CoreCloud**: Cloud provider connection configurations
- **UtilAsset**: Abstract base for all monitored infrastructure assets

### Configuration
- All configuration comes from environment variables / `.env` (see `.env.example`),
  or a JSON blob in the `CLOUDMOO_SECRETS` env var.
- Optional services are off unless configured: Sentry (`SENTRY_DSN`),
  reCAPTCHA (`RECAPTCHA_*`), SMTP (`EMAIL_BACKEND` + `SMTP_*`).
- `REGISTRATION_OPEN=false` disables public sign-ups.

### Monitoring Engine
The monitoring engine is built in — no external cloud services required:
- **Celery beat** (`DatabaseScheduler` from django-celery-beat): enqueues one
  `PeriodicTask` per monitored asset (`cloudmoo.check_asset_status`) and one
  per connected cloud (`cloudmoo.sync_cloud_assets`) onto RabbitMQ
- **Celery worker**: executes the tasks — checks call provider APIs directly,
  write `AssetStatusLog`/`AssetStatusEmail` rows (PostgreSQL tables in
  `apps/monitoring/models.py`), and send alert emails through Django's
  `EMAIL_BACKEND`; `cloudmoo.prune_status_logs` prunes old logs daily,
  honoring each plan's `log_retention_days`
- **`apps/monitoring/`** package: `checks/` (per-provider status checks),
  `metadata.py`, `timeline.py`, `email.py`, `tasks.py`, `schedules.py`,
  `models.py`

### Email
Transactional email goes through Django's `EMAIL_BACKEND` (SMTP by default in
production, console in debug, `django_ses.SESBackend` for Amazon SES).
`apps/console/utils/email.py` (`EmailSender`) is the shared sender helper.

### Cloud Provider Integration
Each cloud provider has dedicated modules in `apps/console/cloud/[provider]/` with standardized interfaces for:
- Asset discovery and synchronization
- Status monitoring
- Provider-specific API interactions

Connecting an AWS account as a monitored provider uses that account's own
credentials (`apps/console/cloud/aws/`); no platform-level AWS credentials or
services are needed to run CloudMoo.

## Key Configuration

### Settings Structure
- Main settings in `app_cloudmoo_com/settings.py`
- Database: PostgreSQL (configurable via `DB_*`)
- Caching: Database-backed Django cache (run `createcachetable` on fresh installs)

### Static Files Organization
- Console assets: `apps/console/_static/console/`
- Provider logos under `apps/console/_static/console/images/clouds/`

### Template Structure
- Templates in `apps/console/_templates/`
- Email templates include both HTML and text versions
- Custom error pages for 400, 403, 404, 500

## Migration Management

All migrations are consolidated in `apps/_migrations/` directory rather than per-app migrations. When creating new migrations, they will be placed here automatically due to the `MIGRATION_MODULES` setting.

## Authentication & Security

### Multi-Factor Authentication
- Built-in Django authentication with custom extensions
- TOTP-based 2FA support via `pyotp` and QR codes
- Email verification tokens for account activation
- Session-based authentication for web, token-based for API

### Plan-Based Limits
Accounts are limited by their assigned `CorePlan` (cloud/asset/team limits and
monitoring intervals). Self-hosted instances use the single default
"Self-Hosted" plan with high limits; instance admins can tune it in Django admin.

### Rate Limiting
- Login and password-reset endpoints are rate limited via
  `apps/console/utils/decorators.py::rate_limit` (cache-backed)

## Monitoring System

### Asset Status Pipeline
1. **Scheduling**: Celery beat (DatabaseScheduler) enqueues periodic tasks on plan intervals
2. **Checking**: The Celery worker queries cloud provider APIs for asset status
3. **Storage**: Status data stored in PostgreSQL with per-plan retention
4. **Notification**: Status changes trigger email notifications

### Status Timeline
Each asset maintains a 30-day status timeline accessible via the web interface, showing uptime trends and incident history.

## External Integrations (all optional)

### Error Tracking
- Sentry integration when `SENTRY_DSN` is set

### Bot Protection
- reCAPTCHA v3 on signup/login/password-reset when `RECAPTCHA_*` keys are set

## Development Notes

### Provider Integration Pattern
When adding new cloud providers, follow the established pattern:
1. Create provider module in `apps/console/cloud/[provider]/`
2. Implement standardized asset discovery methods
3. Add provider configuration to `CoreCloudServiceProvider` model
4. Add a `check_<provider>_<asset_type>_status` function in `apps/monitoring/checks/` for monitoring

### Database Queries
The application uses select_related and prefetch_related extensively for performance. When modifying queries, maintain this pattern to avoid N+1 query problems.

### Provider Status Checks
Provider status checks live in `apps/monitoring/checks/` (ported from the retired AWS Lambda implementation). New providers need a `check_<provider>_<asset_type>_status` function there plus a sync implementation in `apps/console/cloud/<provider>/`.
