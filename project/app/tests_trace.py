"""Structured rule-trace tests (MUS-42, CONTRACT-MUS-35.md §3).

The trace is an *instrumentation* layer over the two rule functions the whole
product rests on. These tests exist to prove the instrumentation changed
nothing: same arity, same classifications, byte-identical prose reasons, and a
golden eval that still passes against an unregenerated baseline.

Pure Python — no Django, no database. Run standalone::

    ./.venv/bin/python -m unittest project.app.tests_trace
"""

import datetime
import json
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from evals import run_rules_eval as rules_eval
from project.app.services import actions, outreach

REPO_ROOT = Path(__file__).resolve().parents[2]
PARITY_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "reason_parity.json"

TODAY = rules_eval.TODAY  # datetime.date(2026, 6, 12)

# Every condition/group id the contract pins in §3.4 / §3.5. A pinned id that
# no golden record produces means a missing `_cond()` call (§3.6 item 5).
PINNED_CONDITION_IDS = frozenset(
    {
        "book_size_very_large",
        "book_size_large",
        "demo_without_signup",
        "stage_is_demo_completed",
        "signed_up_date_absent",
        "gone_quiet",
        "contact_old_enough",
        "no_reply_email_present",
        "stall_phrase_in_notes",
        "contact_stale",
        "trial_at_risk",
        "signup_old_enough",
        "zero_deals",
        "hot_engagement",
        "deals_power_user",
        "submissions_power_user",
        "milestone_from_notes",
        "deals_remaining_to_milestone",
    }
)

PINNED_RULE_IDS = [
    "R1_complete_onboarding",
    "R2_power_user",
    "R3_follow_up_after_hold",
    "R4_reengage_dormant",
    "R5_nudge_usage",
    "R6_unknown",
]

_CONDITION_KEYS = {
    "kind",
    "id",
    "field",
    "label",
    "operator",
    "threshold",
    "value",
    "unit",
    "passed",
    "weight",
    "source",
    "display",
}
_GROUP_KEYS = {"kind", "id", "label", "operator", "passed", "weight", "display", "conditions"}
_UNITS = {"days", "usd", "count", "date", "text", "bool", "none"}
_SOURCES = {"lead", "events", "notes", "derived"}


def _golden_leads():
    """(record, duck-typed lead) for every golden record, built exactly as the
    eval harness builds them."""
    return [
        (rec, rules_eval.build_lead(rec)) for rec in rules_eval.load_golden(rules_eval.GOLDEN_PATH)
    ]


def _flatten(items):
    """Yield every condition dict, descending one level into groups."""
    for item in items:
        if item.get("kind") == "group":
            yield from item["conditions"]
        else:
            yield item


def _all_ids(envelope):
    ids = set()
    buckets = [envelope["priority"]["signals"], envelope["action"]["conditions"]]
    buckets += [r["conditions"] for r in envelope["action"]["rejected_rules"]]
    for bucket in buckets:
        for item in bucket:
            ids.add(item["id"])
            for child in item.get("conditions", []):
                ids.add(child["id"])
    return ids


# ---------------------------------------------------------------------------
# §3.6 — backward-compatibility guarantees
# ---------------------------------------------------------------------------


class ArityTests(unittest.TestCase):
    """§3.6 item 1 — the signatures four other modules depend on."""

    def test_determine_action_returns_a_two_tuple_of_strings(self):
        for rec, lead in _golden_leads():
            with self.subTest(rec["id"]):
                result = outreach.determine_action(lead, today=TODAY)
                self.assertIsInstance(result, tuple)
                self.assertEqual(len(result), 2)
                action_type, reason = result  # the unpack every caller does
                self.assertIsInstance(action_type, str)
                self.assertIsInstance(reason, str)
                self.assertIn(action_type, actions.ACTION_TYPES)

    def test_determine_priority_returns_an_int(self):
        for rec, lead in _golden_leads():
            with self.subTest(rec["id"]):
                priority = outreach.determine_priority(lead, today=TODAY)
                self.assertIsInstance(priority, int)
                self.assertIn(priority, (1, 2, 3))

    def test_trace_is_keyword_only(self):
        lead = _golden_leads()[0][1]
        with self.assertRaises(TypeError):
            outreach.determine_action(lead, TODAY, [])
        with self.assertRaises(TypeError):
            outreach.determine_priority(lead, TODAY, [])


