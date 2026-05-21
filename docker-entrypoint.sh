#!/bin/sh
set -e
cd /app/django_project
python manage.py migrate --noinput
exec "$@"
