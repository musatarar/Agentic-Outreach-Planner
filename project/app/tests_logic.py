"""Pure-Python tests for the outreach business logic.

No Django, no database: leads are SimpleNamespace stubs mirroring the real
data in `raw_data/leads.json` + `events.json`, and the
anthropic client is mocked.

Run standalone:
    ./venv/bin/python -m unittest project.app.tests_logic
"""

import datetime
import unittest
from types import SimpleNamespace
from unittest import mock

from project.app.services import actions, outreach

# Frozen "today" so the date-based rules are deterministic.
TODAY = datetime.date(2026, 6, 12)


class _EventSet:
    """Duck-types a Django related manager (`lead.events.all()`)."""

    def __init__(self, events):
        self._events = list(events)

    def all(self):
        return list(self._events)


def _event(type_, ts, **meta):
    return SimpleNamespace(type=type_, timestamp=ts, meta=meta)


def _lead(**kwargs):
    defaults = dict(
        id="lead_x",
        agency_name="Agency",
        contact_name="Contact",
        contact_email="contact@example.com",
        contact_phone="555-0000",
        state="CO",
        num_producers=1,
        years_in_business=1,
        estimated_book_size_usd=0,
        stage="active_trial",
        signed_up_date=None,
        last_login_date=None,
        quotes_created=0,
        quotes_submitted=0,
        deals_closed=0,
        last_contacted_date=None,
        hubspot_notes="",
        events=_EventSet([]),
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _dt(y, m, d, h=9):
    return datetime.datetime(y, m, d, h, 0)


# ---------------------------------------------------------------------------
# Stubs mirroring the five real leads
# ---------------------------------------------------------------------------


def priya():
    return _lead(
        id="lead_001",
        agency_name="Summit Risk Advisors",
        contact_name="Priya Nair",
        contact_email="priya.nair@summitrisk.com",
        state="CO",
        num_producers=4,
        years_in_business=6,
        estimated_book_size_usd=1_400_000,
        stage="active_trial",
        signed_up_date=datetime.date(2026, 4, 22),
        last_login_date=datetime.date(2026, 5, 26),
        quotes_created=19,
        quotes_submitted=14,
        deals_closed=6,
        last_contacted_date=datetime.date(2026, 5, 15),
        hubspot_notes=(
            "Priya is a power user. She's been running quotes constantly and "
            "her close rate is high. She mentioned wanting to understand volume "
            "pricing if she hits 20 closed deals. She's close."
        ),
        events=_EventSet(
            [
                _event(
                    "deal_closed",
                    _dt(2026, 5, 25),
                    client="Telluride Outfitters",
                    premium=9200,
                ),
                _event(
                    "call_logged",
                    _dt(2026, 5, 15),
                    duration_min=18,
                    notes="Priya asked about volume pricing again. She said she's "
                    "telling other agencies about us.",
                ),
            ]
        ),
    )


def tom():
    return _lead(
        id="lead_002",
        agency_name="Meridian Benefits & Insurance",
        contact_name="Tom Kladis",
        contact_email="tkladis@meridianbenefits.net",
        state="OH",
        num_producers=9,
        years_in_business=18,
        estimated_book_size_usd=5_600_000,
        stage="active_trial",
        signed_up_date=datetime.date(2026, 2, 28),
        last_login_date=datetime.date(2026, 5, 8),
        quotes_created=5,
        quotes_submitted=1,
        deals_closed=0,
        last_contacted_date=datetime.date(2026, 5, 1),
        hubspot_notes=(
            "Tom was enthusiastic early on but activity has slowed way down. "
            "Said in March he was waiting on Q2 budget approval to push it to "
            "his team. Q2 started. Haven't heard back."
        ),
        events=_EventSet(
            [
                _event(
                    "call_logged",
                    _dt(2026, 5, 1),
                    duration_min=8,
                    notes="Tom said Q2 budget is approved and he's planning to "
                    "bring it to his team in the next few weeks.",
                ),
                _event(
                    "email_sent",
                    _dt(2026, 5, 14),
                    subject="Checking in — ready to loop in your team?",
                    outcome="no_reply",
                ),
            ]
        ),
    )


def dana():
    return _lead(
        id="lead_003",
        agency_name="Bluegrass Coverage Group",
        contact_name="Dana Mosely",
        contact_email="dana@bluegrasscoverage.com",
        state="KY",
        num_producers=12,
        years_in_business=22,
        estimated_book_size_usd=8_100_000,
        stage="demo_completed",
        signed_up_date=None,
        last_login_date=None,
        quotes_created=0,
        quotes_submitted=0,
        deals_closed=0,
        last_contacted_date=datetime.date(2026, 4, 30),
        hubspot_notes=(
            "Demo went well. Dana liked the product but said she needs buy-in "
            "from her business partner Ray before moving forward. Said she'd "
            "follow up in May. Haven't heard back."
        ),
        events=_EventSet(
            [
                _event(
                    "demo_completed",
                    _dt(2026, 4, 29),
                    notes="Dana engaged throughout. Her business partner Ray wasn't on the call.",
                ),
                _event(
                    "email_sent",
                    _dt(2026, 5, 13),
                    subject="Following up from our demo",
                    outcome="no_reply",
                ),
            ]
        ),
    )


def derek():
    return _lead(
        id="lead_004",
        agency_name="Highline Group Insurance",
        contact_name="Derek Sohn",
        contact_email="derek.sohn@highlinegroup.com",
        state="WA",
        num_producers=8,
        years_in_business=11,
        estimated_book_size_usd=4_300_000,
        stage="active_trial",
        signed_up_date=datetime.date(2026, 5, 1),
        last_login_date=datetime.date(2026, 5, 27),
        quotes_created=2,
        quotes_submitted=0,
        deals_closed=0,
        last_contacted_date=datetime.date(2026, 5, 1),
        hubspot_notes=(
            "New trial signup. Derek found us through a LinkedIn post. Has "
            "logged in a few times but hasn't submitted anything yet. No "
            "contact since onboarding call."
        ),
        events=_EventSet(
            [
                _event(
                    "quote_created",
                    _dt(2026, 5, 21),
                    client="Pacific Rim Imports",
                    premium=8900,
                ),
                _event(
                    "onboarding_call",
                    _dt(2026, 5, 1),
                    duration_min=25,
                    notes="Derek is sharp. Said he had two clients in mind already.",
                ),
            ]
        ),
    )


def susan():
    return _lead(
        id="lead_005",
        agency_name="Lakewood & Associates",
        contact_name="Susan Lakewood",
        contact_email="susan@lakewoodassoc.com",
        state="MI",
        num_producers=15,
        years_in_business=27,
        estimated_book_size_usd=9_800_000,
        stage="active_trial",
        signed_up_date=datetime.date(2026, 4, 5),
        last_login_date=datetime.date(2026, 5, 25),
        quotes_created=7,
        quotes_submitted=3,
        deals_closed=2,
        last_contacted_date=datetime.date(2026, 5, 10),
        hubspot_notes=(
            "Susan is methodical. She's been using it steadily but carefully. "
            "She mentioned she wants to close 5 deals before committing to a "
            "paid plan. She's at 2. Seems like she just needs time and maybe a "
            "nudge."
        ),
        events=_EventSet(
            [
                _event(
                    "deal_closed",
                    _dt(2026, 5, 18),
                    client="Dearborn Auto Parts",
                    premium=5400,
                ),
                _event(
                    "call_logged",
                    _dt(2026, 5, 10),
                    duration_min=15,
                    notes="Susan is tracking toward her 5-deal target.",
                ),
            ]
        ),
    )


# ---------------------------------------------------------------------------
# Priority
# ---------------------------------------------------------------------------


class DeterminePriorityTests(unittest.TestCase):
    def test_tom_is_priority_1(self):
        # $5.6M book, said budget was approved, then went quiet (email no_reply).
        self.assertEqual(outreach.determine_priority(tom(), today=TODAY), 1)

    def test_dana_is_priority_1(self):
        # $8.1M book, demo'd, never signed up, promised May follow-up — overdue.
        self.assertEqual(outreach.determine_priority(dana(), today=TODAY), 1)

    def test_engaged_leads_are_medium_priority(self):
        for lead in (priya(), derek(), susan()):
            with self.subTest(lead=lead.id):
                self.assertEqual(outreach.determine_priority(lead, today=TODAY), 2)

    def test_small_fresh_lead_is_priority_3(self):
        lead = _lead(
            estimated_book_size_usd=500_000,
            signed_up_date=TODAY - datetime.timedelta(days=3),
            last_login_date=TODAY - datetime.timedelta(days=1),
            last_contacted_date=TODAY - datetime.timedelta(days=2),
            quotes_created=1,
        )
        self.assertEqual(outreach.determine_priority(lead, today=TODAY), 3)

    def test_priority_in_valid_range_for_all_leads(self):
        for lead in (priya(), tom(), dana(), derek(), susan()):
            p = outreach.determine_priority(lead, today=TODAY)
            self.assertIn(p, (1, 2, 3))


# ---------------------------------------------------------------------------
# Action classification
# ---------------------------------------------------------------------------


class DetermineActionTests(unittest.TestCase):
    def test_priya_power_user_reward(self):
        action, reason = outreach.determine_action(priya(), today=TODAY)
        self.assertEqual(action, actions.POWER_USER_REWARD)
        self.assertIn("20", reason)  # milestone from the notes
        self.assertIn("6", reason)  # deals closed
        self.assertIn("volume pricing", reason.lower())

    def test_tom_follow_up_after_hold(self):
        action, reason = outreach.determine_action(tom(), today=TODAY)
        self.assertEqual(action, actions.FOLLOW_UP_AFTER_HOLD)
        self.assertIn("budget", reason.lower())  # quotes the hold note
        self.assertIn("no reply", reason.lower())

    def test_dana_complete_onboarding(self):
        action, reason = outreach.determine_action(dana(), today=TODAY)
        self.assertEqual(action, actions.COMPLETE_ONBOARDING)
        self.assertIn("8,100,000", reason)  # big book is the why-now
        self.assertIn("never signed up", reason)

    def test_derek_nudge_usage(self):
        action, reason = outreach.determine_action(derek(), today=TODAY)
        self.assertEqual(action, actions.NUDGE_USAGE)
        self.assertIn("never submitted", reason)
        self.assertIn("2", reason)  # quotes created

    def test_susan_nudge_usage_toward_commitment(self):
        action, reason = outreach.determine_action(susan(), today=TODAY)
        self.assertEqual(action, actions.NUDGE_USAGE)
        self.assertIn("5-deal", reason)  # commitment target from notes
        self.assertIn("3", reason)  # 3 deals short

    def test_dormant_lead_reengaged(self):
        lead = _lead(
            contact_name="Dormant Dan",
            signed_up_date=TODAY - datetime.timedelta(days=90),
            last_login_date=TODAY - datetime.timedelta(days=45),
            quotes_created=4,
            quotes_submitted=2,
            deals_closed=1,
            last_contacted_date=TODAY - datetime.timedelta(days=10),
        )
        action, reason = outreach.determine_action(lead, today=TODAY)
        self.assertEqual(action, actions.REENGAGE_DORMANT)
        self.assertIn("45 days", reason)

    def test_unmatched_lead_is_unknown(self):
        lead = _lead(contact_name="Mystery Mo")  # no signup, no logins, no notes
        action, reason = outreach.determine_action(lead, today=TODAY)
        self.assertEqual(action, actions.UNKNOWN)
        self.assertIn("Mystery Mo", reason)
        self.assertIn("review", reason.lower())

    def test_all_returned_actions_are_valid(self):
        for lead in (priya(), tom(), dana(), derek(), susan(), _lead()):
            action, reason = outreach.determine_action(lead, today=TODAY)
            self.assertIn(action, actions.ACTION_TYPES)
            self.assertTrue(reason)


# ---------------------------------------------------------------------------
# Copy generation (LLM provider boundary mocked)
# ---------------------------------------------------------------------------
#
# generate_copy is now provider-agnostic: it builds the prompt and delegates to
# the configured LLM client. We mock get_llm_client so these tests stay
# independent of which provider config.toml selects. Provider adapters
# (Claude / OpenAI-compatible) are tested in tests_llm.py.


class GenerateCopyTests(unittest.TestCase):
    def test_generate_copy_builds_prompt_with_lead_context_and_delegates(self):
        lead = priya()
        fake_client = mock.Mock()
        fake_client.complete.return_value = "Subject: Volume pricing\n\nHi Priya, ..."

        with mock.patch.object(outreach, "get_llm_client", return_value=fake_client):
            result = outreach.generate_copy(
                lead, actions.POWER_USER_REWARD, "Priya is 14 deals from her milestone."
            )

        self.assertEqual(result, "Subject: Volume pricing\n\nHi Priya, ...")
        # generate_copy passes the prompt positionally and the token cap by name.
        args, kwargs = fake_client.complete.call_args
        self.assertEqual(kwargs["max_tokens"], outreach.MAX_COPY_TOKENS)
        prompt = args[0]
        self.assertIn(lead.hubspot_notes, prompt)  # notes in prompt
        self.assertIn(lead.contact_name, prompt)
        self.assertIn("Summit Risk Advisors", prompt)
        self.assertIn(actions.POWER_USER_REWARD, prompt)  # action type
        self.assertIn("Priya is 14 deals from her milestone.", prompt)  # reason
        self.assertIn("volume pricing", prompt)  # event note text

    def test_generate_copy_returns_client_text(self):
        lead = tom()
        fake_client = mock.Mock()
        fake_client.complete.return_value = "Subject: Budget approved!\n\nHi Tom, ..."

        with mock.patch.object(outreach, "get_llm_client", return_value=fake_client):
            result = outreach.generate_copy(lead, actions.FOLLOW_UP_AFTER_HOLD, "hold passed")

        self.assertEqual(result, "Subject: Budget approved!\n\nHi Tom, ...")


if __name__ == "__main__":
    unittest.main()
