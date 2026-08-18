"""Bulk synthetic leads for the planner benchmark (MUS-26e).

Templates come from ``evals/golden/leads.jsonl``, so the synthetic action-type
mix mirrors the labelled distribution -- including the ~7% that classify as
``unknown`` and skip generation entirely. IDs are ``synth_0001…``, which cannot
collide with demo or golden ids, so ``--flush`` can safely target that prefix.
"""

import datetime
import random
import sys
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from project.app.models import Event, Lead

ID_PREFIX = "synth_"

# `evals/` is a top-level package but is not on the path for a management
# command run from an installed project, so make sure the repo root is.
REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _golden_records():
    """The template pool, read through the rules eval's own loader.

    Reused rather than re-parsed so there is only one definition of what a
    valid golden record is.
    """
    from evals.run_rules_eval import GOLDEN_PATH, build_lead, load_golden

    return [(record, build_lead(record)) for record in load_golden(GOLDEN_PATH)]


class Command(BaseCommand):
    help = "Seed N synthetic leads modelled on the golden dataset (benchmark fixture)."

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=200, help="How many leads to create.")
        parser.add_argument("--seed", type=int, default=1234, help="RNG seed, for reproducibility.")
        parser.add_argument(
            "--flush",
            action="store_true",
            help=f"Delete existing leads whose id starts with '{ID_PREFIX}' first. "
            "Never touches leads created by any other source.",
        )

    def handle(self, *args, **options):
        count = options["count"]
        if count < 1:
            self.stderr.write("--count must be at least 1.")
            return
        rng = random.Random(options["seed"])
        templates = _golden_records()

        with transaction.atomic():
            if options["flush"]:
                deleted, _ = Lead.objects.filter(id__startswith=ID_PREFIX).delete()
                self.stdout.write(f"Flushed {deleted} synthetic row(s).")

            leads, events = self._build(count, templates, rng)
            Lead.objects.bulk_create(leads)
            Event.objects.bulk_create(events)

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {len(leads)} synthetic leads and {len(events)} events "
                f"from {len(templates)} golden templates."
            )
        )

    def _build(self, count, templates, rng):
        # Golden dates are relative to that harness's frozen TODAY; re-anchoring
        # on the real today preserves the labelled classification.
        today = datetime.date.today()
        from evals.run_rules_eval import TODAY as GOLDEN_TODAY

        shift = today - GOLDEN_TODAY

        leads = []
        events = []
        for index in range(1, count + 1):
            record, template = templates[(index - 1) % len(templates)]
            lead_id = f"{ID_PREFIX}{index:04d}"
            leads.append(
                Lead(
                    id=lead_id,
                    # Names vary per lead so the generated copy differs per row
                    # and the verifier has something real to ground against.
                    agency_name=f"{_AGENCY_NAMES[index % len(_AGENCY_NAMES)]} {index:04d}",
                    contact_name=f"{_FIRST_NAMES[index % len(_FIRST_NAMES)]} "
                    f"{_LAST_NAMES[(index // 7) % len(_LAST_NAMES)]}",
                    contact_email=f"contact{index:04d}@synthetic.test",
                    contact_phone="555-0100",
                    state=_STATES[index % len(_STATES)],
                    num_producers=template.num_producers,
                    years_in_business=template.years_in_business,
                    estimated_book_size_usd=template.estimated_book_size_usd,
                    stage=template.stage,
                    signed_up_date=_shift(template.signed_up_date, shift),
                    last_login_date=_shift(template.last_login_date, shift),
                    last_contacted_date=_shift(template.last_contacted_date, shift),
                    quotes_created=template.quotes_created,
                    quotes_submitted=template.quotes_submitted,
                    deals_closed=template.deals_closed,
                    hubspot_notes=template.hubspot_notes,
                )
            )
            for event in template.events.all():
                events.append(
                    Event(
                        lead_id=lead_id,
                        type=event.type,
                        timestamp=_shift_datetime(event.timestamp, shift),
                        meta=dict(event.meta or {}),
                    )
                )
            events.extend(_padding_events(lead_id, index, today))
        # Only shuffles which template lands on which id: a different seed
        # gives a different assignment with the same mix.
        rng.shuffle(leads)
        return leads, events


#: Extra events per lead, on top of whatever the golden template carried --
#: most golden records have none, and event-less leads would understate the
#: prefetch and give the grounding verifier nothing to walk.
PADDING_EVENTS_PER_LEAD = 3


def _padding_events(lead_id, index, today):
    """Neutral ``login`` events, chosen so they cannot change a classification.

    ``login`` is read by no action rule and these carry no ``meta``, so they
    feed neither the notes scan nor the no-reply check. Each is at least a day
    old, so they cannot make a dormant lead look active.
    """
    base = datetime.datetime.combine(today, datetime.time(9), tzinfo=datetime.timezone.utc)
    return [
        Event(
            lead_id=lead_id,
            type="login",
            timestamp=base - datetime.timedelta(days=(index % 30) + offset + 1),
            meta={},
        )
        for offset in range(PADDING_EVENTS_PER_LEAD)
    ]


def _shift(value, delta):
    return None if value is None else value + delta


def _shift_datetime(value, delta):
    if value is None:
        return timezone.now()
    shifted = value + delta
    if timezone.is_naive(shifted):
        return timezone.make_aware(shifted, datetime.timezone.utc)
    return shifted


_AGENCY_NAMES = (
    "Summit Risk Advisors",
    "Cascade Underwriters",
    "Harbor Point Insurance",
    "Ironwood Agency",
    "Clearwater Brokers",
    "Northgate Assurance",
    "Willow Creek Insurance",
)
_FIRST_NAMES = ("Priya", "Dana", "Marcus", "Elena", "Tomas", "Ruth", "Kofi", "Mei", "Alex")
_LAST_NAMES = ("Nair", "Whitfield", "Okafor", "Reyes", "Lindqvist", "Brennan", "Sato")
_STATES = ("CO", "CA", "TX", "NY", "WA", "FL", "IL", "OH")
