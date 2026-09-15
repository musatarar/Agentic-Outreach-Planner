"""The rule functions' frozen behaviour.

``determine_action`` / ``determine_priority`` are the business logic the whole
product hangs off, and their ``reason`` strings are read by a human and fed to
the prompt. These tests pin the classifications, the priorities and the reason
text byte-for-byte against the golden corpus, so a refactor of the rules can
never change what the planner decides. Pure Python, no Django.
"""

import datetime
import json
import unittest
from pathlib import Path

from evals import run_rules_eval as rules_eval
from project.app.services import actions, outreach

PARITY_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "reason_parity.json"

TODAY = rules_eval.TODAY  # datetime.date(2026, 6, 12)


def _golden_leads():
    """(record, duck-typed lead) per golden record, built as the eval harness builds them."""
    return [
        (rec, rules_eval.build_lead(rec)) for rec in rules_eval.load_golden(rules_eval.GOLDEN_PATH)
    ]


class ArityTests(unittest.TestCase):
    """the signatures four other modules depend on."""

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


class ReasonParityTests(unittest.TestCase):
    """``reason`` strings stay byte-identical to the frozen fixture — they are
    displayed and fed to ``generate_copy``."""

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


class DefaultTodayTests(unittest.TestCase):
    """``today=None`` still means "the system date" for both entry points."""

    def test_both_entry_points_default_today_to_the_system_date(self):
        lead = _golden_leads()[0][1]
        pinned = outreach.determine_action(lead, today=datetime.date.today())
        self.assertEqual(outreach.determine_action(lead), pinned)
        self.assertEqual(
            outreach.determine_priority(lead),
            outreach.determine_priority(lead, today=datetime.date.today()),
        )


if __name__ == "__main__":
    unittest.main()
