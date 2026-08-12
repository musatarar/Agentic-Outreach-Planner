import json
from datetime import date, datetime, time, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils.dateparse import parse_datetime
from django.utils.timezone import is_naive, make_aware

from project.app.models import AEAvailabilitySlot, Event, Lead

DEFAULT_LEADS = "raw_data/leads.json"
DEFAULT_EVENTS = "raw_data/events.json"

# Synthetic AE calendar backing the agent loop's `check_ae_calendar` tool
# (MUS-29). Fixed names and a fixed anchor keep the seed deterministic; the
# anchor is the Monday after the newest demo event (2026-06-18), so slots sit
# in the demo's frozen date-space rather than drifting with the wall clock.
SYNTHETIC_AES = (
    ("Avery Collins", "avery.collins@lockedin.example"),
    ("Jordan Reyes", "jordan.reyes@lockedin.example"),
)
AE_SLOT_ANCHOR = date(2026, 6, 22)
AE_SLOT_HOURS = {"Avery Collins": 10, "Jordan Reyes": 14}

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

    def _resolve(self, path):
        """Resolve a path relative to BASE_DIR when not absolute."""
        import os

        if os.path.isabs(path):
            return path
        return os.path.join(settings.BASE_DIR, path)

    @transaction.atomic
    def handle(self, *args, **options):
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
            Lead.objects.update_or_create(id=row["id"], defaults=defaults)
            lead_count += 1

        event_count = 0
        for block in events_data:
            lead_id = block["lead_id"]
            # Idempotent: clear and recreate events per lead.
            Event.objects.filter(lead_id=lead_id).delete()
            for ev in block.get("events", []):
                Event.objects.create(
                    lead_id=lead_id,
                    type=ev["type"],
                    timestamp=_parse_timestamp(ev["timestamp"]),
                    meta=ev.get("meta", {}) or {},
                )
                event_count += 1

        slot_count = self._seed_ae_slots()

        self.stdout.write(
            self.style.SUCCESS(
                f"Ingested {lead_count} leads and {event_count} events; "
                f"seeded {slot_count} synthetic AE slots."
            )
        )

    def _seed_ae_slots(self):
        """Seed the synthetic AE calendar: one 30-minute slot per AE per
        weekday of the anchor week. Idempotent by delete-and-recreate of
        `synthetic=True` rows — never touches a manually created slot."""
        AEAvailabilitySlot.objects.filter(synthetic=True).delete()
        slots = []
        for day_offset in range(5):
            day = AE_SLOT_ANCHOR + timedelta(days=day_offset)
            for ae_name, ae_email in SYNTHETIC_AES:
                start = make_aware(datetime.combine(day, time(hour=AE_SLOT_HOURS[ae_name])))
                slots.append(
                    AEAvailabilitySlot(
                        ae_name=ae_name,
                        ae_email=ae_email,
                        slot_start=start,
                        slot_end=start + timedelta(minutes=30),
                        synthetic=True,
                    )
                )
        AEAvailabilitySlot.objects.bulk_create(slots)
        return len(slots)
