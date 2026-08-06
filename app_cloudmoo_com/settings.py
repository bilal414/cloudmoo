"""
Django settings for the CloudMoo project.

Configuration is resolved in this order:
1. CLOUDMOO_SECRETS environment variable (JSON blob)
2. Environment variables
3. .env file in the project root (local development)

See .env.example for a documented list of all variables.
"""
import json
import os
from urllib.parse import quote, urlparse

from celery.schedules import crontab
from dotenv import dotenv_values

# Build paths inside the project like this: BASE_DIR / 'subdir'.
ROOT_PATH = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(os.path.dirname(__file__))

if "CLOUDMOO_SECRETS" in os.environ:
    config = json.loads(os.environ.get("CLOUDMOO_SECRETS"))
else:
    config = {
        **dotenv_values(".env"),  # load shared development variables
        **os.environ,  # override loaded values with environment variables
    }


def env_bool(key, default=False):
    """Read a boolean flag from the resolved configuration."""
    value = config.get(key)
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def env_list(key, default=None):
    """Read a comma-separated list from the resolved configuration."""
    value = config.get(key)
    if not value:
        return default if default is not None else []
    return [item.strip() for item in str(value).split(",") if item.strip()]


# ---------------------------------------------------------------------------
# Public URL
# ---------------------------------------------------------------------------
# APP_DOMAIN remains the explicit override. The platform fallbacks make a
# first deployment usable without asking users to copy a generated hostname
# into another field on Render, Railway, or Heroku.
APP_DOMAIN = (
    config.get("APP_DOMAIN")
    or config.get("RENDER_EXTERNAL_HOSTNAME")
    or config.get("RAILWAY_PUBLIC_DOMAIN")
    or config.get("HEROKU_APP_DEFAULT_DOMAIN_NAME")
    or (
        f"{config['HEROKU_APP_NAME']}.herokuapp.com"
        if config.get("HEROKU_APP_NAME")
        else None
    )
    or "localhost:8000"
)
APP_PROTOCOL = config.get("APP_PROTOCOL", "http://")
APP_URL = f"{APP_PROTOCOL}{APP_DOMAIN}"
APP_HOSTNAME = str(APP_DOMAIN).split(":", 1)[0].strip()


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
SECRET_KEY = config["DJANGO_SECRET_KEY"]
DEBUG = env_bool("DJANGO_DEBUG", default=False)
DJANGO_SERVER = config.get("DJANGO_SERVER", "development")
DEFAULT_ALLOWED_HOSTS = (
    ["localhost", "127.0.0.1"]
    if DEBUG
    else ([APP_HOSTNAME] if APP_HOSTNAME not in {"", "localhost", "127.0.0.1"} else [])
)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", default=DEFAULT_ALLOWED_HOSTS)
if APP_HOSTNAME not in {"", "localhost", "127.0.0.1"} and APP_HOSTNAME not in ALLOWED_HOSTS:
    # APP_DOMAIN is the public URL override, so include it even when a
    # platform manifest supplies a broader default such as .herokuapp.com.
    ALLOWED_HOSTS.append(APP_HOSTNAME)
DEFAULT_CSRF_TRUSTED_ORIGINS = (
    [APP_URL]
    if APP_DOMAIN != "localhost:8000" and APP_PROTOCOL in {"http://", "https://"}
    else []
)
CSRF_TRUSTED_ORIGINS = env_list(
    "DJANGO_CSRF_TRUSTED_ORIGINS", default=DEFAULT_CSRF_TRUSTED_ORIGINS
)

HTTPS_ENABLED = env_bool("HTTPS_ENABLED", default=False)
if HTTPS_ENABLED:
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    # Trust the proxy's protocol header (needed behind PaaS load balancers)
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "rest_framework",
    "rest_framework.authtoken",
    "django.contrib.humanize",
    "django_celery_beat",
    "apps",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "app_cloudmoo_com.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [
            BASE_DIR + "/apps/console/_templates/",
        ],
        "APP_DIRS": True,
        "OPTIONS": {
            "debug": DEBUG,
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "django.template.context_processors.static",
                "django.template.context_processors.media",
                "django.template.context_processors.tz",
                "django.template.context_processors.i18n",
                "apps.console.utils.context_processors.active_url_processor",
                "apps.console.utils.context_processors.recaptcha_settings",
            ],
        },
    },
]

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "core_cache",
    }
}

WSGI_APPLICATION = "app_cloudmoo_com.wsgi.application"

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
]

REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_PARSER_CLASSES": (
        "rest_framework.parsers.JSONParser",
        "rest_framework.parsers.MultiPartParser",
    ),
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework.authentication.SessionAuthentication",
        "apps.api.v1.utils.api_authentication.CustomTokenAuthentication",
    ),
    "DEFAULT_RENDERER_CLASSES": (
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ),
    "EXCEPTION_HANDLER": "rest_framework.views.exception_handler",
}

# Database
# https://docs.djangoproject.com/en/4.2/ref/settings/#databases
MIGRATION_MODULES = {"apps": "apps._migrations"}

DATABASES = {
    "default": {
        "ENGINE": config.get("DB_ENGINE", "django.db.backends.postgresql"),
        "NAME": config.get("DB_NAME", "cloudmoo"),
        "USER": config.get("DB_USER", "cloudmoo"),
        "PASSWORD": config.get("DB_PASSWORD", ""),
        "HOST": config.get("DB_HOST", "localhost"),
        "PORT": config.get("DB_PORT", "5432"),
    },
}

