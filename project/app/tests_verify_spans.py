"""Verification-span tests (MUS-42).

Two jobs:

1. Prove ``verify_copy`` still behaves exactly as it did before the spans were
   added. ``PRE_CHANGE_VIOLATIONS`` is a frozen table captured by running the
   pre-change ``verify.py`` over ``CASES``; the kinds *and* messages, in order,
   must match. ``tests_verify.py`` (58 tests) and ``tests_redteam.py``
   (18 tests) additionally pass unmodified.
2. Prove the new span output is usable — every span slices back to its own
   ``text``, is whitespace-trimmed, survives ``\\r\\n`` and astral characters,
   and round-trips through ``json.dumps``.

Pure Python — no Django, no database. Run standalone::

    ./.venv/bin/python -m unittest project.app.tests_verify_spans
"""

import datetime
import json
import unittest
from types import SimpleNamespace

from project.app.services import actions, verify

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


# (name, lead, copy, action_type, level) — mirrors every fixture shape in
# tests_verify.py plus the paths that only the span output can reach.
CASES = [
    (
        "clean_standard",
        _lead(),
        "Hi Priya,\nSummit Risk Advisors closed 4 deals.",
        actions.NUDGE_USAGE,
        None,
    ),
    (
        "inflated_deals",
        _lead(deals_closed=4),
        "Hi Priya,\nCongrats on your 47 closed deals!",
        actions.NUDGE_USAGE,
        None,
    ),
    (
        "deals_closed_order",
        _lead(deals_closed=4),
        "Hi Priya,\nYou closed 9 deals already.",
        actions.NUDGE_USAGE,
        None,
    ),
    (
        "invented_amount",
        _lead(),
        "Hi Priya,\nYour $99 million book is impressive.",
        actions.NUDGE_USAGE,
        None,
    ),
    (
        "rounded_amount_ok",
        _lead(estimated_book_size_usd=5_000_000),
        "Hi Priya,\nYour $5M book.",
        actions.NUDGE_USAGE,
        None,
    ),
    (
        "premium_amount_ok",
        _lead(
            events=_EventSet(
                [_event("deal_closed", datetime.datetime(2026, 5, 25, 9), premium=9200)]
            )
        ),
        "Hi Priya,\nThat $9,200 premium was great.",
        actions.NUDGE_USAGE,
        None,
    ),
    (
        "wrong_name",
        _lead(contact_name="Priya Nair"),
        "Hi David,\nGood to see 4 closed deals.",
        actions.NUDGE_USAGE,
        None,
    ),
    (
        "generic_salutation",
        _lead(),
        "Hi there,\nSummit Risk Advisors is doing well.",
        actions.NUDGE_USAGE,
        None,
    ),
    (
        "honorific_only",
        _lead(),
        "Dear Sir,\nSummit Risk Advisors is doing well.",
        actions.NUDGE_USAGE,
        None,
    ),
    (
        "quotes_created_wrong",
        _lead(quotes_created=8),
        "Hi Priya,\nYou created 12 quotes.",
        actions.NUDGE_USAGE,
        None,
    ),
    (
        "quotes_submitted_ok",
        _lead(quotes_submitted=3),
        "Hi Priya,\nYour 3 quotes submitted is a start.",
        actions.NUDGE_USAGE,
        None,
    ),
    (
        "producers_wrong",
        _lead(num_producers=4),
        "Hi Priya,\nYour team of 20 producers is large.",
        actions.NUDGE_USAGE,
        None,
    ),
    (
        "producers_ok",
        _lead(num_producers=4),
        "Hi Priya,\nYour team of 4 producers is great.",
        actions.NUDGE_USAGE,
        None,
    ),
    (
        "years_wrong",
        _lead(years_in_business=12),
        "Hi Priya,\nAfter 30 years in business you know this.",
        actions.NUDGE_USAGE,
        None,
    ),
    (
        "years_ok",
        _lead(years_in_business=12),
        "Hi Priya,\nAfter 12 years in business you know this.",
        actions.NUDGE_USAGE,
        None,
    ),
    (
        "goal_context",
        _lead(deals_closed=4),
        "Hi Priya,\nOnce you hit 20 closed deals we should talk.",
        actions.NUDGE_USAGE,
        None,
    ),
    (
        "goal_milestone_after",
        _lead(deals_closed=4),
        "Hi Priya,\nThe 20 closed deals milestone is close.",
        actions.NUDGE_USAGE,
        None,
    ),
    (
        "offer_unauthorized",
        _lead(),
        "Hi Priya,\nHere is 20% off just for you.",
        actions.REENGAGE_DORMANT,
        None,
    ),
    (
        "offer_authorized",
        _lead(),
        "Hi Priya,\nLet's talk volume pricing.",
        actions.POWER_USER_REWARD,
        None,
    ),
    (
        "iso_date_unsupported",
        _lead(),
        "Hi Priya,\nYour login on 2026-05-20 was a while ago.",
        actions.NUDGE_USAGE,
        None,
    ),
    (
        "iso_date_grounded",
        _lead(last_login_date=datetime.date(2026, 5, 20)),
        "Hi Priya,\nYour login on 2026-05-20 was a while ago.",
        actions.NUDGE_USAGE,
        None,
    ),
    ("iso_date_future", _lead(), "Hi Priya,\nLet's talk on 2026-07-01.", actions.NUDGE_USAGE, None),
    (
        "iso_date_invalid",
        _lead(),
        "Hi Priya,\nReference 2026-13-45 is odd.",
        actions.NUDGE_USAGE,
        None,
    ),
    (
        "everything_wrong",
        _lead(deals_closed=4),
        "Hi David,\nYour 47 closed deals and $99 million book! 20% off just for you.",
        actions.REENGAGE_DORMANT,
        None,
    ),
    (
        "repeated_problem",
        _lead(deals_closed=4),
        "Hi Priya,\n47 closed deals! Yes, 47 closed deals.",
        actions.NUDGE_USAGE,
        None,
    ),
    (
        "level_off",
        _lead(deals_closed=4),
        "Hi David,\nYour 47 closed deals and $99 million book. 20% off!",
        actions.REENGAGE_DORMANT,
        verify.LEVEL_OFF,
    ),
    ("empty_copy", _lead(), "", actions.NUDGE_USAGE, None),
    (
        "strict_contact_absent",
        _lead(),
        "Hi there,\nSummit Risk Advisors is doing great in 2026.",
        actions.NUDGE_USAGE,
        verify.LEVEL_STRICT,
    ),
    (
        "strict_agency_absent",
        _lead(),
        "Hi Priya,\nYour agency is thriving in 2026.",
        actions.NUDGE_USAGE,
        verify.LEVEL_STRICT,
    ),
    (
        "strict_year_unsupported",
        _lead(),
        "Hi Priya,\nSummit has grown a lot since 2005.",
        actions.NUDGE_USAGE,
        verify.LEVEL_STRICT,
    ),
    (
        "strict_clean",
        _lead(),
        "Hi Priya,\nSummit is thriving and we should reconnect in 2026.",
        actions.NUDGE_USAGE,
        verify.LEVEL_STRICT,
    ),
    (
        "strict_agency_stopwords",
        _lead(agency_name="Insurance Group"),
        "Hi Priya,\nHello there in 2026.",
        actions.NUDGE_USAGE,
        verify.LEVEL_STRICT,
    ),
    (
        "no_contact_name",
        _lead(contact_name=""),
        "Hi David,\nSummit Risk Advisors is doing well.",
        actions.NUDGE_USAGE,
        None,
    ),
    (
        "no_book_size",
        _lead(estimated_book_size_usd=None),
        "Hi Priya,\nYour $99 million book.",
        actions.NUDGE_USAGE,
        None,
    ),
    (
        "crlf_copy",
        _lead(deals_closed=4),
        "Hi Priya,\r\nCongrats on your 47 closed deals!",
        actions.NUDGE_USAGE,
        None,
    ),
    (
        "multiline_amounts",
        _lead(),
        "Hi Priya,\nYour $5,000,000 book and the $1,234,567 deal.",
        actions.NUDGE_USAGE,
        None,
    ),
]

