"""Bootstrap Django and populate the database with demo data.

Single source of truth for demo state; run after `manage.py migrate`. The
default run only seeds — `ingest_data` and `seed_rules_catalog` are both
idempotent, so it is safe to re-run and safe for a container to run on every
start.

`--reset` additionally empties EVERY table first: leads and events, but also
users, sessions, login tokens, and the review queue with its edits and
approvals. That is for starting a local demo from a clean slate, never for a
database anyone is using.
"""

import argparse
import os
import sys

# Make the project package importable when run as a standalone script.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "project.settings")

import django  # noqa: E402

django.setup()

from django.core.management import call_command  # noqa: E402


def populate_insurance_agent_demo_data(reset=False):
    if reset:
        call_command("flush", interactive=False)
    call_command("ingest_data")
    call_command("seed_rules_catalog")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Populate the demo database.")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Empty every table first, including users, sessions and login tokens.",
    )
    populate_insurance_agent_demo_data(**vars(parser.parse_args()))
