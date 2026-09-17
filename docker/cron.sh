#!/usr/bin/env sh
# The actions engine's scheduler: one `run_action_jobs` tick every
# ACTIONS_CRON_INTERVAL_SECONDS, forever. There is no system cron in the image;
# this loop is the whole of it, and it shares the web image so there is one build.

INTERVAL="${ACTIONS_CRON_INTERVAL_SECONDS:-300}"

# The web container owns migrations and seeding. A tick before those land would
# fail on a missing table, so wait for the schema rather than racing it.
until python manage.py migrate --check >/dev/null 2>&1; do
    echo "actions cron: waiting for migrations"
    sleep 2
done

echo "actions cron: a tick every ${INTERVAL}s"
while true; do
    # A failed tick must not end the scheduler: the next one retries, and the
    # engine already records per-job failures on the jobs themselves.
    python manage.py run_action_jobs || echo "actions cron: tick failed, retrying next interval"
    sleep "$INTERVAL"
done
