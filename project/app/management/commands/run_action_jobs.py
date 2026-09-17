"""The actions engine's cron entry point: fill the queue, then drain it.

One tick queues every lead that has no open job and runs up to --limit of them
through the deterministic and inference passes. Safe to run concurrently: jobs
are claimed with a conditional UPDATE, so two ticks never process the same one.
"""

from django.core.management.base import BaseCommand

from project.app.actions import services
from project.app.actions.models import ActionJob


class Command(BaseCommand):
    help = (
        "Run queued action jobs: pull the lead's tenant catalog, run the "
        "deterministic rules, then the (stubbed) inference pass, and record "
        "the action the weight tally chose."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=services.DEFAULT_BATCH_SIZE,
            help=f"Jobs to run this tick (default {services.DEFAULT_BATCH_SIZE}).",
        )
        parser.add_argument(
            "--no-enqueue",
            action="store_true",
            help="Drain the queue only; do not queue leads that have no open job.",
        )

    def handle(self, *args, **options):
        if not options["no_enqueue"]:
            queued = services.enqueue_pending_leads()
            self.stdout.write(f"queued {len(queued)} lead(s)")

        jobs = services.run_queue(limit=options["limit"])
        counts = {}
        for job in jobs:
            counts[job.status] = counts.get(job.status, 0) + 1
        self.stdout.write(f"ran {len(jobs)} job(s)")
        for status, _label in ActionJob.STATUS_CHOICES:
            if counts.get(status):
                self.stdout.write(f"  {status}: {counts[status]}")
