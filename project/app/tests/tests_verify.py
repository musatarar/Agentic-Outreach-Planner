"""Pure-Python tests for the grounding verifier (project.app.services.verify).

No database: leads are SimpleNamespace stubs carrying the demo shape, which is
what tells the verifier which columns are figures and which hold the two roles.
"""

import datetime
import unittest
from types import SimpleNamespace

from project.app.services import actions, verify
from project.app.tests.tests_shape_utils import shape

# Frozen "today" so the date-based rules are deterministic.
TODAY = datetime.date(2026, 6, 12)

SHAPE = shape()


class _EventSet:
    """Duck-types a Django related manager (`lead.events.all()`)."""

    def __init__(self, events):
        self._events = list(events)

    def all(self):
        return list(self._events)


def _event(type_, ts, **data):
    return SimpleNamespace(timestamp=ts, data=dict(data, type=type_))


# Every number column, so a test that wants nothing grounded can clear them all.
FIGURES = (
    "num_producers",
    "years_in_business",
    "estimated_book_size_usd",
    "quotes_created",
    "quotes_submitted",
    "deals_closed",
)


def _lead(**kwargs):
    events = kwargs.pop("events", _EventSet([]))
    data = dict(
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
    )
    data.update(kwargs)
    return SimpleNamespace(id="lead_x", data=data, shape=SHAPE, events=events)


def _no_figures(**kwargs):
    """A lead with only the number columns the caller names: nothing else for a
    figure to match against."""
    return _lead(**{name: None for name in FIGURES} | kwargs)


def _kinds(violations):
    return {v.kind for v in violations}


def _verify(lead, copy, action_type=actions.NUDGE_USAGE, **kwargs):
    kwargs.setdefault("today", TODAY)
    return verify.verify_copy(lead, copy, action_type, **kwargs)


# ---------------------------------------------------------------------------
# The four acceptance cases (all fire at the default `standard` level)
# ---------------------------------------------------------------------------


