"""Bootstrap Django and populate the database with demo data.

Single source of truth for demo state. Run after `manage.py migrate`:

    ./venv/bin/python scripts/populate_demo_data.py

Idempotent: re-running refreshes leads and their events without duplicating.
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


def main():
    call_command("ingest_data")


if __name__ == "__main__":
    main()
