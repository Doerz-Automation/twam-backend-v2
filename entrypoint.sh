#!/bin/bash

echo "Waiting for database..."

echo "Running migrations..."
python manage.py migrate --noinput --fake-initial

echo "Starting Gunicorn server for TWAM..."
exec gunicorn --bind 0.0.0.0:8000 twam_api.wsgi:application