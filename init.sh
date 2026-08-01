#!/bin/bash
# Entrypoint for all CloudMoo services (web, worker, beat, one-off commands).
#
# Role dispatch:
#   - With arguments (compose `command:`, PaaS start commands): run them instead of
#     the web path below. This is how the Celery worker, beat, and one-off commands
#     such as `python manage.py ...` reuse this image.
#   - Without arguments: web role. Render the nginx config for $PORT (default 80;
#     Heroku/Render/Railway assign $PORT), start nginx, collect static files, run
#     migrations unless the compose `migrate` service handles them
#     (SKIP_MIGRATIONS=true), then exec gunicorn on 0.0.0.0:8000 — nginx proxies
#     to it from $PORT.
set -e

if [ "$#" -gt 0 ]; then
  exec "$@"
fi

# Web role (default).
sed "s/__PORT__/${PORT:-80}/" /code/.nginx/default_80.conf > /etc/nginx/sites-available/default

# Start nginx (serves /static and proxies to gunicorn)
service nginx start

python manage.py collectstatic --noinput

if [ "${SKIP_MIGRATIONS:-false}" != "true" ]; then
  python manage.py migrate --noinput
  python manage.py createcachetable || true
fi

exec su -s /bin/sh cloudmoo -c \
  'HOME=/tmp exec gunicorn app_cloudmoo_com.wsgi:application --workers=4 --timeout=3600 --bind 0.0.0.0:8000'
