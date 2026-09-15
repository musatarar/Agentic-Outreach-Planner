"""Seed the demo user's outreach rules catalog (idempotent, safe to re-run)."""

from django.core.management.base import BaseCommand

from project.app.rules.services import DEFAULT_OWNER_EMAIL, SeedRulesCatalog


class Command(BaseCommand):
    help = (
        "Seed one user's action/rule catalog (idempotent; resets that user's "
        "rules). Owner: --owner, else the first LOGIN_ALLOWED_EMAILS entry, "
        "else " + DEFAULT_OWNER_EMAIL + "."
    )

    def add_arguments(self, parser):
        parser.add_argument("--owner", help="Email of the user who owns the catalog.")

    def handle(self, *args, **options):
        result = SeedRulesCatalog().handle(owner_email=options.get("owner"))
        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {result['actions']} action types and {result['rules']} "
                f"rules for {result['owner']}."
            )
        )