class TraceNeutralityTests(unittest.TestCase):
    """§3.6 item 2 — the test that catches a botched ``if``-transform.

    If any branch were taken on a re-evaluated expression rather than on the
    recorded ``Condition.passed``, the traced and untraced calls could diverge.
    """

    def test_determine_action_is_identical_with_and_without_a_trace(self):
        for rec, lead in _golden_leads():
            with self.subTest(rec["id"]):
                trace: list = []
                self.assertEqual(
                    outreach.determine_action(lead, today=TODAY),
                    outreach.determine_action(lead, today=TODAY, trace=trace),
                )
                self.assertTrue(trace, "a traced call must record at least one condition")

    def test_determine_priority_is_identical_with_and_without_a_trace(self):
        for rec, lead in _golden_leads():
            with self.subTest(rec["id"]):
                trace: list = []
                self.assertEqual(
                    outreach.determine_priority(lead, today=TODAY),
                    outreach.determine_priority(lead, today=TODAY, trace=trace),
                )
                self.assertTrue(trace)

    def test_trace_none_records_nothing_anywhere(self):
        # A no-op recording call must not mutate a shared default or leak state.
        lead = _golden_leads()[0][1]
        first: list = []
        outreach.determine_action(lead, today=TODAY, trace=first)
        outreach.determine_action(lead, today=TODAY)  # trace=None
        second: list = []
        outreach.determine_action(lead, today=TODAY, trace=second)
        self.assertEqual(first, second)


class ReasonParityTests(unittest.TestCase):
    """§3.6 item 3 — ``reason`` is displayed *and* fed to ``generate_copy``.

    The fixture was captured from the pre-transform code; a whitespace change
    here silently perturbs the copy eval.
    """

    @classmethod
    def setUpClass(cls):
        with open(PARITY_FIXTURE, encoding="utf-8") as fh:
            cls.fixture = json.load(fh)
        cls.expected = {r["id"]: r for r in cls.fixture["records"]}

    def test_fixture_covers_every_golden_record(self):
        golden_ids = [rec["id"] for rec, _ in _golden_leads()]
        self.assertEqual(self.fixture["today"], TODAY.isoformat())
        self.assertEqual(sorted(self.expected), sorted(golden_ids))
        self.assertEqual(len(golden_ids), 41)

    def test_reasons_are_byte_identical_to_the_frozen_fixture(self):
        for rec, lead in _golden_leads():
            expected = self.expected[rec["id"]]
            with self.subTest(rec["id"]):
                action_type, reason = outreach.determine_action(lead, today=TODAY)
                self.assertEqual(action_type, expected["action"])
                self.assertEqual(reason, expected["reason"])
                self.assertEqual(
                    outreach.determine_priority(lead, today=TODAY), expected["priority"]
                )


class GoldenEvalGateTests(unittest.TestCase):
    """§3.6 item 4 — the baseline must not need regenerating."""

    def test_rules_eval_exits_zero(self):
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "evals" / "run_rules_eval.py")],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("Gate: PASS", proc.stdout)


class ConditionCoverageTests(unittest.TestCase):
    """§3.6 item 5 — a condition that is never recorded is a missing call."""

    def test_every_pinned_condition_id_is_reachable(self):
        seen: set = set()
        for _rec, lead in _golden_leads():
            seen |= _all_ids(outreach.explain(lead, today=TODAY))
        self.assertEqual(PINNED_CONDITION_IDS - seen, set())

    def test_every_rule_id_is_reachable(self):
        seen = set()
        for _rec, lead in _golden_leads():
            envelope = outreach.explain(lead, today=TODAY)
            seen.add(envelope["action"]["rule_id"])
            seen.update(r["rule_id"] for r in envelope["action"]["rejected_rules"])
        self.assertEqual(sorted(seen), PINNED_RULE_IDS)


# ---------------------------------------------------------------------------
# §3.4 — envelope shape
# ---------------------------------------------------------------------------