# Captured by running the PRE-CHANGE verify.py over CASES. `verify_copy`'s
# return value, its order and its by-message de-duplication are frozen output:
# plan_outreach() writes format_violations() straight into `further_action`.
PRE_CHANGE_VIOLATIONS = {
    "clean_standard": [],
    "inflated_deals": [
        ("wrong_count", "Copy claims 47 closed deals but the record shows 4."),
    ],
    "deals_closed_order": [
        ("wrong_count", "Copy claims 9 closed deals but the record shows 4."),
    ],
    "invented_amount": [
        (
            "unsupported_amount",
            "Copy cites $99 million but no matching dollar figure is in the lead record.",
        ),
    ],
    "rounded_amount_ok": [],
    "premium_amount_ok": [],
    "wrong_name": [
        ("wrong_contact_name", 'Copy greets "David" but the lead contact is Priya Nair.'),
    ],
    "generic_salutation": [],
    "honorific_only": [],
    "quotes_created_wrong": [
        ("wrong_count", "Copy claims 12 quotes created but the record shows 8."),
    ],
    "quotes_submitted_ok": [],
    "producers_wrong": [
        ("wrong_count", "Copy claims 20 producers but the record shows 4."),
    ],
    "producers_ok": [],
    "years_wrong": [
        ("wrong_count", "Copy claims 30 years in business but the record shows 12."),
    ],
    "years_ok": [],
    "goal_context": [],
    "goal_milestone_after": [],
    "offer_unauthorized": [
        (
            "unauthorized_offer",
            'Copy makes a commercial promise ("20% off") that is not authorized for a '
            "reengage_dormant action.",
        ),
    ],
    "offer_authorized": [],
    "iso_date_unsupported": [
        ("unsupported_date", "Copy cites the date 2026-05-20, which is not in the lead record."),
    ],
    "iso_date_grounded": [],
    "iso_date_future": [],
    "iso_date_invalid": [],
    "everything_wrong": [
        (
            "unsupported_amount",
            "Copy cites $99 million but no matching dollar figure is in the lead record.",
        ),
        ("wrong_count", "Copy claims 47 closed deals but the record shows 4."),
        ("wrong_contact_name", 'Copy greets "David" but the lead contact is Priya Nair.'),
        (
            "unauthorized_offer",
            'Copy makes a commercial promise ("20% off") that is not authorized for a '
            "reengage_dormant action.",
        ),
    ],
    "repeated_problem": [
        ("wrong_count", "Copy claims 47 closed deals but the record shows 4."),
    ],
    "level_off": [],
    "empty_copy": [],
    "strict_contact_absent": [
        ("contact_name_absent", "Copy never addresses the contact by name (Priya Nair)."),
    ],
    "strict_agency_absent": [
        ("agency_name_absent", 'Copy never names the agency ("Summit Risk Advisors").'),
    ],
    "strict_year_unsupported": [
        ("unsupported_year", "Copy mentions the year 2005, which is not tied to any record date."),
    ],
    "strict_clean": [],
    "strict_agency_stopwords": [],
    "no_contact_name": [],
    "no_book_size": [],
    "crlf_copy": [
        ("wrong_count", "Copy claims 47 closed deals but the record shows 4."),
    ],
    "multiline_amounts": [
        (
            "unsupported_amount",
            "Copy cites $1,234,567 but no matching dollar figure is in the lead record.",
        ),
    ],
}


