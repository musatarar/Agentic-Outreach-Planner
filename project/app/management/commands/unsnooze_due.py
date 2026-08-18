"""Return snoozed triage items to the queue when they are due (MUS-39).

Safe to run every minute: both filters require ``status="snoozed"``, which the
update itself clears, so a concurrent second run finds nothing.
"""

from django.core.management.base import BaseCommand
from django.db.models import Exists, OuterRef
from django.utils import timezone

from project.app.models import Event, OutreachAction


class Command(BaseCommand):
    help = "Return due snoozed outreach actions to the pending triage queue."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be unsnoozed without writing anything.",
        )

    def handle(self, *args, **options):
        now = timezone.now()
        dry_run = options["dry_run"]

        # 1. Time-based. Also catches the on_activity backstop, so a lead that
        #    never does anything still comes back.
        due = OutreachAction.objects.filter(
            status=OutreachAction.STATUS_SNOOZED, snooze_until__lte=now
        )

        # 2. Activity-based, against the watermark captured at snooze time, so
        #    historical events cannot wake it.
        woke = Event.objects.filter(
            lead=OuterRef("lead"), timestamp__gt=OuterRef("snooze_activity_after")
        )
        on_activity = (
            OutreachAction.objects.filter(
                status=OutreachAction.STATUS_SNOOZED,
                snooze_trigger=OutreachAction.TRIGGER_ON_ACTIVITY,
                snooze_activity_after__isnull=False,
            )
            .filter(Exists(woke))
            .exclude(snooze_until__lte=now)  # already counted as due above
        )

        if dry_run:
            n_due, n_activity = due.count(), on_activity.count()
        else:
            n_due = due.update(**self._woken(now))
            n_activity = on_activity.update(**self._woken(now))

        verb = "would unsnooze" if dry_run else "unsnoozed"
        self.stdout.write(f"{verb} {n_due + n_activity} ({n_due} due, {n_activity} on activity)")

    @staticmethod
    def _woken(now):
        return {
            "status": OutreachAction.STATUS_PENDING,
            "status_changed_at": now,
            "snooze_until": None,
            "snooze_trigger": "",
            "snooze_activity_after": None,
        }
