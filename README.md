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
- **Scheduled status monitoring** — per-asset uptime checks on the built-in
  scheduler (Celery Beat + worker), from 1-minute intervals up, honoring
  per-plan intervals
- **30-day status timeline** — uptime history and incident log per asset,
  stored in PostgreSQL
- **Email notifications** — status-change alerts to per-asset recipient lists,
  through any Django email backend (SMTP, Amazon SES, console for dev)
- **Team accounts** — multiple members per account with roles
- **Security built in** — TOTP two-factor authentication, rate-limited login
  and password reset, optional reCAPTCHA v3
- **REST API + token auth** — for automation and integrations

## Architecture

```
┌────────────┐     ┌──────────────┐     ┌───────────────────┐
│  Browser   │────▶│  Django app  │────▶│     PostgreSQL    │
└────────────┘     │  (console +  │     │ (accounts, assets,│
                   │   REST API)  │     │  status logs,     │
                   └──────┬───────┘     │  beat schedules)  │
                          │             └────────▲──────────┘
              ┌───────────▼───────────┐          │
              │  Celery beat          │          │
              │  (DatabaseScheduler,  │          │
              │   PeriodicTasks)      │          │
              └───────────┬───────────┘          │
                          │ enqueues             │
                   ┌──────▼───────┐              │
                   │   RabbitMQ   │              │
                   └──────┬───────┘              │
                          │                      │
              ┌───────────▼───────────┐──────────┘
              │  Celery worker        │
              │  (cloudmoo.* tasks)   │
              └───────────┬───────────┘
                          │
            ┌─────────────┼───────────┐
            ▼                         ▼
   ┌─────────────────┐       ┌─────────────────┐
   │  Cloud provider │       │  Django email   │
   │  APIs           │       │  backend (SMTP, │
   │                 │       │  SES, console)  │
   └─────────────────┘       └─────────────────┘
```

- **`apps/console/`** — web interface: accounts, cloud connections, asset
  dashboards, notifications, security settings
- **`apps/api/`** — REST API (v1) and the webhook for external sync triggers
- **`apps/monitoring/`** — the monitoring engine: provider status checks,
  Celery tasks, beat schedules, and the status-log models
- **Services:** PostgreSQL (application data, status timeline, beat schedules)
  and RabbitMQ (Celery broker)

## Requirements

- Docker (recommended), or Python 3.12+ with PostgreSQL and RabbitMQ
- Cloud provider API credentials with **read-only** permissions
- **No AWS account required** — the monitoring engine is built in

## Quick Start (Docker Compose)

```bash
git clone https://github.com/bilal414/cloudmoo.git
cd cloudmoo

cp .env.example .env
# Edit .env: at minimum set DJANGO_SECRET_KEY.
# Generate one with:
#   python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"

docker compose up --build
```

This starts the full stack — `db` (PostgreSQL), `rabbitmq`, `web`, `worker`,
`beat` — and applies the database migrations automatically (one-shot `migrate`
service).

The app listens on `http://localhost:8000`. Create the first (admin) user:

```bash
docker compose exec web python manage.py createsuperuser
```

or sign up at `/signup/` (disable public sign-ups later with
`REGISTRATION_OPEN=false`).

## Manual Setup (Development)

You need PostgreSQL and RabbitMQ running locally (or point `CELERY_BROKER_URL`
at a broker elsewhere). Then:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # fill in DJANGO_SECRET_KEY and DB_* settings