def _kwargs(level):
    kwargs = {"today": TODAY}
    if level is not None:
        kwargs["level"] = level
    return kwargs


# ---------------------------------------------------------------------------
# verify_copy is unchanged
# ---------------------------------------------------------------------------


class VerifyCopyParityTests(unittest.TestCase):
    def test_every_case_has_a_frozen_expectation(self):
        self.assertEqual(sorted(name for name, *_ in CASES), sorted(PRE_CHANGE_VIOLATIONS))

    def test_violations_match_the_pre_change_output_in_order(self):
        for name, lead, copy, action_type, level in CASES:
            with self.subTest(name):
                violations = verify.verify_copy(lead, copy, action_type, **_kwargs(level))
                self.assertEqual(
                    [(v.kind, v.message) for v in violations], PRE_CHANGE_VIOLATIONS[name]
                )

    def test_violation_supports_positional_two_argument_construction(self):
        violation = verify.Violation("wrong_count", "msg")
        self.assertEqual((violation.kind, violation.message), ("wrong_count", "msg"))
        self.assertIsNone(violation.start)
        self.assertIsNone(violation.end)
        self.assertEqual(violation.field, "")

    def test_violations_carry_the_offsets_of_their_claim(self):
        lead = _lead(deals_closed=4)
        copy = "Hi Priya,\nCongrats on your 47 closed deals!"
        violation = verify.verify_copy(lead, copy, actions.NUDGE_USAGE, today=TODAY)[0]
        self.assertEqual(copy[violation.start : violation.end], "47 closed deals")
        self.assertEqual(violation.field, "deals_closed")

    def test_omission_violations_have_no_offsets(self):
        lead = _lead()
        copy = "Hi there,\nSummit Risk Advisors is doing great in 2026."
        violation = verify.verify_copy(
            lead, copy, actions.NUDGE_USAGE, level=verify.LEVEL_STRICT, today=TODAY
        )[0]
        self.assertEqual(violation.kind, "contact_name_absent")
        self.assertIsNone(violation.start)
        self.assertIsNone(violation.end)

    def test_claims_out_parameter_does_not_change_the_return_value(self):
        for name, lead, copy, action_type, level in CASES:
            with self.subTest(name):
                claims: list = []
                self.assertEqual(
                    verify.verify_copy(lead, copy, action_type, **_kwargs(level)),
                    verify.verify_copy(lead, copy, action_type, claims=claims, **_kwargs(level)),
                )

    def test_violations_are_exactly_the_contradicted_claims_after_dedupe(self):
        for name, lead, copy, action_type, level in CASES:
            with self.subTest(name):
                claims: list = []
                violations = verify.verify_copy(
                    lead, copy, action_type, claims=claims, **_kwargs(level)
                )
                failed = [c for c in claims if c.verified is False]
                self.assertEqual(
                    [v.message for v in violations],
                    list(dict.fromkeys(c.message for c in failed)),
                )