class EnvelopeShapeTests(unittest.TestCase):
    def test_envelope_is_well_formed_for_every_golden_record(self):
        for rec, lead in _golden_leads():
            with self.subTest(rec["id"]):
                env = outreach.explain(lead, today=TODAY)
                self.assertEqual(env["version"], 1)
                self.assertEqual(env["today"], TODAY.isoformat())
                datetime.datetime.strptime(env["generated_at"], "%Y-%m-%dT%H:%M:%SZ")

                priority = env["priority"]
                self.assertEqual(priority["value"], outreach.determine_priority(lead, today=TODAY))
                self.assertIsInstance(priority["score"], int)
                self.assertEqual(
                    priority["bands"],
                    [
                        {"priority": 1, "min_score": 5},
                        {"priority": 2, "min_score": 2},
                        {"priority": 3, "min_score": 0},
                    ],
                )

                action = env["action"]
                self.assertEqual(action["value"], outreach.determine_action(lead, today=TODAY)[0])
                index = action["matched_rule_index"]
                self.assertEqual(
                    (action["rule_id"], action["rule_label"]), outreach.ACTION_RULES[index]
                )
                # Rejected rules are exactly the rules evaluated *before* the
                # match, in order — nothing after it was ever evaluated.
                self.assertEqual(
                    [r["rule_id"] for r in action["rejected_rules"]], PINNED_RULE_IDS[:index]
                )
                self.assertTrue(all(r["matched"] is False for r in action["rejected_rules"]))

    def test_condition_and_group_dicts_have_the_pinned_keys(self):
        for rec, lead in _golden_leads():
            env = outreach.explain(lead, today=TODAY)
            buckets = [env["priority"]["signals"], env["action"]["conditions"]]
            buckets += [r["conditions"] for r in env["action"]["rejected_rules"]]
            for bucket in buckets:
                for item in bucket:
                    with self.subTest(rec["id"], id=item["id"]):
                        if item["kind"] == "group":
                            self.assertEqual(set(item), _GROUP_KEYS)
                            self.assertIn(item["operator"], ("all_of", "any_of"))
                            self.assertTrue(item["conditions"])
                            # Nesting is one level only.
                            for child in item["conditions"]:
                                self.assertEqual(child["kind"], "condition")
                                self.assertEqual(set(child), _CONDITION_KEYS)
                        else:
                            self.assertEqual(set(item), _CONDITION_KEYS)
                    for cond in _flatten([item]):
                        with self.subTest(rec["id"], id=cond["id"]):
                            self.assertIn(cond["unit"], _UNITS)
                            self.assertIn(cond["source"], _SOURCES)
                            self.assertIsInstance(cond["passed"], bool)
                            self.assertIsInstance(cond["weight"], int)
                            self.assertTrue(cond["display"])

    def test_action_conditions_are_never_groups(self):
        for _rec, lead in _golden_leads():
            env = outreach.explain(lead, today=TODAY)
            buckets = [env["action"]["conditions"]]
            buckets += [r["conditions"] for r in env["action"]["rejected_rules"]]
            for bucket in buckets:
                for item in bucket:
                    self.assertEqual(item["kind"], "condition")

    def test_envelope_round_trips_through_json(self):
        # MUS-39 persists this to a JSONField.
        for rec, lead in _golden_leads():
            with self.subTest(rec["id"]):
                env = outreach.explain(lead, today=TODAY)
                self.assertEqual(json.loads(json.dumps(env)), env)

    def test_condition_ids_are_unique_within_each_list(self):
        # The FE keys React lists on `id`.
        for rec, lead in _golden_leads():
            env = outreach.explain(lead, today=TODAY)
            buckets = [env["priority"]["signals"], env["action"]["conditions"]]
            buckets += [r["conditions"] for r in env["action"]["rejected_rules"]]
            for bucket in buckets:
                ids = [item["id"] for item in bucket]
                with self.subTest(rec["id"]):
                    self.assertEqual(len(ids), len(set(ids)))

    def test_weights_sum_to_the_recorded_score(self):
        for rec, lead in _golden_leads():
            with self.subTest(rec["id"]):
                env = outreach.explain(lead, today=TODAY)
                signals = env["priority"]["signals"]
                fired = sum(s["weight"] for s in signals if s["passed"])
                # `book_size_large` is an `elif` of `book_size_very_large`, so
                # its weight only counts when the larger band did not fire.
                by_id = {s["id"]: s for s in signals}
                if by_id["book_size_very_large"]["passed"] and by_id["book_size_large"]["passed"]:
                    fired -= by_id["book_size_large"]["weight"]
                self.assertEqual(fired, env["priority"]["score"])