python manage.py migrate
python manage.py createcachetable
```

Run the three processes:

```bash
python manage.py runserver
celery -A app_cloudmoo_com worker --loglevel=info
celery -A app_cloudmoo_com beat --loglevel=info --scheduler django_celery_beat.schedulers:DatabaseScheduler
```

## Monitoring engine

Scheduled checks are fully self-hosted — no external scheduler or cloud
services involved. Celery beat (with the database scheduler) enqueues one
periodic task per monitored asset (`cloudmoo.check_asset_status`) and one per
connected cloud (`cloudmoo.sync_cloud_assets`); a Celery worker executes them
against the provider APIs, writes status logs to PostgreSQL, sends alert
emails through Django's email backend, and prunes old logs daily per plan
retention. The webhook `/api/v1/webhook/cloud/sync_assets/` (header
`X-API-KEY: <CLOUDMOO_API_KEY>`) still exists for triggering a cloud sync
from external systems.

`/healthz/` is a cheap process liveness probe. Deployments should use
`/readyz/`, which also verifies that PostgreSQL is reachable before routing
traffic to the web process.

## Deployment

### VPS (one-liner installer)

```bash
curl -fsSL https://raw.githubusercontent.com/bilal414/cloudmoo/main/install.sh | sudo bash
```

Installs Docker and the Compose stack under `/opt/cloudmoo` on Debian/Ubuntu.
Pass `--domain` to configure the public hostname:

```bash
curl -fsSL https://raw.githubusercontent.com/bilal414/cloudmoo/main/install.sh | sudo bash -s -- --domain monitors.example.com
```

### Deploy to Render

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/bilal414/cloudmoo)

The Blueprint (`render.yaml`) provisions the web, worker, and beat services, a
managed PostgreSQL database, and a private RabbitMQ broker.

### Deploy to Heroku

[![Deploy to Heroku](https://www.herokucdn.com/deploy/button.svg)](https://heroku.com/deploy?template=https://github.com/bilal414/cloudmoo)

Runs a web/worker/beat formation (`heroku.yml`) with the Heroku Postgres and
CloudAMQP (RabbitMQ) add-ons from `app.json`.

### Railway

Per-service configs live in `deploy/railway/` (`web`, `worker`, `beat`).
Create three services from the same repository with the matching config file,
plus PostgreSQL and RabbitMQ.

### cloud-init

`deploy/cloud-init/cloudmoo.yaml` is a ready-made cloud-config that runs the
installer on first boot of a fresh Ubuntu/Debian VM.

### Marketing website

`website/` is a static site with no build step. Deploy it on Cloudflare Pages
with output directory `website` and no build command, or from the CLI:

```bash
npx wrangler pages deploy website
```

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
| `DATABASE_URL` | Optional; overrides the `DB_*` settings (`DB_SSLMODE` optional) | — |
| `CELERY_BROKER_URL` | Celery broker URL; falls back to `CLOUDAMQP_URL` on Heroku, or is assembled from `RABBITMQ_HOST`/`PORT`/`USER`/`PASSWORD`/`VHOST` | `amqp://guest:guest@rabbitmq:5672//` |
| `CLOUDMOO_API_KEY` | Optional shared secret for the sync webhook | — |
| `RECAPTCHA_*` | Optional reCAPTCHA v3 | disabled |
| `SENTRY_DSN` | Optional error tracking | disabled |

## Management Commands

```bash
# Create database scheduler entries for all clouds/assets (e.g. after a restore)
python manage.py create_all_cloud_schedules --confirm

# Remove all database scheduler entries
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
app_cloudmoo_com/     Django project (settings, urls, wsgi, celery)
apps/
  console/            Web app: home, account, cloud, asset, security,
                      notifications, login, signup, logout
  api/v1/             REST API + sync webhook
  monitoring/         Monitoring engine: checks, tasks, schedules, models
  management/         Management commands
  _migrations/        Consolidated migrations (single 'apps' module)
tests/                Provider connection tests + fixtures
website/              Marketing site (static, for Cloudflare Pages)
deploy/               Railway service configs + cloud-init user data
install.sh            VPS installer
render.yaml           Render Blueprint
app.json, heroku.yml  Heroku button + container formation
```

## Security

See [SECURITY.md](SECURITY.md). Connect cloud providers with read-only
credentials — an AWS example policy is in
[`aws-cloudmoo-readonly-policy.json`](aws-cloudmoo-readonly-policy.json).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE) © CloudMoo
