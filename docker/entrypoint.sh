#!/usr/bin/env sh
# Container startup: apply migrations, seed the demo pipeline, then serve.
# Both steps are idempotent, so restarting the container is safe. Never add
# --reset here: it empties every table, and this runs on every restart.
set -e

python manage.py migrate --noinput
python scripts/populate_demo_data.py

# This is the Django dev server, not a production setup. --insecure makes it
# serve the committed React bundle even with DEBUG off; compose interpolates
# .env, so whether DEBUG is off is the operator's setting, and without the flag
# every page would render blank whenever it is.
exec python manage.py runserver 0.0.0.0:8000 --insecure