class AcceptanceTests(unittest.TestCase):
    def test_inflated_deal_count(self):
        v = _verify(_lead(deals_closed=4), "Hi Priya,\nCongrats on your 47 closed deals!")
        self.assertIn("wrong_count", _kinds(v))
        self.assertTrue(any("47" in x.message and "4" in x.message for x in v))

    def test_invented_dollar_figure(self):
        v = _verify(
            _no_figures(estimated_book_size_usd=5_000_000),
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
    """Every standalone integer is checked against every number column.

    Without a noun per column the verifier cannot bind "14 quotes" to
    ``quotes_submitted``, so a figure the record does not hold fails closed
    wherever it appears. Noisier than per-noun patterns, not weaker.
    """

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

    def test_a_figure_the_record_does_not_hold_is_flagged(self):
        v = _verify(_lead(quotes_created=8), "Hi Priya,\nImpressive: 80 quotes created.")
        self.assertIn("wrong_count", _kinds(v))
        self.assertTrue(any("80" in x.message for x in v))

    def test_a_figure_matching_any_number_column_passes(self):
        # 12 is `years_in_business`, not a quote count -- with no noun to bind
        # it, any number the record holds grounds it.
        self.assertEqual(_verify(_lead(), "Hi Priya,\nYou created 12 quotes."), [])

    def test_an_event_figure_grounds_a_count(self):
        lead = _lead(
            events=_EventSet(
                [_event("deal_closed", datetime.datetime(2026, 5, 1, 9), lock_term_months=24)]
            )
        )
        self.assertEqual(_verify(lead, "Hi Priya,\nA 24 month lock suits you."), [])

    def test_an_incidental_number_is_flagged_because_nothing_binds_it(self):
        # The documented cost of dropping per-column nouns: "15 minutes" is not
        # a claim about the record, but the verifier cannot tell.
        copy = "Hi Priya,\nDo you have 15 minutes for a call?"
        self.assertIn("wrong_count", _kinds(_verify(_lead(), copy)))

    def test_a_currency_amount_is_the_amount_check_not_a_count(self):
        lead = _lead(estimated_book_size_usd=5_000_000)
        self.assertEqual(_verify(lead, "Hi Priya,\nYour $5,000,000 book stands out."), [])

    def test_a_year_is_not_a_count(self):
        self.assertEqual(_verify(_no_figures(), "Hi Priya,\nA good 2026 so far."), [])

    def test_an_iso_date_is_not_a_count(self):
        lead = _lead(last_login_date=datetime.date(2026, 6, 1))
        self.assertEqual(_verify(lead, "Hi Priya,\nGreat to see you on 2026-06-01."), [])

    def test_a_lead_with_no_figures_skips_the_check(self):
        self.assertEqual(_verify(_no_figures(), "Hi Priya,\nYou've closed 47 deals."), [])


# ---------------------------------------------------------------------------
# Goal / milestone framing (a *target*, not a claim about the record)
# ---------------------------------------------------------------------------


class GoalContextTests(unittest.TestCase):
    """Goal-framed counts ("20 closed deals" as a target) are not contradictions, while
    genuine achievement claims still are."""

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
        lead = _no_figures(estimated_book_size_usd=5_000_000)
        for figure in ("$5,000,000", "$5M", "$5 million", "$4.8M", "$5.2M"):
            with self.subTest(figure=figure):
                copy = f"Hi Priya,\nYour book of {figure} stands out."
                self.assertEqual(_verify(lead, copy), [])

    def test_wrong_magnitude_flagged(self):
        v = _verify(
            _no_figures(estimated_book_size_usd=5_000_000), "Hi Priya,\nYour $500K book is solid."
        )
        self.assertIn("unsupported_amount", _kinds(v))

    def test_event_premium_is_grounded(self):
        lead = _no_figures(
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
        lead = _no_figures(
            events=_EventSet(
                [_event("deal_closed", datetime.datetime(2026, 5, 1, 9), premium="$12,000")]
            )
        )
        self.assertEqual(_verify(lead, "Hi Priya,\nNice $12K premium there."), [])

    def test_no_grounded_amounts_skips_check(self):
        # A lead with no number column filled has nothing to verify against.
        v = _verify(_no_figures(), "Hi Priya,\nA $999,999 figure appears.")
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
            _no_figures(deals_closed=20),
            "Hi Priya,\n20% off as a thank you.",
            action_type=actions.POWER_USER_REWARD,
        )
        self.assertEqual(v, [])

    def test_feel_free_not_flagged(self):
        v = _verify(
            _lead(), "Hi Priya,\nFeel free to reach out anytime.", action_type=actions.NUDGE_USAGE
        )
        self.assertEqual(v, [])

    def test_bare_percent_not_flagged(self):
        v = _verify(
            _no_figures(deals_closed=20),
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
# What the shape decides
# ---------------------------------------------------------------------------


class ShapeReadingTests(unittest.TestCase):
    """Nothing here names a column: which ones are figures, which are dates and
    which two hold the roles all come off the declaration."""

    def test_an_amount_grounds_against_any_declared_number_column(self):
        lead = _no_figures(num_producers=40)
        self.assertEqual(_verify(lead, "Hi Priya,\nA $40 charge appeared."), [])

    def test_a_date_grounds_against_any_declared_date_column(self):
        lead = _lead(last_contacted_date="2026-05-15")
        self.assertEqual(_verify(lead, "Hi Priya,\nWe last spoke on 2026-05-15."), [])

    def test_a_date_stored_as_iso_text_reads_as_a_date(self):
        # Ingest keeps dates as the raw file's strings.
        lead = _lead(signed_up_date="2026-04-22")
        self.assertEqual(_verify(lead, "Hi Priya,\nSince 2026-04-22 you've been with us."), [])

    def test_the_greeting_is_checked_against_the_column_the_contact_role_names(self):
        renamed = shape(
            lead_columns=[
                {"name": "who_we_call", "type": "text", "lead_authored": False},
                {"name": "the_agency", "type": "text", "lead_authored": False},
            ],
            roles={"contact_name": "who_we_call", "agency_name": "the_agency"},
        )
        lead = SimpleNamespace(
            id="lead_y",
            data={"who_we_call": "Priya Nair", "the_agency": "Summit Risk Advisors"},
            shape=renamed,
            events=_EventSet([]),
        )
        self.assertEqual(_verify(lead, "Hi Priya,\nHello."), [])
        self.assertIn("wrong_contact_name", _kinds(_verify(lead, "Hi David,\nHello.")))

    def test_the_agency_omission_check_reads_the_agency_role(self):
        lead = _lead(agency_name="Harbor & Main Insurance")
        strict = verify.verify_copy(
            lead,
            "Hi Priya,\nHello there.",
            actions.NUDGE_USAGE,
            level=verify.LEVEL_STRICT,
            today=TODAY,
        )
        self.assertIn("agency_name_absent", _kinds(strict))

    def test_a_column_the_shape_does_not_declare_grounds_nothing(self):
        lead = _no_figures(deals_closed=4)
        lead.data["secret_total"] = 777
        self.assertIn("unsupported_amount", _kinds(_verify(lead, "Hi Priya,\nA $777 figure.")))

    def test_a_lead_with_no_shape_has_nothing_to_check_against(self):
        lead = _lead()
        lead.shape = None
        self.assertEqual(_verify(lead, "Hi David,\nYour 47 closed deals and $99 million book."), [])


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
        lead = _no_figures(estimated_book_size_usd=5_000_000)
        v = _verify(lead, "Hi Priya,\nYour $47 million book, yes $47 million, is huge.")
        self.assertEqual(len(v), 1)

    def test_boolean_premium_ignored(self):
        lead = _no_figures(
            estimated_book_size_usd=5_000_000,
            events=_EventSet(
                [_event("deal_closed", datetime.datetime(2026, 5, 1, 9), premium=True)]
            ),
        )
        self.assertEqual(_verify(lead, "Hi Priya,\nYour $5M book is strong."), [])

    def test_non_numeric_premium_ignored(self):
        lead = _no_figures(
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
