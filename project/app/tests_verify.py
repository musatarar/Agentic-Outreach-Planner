"""Pure-Python tests for the grounding verifier (project.app.services.verify).

No Django, no database: leads are SimpleNamespace stubs mirroring the real data
shape, exactly like tests_logic.py. The verifier itself makes no LLM calls.

Run standalone:
    ./venv/bin/python -m unittest project.app.tests_verify
"""

import datetime
import unittest
from types import SimpleNamespace

from project.app.services import actions, verify

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
        agency_name="Summit Risk Advisors",
        contact_name="Priya Nair",
        contact_email="priya.nair@summitrisk.com",
        contact_phone="555-0000",
        state="CO",
        num_producers=4,
        years_in_business=12,
        estimated_book_size_usd=5_000_000,
        stage="active_trial",
        signed_up_date=None,
        last_login_date=None,
        quotes_created=8,
        quotes_submitted=3,
        deals_closed=4,
        last_contacted_date=None,
        hubspot_notes="",
        events=_EventSet([]),
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _kinds(violations):
    return {v.kind for v in violations}


def _verify(lead, copy, action_type=actions.NUDGE_USAGE, **kwargs):
    kwargs.setdefault("today", TODAY)
    return verify.verify_copy(lead, copy, action_type, **kwargs)


# ---------------------------------------------------------------------------
# The four MUS-22 acceptance cases (all fire at the default `standard` level)
# ---------------------------------------------------------------------------


class AcceptanceTests(unittest.TestCase):
    def test_inflated_deal_count(self):
        v = _verify(_lead(deals_closed=4), "Hi Priya,\nCongrats on your 47 closed deals!")
        self.assertIn("wrong_count", _kinds(v))
        self.assertTrue(any("47" in x.message and "4" in x.message for x in v))

    def test_invented_dollar_figure(self):
        v = _verify(
            _lead(estimated_book_size_usd=5_000_000),
            "Hi Priya,\nYour $47 million book is impressive.",
        )
        self.assertIn("unsupported_amount", _kinds(v))

    def test_wrong_contact_name(self):
        v = _verify(_lead(contact_name="Priya Nair"), "Hi David,\nA quick question for you.")
        self.assertIn("wrong_contact_name", _kinds(v))

    def test_unauthorized_discount_offer(self):
        v = _verify(
            _lead(),
            "Hi Priya,\nWe can offer 20% off your first year.",
            action_type=actions.REENGAGE_DORMANT,
        )
        self.assertIn("unauthorized_offer", _kinds(v))


# ---------------------------------------------------------------------------
# Counts
# ---------------------------------------------------------------------------


