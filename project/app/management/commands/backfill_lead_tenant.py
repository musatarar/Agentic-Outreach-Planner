"""Assign the owning user (tenant) to leads that do not have one yet.

The backfill half of the nullable-first column rule: migration 0002 adds
``Lead.tenant`` empty, this command fills it, and only once every environment
has run it does constraining the column become a sensible (human-reviewed) next
step. Idempotent, and never a reassignment: an already-owned lead is left
alone, because moving a book between tenants is a different decision than
naming its first owner.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from project.app.models import Lead


class Command(BaseCommand):
    help = "Set Lead.tenant on leads that have none (idempotent; never reassigns)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--user",
            required=True,
            help="Username (the magic-link flow uses the email address) of the owning user.",
        )
        parser.add_argument(
            "--lead-id",
            action="append",
            dest="lead_ids",
            help="Limit to this lead id; repeatable. Omit to take every unassigned lead.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be assigned and write nothing.",
        )

    def handle(self, *args, **options):
        user_model = get_user_model()
        username = options["user"]
        user = user_model.objects.filter(username=username).first()
        if user is None:
            raise CommandError(f"No user with username {username!r}.")

        # `tenant__isnull=True` is the idempotency guard AND the no-reassignment
        # rule: a second run, or a run naming an already-owned lead, is a no-op.
        unassigned = Lead.objects.filter(tenant__isnull=True)
        lead_ids = options.get("lead_ids")
        if lead_ids:
            unassigned = unassigned.filter(id__in=lead_ids)

        if options["dry_run"]:
            count = unassigned.count()
            self.stdout.write(f"Would assign {count} lead(s) to {username}.")
            return

        with transaction.atomic():
            count = unassigned.update(tenant=user)

        self.stdout.write(self.style.SUCCESS(f"Assigned {count} lead(s) to {username}."))
