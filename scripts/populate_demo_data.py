"""Bootstrap Django and rebuild the demo database from scratch.

Single source of truth for demo state; run after `manage.py migrate`.
Empties the database first (all rows, including users and sessions), then
reseeds leads/events and the demo user's outreach rules catalog.
"""

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


def populate_insurance_agent_demo_data():
    call_command("flush", interactive=False)
    call_command("ingest_data")
    call_command("seed_rules_catalog")


if __name__ == "__main__":
    populate_insurance_agent_demo_data()
