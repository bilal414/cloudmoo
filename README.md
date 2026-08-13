# CloudMoo

**Open-source multi-cloud infrastructure monitoring.** Track the status of your
servers, volumes, databases, and other assets across cloud providers — from a
single self-hosted dashboard.

CloudMoo watches your infrastructure on a schedule, keeps a 30-day uptime
timeline per asset, and emails you when something goes down or comes back.

## Features

- **Multi-cloud asset discovery & sync** — connect accounts and CloudMoo
  imports your infrastructure automatically:
  - **DigitalOcean** — Droplets, Managed Databases, Volumes, backups and
    snapshots, Reserved IPs, Firewalls, Load Balancers, App Platform, Spaces,
    Container Registry, Kubernetes, VPC networking, DNS, CDN endpoints, and
    certificates. See [DigitalOcean resource coverage](docs/digitalocean-resources.md).
  - **AWS** — EC2, EBS, RDS, Lambda, DynamoDB, S3, ACM, Snapshots, Elastic IPs,
    Load Balancers, Security Groups, ECS, and read-only Lightsail resources
    (instances, disks, snapshots, databases, networking, storage, CDN, DNS,
    containers, alarms, operations, and auto-snapshots). See [AWS Lightsail
    resource coverage](docs/aws-lightsail-resources.md) and the [AWS resource
    integration map](docs/aws-resources.md).
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

CloudMoo deploys as five pieces: a web process, a Celery worker, a single
Celery beat scheduler, PostgreSQL, and RabbitMQ. Use a provider blueprint when
you want those services provisioned together, or use the Docker installer on
any Ubuntu/Debian VM.

### One-click on Render

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/bilal414/cloudmoo)

The [`render.yaml`](render.yaml) Blueprint provisions the web, worker, and beat
services, a managed PostgreSQL database, and a private RabbitMQ service. It
also derives the Render hostname automatically and leaves first-run sign-up
open; disable `REGISTRATION_OPEN` after the instance owner has verified the
initial account. The Blueprint uses paid Render plans, so review the current
plan cost in Render before deploying.

After the first deploy, configure SMTP and add the cloud-provider credentials
from the CloudMoo console before relying on scheduled checks or email alerts.

### One-click on Heroku

[![Deploy to Heroku](https://www.herokucdn.com/deploy/button.svg)](https://heroku.com/deploy?template=https://github.com/bilal414/cloudmoo)

The Heroku Button provisions the Essential-0 Postgres and CloudAMQP RabbitMQ
add-ons and a web/worker/beat formation from [`heroku.yml`](heroku.yml). The
container detects Heroku's non-root runtime and runs Gunicorn directly on
`$PORT`, with static files served by WhiteNoise. `APP_DOMAIN` is optional for
the generated Heroku hostname; set it for a custom domain or stable
verification links. Heroku Buttons currently create Cedar/container apps, not
Fir apps.

### Railway template

Railway supports a true one-click flow through a published template. The
source-controlled service definitions and variable recipe are in
[`deploy/railway/`](deploy/railway/). Follow its short setup once, generate a
template from the configured project, and use the generated template URL for a
Railway button. The URL is account-owned infrastructure, so it cannot be
invented safely in source control.

### Any VPS: DigitalOcean, Hetzner, Linode, Vultr, UpCloud, or AWS

```bash
curl -fsSL https://raw.githubusercontent.com/bilal414/cloudmoo/main/install.sh | sudo bash
```

The installer provisions Docker and the complete Compose stack under
`/opt/cloudmoo` on Debian/Ubuntu. It is suitable for a fresh VM from the
providers above. Pass `--domain` to configure the public hostname:

```bash
curl -fsSL https://raw.githubusercontent.com/bilal414/cloudmoo/main/install.sh | sudo bash -s -- --domain monitors.example.com
```

For first-boot automation, paste
[`deploy/cloud-init/cloudmoo.yaml`](deploy/cloud-init/cloudmoo.yaml) into the
provider's cloud-init/user-data field. The quick-start installer exposes HTTP
on port 8000; put the VM behind a TLS reverse proxy before exposing it to the
public internet, and pin `--branch` to a release tag for reproducible installs.

## Configuration

All configuration is via environment variables or `.env` — see the fully
documented [`.env.example`](.env.example). Highlights:

| Variable | Purpose | Default |
| --- | --- | --- |
| `DJANGO_SECRET_KEY` | Django secret key (**required**) | — |
| `DJANGO_DEBUG` | Debug mode — `false` in production | `false` |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated hostnames | — |
| `HTTPS_ENABLED` | Secure cookies, HSTS, SSL redirect | `false` |
| `DB_CONN_MAX_AGE` | Persistent PostgreSQL connection lifetime in seconds | `0` in debug / `60` in production |
| `DB_CONNECT_TIMEOUT` | PostgreSQL connection timeout in seconds | driver default |
| `DB_SSLMODE` | Optional PostgreSQL TLS mode | driver default |
| `RATE_LIMIT_TRUST_X_FORWARDED_FOR` | Trust a controlled proxy's client-address header for login throttling | `false` |
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

The default suite is mocked and never creates provider resources. Live E2E
harnesses are separate, explicit commands and require credentials through
environment variables or an ignored local configuration file. See
[`tests/README.md`](tests/README.md); never place real credentials in the
tracked `tests/test_accounts.json` fixture.

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