class CountTests(unittest.TestCase):
    def test_correct_counts_pass(self):
        lead = _lead(
            deals_closed=4,
            quotes_created=8,
            quotes_submitted=3,
            num_producers=4,
            years_in_business=12,
        )
        copy = (
            "Hi Priya,\nYou've got 4 closed deals, 8 quotes created, 3 quotes "
            "submitted, 4 producers, and 12 years in business."
        )
        self.assertEqual(_verify(lead, copy), [])

    def test_wrong_quotes_created(self):
        v = _verify(_lead(quotes_created=8), "Hi Priya,\nImpressive: 80 quotes created.")
        self.assertIn("wrong_count", _kinds(v))
        self.assertTrue(any("80 quotes created" in x.message for x in v))

    def test_created_before_number_phrasing(self):
        v = _verify(_lead(quotes_created=8), "Hi Priya,\nYou created 80 quotes.")
        self.assertIn("wrong_count", _kinds(v))

    def test_closed_before_number_phrasing(self):
        v = _verify(_lead(deals_closed=4), "Hi Priya,\nYou've closed 47 deals.")
        self.assertIn("wrong_count", _kinds(v))

    def test_deals_closed_after_number_phrasing(self):
        v = _verify(_lead(deals_closed=4), "Hi Priya,\nYou have 47 deals closed.")
        self.assertIn("wrong_count", _kinds(v))

    def test_wrong_producers(self):
        v = _verify(_lead(num_producers=4), "Hi Priya,\nYour team of 40 producers is huge.")
        self.assertIn("wrong_count", _kinds(v))

    def test_wrong_producers_you_have_phrasing(self):
        v = _verify(_lead(num_producers=4), "Hi Priya,\nYou have 40 producers on staff.")
        self.assertIn("wrong_count", _kinds(v))

    def test_producer_comparison_not_flagged(self):
        # "agencies with 50 producers" is a comparison, not a claim about *this*
        # lead's team, so the count must not be flagged.
        v = _verify(_lead(num_producers=4), "Hi Priya,\nEven agencies with 50 producers struggle.")
        self.assertEqual(v, [])

    def test_producer_hypothetical_not_flagged(self):
        # "as you add 2 producers" is a hypothetical, not a record claim.
        v = _verify(_lead(num_producers=4), "Hi Priya,\nAs you add 2 producers, we can help.")
        self.assertEqual(v, [])

    def test_year_old_phrasing(self):
        v = _verify(_lead(years_in_business=12), "Hi Priya,\nYour 30-year-old agency thrives.")
        self.assertIn("wrong_count", _kinds(v))

    def test_milestone_target_not_flagged(self):
        # Goals ("5-deal milestone", "1 deal short", "close 3 more deals") are
        # not claims about the record, so they must not be flagged.
        lead = _lead(deals_closed=4)
        copy = "Hi Priya,\nYou're just 1 deal short of the 5-deal milestone — close 3 more deals!"
        self.assertEqual(_verify(lead, copy), [])

    def test_incidental_numbers_not_flagged(self):
        copy = "Hi Priya,\nDo you have 15 minutes for a call in the next 2 weeks?"
        self.assertEqual(_verify(_lead(), copy), [])


# ---------------------------------------------------------------------------
# Goal / milestone framing (a *target*, not a claim about the record)
# ---------------------------------------------------------------------------


class GoalContextTests(unittest.TestCase):
    """A spaced milestone ("20 closed deals") appears verbatim in real HubSpot
    notes (lead_001: "if she hits 20 closed deals") and is exactly what a
    power-user email should echo, so goal-framed counts must not be flagged as
    contradictions — while genuine achievement claims still are."""

    def setUp(self):
        # deals_closed=6 like lead_001; the milestone is 20.
        self.lead = _lead(deals_closed=6, num_producers=4, quotes_submitted=14)

    def test_conditional_milestone_not_flagged(self):
        for copy in (
            "Hi Priya,\nExplore volume pricing once you hit 20 closed deals.",
            "Hi Priya,\nYou wanted volume pricing if you hit 20 closed deals.",
            "Hi Priya,\nWhen you reach 20 closed deals, let's talk pricing.",
        ):
            with self.subTest(copy=copy):
                self.assertEqual(
                    _verify(self.lead, copy, action_type=actions.POWER_USER_REWARD), []
                )

    def test_directional_milestone_not_flagged(self):
        for copy in (
            "Hi Priya,\nGreat progress toward 20 closed deals!",
            "Hi Priya,\nYou're on track to reach your goal of 20 closed deals.",
            "Hi Priya,\nYou're 14 deals away from 20 deals closed.",
        ):
            with self.subTest(copy=copy):
                self.assertEqual(
                    _verify(self.lead, copy, action_type=actions.POWER_USER_REWARD), []
                )

    def test_milestone_noun_after_count_not_flagged(self):
        copy = "Hi Priya,\nYou're nearing the 20 closed deals milestone."
        self.assertEqual(_verify(self.lead, copy, action_type=actions.POWER_USER_REWARD), [])

    def test_goal_framing_does_not_suppress_wrong_claim(self):
        # An achievement claim (even with a gerund) is still a claim, not a goal.
        for copy in (
            "Hi Priya,\nCongrats on hitting 47 closed deals!",
            "Hi Priya,\nAmazing — you reached 47 closed deals.",
        ):
            with self.subTest(copy=copy):
                self.assertIn(
                    "wrong_count",
                    _kinds(_verify(self.lead, copy, action_type=actions.POWER_USER_REWARD)),
                )

    def test_goal_in_prior_sentence_does_not_suppress_next(self):
        # A goal word in one sentence must not shield a wrong claim in the next.
        copy = "Hi Priya,\nYour goal is close. Congrats on your 47 closed deals!"
        self.assertIn(
            "wrong_count", _kinds(_verify(self.lead, copy, action_type=actions.POWER_USER_REWARD))
        )


