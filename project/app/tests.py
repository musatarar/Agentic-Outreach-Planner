import json
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils.timezone import is_aware

from project.app.models import Event, Lead, OutreachAction
from project.app.services.outreach import plan_outreach


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


class PlanOutreachGroundingTests(TestCase):
    """plan_outreach runs the deterministic grounding verifier on generated
    copy and fails closed: a contradicted draft is kept but routed to the BD
    review queue (needs_human=True) with the specific problems spelled out."""

    def _make_lead(self, **kwargs):
        # demo_completed + no signup -> complete_onboarding (date-independent),
        # so classification stays deterministic regardless of the wall clock.
        defaults = dict(
            id="lead_ground",
            agency_name="Summit Risk Advisors",
            contact_name="Priya Nair",
            contact_email="priya.nair@summitrisk.com",
            contact_phone="555-0000",
            state="CO",
            num_producers=4,
            years_in_business=12,
            estimated_book_size_usd=5_000_000,
            stage="demo_completed",
            signed_up_date=None,
            deals_closed=4,
        )
        defaults.update(kwargs)
        return Lead.objects.create(**defaults)

    def test_contradicted_copy_is_flagged_and_draft_kept(self):
        self._make_lead(deals_closed=4)
        bad_copy = "Subject: Amazing work\n\nHi Priya,\n\nCongrats on your 47 closed deals!\n\nBest,\nThe Eventual team"
        with patch("project.app.services.outreach.generate_copy", return_value=bad_copy):
            planned = plan_outreach()

        self.assertEqual(len(planned), 1)
        action = OutreachAction.objects.get()
        self.assertTrue(action.needs_human)
        self.assertEqual(action.suggested_copy, bad_copy)  # draft is kept, not blanked
        self.assertIn("47 closed deals", action.further_action)
        self.assertIn("record shows 4", action.further_action)

    def test_grounded_copy_passes(self):
        self._make_lead()
        # Well-formed (subject, one CTA, sane length) AND grounded — must pass
        # both the shape gate (MUS-23) and the grounding gate (MUS-22).
        good_copy = (
            "Subject: Let's finish setting up\n\n"
            "Hi Priya,\n\n"
            "Summit Risk Advisors has a strong $5M book, and I'd hate to see that "
            "momentum stall before your account is live. Getting fully set up takes "
            "about fifteen minutes, and once it's done your producers can start "
            "protecting premiums right away. I know the demo covered a lot, so I'm "
            "happy to walk your team through the final steps personally and answer "
            "anything that came up afterward. Would you have time for a quick call "
            "this week to wrap up onboarding?\n\n"
            "Best,\nThe Eventual team"
        )
        with patch("project.app.services.outreach.generate_copy", return_value=good_copy):
            plan_outreach()

        action = OutreachAction.objects.get()
        self.assertFalse(action.needs_human)
        self.assertEqual(action.suggested_copy, good_copy)
        self.assertEqual(action.further_action, "")

    @override_settings(COPY_VERIFY_LEVEL="off")
    def test_verification_can_be_disabled_via_setting(self):
        self._make_lead(deals_closed=4)
        # Well-formed shape but a grounding contradiction (47 vs 4 deals). With
        # grounding disabled the contradiction is not flagged; the shape gate
        # (which is not governed by COPY_VERIFY_LEVEL) still passes.
        bad_copy = (
            "Subject: Your incredible momentum\n\n"
            "Hi Priya,\n\n"
            "Your 47 closed deals this quarter are genuinely incredible, and everyone "
            "on our side has noticed how quickly Summit Risk Advisors is moving. I "
            "wanted to write personally to say how great it has been watching your team "
            "put Premium Lock to work for your clients across the state. There is real "
            "momentum here, and I would be glad to help you keep it building with "
            "whatever comes next for the agency. Would you be open to a quick call this "
            "week to talk through what is ahead?\n\n"
            "Best,\nThe Eventual team"
        )
        with patch("project.app.services.outreach.generate_copy", return_value=bad_copy):
            plan_outreach()

        action = OutreachAction.objects.get()
        self.assertFalse(action.needs_human)  # verification off -> nothing flagged
        self.assertEqual(action.further_action, "")