# ---------------------------------------------------------------------------
# the report envelope
# ---------------------------------------------------------------------------


class ReportEnvelopeTests(unittest.TestCase):
    def _report(self, lead, copy, action_type=actions.NUDGE_USAGE, level=None):
        return verify.verify_spans(lead, copy, action_type, **_kwargs(level))

    def test_envelope_shape_for_every_case(self):
        for name, lead, copy, action_type, level in CASES:
            with self.subTest(name):
                report = self._report(lead, copy, action_type, level)
                self.assertEqual(report["version"], 1)
                self.assertEqual(report["level"], level or verify.DEFAULT_LEVEL)
                self.assertEqual(report["today"], TODAY.isoformat())
                self.assertEqual(report["copy"], verify.normalize_copy(copy))
                self.assertEqual(report["copy_length"], len(report["copy"]))
                self.assertEqual(
                    report["checked_count"],
                    report["verified_count"] + report["unverified_count"],
                )
                self.assertEqual(
                    report["summary"],
                    f"{report['verified_count']} of {report['checked_count']} claims verified",
                )
                blocked = any(c["kind"] in verify.BLOCKING_KINDS for c in report["claims"])
                self.assertEqual(
                    report["can_approve"],
                    report["unverified_count"] == 0 and not blocked,
                )

    def test_every_span_slices_back_to_its_own_text(self):
        for name, lead, copy, action_type, level in CASES:
            report = self._report(lead, copy, action_type, level)
            for claim in report["claims"]:
                with self.subTest(name, id=claim["id"]):
                    if claim["start"] is None:
                        self.assertIsNone(claim["end"])
                        self.assertEqual(claim["text"], "")
                        continue
                    self.assertEqual(report["copy"][claim["start"] : claim["end"]], claim["text"])
                    # no span carries surrounding whitespace.
                    self.assertEqual(claim["text"], claim["text"].strip())

    def test_claims_are_ordered_by_offset_and_ids_follow(self):
        for name, lead, copy, action_type, level in CASES:
            report = self._report(lead, copy, action_type, level)
            with self.subTest(name):
                keys = [
                    (c["start"] is None, c["start"] or 0, c["end"] or 0) for c in report["claims"]
                ]
                self.assertEqual(keys, sorted(keys))
                self.assertEqual(
                    [c["id"] for c in report["claims"]],
                    [f"claim-{i:04d}" for i in range(1, len(report["claims"]) + 1)],
                )

    def test_report_round_trips_through_json(self):
        # MUS-39 persists this to a JSONField.
        for name, lead, copy, action_type, level in CASES:
            with self.subTest(name):
                report = self._report(lead, copy, action_type, level)
                self.assertEqual(json.loads(json.dumps(report)), report)

    def test_verified_claims_carry_no_message(self):
        for name, lead, copy, action_type, level in CASES:
            report = self._report(lead, copy, action_type, level)
            for claim in report["claims"]:
                if claim["verified"] is True:
                    with self.subTest(name, id=claim["id"]):
                        self.assertEqual(claim["message"], "")

    def test_level_off_reports_nothing(self):
        report = self._report(
            _lead(deals_closed=4),
            "Hi David,\nYour 47 closed deals and $99 million book. 20% off!",
            actions.REENGAGE_DORMANT,
            verify.LEVEL_OFF,
        )
        self.assertEqual(report["claims"], [])
        self.assertEqual(report["summary"], "0 of 0 claims verified")
        self.assertTrue(report["can_approve"])

    def test_uncounted_kinds_never_move_the_ratio(self):
        # A goal reference, a future date and an unauthorized offer are all
        # inspected, none of them is an assertion about the record.
        lead = _lead(deals_closed=4)
        copy = (
            "Hi there,\nOnce you hit 20 closed deals we should talk. "
            "Let's meet on 2026-07-01 — 20% off for you."
        )
        report = self._report(lead, copy, actions.REENGAGE_DORMANT)
        kinds = {c["kind"] for c in report["claims"]}
        self.assertEqual(kinds, {"goal_reference", "future_date", "unauthorized_offer"})
        self.assertTrue(all(c["counts_toward_summary"] is False for c in report["claims"]))
        self.assertEqual(report["summary"], "0 of 0 claims verified")