# ---------------------------------------------------------------------------
# Currency amounts
# ---------------------------------------------------------------------------


class AmountTests(unittest.TestCase):
    def test_exact_and_rounded_book_size_pass(self):
        lead = _lead(estimated_book_size_usd=5_000_000)
        for figure in ("$5,000,000", "$5M", "$5 million", "$4.8M", "$5.2M"):
            with self.subTest(figure=figure):
                copy = f"Hi Priya,\nYour book of {figure} stands out."
                self.assertEqual(_verify(lead, copy), [])

    def test_wrong_magnitude_flagged(self):
        v = _verify(
            _lead(estimated_book_size_usd=5_000_000), "Hi Priya,\nYour $500K book is solid."
        )
        self.assertIn("unsupported_amount", _kinds(v))

    def test_event_premium_is_grounded(self):
        lead = _lead(
            events=_EventSet(
                [
                    _event(
                        "deal_closed",
                        datetime.datetime(2026, 5, 1, 9),
                        client="Acme",
                        premium=12000,
                    )
                ]
            )
        )
        copy = "Hi Priya,\nThat $12,000 premium on the Acme deal is great."
        self.assertEqual(_verify(lead, copy), [])

    def test_premium_as_string_is_grounded(self):
        lead = _lead(
            events=_EventSet(
                [_event("deal_closed", datetime.datetime(2026, 5, 1, 9), premium="$12,000")]
            )
        )
        self.assertEqual(_verify(lead, "Hi Priya,\nNice $12K premium there."), [])

    def test_no_grounded_amounts_skips_check(self):
        # A lead with no book size and no premiums has nothing to verify against.
        v = _verify(_lead(estimated_book_size_usd=None), "Hi Priya,\nA $999,999 figure appears.")
        self.assertNotIn("unsupported_amount", _kinds(v))


# ---------------------------------------------------------------------------
# Contact name (salutation)
# ---------------------------------------------------------------------------


class ContactNameTests(unittest.TestCase):
    def test_first_name_salutation_passes(self):
        self.assertEqual(_verify(_lead(contact_name="Priya Nair"), "Hi Priya,\nHello."), [])

    def test_full_name_salutation_passes(self):
        self.assertEqual(_verify(_lead(contact_name="Priya Nair"), "Hello Priya Nair,\nHello."), [])

    def test_generic_salutation_passes(self):
        self.assertEqual(_verify(_lead(contact_name="Priya Nair"), "Hi there,\nHello."), [])

    def test_honorific_plus_surname_passes(self):
        self.assertEqual(_verify(_lead(contact_name="Priya Nair"), "Dear Ms. Nair,\nHello."), [])

    def test_honorific_only_salutation_passes(self):
        # "Dear Sir," has nothing that could contradict the record.
        self.assertEqual(_verify(_lead(contact_name="Priya Nair"), "Dear Sir,\nHello."), [])

    def test_no_salutation_no_violation(self):
        self.assertEqual(
            _verify(_lead(contact_name="Priya Nair"), "A quick note about your account."), []
        )

    def test_missing_contact_name_skips_check(self):
        self.assertEqual(_verify(_lead(contact_name=""), "Hi David,\nHello."), [])


# ---------------------------------------------------------------------------
# Unauthorized offers
# ---------------------------------------------------------------------------


