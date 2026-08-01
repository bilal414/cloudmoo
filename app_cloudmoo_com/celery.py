import os

# Set the default Django settings module for the 'celery' program.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "app_cloudmoo_com.settings")

from celery import Celery
from django.conf import settings

app = Celery("cloudmoo")

# Using a string here means the worker doesn't have to serialize
# the configuration object to child processes.
# - namespace='CELERY' means all celery-related configuration keys
#   should have a `CELERY_` prefix.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Celery 5.x gives the process-level CELERY_BROKER_URL environment variable
# precedence through ``broker_read_url``/``broker_write_url``.  That can
# silently override the broker URL assembled by Django from RABBITMQ_* parts
# (a common failure mode in Docker deployments where an old .env value points
# at localhost).  Set the explicit read/write endpoints from the resolved
# Django setting so every Celery entry point uses the same broker.
app.conf.update(
    broker_read_url=settings.CELERY_BROKER_URL,
    broker_write_url=settings.CELERY_BROKER_URL,
)

# Monitoring engine tasks live in apps.monitoring (not a separate Django app,
# so autodiscovery alone would miss them).
app.conf.imports = ("apps.monitoring.tasks",)

# Load task modules from all registered Django apps.
app.autodiscover_tasks()
