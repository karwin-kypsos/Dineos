#!/usr/bin/env bash
set -e

python manage.py migrate --noinput
daphne -b 0.0.0.0 -p "${PORT:-8000}" dineos.asgi:application
