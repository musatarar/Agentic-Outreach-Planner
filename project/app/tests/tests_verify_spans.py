"""Verification-span tests.

Every emitted span slices back to its own ``text``, and the span report agrees
with ``verify_copy`` case for case.
"""

import datetime
import json
import unittest
from types import SimpleNamespace

from project.app.services import actions, verify
from project.app.tests.tests_shape_utils import shape

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


def _lead(**kwargs):
    events = kwargs.pop("events", _EventSet([]))
    lead_id = kwargs.pop("id", "lead_x")
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
    return SimpleNamespace(id=lead_id, data=data, shape=SHAPE, events=events)


# (name, lead, copy, action_type, level)
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


def _kwargs(level):
    kwargs = {"today": TODAY}
    if level is not None:
        kwargs["level"] = level
    return kwargs


# ---------------------------------------------------------------------------
# verify_copy
# ---------------------------------------------------------------------------


class VerifyCopyParityTests(unittest.TestCase):
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
        # The claim is the figure; no noun binds it to a column, so no field.
        self.assertEqual(copy[violation.start : violation.end], "47")
        self.assertEqual(violation.field, "")

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
        # This is persisted to a JSONField.
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
        lead = _lead(deals_closed=4)
        copy = (
            "Hi there,\nOnce you hit 20 closed deals we should talk. "
            "Let's meet on 2026-07-01 — a discount for you."
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
    """The summary ratio and the approve gate are independent: an unauthorized offer
    stays out of the ratio but still blocks approval."""

    def _report(self, lead, copy, action_type, level=None):
        return verify.verify_spans(lead, copy, action_type, **_kwargs(level))

    def test_an_offer_blocks_approval_while_every_graded_claim_still_passes(self):
        lead = _lead(deals_closed=4, quotes_submitted=3, estimated_book_size_usd=5_000_000)
        copy = (
            "Hi Priya,\nYour 4 closed deals and 3 quotes submitted against a "
            "$5,000,000 book are great — here is a discount on your renewal."
        )
        report = self._report(lead, copy, actions.REENGAGE_DORMANT)

        self.assertEqual(report["verified_count"], 4)
        self.assertEqual(report["unverified_count"], 0)
        self.assertEqual(report["checked_count"], 4)
        self.assertEqual(report["summary"], "4 of 4 claims verified")

        # The offer is excluded from BOTH counts.
        offer = next(c for c in report["claims"] if c["kind"] == "unauthorized_offer")
        self.assertFalse(offer["counts_toward_summary"])
        self.assertIs(offer["verified"], False)
        self.assertEqual(report["copy"][offer["start"] : offer["end"]], "discount")

        # ...yet approval is blocked.
        self.assertFalse(report["can_approve"])

    def test_the_two_causes_compose_rather_than_override(self):
        lead = _lead(deals_closed=4)
        copy = "Hi Priya,\nYour 47 closed deals are great — here is a discount."
        report = self._report(lead, copy, actions.REENGAGE_DORMANT)
        self.assertEqual(report["unverified_count"], 1)  # the figure
        self.assertTrue(any(c["kind"] == "unauthorized_offer" for c in report["claims"]))
        self.assertFalse(report["can_approve"])

    def test_an_authorized_offer_does_not_block(self):
        # Volume pricing is authorized for a power-user reward, so no claim is recorded.
        lead = _lead()
        copy = "Hi Priya,\nLet's talk volume pricing for Summit Risk Advisors."
        report = self._report(lead, copy, actions.POWER_USER_REWARD)
        self.assertFalse(any(c["kind"] == "unauthorized_offer" for c in report["claims"]))
        self.assertTrue(report["can_approve"])

    def test_blocking_kinds_is_a_strict_subset_of_the_uncounted_kinds(self):
        # A blocking kind that also counted would be punished twice.
        self.assertTrue(verify.BLOCKING_KINDS)
        for kind in verify.BLOCKING_KINDS:
            self.assertIn(kind, verify._UNCOUNTED_KINDS)


# ---------------------------------------------------------------------------
# the worked examples, pinned
# ---------------------------------------------------------------------------


class GoalReferenceTests(unittest.TestCase):
    """Every count check routes a target through ``goal_reference`` — looked at,
    deliberately not graded."""

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
        self.assertEqual(goal.text, "20")
        self.assertEqual(goal.claimed, 20)
        self.assertIn(3, goal.expected)
        self.assertIsNone(goal.verified)
        self.assertFalse(goal.counts_toward_summary)

    def test_producers_target(self):
        claims = self._claims(
            _lead(num_producers=4),
            "Hi Priya,\nWe can talk once your team of 20 producers is in place.",
        )
        goal = next(c for c in claims if c.kind == "goal_reference")
        self.assertEqual((goal.text, goal.claimed), ("20", 20))

    def test_years_target(self):
        claims = self._claims(
            _lead(years_in_business=12),
            "Hi Priya,\nOnce you reach 20 years in business let's celebrate.",
        )
        goal = next(c for c in claims if c.kind == "goal_reference")
        self.assertEqual((goal.text, goal.claimed), ("20", 20))

    def test_a_record_with_no_figures_grounds_no_count(self):
        # Nothing to compare against, so the integer is not inspected at all.
        blank = {
            name: None
            for name in (
                "num_producers",
                "years_in_business",
                "estimated_book_size_usd",
                "quotes_created",
                "quotes_submitted",
                "deals_closed",
            )
        }
        claims = self._claims(_lead(**blank), "Hi Priya,\nYou created 12 quotes.")
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
    "Worth a quick call this week to walk through volume pricing before you "
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
        self.assertEqual(report["copy_length"], 365)
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
                ("claim-0001", "goal_reference", 38, 40, "20", None),
                ("claim-0002", "contact_name", 60, 65, "Priya", True),
                ("claim-0003", "count", 82, 83, "6", True),
                ("claim-0004", "count", 97, 99, "14", True),
                ("claim-0005", "goal_reference", 179, 181, "20", None),
                ("claim-0006", "amount", 220, 230, "$1,400,000", True),
            ],
        )
        # _CURRENCY_RE's match is "$1,400,000 " (220–231).
        self.assertEqual(report["copy"][220:231], "$1,400,000 ")

    def test_example_b_mixed(self):
        copy = PRIYA_COPY.replace("closed 6 deals", "closed 9 deals").replace(
            "$1,400,000", "$2,500,000"
        )
        report = verify.verify_spans(_priya(), copy, actions.POWER_USER_REWARD, today=TODAY)
        self.assertEqual(report["copy_length"], 365)  # identical character lengths
        self.assertEqual(report["verified_count"], 2)
        self.assertEqual(report["unverified_count"], 2)
        self.assertEqual(report["checked_count"], 4)
        self.assertEqual(report["summary"], "2 of 4 claims verified")
        # Blocked purely by the unverified-claims path: no blocking claim here.
        self.assertFalse(report["can_approve"])
        self.assertFalse(
            any(c["kind"] in verify.BLOCKING_KINDS for c in report["claims"]),
        )
        by_id = {c["id"]: c for c in report["claims"]}
        self.assertEqual((by_id["claim-0003"]["start"], by_id["claim-0003"]["end"]), (82, 83))
        self.assertIs(by_id["claim-0003"]["verified"], False)
        self.assertNotIn(9, by_id["claim-0003"]["expected"])
        self.assertEqual(by_id["claim-0003"]["claimed"], 9)
        self.assertEqual(
            by_id["claim-0003"]["message"],
            "Copy claims 9, which is not a figure in the lead record.",
        )
        self.assertEqual((by_id["claim-0006"]["start"], by_id["claim-0006"]["end"]), (220, 230))
        self.assertIs(by_id["claim-0006"]["verified"], False)
        self.assertEqual(by_id["claim-0006"]["claimed"], 2500000)


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
        # The FE must slice via Array.from() when is_astral_safe is false.
        deals = next(c for c in report["claims"] if c["kind"] == "count")
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
        claim = next(c for c in report["claims"] if c["kind"] == "count")
        self.assertEqual(report["copy"][claim["start"] : claim["end"]], "47")
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
        deals = [c for c in claims if c.kind == "count"]
        self.assertEqual(len(deals), 2)
        self.assertNotEqual(deals[0].start, deals[1].start)
        self.assertEqual(deals[0].message, deals[1].message)
        # verify_copy keeps its by-message dedupe; the claims do not.
        self.assertEqual(len(violations), 1)

    def test_an_identical_claim_at_an_identical_span_is_recorded_once(self):
        # Both the ISO-date and year scanners see this text at strict level.
        lead = _lead()
        copy = "Hi Priya,\nSummit Risk Advisors since 2005 and 2005."
        report = verify.verify_spans(
            lead, copy, actions.NUDGE_USAGE, level=verify.LEVEL_STRICT, today=TODAY
        )
        keys = [(c["kind"], c["start"], c["end"], c["message"]) for c in report["claims"]]
        self.assertEqual(len(keys), len(set(keys)))


if __name__ == "__main__":
    unittest.main()