# ---------------------------------------------------------------------------
# can_approve has two independent causes
# ---------------------------------------------------------------------------


class ApproveGateTests(unittest.TestCase):
    """The summary ratio and the approve gate answer different questions.

    "How much of this copy did we grade against the record?" is not "may a
    reviewer send it?". An unauthorized commercial promise is not a claim about
    the record — it must stay out of the ratio — but it is the single most
    consequential thing generated copy can contain, and ``plan_outreach()``
    already fails closed on it. The gate must agree.
    """

    def _report(self, lead, copy, action_type, level=None):
        return verify.verify_spans(lead, copy, action_type, **_kwargs(level))

    def test_an_offer_blocks_approval_while_every_graded_claim_still_passes(self):
        lead = _lead(deals_closed=4, quotes_submitted=3, estimated_book_size_usd=5_000_000)
        copy = (
            "Hi Priya,\nYour 4 closed deals and 3 quotes submitted against a "
            "$5,000,000 book are great — here is 20% off your renewal."
        )
        report = self._report(lead, copy, actions.REENGAGE_DORMANT)

        # Everything the verifier actually graded is grounded...
        self.assertEqual(report["verified_count"], 4)
        self.assertEqual(report["unverified_count"], 0)
        self.assertEqual(report["checked_count"], 4)
        self.assertEqual(report["summary"], "4 of 4 claims verified")

        # ...and the offer is excluded from BOTH counts.
        offer = next(c for c in report["claims"] if c["kind"] == "unauthorized_offer")
        self.assertFalse(offer["counts_toward_summary"])
        self.assertIs(offer["verified"], False)
        self.assertEqual(report["copy"][offer["start"] : offer["end"]], "20% off")

        # ...yet approval is blocked. This pairing is the surprising part.
        self.assertFalse(report["can_approve"])

    def test_the_two_causes_compose_rather_than_override(self):
        lead = _lead(deals_closed=4)
        copy = "Hi Priya,\nYour 47 closed deals are great — here is 20% off."
        report = self._report(lead, copy, actions.REENGAGE_DORMANT)
        self.assertEqual(report["unverified_count"], 1)  # the deal count
        self.assertTrue(any(c["kind"] == "unauthorized_offer" for c in report["claims"]))
        self.assertFalse(report["can_approve"])

    def test_an_authorized_offer_does_not_block(self):
        # Volume pricing is exactly what a power-user reward email is for, so no
        # unauthorized_offer claim is recorded at all.
        lead = _lead()
        copy = "Hi Priya,\nLet's talk volume pricing for Summit Risk Advisors."
        report = self._report(lead, copy, actions.POWER_USER_REWARD)
        self.assertFalse(any(c["kind"] == "unauthorized_offer" for c in report["claims"]))
        self.assertTrue(report["can_approve"])

    def test_blocking_kinds_is_a_strict_subset_of_the_uncounted_kinds(self):
        # A blocking kind that also counted would be double-punished: once in
        # the ratio and once in the gate.
        self.assertTrue(verify.BLOCKING_KINDS)
        for kind in verify.BLOCKING_KINDS:
            self.assertIn(kind, verify._UNCOUNTED_KINDS)


