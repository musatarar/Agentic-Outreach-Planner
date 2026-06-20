import json
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.test import TestCase
from django.utils.timezone import is_aware

from project.app.models import Event, Lead, OutreachAction


def _raw_json(name):
    with open(Path(settings.BASE_DIR) / "raw_data" / name, encoding="utf-8") as fh:
        return json.load(fh)


# Expected counts derived from the real seed files, so the tests keep passing
# as the dataset grows (new leads/events can be added without editing these).
EXPECTED_LEADS = len(_raw_json("leads.json"))
EXPECTED_EVENTS = sum(len(block.get("events", [])) for block in _raw_json("events.json"))


class IngestDataCommandTests(TestCase):
    """Ingestion command loads the real JSON fixtures correctly."""

    def test_loads_all_leads_and_their_events(self):
        call_command("ingest_data")

        self.assertEqual(Lead.objects.count(), EXPECTED_LEADS)
        self.assertEqual(Event.objects.count(), EXPECTED_EVENTS)

        # Spot-check lead_001 fields parse correctly.
        lead = Lead.objects.get(id="lead_001")
        self.assertEqual(lead.agency_name, "Summit Risk Advisors")
        self.assertEqual(lead.contact_email, "priya.nair@summitrisk.com")
        self.assertEqual(lead.state, "CO")
        self.assertEqual(lead.stage, "active_trial")
        self.assertEqual(lead.estimated_book_size_usd, 1400000)
        self.assertEqual(lead.deals_closed, 6)
        self.assertEqual(str(lead.signed_up_date), "2026-04-22")

        # Events attach to the right lead, with timezone-aware timestamps.
        self.assertEqual(lead.events.count(), 8)
        ts = lead.events.first().timestamp
        self.assertTrue(is_aware(ts))

        # Meta JSON survives the round trip.
        deal = lead.events.filter(type="deal_closed").first()
        self.assertIn("premium", deal.meta)

    def test_handles_null_date_fields(self):
        call_command("ingest_data")
        # lead_003 (demo_completed) has null signed_up_date / last_login_date.
        lead = Lead.objects.get(id="lead_003")
        self.assertIsNone(lead.signed_up_date)
        self.assertIsNone(lead.last_login_date)
        self.assertEqual(lead.events.count(), 2)

    def test_idempotent_no_duplicates(self):
        call_command("ingest_data")
        call_command("ingest_data")

        self.assertEqual(Lead.objects.count(), EXPECTED_LEADS)
        self.assertEqual(Event.objects.count(), EXPECTED_EVENTS)
        self.assertEqual(Lead.objects.get(id="lead_001").events.count(), 8)


class ModelBasicsTests(TestCase):
    def test_lead_str(self):
        lead = Lead.objects.create(
            id="lead_999",
            agency_name="Test Agency",
            contact_name="Jane Doe",
            contact_email="jane@test.com",
            contact_phone="555-0000",
            state="NY",
            num_producers=3,
            years_in_business=5,
            estimated_book_size_usd=1000000,
            stage="active_trial",
        )
        self.assertEqual(str(lead), "lead_999 - Test Agency")
        # Defaults applied.
        self.assertEqual(lead.quotes_created, 0)
        self.assertEqual(lead.hubspot_notes, "")

    def test_event_meta_defaults_to_dict(self):
        from django.utils import timezone

        lead = Lead.objects.create(
            id="lead_998",
            agency_name="A",
            contact_name="B",
            contact_email="b@a.com",
            contact_phone="1",
            state="CA",
            num_producers=1,
            years_in_business=1,
            estimated_book_size_usd=1,
            stage="active_trial",
        )
        event = Event.objects.create(lead=lead, type="login", timestamp=timezone.now())
        self.assertEqual(event.meta, {})

    def test_outreach_action_defaults_and_str(self):
        lead = Lead.objects.create(
            id="lead_997",
            agency_name="Acme",
            contact_name="C",
            contact_email="c@a.com",
            contact_phone="1",
            state="TX",
            num_producers=1,
            years_in_business=1,
            estimated_book_size_usd=1,
            stage="active_trial",
        )
        action = OutreachAction.objects.create(
            lead=lead,
            priority=1,
            action_type="nudge_usage",
            reason="Underusing the portal.",
        )
        self.assertFalse(action.needs_human)
        self.assertEqual(action.suggested_copy, "")
        self.assertEqual(str(action), "lead_997 - nudge_usage (p1)")