class WorkedExampleTests(unittest.TestCase):
    """The §3.5 worked example, pinned line for line.

    Four other branches consume these exact strings; MUS-40 snapshots them.
    """

    def _priya(self):
        lead = SimpleNamespace(
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
        )
        lead.events = []
        return lead

    def test_priority_block_matches_the_contract(self):
        env = outreach.explain(self._priya(), today=TODAY)
        self.assertEqual(env["priority"]["value"], 2)
        self.assertEqual(env["priority"]["score"], 2)
        self.assertEqual(
            [(s["id"], s["passed"], s["display"]) for s in env["priority"]["signals"]],
            [
                (
                    "book_size_very_large",
                    False,
                    "estimated_book_size_usd >= $5,000,000 → $1,400,000",
                ),
                ("book_size_large", False, "estimated_book_size_usd >= $2,000,000 → $1,400,000"),
                ("demo_without_signup", False, "demo_without_signup → false"),
                ("gone_quiet", False, "gone_quiet → false"),
                ("contact_stale", True, "days_since_last_contact > 21d → 28d"),
                ("trial_at_risk", False, "trial_at_risk → false"),
                ("hot_engagement", True, "hot_engagement → true"),
            ],
        )

    def test_group_children_match_the_contract(self):
        env = outreach.explain(self._priya(), today=TODAY)
        groups = {s["id"]: s for s in env["priority"]["signals"] if s["kind"] == "group"}
        self.assertEqual(
            [(c["id"], c["display"]) for c in groups["demo_without_signup"]["conditions"]],
            [
                ("stage_is_demo_completed", 'stage == "demo_completed" → "active_trial"'),
                ("signed_up_date_absent", "signed_up_date absent → 2026-04-22"),
            ],
        )
        self.assertEqual(
            [(c["id"], c["passed"], c["display"]) for c in groups["gone_quiet"]["conditions"]],
            [
                ("contact_old_enough", True, "days_since_last_contact >= 14d → 28d"),
                ("no_reply_email_present", False, "no_reply_email_present → false"),
                ("stall_phrase_in_notes", False, "stall_phrase_in_notes → (none)"),
            ],
        )
        self.assertEqual(
            [(c["id"], c["display"]) for c in groups["trial_at_risk"]["conditions"]],
            [
                ("signup_old_enough", "days_since_signup > 30d → 51d"),
                ("zero_deals", "deals_closed == 0 → 6"),
            ],
        )
        self.assertEqual(
            [(c["id"], c["display"]) for c in groups["hot_engagement"]["conditions"]],
            [
                ("deals_power_user", "deals_closed >= 5 → 6"),
                ("submissions_power_user", "quotes_submitted >= 10 → 14"),
            ],
        )

    def test_action_block_matches_the_contract(self):
        action = outreach.explain(self._priya(), today=TODAY)["action"]
        self.assertEqual(action["value"], actions.POWER_USER_REWARD)
        self.assertEqual(action["rule_id"], "R2_power_user")
        self.assertEqual(
            action["rule_label"], "Power user near a reward / volume-pricing milestone"
        )
        self.assertEqual(action["matched_rule_index"], 1)
        self.assertEqual(
            [(c["id"], c["display"]) for c in action["conditions"]],
            [
                ("deals_power_user", "deals_closed >= 5 → 6"),
                ("submissions_power_user", "quotes_submitted >= 10 → 14"),
                ("milestone_from_notes", "milestone_from_notes → 20"),
                ("deals_remaining_to_milestone", "deals_remaining > 0 → 14"),
            ],
        )
        self.assertEqual(len(action["rejected_rules"]), 1)
        rejected = action["rejected_rules"][0]
        self.assertEqual(rejected["rule_id"], "R1_complete_onboarding")
        self.assertEqual(rejected["rule_label"], "Demo completed but never signed up")
        self.assertIs(rejected["matched"], False)
        # Short-circuited: `signed_up_date_absent` was never reached.
        self.assertEqual([c["id"] for c in rejected["conditions"]], ["stage_is_demo_completed"])


