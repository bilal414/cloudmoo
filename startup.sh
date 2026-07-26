#!/bin/bash
python3 manage.py migrate \
  && python3 manage.py createcachetable \
  && python3 manage.py collectstatic --noinput \
  && gunicorn app_cloudmoo_com.wsgi:application --workers=4 --timeout=3600
