#!/bin/sh
set -eu

mkdir -p /app/staticfiles /app/media
chown -R shoplite:shoplite /app/staticfiles /app/media

gosu shoplite python manage.py migrate --noinput
gosu shoplite python manage.py collectstatic --noinput

exec gosu shoplite "$@"