# ---------------------------------------------------------------------------
# the worked examples, pinned
# ---------------------------------------------------------------------------


class GoalReferenceTests(unittest.TestCase):
    """Every count check routes a target through ``goal_reference``.

    These produced no violation before the change either — they were a bare
    ``continue``. The claim is the new part: the reviewer can now see the
    number was looked at and deliberately not graded.
    """

    def _claims(self, lead, copy):
        claims: list = []
        violations = verify.verify_copy(lead, copy, actions.NUDGE_USAGE, claims=claims, today=TODAY)
        self.assertEqual(violations, [])
        return claims

    def test_quotes_target(self):
        claims = self._claims(
            _lead(quotes_submitted=3), "Hi Priya,\nOnce you hit 20 quotes submitted we should talk."
        )
        goal = next(c for c in claims if c.kind == "goal_reference")
        self.assertEqual(goal.text, "20 quotes submitted")
        self.assertEqual((goal.field, goal.expected, goal.claimed), ("quotes_submitted", 3, 20))
        self.assertIsNone(goal.verified)
        self.assertFalse(goal.counts_toward_summary)

    def test_producers_target(self):
        claims = self._claims(
            _lead(num_producers=4),
            "Hi Priya,\nWe can talk once your team of 20 producers is in place.",
        )
        goal = next(c for c in claims if c.kind == "goal_reference")
        self.assertEqual(goal.text, "your team of 20 producers")
        self.assertEqual((goal.field, goal.expected, goal.claimed), ("num_producers", 4, 20))

    def test_years_target(self):
        claims = self._claims(
            _lead(years_in_business=12),
            "Hi Priya,\nOnce you reach 20 years in business let's celebrate.",
        )
        goal = next(c for c in claims if c.kind == "goal_reference")
        self.assertEqual(goal.text, "20 years in business")
        self.assertEqual((goal.field, goal.expected, goal.claimed), ("years_in_business", 12, 20))

    def test_a_missing_record_value_is_not_a_claim(self):
        claims = self._claims(
            _lead(quotes_created=None, quotes_submitted=None), "Hi Priya,\nYou created 12 quotes."
        )
        self.assertEqual([c.kind for c in claims], ["contact_name"])

    def test_future_date_is_recorded_but_ungraded(self):
        claims = self._claims(_lead(), "Hi Priya,\nLet's talk on 2026-07-01.")
        future = next(c for c in claims if c.kind == "future_date")
        self.assertIsNone(future.verified)
        self.assertFalse(future.counts_toward_summary)
        self.assertEqual(future.text, "2026-07-01")


PRIYA_COPY = (
    "Subject: Volume pricing ahead of your 20-deal milestone\n"
    "\n"
    "Hi Priya,\n"
    "\n"
    "You've closed 6 deals out of 14 quotes submitted since April, which puts "
    "Summit Risk Advisors on track for the 20 closed deals mark you mentioned. "
    "On a $1,400,000 book that pace is genuinely impressive.\n"
    "\n"
    "Worth a 15-minute call this week to walk through volume pricing before you "
    "get there?\n"
    "\n"
    "Best,\n"
    "Dana"
)


