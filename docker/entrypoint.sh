#!/usr/bin/env sh
# Container startup: apply migrations, seed the demo pipeline, then serve.
# Both steps are idempotent, so restarting the container is safe.
set -e

python manage.py migrate --noinput
python scripts/populate_demo_data.py

# --insecure: the image runs with DEBUG off, and `runserver` then serves no
# static files -- so the committed React bundle 404s and every page renders
# blank. There is no static file server in front of it to take the job over.
exec python manage.py runserver 0.0.0.0:8000 --insecure
