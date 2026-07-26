#!/bin/bash

# Start nginx (serves /static and proxies to gunicorn)
service nginx start

python manage.py collectstatic --noinput
python manage.py migrate
python manage.py createcachetable || true

gunicorn app_cloudmoo_com.wsgi:application --workers=4 --timeout=3600 --bind 0.0.0.0:8000
