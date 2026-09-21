"""Load the raw lead and event files as they stand.

Nothing here interprets a column: a lead row minus its id is the lead's blob,
and an event's payload is flattened beside its own keys. What those keys mean
is the owner's shape to say.
"""

import json
from datetime import datetime

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils.dateparse import parse_datetime
from django.utils.timezone import is_naive, make_aware

from project.app.models import Event, Lead

DEFAULT_LEADS = "raw_data/leads.json"
DEFAULT_EVENTS = "raw_data/events.json"

# The two structural keys: the lead's own id, and the event's timestamp plus
# the nested payload that is flattened beside its siblings.
LEAD_ID_KEY = "id"
EVENT_TIMESTAMP_KEY = "timestamp"
EVENT_META_KEY = "meta"


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
            data = {key: value for key, value in row.items() if key != LEAD_ID_KEY}
            Lead.objects.update_or_create(id=row[LEAD_ID_KEY], defaults={"data": data})
            lead_count += 1

        event_count = 0
        for block in events_data:
            lead_id = block["lead_id"]
            # Idempotent: clear and recreate events per lead.
            Event.objects.filter(lead_id=lead_id).delete()
            for ev in block.get("events", []):
                data = {
                    key: value
                    for key, value in ev.items()
                    if key not in (EVENT_TIMESTAMP_KEY, EVENT_META_KEY)
                }
                data.update(ev.get(EVENT_META_KEY) or {})
                Event.objects.create(
                    lead_id=lead_id,
                    timestamp=_parse_timestamp(ev[EVENT_TIMESTAMP_KEY]),
                    data=data,
                )
                event_count += 1

        self.stdout.write(
            self.style.SUCCESS(f"Ingested {lead_count} leads and {event_count} events.")
        )
