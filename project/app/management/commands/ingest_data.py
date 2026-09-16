import json
from datetime import date, datetime

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.dateparse import parse_datetime
from django.utils.timezone import is_naive, make_aware

from project.app.models import Event, Lead, Tenant

DEFAULT_LEADS = "raw_data/leads.json"
DEFAULT_EVENTS = "raw_data/events.json"

# Lead fields parsed as dates (ISO YYYY-MM-DD, nullable)
DATE_FIELDS = (
    "signed_up_date",
    "last_login_date",
    "last_contacted_date",
)

LEAD_FIELDS = (
    "agency_name",
    "contact_name",
    "contact_email",
    "contact_phone",
    "state",
    "num_producers",
    "years_in_business",
    "estimated_book_size_usd",
    "stage",
    "quotes_created",
    "quotes_submitted",
    "deals_closed",
    "hubspot_notes",
)


def _parse_date(value):
    if not value:
        return None
    return date.fromisoformat(value)


def _parse_timestamp(value):
    """Parse an ISO timestamp into a timezone-aware datetime (USE_TZ=True)."""
    dt = parse_datetime(value)
    if dt is None:
        # Fallback for plain dates used as timestamps.
        dt = datetime.fromisoformat(value)
    if is_naive(dt):
        dt = make_aware(dt)
    return dt


class Command(BaseCommand):
    help = "Ingest leads.json and events.json into Lead/Event models (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--leads", default=DEFAULT_LEADS, help="Path to leads JSON file.")
        parser.add_argument("--events", default=DEFAULT_EVENTS, help="Path to events JSON file.")
        parser.add_argument(
            "--tenant",
            default="",
            help="Slug of the workspace the ingested leads and events belong to (required).",
        )

    def _resolve(self, path):
        """Resolve a path relative to BASE_DIR when not absolute."""
        import os

        if os.path.isabs(path):
            return path
        return os.path.join(settings.BASE_DIR, path)

    @transaction.atomic
    def handle(self, *args, **options):
        # Required, not defaulted: an ingest that guessed the workspace would
        # silently publish one customer's book into another's.
        slug = (options.get("tenant") or "").strip()
        if not slug:
            raise CommandError("--tenant <slug> is required; ingest is always into a workspace.")
        tenant = Tenant.objects.filter(slug=slug).first()
        if tenant is None:
            raise CommandError(f'No workspace with slug "{slug}".')

        leads_path = self._resolve(options["leads"])
        events_path = self._resolve(options["events"])

        with open(leads_path, encoding="utf-8") as fh:
            leads_data = json.load(fh)
        with open(events_path, encoding="utf-8") as fh:
            events_data = json.load(fh)

        lead_count = 0
        for row in leads_data:
            defaults = {field: row.get(field) for field in LEAD_FIELDS}
            for field in DATE_FIELDS:
                defaults[field] = _parse_date(row.get(field))
            if defaults.get("hubspot_notes") is None:
                defaults["hubspot_notes"] = ""
            defaults["tenant"] = tenant
            # Lead ids are global (one CharField primary key, no surrogate key
            # yet), so an id already owned elsewhere is a collision, not an
            # update. The whole run rolls back -- the command is atomic.
            existing = Lead.objects.filter(pk=row["id"]).values_list("tenant__slug", flat=True)
            for owner in existing:
                if owner is not None and owner != tenant.slug:
                    raise CommandError(
                        f'Lead "{row["id"]}" already belongs to workspace "{owner}"; '
                        f'refusing to move it to "{tenant.slug}".'
                    )
            Lead.objects.update_or_create(id=row["id"], defaults=defaults)
            lead_count += 1

        event_count = 0
        owner_by_lead = dict(Lead.objects.values_list("pk", "tenant__slug"))
        for block in events_data:
            lead_id = block["lead_id"]
            owner = owner_by_lead.get(lead_id)
            if owner is not None and owner != tenant.slug:
                # The invariant event.tenant == event.lead.tenant is
                # service-level; no CHECK can express it, so the writer holds it.
                raise CommandError(
                    f'Lead "{lead_id}" already belongs to workspace "{owner}"; '
                    f'refusing to write its events into "{tenant.slug}".'
                )
            # Idempotent: clear and recreate events per lead.
            Event.objects.filter(lead_id=lead_id).delete()
            for ev in block.get("events", []):
                Event.objects.create(
                    lead_id=lead_id,
                    # Denormalized, and it must match the lead's tenant.
                    tenant=tenant,
                    type=ev["type"],
                    timestamp=_parse_timestamp(ev["timestamp"]),
                    meta=ev.get("meta", {}) or {},
                )
                event_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f'Ingested {lead_count} leads and {event_count} events into "{tenant.slug}".'
            )
        )
