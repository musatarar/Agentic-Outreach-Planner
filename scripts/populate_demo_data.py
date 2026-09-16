"""Bootstrap Django and populate the database with demo data.

Single source of truth for demo state; run after `manage.py migrate`.
Idempotent: re-running refreshes leads and their events without duplicating.

Everything lands in one workspace, "demo", and every allowlisted operator
(LOGIN_ALLOWED_EMAILS) is enrolled in it -- so the quickstart still signs you
in to a book of leads you can see.
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

from django.conf import settings  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.core.management.base import CommandError  # noqa: E402

DEMO_TENANT_SLUG = "demo"
DEMO_TENANT_NAME = "Demo workspace"


def main():
    call_command("create_tenant", DEMO_TENANT_SLUG, name=DEMO_TENANT_NAME)
    call_command("ingest_data", tenant=DEMO_TENANT_SLUG)
    for email in sorted(settings.LOGIN_ALLOWED_EMAILS):
        try:
            call_command("add_tenant_member", DEMO_TENANT_SLUG, email)
        except CommandError as exc:
            # Already somewhere else: never move someone between workspaces
            # as a side effect of seeding demo data.
            print(f"Skipped {email}: {exc}")


if __name__ == "__main__":
    main()