# ---------------------------------------------------------------------------
# primitives
# ---------------------------------------------------------------------------


def _cond(**kwargs):
    kwargs.setdefault("id", "x")
    kwargs.setdefault("field", "f")
    kwargs.setdefault("label", "l")
    kwargs.setdefault("threshold", 0)
    kwargs.setdefault("unit", "count")
    return outreach._cond(**kwargs)


class ConditionPrimitiveTests(unittest.TestCase):
    def test_ordering_operators(self):
        self.assertTrue(_cond(operator=">=", threshold=5, value=5).passed)
        self.assertFalse(_cond(operator=">", threshold=5, value=5).passed)
        self.assertTrue(_cond(operator="<=", threshold=5, value=5).passed)
        self.assertFalse(_cond(operator="<", threshold=5, value=5).passed)

    def test_equality_and_membership_operators(self):
        self.assertTrue(_cond(operator="==", threshold=3, value=3).passed)
        self.assertTrue(_cond(operator="!=", threshold=3, value=4).passed)
        self.assertTrue(_cond(operator="in", threshold=(1, 2, 3), value=2).passed)
        self.assertFalse(_cond(operator="in", threshold=(1, 2, 3), value=9).passed)

    def test_presence_operators(self):
        self.assertTrue(_cond(operator="exists", threshold=None, value=7).passed)
        self.assertFalse(_cond(operator="exists", threshold=None, value=0).passed)
        self.assertFalse(_cond(operator="exists", threshold=None, value=None).passed)
        self.assertTrue(_cond(operator="absent", threshold=None, value=None).passed)
        self.assertFalse(_cond(operator="absent", threshold=None, value=7).passed)

    def test_contains_passes_on_a_matched_needle(self):
        matched = _cond(operator="contains", threshold="PHRASES", value="went quiet", unit="text")
        self.assertTrue(matched.passed)
        self.assertEqual(matched.display, 'f contains "PHRASES" → "went quiet"')
        self.assertFalse(_cond(operator="contains", threshold="PHRASES", value=None).passed)

    def test_null_value_fails_unless_null_passes(self):
        self.assertFalse(_cond(operator=">", threshold=21, value=None, unit="days").passed)
        self.assertTrue(
            _cond(operator=">", threshold=21, value=None, unit="days", null_passes=True).passed
        )

    def test_unsupported_operator_raises(self):
        with self.assertRaises(ValueError):
            _cond(operator="~=", threshold=1, value=1)

    def test_trace_none_is_a_no_op(self):
        condition = _cond(operator=">=", threshold=1, value=2, trace=None)
        recorded: list = []
        _cond(operator=">=", threshold=1, value=2, trace=recorded)
        self.assertEqual(recorded, [condition.to_dict()])


class DisplayFormatterTests(unittest.TestCase):
    """§3.3's pinned formatter table."""

    def test_units(self):
        cases = [
            (dict(operator=">", threshold=21, value=28, unit="days"), "f > 21d → 28d"),
            (
                dict(operator=">=", threshold=5_000_000, value=1_400_000, unit="usd"),
                "f >= $5,000,000 → $1,400,000",
            ),
            (dict(operator="==", threshold=0, value=6, unit="count"), "f == 0 → 6"),
            (
                dict(
                    operator="==",
                    threshold=datetime.date(2026, 1, 1),
                    value=datetime.date(2026, 4, 22),
                    unit="date",
                ),
                "f == 2026-01-01 → 2026-04-22",
            ),
            (
                dict(operator="==", threshold="demo_completed", value="active_trial", unit="text"),
                'f == "demo_completed" → "active_trial"',
            ),
        ]
        for kwargs, expected in cases:
            with self.subTest(expected):
                self.assertEqual(_cond(id="f", field="f", **kwargs).display, expected)

    def test_unset_value(self):
        self.assertEqual(
            _cond(id="f", field="f", operator=">", threshold=21, value=None, unit="days").display,
            "f > 21 → (unset)",
        )

    def test_bool_and_container_predicates_read_as_their_id(self):
        self.assertEqual(
            _cond(
                id="gone_quiet",
                field="gone_quiet",
                operator="==",
                threshold=True,
                value=True,
                unit="bool",
                source="derived",
            ).display,
            "gone_quiet → true",
        )
        self.assertEqual(
            _cond(
                id="milestone_from_notes",
                field="hubspot_notes",
                operator="exists",
                threshold=None,
                value=20,
                unit="count",
                source="notes",
            ).display,
            "milestone_from_notes → 20",
        )

    def test_float_values_render_without_a_trailing_zero(self):
        self.assertEqual(
            _cond(
                id="f", field="f", operator=">=", threshold=2_000_000, value=1_400_000.0, unit="usd"
            ).display,
            "f >= $2,000,000 → $1,400,000",
        )


