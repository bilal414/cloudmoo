"""
Django settings for the CloudMoo project.

Configuration is resolved in this order:
1. AWS_SECRETS environment variable (JSON blob, useful on AWS)
2. Environment variables
3. .env file in the project root (local development)

See .env.example for a documented list of all variables.
"""
import json
import os
from dotenv import dotenv_values

# Build paths inside the project like this: BASE_DIR / 'subdir'.
ROOT_PATH = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(os.path.dirname(__file__))

if "AWS_SECRETS" in os.environ:
    config = json.loads(os.environ.get("AWS_SECRETS"))
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
# Core
# ---------------------------------------------------------------------------
SECRET_KEY = config["DJANGO_SECRET_KEY"]
DEBUG = env_bool("DJANGO_DEBUG", default=False)
DJANGO_SERVER = config.get("DJANGO_SERVER", "development")
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"] if DEBUG else [])
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

HTTPS_ENABLED = env_bool("HTTPS_ENABLED", default=False)
if HTTPS_ENABLED:
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

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
    "apps",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
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

# App Domain
APP_DOMAIN = config.get("APP_DOMAIN", "localhost:8000")
APP_PROTOCOL = config.get("APP_PROTOCOL", "http://")
APP_URL = f"{APP_PROTOCOL}{APP_DOMAIN}"

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
# AWS (monitoring engine)
# Credentials are optional: when AWS_ACCESS_KEY / AWS_SECRET_ACCESS_KEY are
# empty, boto3 falls back to the default credential chain (IAM role, instance
# profile, ~/.aws/credentials, ...).
# ---------------------------------------------------------------------------
AWS_ACCESS_KEY = config.get("AWS_ACCESS_KEY", "")
AWS_SECRET_ACCESS_KEY = config.get("AWS_SECRET_ACCESS_KEY", "")
AWS_REGION = config.get("AWS_REGION", "us-east-1")
AWS_SCHEDULER_ROLE = config.get("AWS_SCHEDULER_ROLE", "")
AWS_LAMBDA_ASSET_STATUS = config.get("AWS_LAMBDA_ASSET_STATUS", "")
AWS_LAMBDA_CLOUD_SYNC_ASSETS = config.get("AWS_LAMBDA_CLOUD_SYNC_ASSETS", "")
AWS_LAMBDA_EMAIL_ASSET_STATUS_CHANGE = config.get("AWS_LAMBDA_EMAIL_ASSET_STATUS_CHANGE", "")

# DynamoDB table names used by the monitoring engine
AWS_DYNAMODB_ASSETS_TABLE = config.get("AWS_DYNAMODB_ASSETS_TABLE", "cloudmoo-assets")
AWS_DYNAMODB_ASSET_LOGS_TABLE = config.get("AWS_DYNAMODB_ASSET_LOGS_TABLE", "cloudmoo-asset-logs")
AWS_DYNAMODB_ASSET_EMAILS_TABLE = config.get("AWS_DYNAMODB_ASSET_EMAILS_TABLE", "cloudmoo-asset-emails")

# Shared API key for internal webhook calls (Lambda -> Django)
CLOUDMOO_API_KEY = config.get("CLOUDMOO_API_KEY", "")

# Google reCAPTCHA (optional — skipped when not configured)
RECAPTCHA_PUBLIC_KEY = config.get("RECAPTCHA_PUBLIC_KEY", "")
RECAPTCHA_PRIVATE_KEY = config.get("RECAPTCHA_PRIVATE_KEY", "")

# CloudMoo
COMPANY_NAME = "CloudMoo"
