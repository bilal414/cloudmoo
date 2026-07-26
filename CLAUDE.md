# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

CloudMoo is an open-source, self-hosted multi-cloud infrastructure monitoring platform that tracks assets (servers, volumes, databases) across cloud providers including DigitalOcean, AWS, Vultr, Hetzner, Linode, and UpCloud. The application uses Django with PostgreSQL and integrates with AWS services for the monitoring engine.

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
# Create AWS EventBridge schedules for all cloud assets
python manage.py create_all_cloud_schedules --confirm

# Remove all AWS EventBridge schedules
python manage.py remove_all_cloud_schedules --confirm
```

### Docker Development
```bash
docker compose up --build   # starts PostgreSQL + the web app
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
  or a JSON blob in the `AWS_SECRETS` env var.
- Optional services are off unless configured: Sentry (`SENTRY_DSN`),
  reCAPTCHA (`RECAPTCHA_*`), SMTP (`EMAIL_BACKEND` + `SMTP_*`).
- `REGISTRATION_OPEN=false` disables public sign-ups.

### AWS Integration Architecture
The monitoring engine relies on AWS services:
- **EventBridge Scheduler**: Triggers asset monitoring on configurable intervals
- **Lambda Functions**: Execute status checks and send notifications (`_lambda/` directory)
- **DynamoDB**: Stores asset status logs and 30-day timeline data
  (table names configurable via `AWS_DYNAMODB_*_TABLE`)
- Platform boto3 calls go through `apps/console/utils/aws.py`
  (`aws_client` / `aws_resource`), which uses explicit credentials when set
  and the default credential chain (IAM role) otherwise.

### Email
Transactional email goes through Django's `EMAIL_BACKEND` (SMTP by default in
production, console in debug, `django_ses.SESBackend` for Amazon SES).
`apps/console/utils/email.py` (`EmailSender`) is the shared sender helper.

### Cloud Provider Integration
Each cloud provider has dedicated modules in `apps/console/cloud/[provider]/` with standardized interfaces for:
- Asset discovery and synchronization
- Status monitoring
- Provider-specific API interactions

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
1. **Scheduling**: AWS EventBridge triggers Lambda functions on plan intervals
2. **Checking**: Lambda functions query cloud provider APIs for asset status
3. **Storage**: Status data stored in DynamoDB with 30-day retention
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
4. Create corresponding Lambda functions for monitoring

### Database Queries
The application uses select_related and prefetch_related extensively for performance. When modifying queries, maintain this pattern to avoid N+1 query problems.

### Lambda Deployment
Lambda functions in `_lambda/` directory are deployed separately from the main application. Changes require updating both the function code and any corresponding IAM policies.
