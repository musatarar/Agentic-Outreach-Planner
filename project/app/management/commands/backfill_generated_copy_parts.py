"""Fill `subject`/`body` on rows written before the columns existed.

Idempotent: only rows carrying copy but no parts are touched, so a second run
is a no-op. Must run between migrations 0006 and 0007 -- the constraint 0007
adds refuses a draft with no parts behind it.
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q

from project.app.models import OutreachGeneratedCopy
from project.app.services.outreach import split_email

# Rows updated per transaction, so one long write does not hold a lock over the
# whole table.
BATCH_SIZE = 500


def unsplit_rows():
    """Rows with copy whose parts were never recorded, generated or edited."""
    return OutreachGeneratedCopy.objects.filter(
        (~Q(suggested_copy="") & Q(subject="", body=""))
        | (~Q(edited_copy="") & Q(edited_subject="", edited_body=""))
    ).order_by("pk")


class Command(BaseCommand):
    help = "Split stored outreach copy into its subject and body columns."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report how many rows would be split; write nothing.",
        )

    def handle(self, *args, **options):
        pending = unsplit_rows()
        if options["dry_run"]:
            self.stdout.write(f"{pending.count()} row(s) would be split")
            return

        split = 0
        # Walked by primary key rather than by re-reading the filter: copy that
        # is only whitespace splits to nothing and would still match it, so a
        # filter-driven loop could never finish.
        cursor = 0
        while True:
            batch = list(pending.filter(pk__gt=cursor)[:BATCH_SIZE])
            if not batch:
                break
            cursor = batch[-1].pk
            for row in batch:
                if row.suggested_copy and not (row.subject or row.body):
                    row.subject, row.body = split_email(row.suggested_copy)
                if row.edited_copy and not (row.edited_subject or row.edited_body):
                    row.edited_subject, row.edited_body = split_email(row.edited_copy)
            with transaction.atomic():
                OutreachGeneratedCopy.objects.bulk_update(
                    batch, ["subject", "body", "edited_subject", "edited_body"]
                )
            split += len(batch)
        self.stdout.write(f"split {split} row(s)")