# Heroku/Render provide a single DATABASE_URL; when present it overrides the
# individual DB_* settings above. Optional DB_SSLMODE (e.g. "require") is
# passed through to the PostgreSQL backend.
DATABASE_URL = config.get("DATABASE_URL")
if DATABASE_URL:
    _db_url = urlparse(DATABASE_URL)
    if _db_url.scheme in ("postgres", "postgresql"):
        DATABASES["default"].update({
            "ENGINE": "django.db.backends.postgresql",
            "NAME": _db_url.path.lstrip("/"),
            "USER": _db_url.username or "",
            "PASSWORD": _db_url.password or "",
            "HOST": _db_url.hostname or "",
            "PORT": str(_db_url.port or 5432),
        })
        if config.get("DB_SSLMODE"):
            DATABASES["default"]["OPTIONS"] = {"sslmode": config["DB_SSLMODE"]}

# Password validation
# https://docs.djangoproject.com/en/4.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

# Internationalization
# https://docs.djangoproject.com/en/4.2/topics/i18n/

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
DATETIME_FORMAT = '%d-%m-%Y %H:%M:%S'

# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/4.2/howto/static-files/

STATICFILES_FINDERS = (
    "django.contrib.staticfiles.finders.FileSystemFinder",
    "django.contrib.staticfiles.finders.AppDirectoriesFinder",
)

STATIC_URL = "/static/"

STATIC_ROOT = BASE_DIR + "/static/"

STATICFILES_DIRS = (
    ("console", BASE_DIR + "/apps/console/_static/console"),
)

# Default primary key field type
# https://docs.djangoproject.com/en/4.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Email
# Any Django email backend works. For production set SMTP_* variables or
# install django-ses and use EMAIL_BACKEND=django_ses.SESBackend.
# ---------------------------------------------------------------------------
EMAIL_BACKEND = config.get(
    "EMAIL_BACKEND",
    "django.core.mail.backends.console.EmailBackend" if DEBUG
    else "django.core.mail.backends.smtp.EmailBackend",
)
EMAIL_HOST = config.get("SMTP_HOST", "localhost")
EMAIL_PORT = int(config.get("SMTP_PORT", "25"))
EMAIL_HOST_USER = config.get("SMTP_USER", "")
EMAIL_HOST_PASSWORD = config.get("SMTP_PASSWORD", "")
EMAIL_USE_TLS = env_bool("SMTP_USE_TLS", default=False)
EMAIL_USE_SSL = env_bool("SMTP_USE_SSL", default=False)
DEFAULT_FROM_EMAIL = config.get("DEFAULT_FROM_EMAIL", "CloudMoo <noreply@example.com>")
EMAIL_SUBJECT_PREFIX = "CloudMoo"

# ---------------------------------------------------------------------------
# Error tracking (optional — enabled only when SENTRY_DSN is set)
# ---------------------------------------------------------------------------
SENTRY_DSN = config.get("SENTRY_DSN", "")
if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        traces_sample_rate=float(config.get("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
        profiles_sample_rate=float(config.get("SENTRY_PROFILES_SAMPLE_RATE", "0.0")),
        integrations=[
            DjangoIntegration(
                transaction_style='url',
                middleware_spans=True,
                signals_spans=False,
                cache_spans=False,
            ),
        ],
        environment=DJANGO_SERVER,
    )

HOME_URL = "/console"
LOGIN_URL = "/login"
API_PATH = "/api/"
CONSOLE_URL = "/console"

# ---------------------------------------------------------------------------
# Registration
# Set REGISTRATION_OPEN=false to disable public sign-ups on your instance.
# ---------------------------------------------------------------------------
REGISTRATION_OPEN = env_bool("REGISTRATION_OPEN", default=True)

# ---------------------------------------------------------------------------
# Celery (monitoring engine task queue)
# Broker resolution order:
# 1. RABBITMQ_HOST — assemble the URL from the RABBITMQ_* parts
# 2. CELERY_BROKER_URL — explicit broker URL
# 3. CLOUDAMQP_URL — provided automatically by the CloudAMQP Heroku add-on
# 4. docker-compose default (internal rabbitmq service)
# ---------------------------------------------------------------------------
if config.get("RABBITMQ_HOST"):
    broker_user = quote(str(config.get("RABBITMQ_USER", "cloudmoo")), safe="")
    broker_password = quote(str(config.get("RABBITMQ_PASSWORD", "cloudmoo")), safe="")
    broker_vhost = quote(str(config.get("RABBITMQ_VHOST", "/")), safe="")
    CELERY_BROKER_URL = (
        f"amqp://{broker_user}:{broker_password}"
        f"@{config['RABBITMQ_HOST']}:{config.get('RABBITMQ_PORT', '5672')}"
        f"/{broker_vhost}"
    )
else:
    CELERY_BROKER_URL = (
        config.get("CELERY_BROKER_URL")
        or config.get("CLOUDAMQP_URL")
        or "amqp://cloudmoo:cloudmoo@rabbitmq:5672/%2F"
    )

CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
CELERY_BEAT_SCHEDULE = {
    "cloudmoo-retry-status-emails": {
        "task": "cloudmoo.retry_pending_status_emails",
        "schedule": crontab(minute="*"),
    },
    "cloudmoo-prune-status-logs": {
        "task": "cloudmoo.prune_status_logs",
        "schedule": crontab(hour=3, minute=17),
    },
}

# Shared secret for the external sync webhook (X-API-KEY header)
CLOUDMOO_API_KEY = config.get("CLOUDMOO_API_KEY", "")

# Google reCAPTCHA (optional — skipped when not configured)
RECAPTCHA_PUBLIC_KEY = config.get("RECAPTCHA_PUBLIC_KEY", "")
RECAPTCHA_PRIVATE_KEY = config.get("RECAPTCHA_PRIVATE_KEY", "")

# CloudMoo
COMPANY_NAME = "CloudMoo"