class GroupPrimitiveTests(unittest.TestCase):
    def _pair(self, a, b):
        return [
            _cond(id="a", operator="==", threshold=1, value=1 if a else 0),
            _cond(id="b", operator="==", threshold=1, value=1 if b else 0),
        ]

    def test_all_of_and_any_of(self):
        for operator, a, b, expected in [
            ("all_of", True, True, True),
            ("all_of", True, False, False),
            ("any_of", True, False, True),
            ("any_of", False, False, False),
        ]:
            with self.subTest(operator, a=a, b=b):
                group = outreach._group(
                    id="g", label="g", operator=operator, weight=1, conditions=self._pair(a, b)
                )
                self.assertIs(group.passed, expected)

    def test_passed_can_be_overridden_for_a_non_conjunctive_predicate(self):
        group = outreach._group(
            id="gone_quiet",
            label="g",
            operator="all_of",
            weight=2,
            conditions=self._pair(True, False),
            passed=True,
        )
        self.assertIs(group.passed, True)
        self.assertEqual(group.display, "gone_quiet → true")

    def test_unsupported_group_operator_raises(self):
        with self.assertRaises(ValueError):
            outreach._group(
                id="g", label="g", operator="none_of", weight=1, conditions=self._pair(True, True)
            )

    def test_group_records_itself_not_its_children(self):
        recorded: list = []
        outreach._group(
            id="g",
            label="g",
            operator="all_of",
            weight=1,
            conditions=self._pair(True, True),
            trace=recorded,
        )
        self.assertEqual([item["id"] for item in recorded], ["g"])
        self.assertEqual([c["id"] for c in recorded[0]["conditions"]], ["a", "b"])


class FlatTraceTests(unittest.TestCase):
    def test_action_trace_is_chronological_rejected_then_matched(self):
        lead = SimpleNamespace(
            contact_name="Nobody",
            stage="active_trial",
            signed_up_date=None,
            last_login_date=None,
            last_contacted_date=None,
            quotes_created=0,
            quotes_submitted=0,
            deals_closed=0,
            estimated_book_size_usd=0,
            hubspot_notes="",
            events=[],
        )
        trace: list = []
        action_type, _reason = outreach.determine_action(lead, today=TODAY, trace=trace)
        self.assertEqual(action_type, actions.UNKNOWN)
        self.assertEqual(
            [item["id"] for item in trace],
            [
                "stage_is_demo_completed",  # R1
                "deals_power_user",  # R2
                "hold_phrase_in_notes",  # R3
                "signed_up_date_present",  # R4
                "login_recent",  # R5
            ],
        )

    def test_priority_trace_matches_the_envelope_signals(self):
        for _rec, lead in _golden_leads():
            trace: list = []
            outreach.determine_priority(lead, today=TODAY, trace=trace)
            self.assertEqual(trace, outreach.explain(lead, today=TODAY)["priority"]["signals"])


class DefaultTodayTests(unittest.TestCase):
    """`today=None` still means "the system date" for all three entry points."""

    def test_explain_defaults_today_to_the_system_date(self):
        lead = _golden_leads()[0][1]
        self.assertEqual(outreach.explain(lead)["today"], datetime.date.today().isoformat())
        self.assertIsInstance(outreach.determine_priority(lead), int)
        self.assertIsInstance(outreach.determine_action(lead)[0], str)


if __name__ == "__main__":
    unittest.main()