class OfferTests(unittest.TestCase):
    def test_percent_off_flagged(self):
        v = _verify(
            _lead(), "Hi Priya,\n20% off just for you.", action_type=actions.REENGAGE_DORMANT
        )
        self.assertIn("unauthorized_offer", _kinds(v))

    def test_free_months_flagged(self):
        v = _verify(
            _lead(), "Hi Priya,\nEnjoy two free months on us.", action_type=actions.NUDGE_USAGE
        )
        self.assertIn("unauthorized_offer", _kinds(v))

    def test_waive_flagged(self):
        v = _verify(
            _lead(), "Hi Priya,\nWe'll waive the setup fee.", action_type=actions.NUDGE_USAGE
        )
        self.assertIn("unauthorized_offer", _kinds(v))

    def test_authorized_for_power_user(self):
        v = _verify(
            _lead(), "Hi Priya,\n20% off as a thank you.", action_type=actions.POWER_USER_REWARD
        )
        self.assertEqual(v, [])

    def test_feel_free_not_flagged(self):
        v = _verify(
            _lead(), "Hi Priya,\nFeel free to reach out anytime.", action_type=actions.NUDGE_USAGE
        )
        self.assertEqual(v, [])

    def test_bare_percent_not_flagged(self):
        v = _verify(
            _lead(),
            "Hi Priya,\nYou saw a 20% jump in submissions.",
            action_type=actions.NUDGE_USAGE,
        )
        self.assertEqual(v, [])


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


class DateTests(unittest.TestCase):
    def test_iso_date_matching_record_passes(self):
        lead = _lead(last_login_date=datetime.date(2026, 6, 1))
        self.assertEqual(_verify(lead, "Hi Priya,\nGreat to see you on 2026-06-01."), [])

    def test_iso_date_not_in_record_flagged(self):
        lead = _lead(last_login_date=datetime.date(2026, 6, 1))
        v = _verify(lead, "Hi Priya,\nSince 2026-03-01 you've been quiet.")
        self.assertIn("unsupported_date", _kinds(v))

    def test_future_iso_date_allowed_at_standard(self):
        # A future ISO date is scheduling language, not a (mis)quoted record fact.
        self.assertEqual(_verify(_lead(), "Hi Priya,\nLet's meet on 2026-12-31."), [])

    def test_invalid_iso_date_ignored(self):
        self.assertEqual(_verify(_lead(), "Hi Priya,\nThe code 2026-13-45 is not a date."), [])

    def test_event_timestamp_grounds_date(self):
        lead = _lead(events=_EventSet([_event("login", datetime.datetime(2026, 5, 20, 9))]))
        self.assertEqual(_verify(lead, "Hi Priya,\nYour login on 2026-05-20 was a while ago."), [])


# ---------------------------------------------------------------------------
# Levels
# ---------------------------------------------------------------------------


class LevelTests(unittest.TestCase):
    def test_off_returns_no_violations(self):
        lead = _lead(deals_closed=4)
        copy = "Hi David,\nYour 47 closed deals and $99 million book are amazing. 20% off!"
        v = verify.verify_copy(
            lead, copy, actions.REENGAGE_DORMANT, level=verify.LEVEL_OFF, today=TODAY
        )
        self.assertEqual(v, [])

    def test_empty_copy_returns_no_violations(self):
        self.assertEqual(_verify(_lead(), ""), [])

    def test_standard_flags_every_concrete_contradiction(self):
        lead = _lead(deals_closed=4, estimated_book_size_usd=5_000_000, contact_name="Priya Nair")
        copy = "Hi David,\nYour 47 closed deals and $99 million book! 20% off just for you."
        kinds = _kinds(_verify(lead, copy, action_type=actions.REENGAGE_DORMANT))
        self.assertEqual(
            kinds, {"wrong_count", "unsupported_amount", "wrong_contact_name", "unauthorized_offer"}
        )


# ---------------------------------------------------------------------------
# Strict-only checks
# ---------------------------------------------------------------------------