def _priya():
    return _lead(
        id="lead_001",
        agency_name="Summit Risk Advisors",
        contact_name="Priya Nair",
        num_producers=4,
        years_in_business=6,
        estimated_book_size_usd=1_400_000,
        quotes_created=19,
        quotes_submitted=14,
        deals_closed=6,
        signed_up_date=datetime.date(2026, 4, 22),
        last_login_date=datetime.date(2026, 5, 26),
        last_contacted_date=datetime.date(2026, 5, 15),
    )


class WorkedExampleTests(unittest.TestCase):
    def test_example_a_all_verified(self):
        report = verify.verify_spans(_priya(), PRIYA_COPY, actions.POWER_USER_REWARD, today=TODAY)
        self.assertEqual(report["copy_length"], 369)
        self.assertTrue(report["is_astral_safe"])
        self.assertEqual(report["verified_count"], 4)
        self.assertEqual(report["unverified_count"], 0)
        self.assertEqual(report["checked_count"], 4)
        self.assertEqual(report["summary"], "4 of 4 claims verified")
        self.assertTrue(report["can_approve"])
        self.assertEqual(
            [
                (c["id"], c["kind"], c["start"], c["end"], c["text"], c["verified"])
                for c in report["claims"]
            ],
            [
                ("claim-0001", "contact_name", 60, 65, "Priya", True),
                ("claim-0002", "deals_count", 75, 89, "closed 6 deals", True),
                ("claim-0003", "quotes_count", 97, 116, "14 quotes submitted", True),
                ("claim-0004", "goal_reference", 179, 194, "20 closed deals", None),
                ("claim-0005", "amount", 220, 230, "$1,400,000", True),
            ],
        )
        # _CURRENCY_RE's match is "$1,400,000 " (220–231).
        self.assertEqual(report["copy"][220:231], "$1,400,000 ")

    def test_example_b_mixed(self):
        copy = PRIYA_COPY.replace("closed 6 deals", "closed 9 deals").replace(
            "$1,400,000", "$2,500,000"
        )
        report = verify.verify_spans(_priya(), copy, actions.POWER_USER_REWARD, today=TODAY)
        self.assertEqual(report["copy_length"], 369)  # identical character lengths
        self.assertEqual(report["verified_count"], 2)
        self.assertEqual(report["unverified_count"], 2)
        self.assertEqual(report["checked_count"], 4)
        self.assertEqual(report["summary"], "2 of 4 claims verified")
        # Blocked purely by the unverified-claims path: this copy contains no
        # blocking claim, so the two causes of can_approve are independent.
        self.assertFalse(report["can_approve"])
        self.assertFalse(
            any(c["kind"] in verify.BLOCKING_KINDS for c in report["claims"]),
        )
        by_id = {c["id"]: c for c in report["claims"]}
        self.assertEqual((by_id["claim-0002"]["start"], by_id["claim-0002"]["end"]), (75, 89))
        self.assertIs(by_id["claim-0002"]["verified"], False)
        self.assertEqual(by_id["claim-0002"]["expected"], 6)
        self.assertEqual(by_id["claim-0002"]["claimed"], 9)
        self.assertEqual(
            by_id["claim-0002"]["message"],
            "Copy claims 9 closed deals but the record shows 6.",
        )
        self.assertEqual((by_id["claim-0005"]["start"], by_id["claim-0005"]["end"]), (220, 230))
        self.assertIs(by_id["claim-0005"]["verified"], False)
        self.assertEqual(by_id["claim-0005"]["claimed"], 2500000)


# ---------------------------------------------------------------------------
# the three ways offsets go wrong
# ---------------------------------------------------------------------------


