#!/usr/bin/env sh
# Container startup: apply migrations, seed the demo pipeline, then serve.
# Both steps are idempotent, so restarting the container is safe.
set -e

python manage.py migrate --noinput
python scripts/populate_demo_data.py

exec python manage.py runserver 0.0.0.0:8000
