"""Assign pre-tenancy rows to a workspace.

The new ``tenant`` columns ship nullable (CLAUDE.md: nullable-or-constant
default first, backfill via a management command, then constrain). This is that
backfill, and it is the only thing that writes those columns outside the normal
write paths. Idempotent: it only ever touches rows where ``tenant IS NULL``.

Events take their lead's tenant, not the argument, so the
``event.tenant == event.lead.tenant`` invariant holds even if leads were
already split across workspaces.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import OuterRef, Subquery

from project.app.models import Event, Lead, Tenant


class Command(BaseCommand):
    help = "Assign leads and events with no tenant to a workspace. Idempotent."

    def add_arguments(self, parser):
        parser.add_argument("slug", help="Workspace slug to assign orphaned rows to.")

    def handle(self, *args, **options):
        slug = options["slug"]
        tenant = Tenant.objects.filter(slug=slug).first()
        if tenant is None:
            raise CommandError(f'No workspace with slug "{slug}".')

        with transaction.atomic():
            leads = Lead.objects.filter(tenant__isnull=True).update(tenant=tenant)
            # One UPDATE with a subquery, so the event count does not become
            # one query per row: each event follows its own lead.
            events = Event.objects.filter(tenant__isnull=True).update(
                tenant=Subquery(Lead.objects.filter(pk=OuterRef("lead_id")).values("tenant_id")[:1])
            )

        self.stdout.write(
            self.style.SUCCESS(
                f'Backfilled {leads} leads and {events} events into "{tenant.slug}".'
            )
        )
