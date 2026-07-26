# CloudMoo

**Open-source multi-cloud infrastructure monitoring.** Track the status of your
servers, volumes, databases, and other assets across cloud providers — from a
single self-hosted dashboard.

CloudMoo watches your infrastructure on a schedule, keeps a 30-day uptime
timeline per asset, and emails you when something goes down or comes back.

## Features

- **Multi-cloud asset discovery & sync** — connect accounts and CloudMoo
  imports your infrastructure automatically:
  - **DigitalOcean** — Droplets, Volumes, Databases
  - **AWS** — EC2, EBS, RDS, Lambda, DynamoDB, S3, ACM, Snapshots, Elastic IPs,
    Load Balancers, Security Groups, ECS
  - **Vultr, Hetzner, Linode, UpCloud** — Servers & Volumes
- **Scheduled status monitoring** — per-asset uptime checks via AWS
  EventBridge + Lambda, from 1-minute intervals up
- **30-day status timeline** — uptime history and incident log per asset
- **Email notifications** — status-change alerts to per-asset recipient lists,
  through any Django email backend (SMTP, Amazon SES, console for dev)
- **Team accounts** — multiple members per account with roles
- **Security built in** — TOTP two-factor authentication, rate-limited login
  and password reset, optional reCAPTCHA v3
- **REST API + token auth** — for automation and integrations

## Architecture

```
┌────────────┐     ┌──────────────┐     ┌───────────────────┐
│  Browser   │────▶│  Django app  │────▶│    PostgreSQL     │
└────────────┘     │  (console +  │     │ (accounts, assets)│
                   │   REST API)  │     └───────────────────┘
                   └──────┬───────┘
                         │ schedules / invokes
              ┌──────────▼───────────┐
              │  AWS EventBridge     │
              │  Scheduler           │
              └──────────┬───────────┘
                         │ triggers
        ┌────────────────┼─────────────────┐
        ▼                ▼                 ▼
 ┌─────────────┐ ┌───────────────┐ ┌────────────────┐
 │ Lambda:     │ │ Lambda:       │ │ Lambda:        │
 │ check asset │ │ sync cloud    │ │ email status   │
 │ status      │ │ assets        │ │ changes        │
 └──────┬──────┘ └───────┬───────┘ └───────┬────────┘
        └────────────────┼─────────────────┘
                         ▼
                ┌─────────────────┐
                │   DynamoDB      │
                │ (status logs,   │
                │  30-day data)   │
                └─────────────────┘
```

- **`apps/console/`** — web interface: accounts, cloud connections, asset
  dashboards, notifications, security settings
- **`apps/api/`** — REST API (v1) and the internal webhook the monitoring
  engine calls back into
- **`_lambda/`** — the three monitoring Lambda functions
- **AWS services used:** EventBridge Scheduler, Lambda, DynamoDB
  (email delivery goes through any Django email backend; SES optional)

## Requirements

- Python 3.12+ and PostgreSQL (or Docker)
- An AWS account for the monitoring engine (EventBridge, Lambda, DynamoDB)
- Cloud provider API credentials with **read-only** permissions

## Quick Start (Docker Compose)

```bash
git clone https://github.com/<your-org>/cloudmoo.git
cd cloudmoo

cp .env.example .env
# Edit .env: at minimum set DJANGO_SECRET_KEY.
# Generate one with:
#   python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"

docker compose up --build
```

The app listens on `http://localhost:8000`. Create the first (admin) user:

```bash
docker compose exec web python manage.py createsuperuser
```

or sign up at `/signup/` (disable public sign-ups later with
`REGISTRATION_OPEN=false`).

## Manual Setup (Development)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # fill in DJANGO_SECRET_KEY and DB_* settings

python manage.py migrate
python manage.py createcachetable
python manage.py runserver
```

## AWS Setup (Monitoring Engine)

1. **Create the DynamoDB tables** (names must match your `.env`):
   - `cloudmoo-assets` — partition key `asset_key` (String)
   - `cloudmoo-asset-logs` — partition key `asset_key` (String),
     sort key `timestamp` (Number)
   - `cloudmoo-asset-emails` — partition key `asset_key` (String)
2. **Deploy the Lambda functions** from `_lambda/`:
   - `cloudMooCheckAssetStatus` — performs per-asset status checks
   - `cloudMooCloudSyncAssets` — syncs assets from provider APIs; must call
     back to `/api/v1/webhook/cloud/sync_assets/` with header
     `X-API-KEY: <CLOUDMOO_API_KEY>`
   - `cloudMooEmailAssetStatusChange` — sends status-change emails
   See `_lambda/policy.json` for their required IAM permissions.
3. **Create an IAM role for EventBridge Scheduler** allowing
   `lambda:InvokeFunction` on those functions, and set it as
   `AWS_SCHEDULER_ROLE` in `.env`.
4. Set the three `AWS_LAMBDA_*` ARNs and `CLOUDMOO_API_KEY` in `.env`.
5. Platform AWS credentials: set `AWS_ACCESS_KEY` / `AWS_SECRET_ACCESS_KEY`,
   or leave empty to use an IAM role / instance profile.

> Without the AWS monitoring engine, the console still works (connect clouds,
> sync and browse assets) but scheduled status checks and alerts won't run.

## Configuration

All configuration is via environment variables or `.env` — see the fully
documented [`.env.example`](.env.example). Highlights:

| Variable | Purpose | Default |
| --- | --- | --- |
| `DJANGO_SECRET_KEY` | Django secret key (**required**) | — |
| `DJANGO_DEBUG` | Debug mode — `false` in production | `false` |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated hostnames | — |
| `HTTPS_ENABLED` | Secure cookies, HSTS, SSL redirect | `false` |
| `REGISTRATION_OPEN` | Allow public sign-ups | `true` |
| `EMAIL_BACKEND` | Any Django email backend | console (dev) / SMTP |
| `AWS_REGION` | Region of the monitoring engine | `us-east-1` |
| `AWS_DYNAMODB_*_TABLE` | DynamoDB table names | `cloudmoo-*` |
| `CLOUDMOO_API_KEY` | Internal webhook shared secret | — |
| `RECAPTCHA_*` | Optional reCAPTCHA v3 | disabled |
| `SENTRY_DSN` | Optional error tracking | disabled |

## Management Commands

```bash
# Create EventBridge schedules for all clouds/assets (e.g. after a restore)
python manage.py create_all_cloud_schedules --confirm

# Remove all EventBridge schedules
python manage.py remove_all_cloud_schedules --confirm

# Seed test cloud accounts from tests/test_accounts.json (dummy credentials)
python manage.py setup_test_accounts --dry-run
```

## Testing

```bash
python manage.py test
```

Connection tests under `tests/` use the dummy credentials in
`tests/test_accounts.json`; point them at real credentials (locally only,
never commit them) to verify provider integrations end-to-end.

## Project Structure

```
app_cloudmoo_com/     Django project (settings, urls, wsgi)
apps/
  console/            Web app: home, account, cloud, asset, security,
                      notifications, login, signup, logout
  api/v1/             REST API + internal webhook
  management/         Management commands
  _migrations/        Consolidated migrations (single 'apps' module)
_lambda/              Monitoring Lambda functions (deployed separately)
tests/                Provider connection tests + fixtures
```

## Security

See [SECURITY.md](SECURITY.md). Connect cloud providers with read-only
credentials — an AWS example policy is in
[`aws-cloudmoo-readonly-policy.json`](aws-cloudmoo-readonly-policy.json).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE) © CloudMoo
