#!/usr/bin/env sh
# The actions engine's scheduler: one `run_action_jobs` tick every
# ACTIONS_CRON_INTERVAL_SECONDS, forever. There is no system cron in the image;
# this loop is the whole of it, and it shares the web image so there is one build.

INTERVAL="${ACTIONS_CRON_INTERVAL_SECONDS:-300}"

# ~2 minutes of polling: a cold start still migrating gets through, a real
# fault does not wait for it forever.
SCHEMA_ATTEMPTS=60

# Which database was checked, with any credentials stripped. An unset
# DATABASE_URL is the interesting case: settings.py falls back to a SQLite file
# that .dockerignore keeps out of the image, so the container checks an empty
# database forever while the web container happily migrates Postgres.
TARGET="$(printf '%s' "${DATABASE_URL:-}" | sed 's#://[^@]*@#://***@#')"
if [ -z "$TARGET" ]; then
    TARGET="DATABASE_URL unset -- the in-image SQLite fallback, which ships empty"
fi

# The web container applies the migrations (docker/entrypoint.sh) and a tick
# before they land would fail on a missing table. `migrate --check` fails the
# same way, and says nothing at all, for a database this container cannot reach
# or was never pointed at -- hence naming the target and printing its output.
attempt=0
until REASON="$(python manage.py migrate --check 2>&1)"; do
    attempt=$((attempt + 1))
    if [ "$attempt" -eq 1 ]; then
        echo "actions cron: waiting for the web container to apply migrations"
        echo "actions cron: checking $TARGET"
        if [ -n "$REASON" ]; then
            echo "$REASON"
        fi
    fi
    if [ "$attempt" -ge "$SCHEMA_ATTEMPTS" ]; then
        echo "actions cron: no usable schema after $attempt attempts, giving up"
        echo "actions cron: checked $TARGET"
        if [ -n "$REASON" ]; then
            echo "$REASON"
        fi
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