class StrictTests(unittest.TestCase):
    def _strict(self, lead, copy):
        return verify.verify_copy(
            lead, copy, actions.NUDGE_USAGE, level=verify.LEVEL_STRICT, today=TODAY
        )

    def test_contact_first_name_absent_strict_only(self):
        lead = _lead(contact_name="Priya Nair")
        copy = "Hi there,\nSummit Risk Advisors is doing great in 2026."
        self.assertEqual(_verify(lead, copy), [])  # clean at standard
        self.assertIn("contact_name_absent", _kinds(self._strict(lead, copy)))

    def test_agency_name_absent_strict_only(self):
        lead = _lead(agency_name="Summit Risk Advisors", contact_name="Priya Nair")
        copy = "Hi Priya,\nYour agency is thriving in 2026."
        self.assertEqual(_verify(lead, copy), [])
        self.assertIn("agency_name_absent", _kinds(self._strict(lead, copy)))

    def test_unsupported_year_strict_only(self):
        lead = _lead(contact_name="Priya Nair", agency_name="Summit Risk Advisors")
        copy = "Hi Priya,\nSummit has grown a lot since 2005."
        self.assertEqual(_verify(lead, copy), [])
        self.assertIn("unsupported_year", _kinds(self._strict(lead, copy)))

    def test_strict_clean_copy_passes(self):
        lead = _lead(contact_name="Priya Nair", agency_name="Summit Risk Advisors")
        copy = "Hi Priya,\nSummit is thriving and we should reconnect in 2026."
        self.assertEqual(self._strict(lead, copy), [])

    def test_agency_of_only_stopwords_skips_agency_check(self):
        lead = _lead(agency_name="Insurance Group", contact_name="Priya Nair")
        v = self._strict(lead, "Hi Priya,\nHello there in 2026.")
        self.assertNotIn("agency_name_absent", _kinds(v))


# ---------------------------------------------------------------------------
# format_violations + de-duplication + internal helper edges
# ---------------------------------------------------------------------------


class FormatAndEdgeTests(unittest.TestCase):
    def test_format_empty(self):
        self.assertEqual(verify.format_violations([]), "")

    def test_format_lists_each_message(self):
        text = verify.format_violations(
            [
                verify.Violation("wrong_count", "msg one"),
                verify.Violation("unsupported_amount", "msg two"),
            ]
        )
        self.assertIn("msg one", text)
        self.assertIn("msg two", text)
        self.assertIn("Grounding check failed", text)

    def test_repeated_problem_deduped(self):
        lead = _lead(estimated_book_size_usd=5_000_000)
        v = _verify(lead, "Hi Priya,\nYour $47 million book, yes $47 million, is huge.")
        self.assertEqual(len(v), 1)

    def test_boolean_premium_ignored(self):
        lead = _lead(
            estimated_book_size_usd=5_000_000,
            events=_EventSet(
                [_event("deal_closed", datetime.datetime(2026, 5, 1, 9), premium=True)]
            ),
        )
        self.assertEqual(_verify(lead, "Hi Priya,\nYour $5M book is strong."), [])

    def test_non_numeric_premium_ignored(self):
        lead = _lead(
            estimated_book_size_usd=5_000_000,
            events=_EventSet(
                [_event("deal_closed", datetime.datetime(2026, 5, 1, 9), premium="n/a")]
            ),
        )
        self.assertIn(
            "unsupported_amount", _kinds(_verify(lead, "Hi Priya,\nA $700K deal closed."))
        )

    def test_coerce_number_edges(self):
        self.assertIsNone(verify._coerce_number(None))
        self.assertIsNone(verify._coerce_number("."))
        self.assertIsNone(verify._coerce_number("1.2.3"))
        self.assertEqual(verify._coerce_number(5), 5.0)
        self.assertEqual(verify._coerce_number("$1,234"), 1234.0)


if __name__ == "__main__":
    unittest.main()