class OffsetHazardTests(unittest.TestCase):
    def test_astral_character_flags_the_report_and_python_offsets_still_hold(self):
        lead = _lead(deals_closed=4)
        copy = "Hi Priya,\n\U0001f389 Congrats on your 4 closed deals and 8 quotes created!"
        report = verify.verify_spans(lead, copy, actions.NUDGE_USAGE, today=TODAY)
        self.assertFalse(report["is_astral_safe"])
        self.assertTrue(report["claims"])
        for claim in report["claims"]:
            if claim["start"] is not None:
                self.assertEqual(report["copy"][claim["start"] : claim["end"]], claim["text"])
        # The FE must slice via Array.from() when this is false; a naive
        # String.slice() would be one UTF-16 code unit off after the emoji.
        deals = next(c for c in report["claims"] if c["kind"] == "deals_count")
        self.assertNotEqual(
            copy.encode("utf-16-le").decode("utf-16-le")[deals["start"] : deals["end"]],
            "",
        )

    def test_ascii_only_copy_is_astral_safe(self):
        report = verify.verify_spans(
            _lead(), "Hi Priya,\nSummit Risk Advisors.", actions.NUDGE_USAGE, today=TODAY
        )
        self.assertTrue(report["is_astral_safe"])

    def test_bmp_non_ascii_is_still_astral_safe(self):
        report = verify.verify_spans(
            _lead(contact_name="Zoë Nair"),
            "Hi Zoë,\nSummit Risk Advisors.",
            actions.NUDGE_USAGE,
            today=TODAY,
        )
        self.assertTrue(report["is_astral_safe"])

    def test_currency_span_is_trimmed_of_the_trailing_space(self):
        lead = _lead(estimated_book_size_usd=1_400_000)
        copy = "Hi Priya,\nOn a $1,400,000 book that is impressive."
        report = verify.verify_spans(lead, copy, actions.NUDGE_USAGE, today=TODAY)
        claim = next(c for c in report["claims"] if c["kind"] == "amount")
        self.assertEqual(claim["text"], "$1,400,000")
        self.assertEqual(copy[claim["start"] : claim["end"]], "$1,400,000")
        self.assertEqual(copy[claim["start"] : claim["end"] + 1], "$1,400,000 ")

    def test_crlf_is_normalized_before_offsets_are_computed(self):
        lead = _lead(deals_closed=4)
        crlf = "Hi Priya,\r\n\r\nCongrats on your 47 closed deals!"
        report = verify.verify_spans(lead, crlf, actions.NUDGE_USAGE, today=TODAY)
        self.assertNotIn("\r", report["copy"])
        self.assertEqual(report["copy_length"], len(report["copy"]))
        claim = next(c for c in report["claims"] if c["kind"] == "deals_count")
        self.assertEqual(report["copy"][claim["start"] : claim["end"]], "47 closed deals")
        # Same copy with \n line endings produces identical offsets.
        lf = verify.verify_spans(lead, crlf.replace("\r\n", "\n"), actions.NUDGE_USAGE, today=TODAY)
        self.assertEqual(lf["claims"], report["claims"])

    def test_lone_carriage_return_is_normalized(self):
        self.assertEqual(verify.normalize_copy("a\rb\r\nc"), "a\nb\nc")
        self.assertEqual(verify.normalize_copy(""), "")


# ---------------------------------------------------------------------------
# claim de-duplication is re-keyed
# ---------------------------------------------------------------------------


class ClaimDedupeTests(unittest.TestCase):
    def test_same_message_at_two_offsets_yields_two_claims_but_one_violation(self):
        lead = _lead(deals_closed=4)
        copy = "Hi Priya,\n47 closed deals! Yes, 47 closed deals."
        claims: list = []
        violations = verify.verify_copy(lead, copy, actions.NUDGE_USAGE, claims=claims, today=TODAY)
        deals = [c for c in claims if c.kind == "deals_count"]
        self.assertEqual(len(deals), 2)
        self.assertNotEqual(deals[0].start, deals[1].start)
        self.assertEqual(deals[0].message, deals[1].message)
        # verify_copy keeps its by-message dedupe; the claims do not.
        self.assertEqual(len(violations), 1)

    def test_an_identical_claim_at_an_identical_span_is_recorded_once(self):
        # _ISO_DATE_RE and _YEAR_RE both scan the same text at strict level;
        # nothing may record the same (kind, start, end, message) twice.
        lead = _lead()
        copy = "Hi Priya,\nSummit Risk Advisors since 2005 and 2005."
        report = verify.verify_spans(
            lead, copy, actions.NUDGE_USAGE, level=verify.LEVEL_STRICT, today=TODAY
        )
        keys = [(c["kind"], c["start"], c["end"], c["message"]) for c in report["claims"]]
        self.assertEqual(len(keys), len(set(keys)))


if __name__ == "__main__":
    unittest.main()
