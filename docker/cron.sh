#!/usr/bin/env sh
# The actions engine's scheduler: one `run_action_jobs` tick every
# ACTIONS_CRON_INTERVAL_SECONDS, forever. There is no system cron in the image;
# this loop is the whole of it, and it shares the web image so there is one build.

INTERVAL="${ACTIONS_CRON_INTERVAL_SECONDS:-300}"

# ~2 minutes of polling: a cold start still migrating gets through, a real
# fault does not wait for it forever.
SCHEMA_ATTEMPTS=60

# The web container applies the migrations (docker/entrypoint.sh) and a tick
# before they land would fail on a missing table. `migrate --check` fails the
# same way for a database this container cannot reach, so print what it says
# rather than swallowing it -- otherwise every fault reads as "still migrating".
attempt=0
until REASON="$(python manage.py migrate --check 2>&1)"; do
    attempt=$((attempt + 1))
    if [ "$attempt" -eq 1 ]; then
        echo "actions cron: waiting for the web container to apply migrations"
        if [ -n "$REASON" ]; then
            echo "$REASON"
        fi
    fi
    if [ "$attempt" -ge "$SCHEMA_ATTEMPTS" ]; then
        echo "actions cron: no usable schema after $attempt attempts, giving up:"
        echo "$REASON"
        exit 1
    fi
    sleep 2
done

echo "actions cron: a tick every ${INTERVAL}s"
while true; do
    # A failed tick must not end the scheduler: the next one retries, and the
    # engine already records per-job failures on the jobs themselves.
    python manage.py run_action_jobs || echo "actions cron: tick failed, retrying next interval"
    sleep "$INTERVAL"
done
