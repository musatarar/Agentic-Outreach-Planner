"""The deterministic pass: a stored conditions payload against one lead. Pure
Python, no Django -- leads are SimpleNamespace stubs."""

import datetime
import unittest
from types import SimpleNamespace

from project.app.actions import evaluate
from project.app.rules import utils
from project.app.rules.utils import _all_of, _cond

TODAY = datetime.date(2026, 6, 12)


def _event(type_, ts, **meta):
    return SimpleNamespace(type=type_, timestamp=ts, meta=meta)


def _lead(**kwargs):
    defaults = dict(
        id="lead_x",
        stage="active_trial",
        state="CO",
        num_producers=4,
        years_in_business=9,
        estimated_book_size_usd=1_400_000,
        signed_up_date=TODAY - datetime.timedelta(days=50),
        last_login_date=TODAY - datetime.timedelta(days=2),
        last_contacted_date=TODAY - datetime.timedelta(days=5),
        quotes_created=10,
        quotes_submitted=6,
        deals_closed=3,
        hubspot_notes="",
        events=[],
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class LeadSourceTests(unittest.TestCase):
    def test_a_number_comparison_reads_the_lead_field(self):
        payload = _all_of(_cond("deals_closed", ">", 2))
        self.assertTrue(evaluate.matches(payload, _lead(deals_closed=3), TODAY))
        self.assertFalse(evaluate.matches(payload, _lead(deals_closed=2), TODAY))

    def test_a_date_threshold_is_compared_as_a_date_not_a_string(self):
        payload = _all_of(_cond("signed_up_date", "<", "2026-01-01"))
        self.assertTrue(
            evaluate.matches(payload, _lead(signed_up_date=datetime.date(2025, 12, 31)), TODAY)
        )
        self.assertFalse(
            evaluate.matches(payload, _lead(signed_up_date=datetime.date(2026, 1, 2)), TODAY)
        )

    def test_exists_and_absent_split_on_a_null_field(self):
        exists = _all_of(_cond("signed_up_date", "exists"))
        absent = _all_of(_cond("signed_up_date", "absent"))
        signed_up = _lead()
        never = _lead(signed_up_date=None)
        self.assertTrue(evaluate.matches(exists, signed_up, TODAY))
        self.assertFalse(evaluate.matches(exists, never, TODAY))
        self.assertTrue(evaluate.matches(absent, never, TODAY))

    def test_a_zero_count_exists_rather_than_reading_as_absent(self):
        self.assertTrue(
            evaluate.matches(_all_of(_cond("deals_closed", "exists")), _lead(deals_closed=0), TODAY)
        )

    def test_in_matches_any_listed_value(self):
        payload = _all_of(_cond("stage", "in", ["demo_completed", "active_trial"]))
        self.assertTrue(evaluate.matches(payload, _lead(), TODAY))
        self.assertFalse(evaluate.matches(payload, _lead(stage="churned"), TODAY))

    def test_a_missing_value_fails_a_comparison_rather_than_erroring(self):
        payload = _all_of(_cond("last_login_date", ">", "2026-01-01"))
        self.assertFalse(evaluate.matches(payload, _lead(last_login_date=None), TODAY))


class DerivedSourceTests(unittest.TestCase):
    def test_days_since_last_login_counts_from_the_run_date(self):
        payload = _all_of(_cond("days_since_last_login", ">", 21, source="derived"))
        self.assertTrue(
            evaluate.matches(
                payload, _lead(last_login_date=TODAY - datetime.timedelta(days=22)), TODAY
            )
        )
        self.assertFalse(
            evaluate.matches(
                payload, _lead(last_login_date=TODAY - datetime.timedelta(days=21)), TODAY
            )
        )


class DerivedDateTests(unittest.TestCase):
    def test_days_since_signup_and_last_contact_count_from_the_run_date(self):
        lead = _lead(
            signed_up_date=TODAY - datetime.timedelta(days=40),
            last_contacted_date=TODAY - datetime.timedelta(days=9),
        )
        self.assertTrue(
            evaluate.matches(
                _all_of(_cond("days_since_signup", ">", 30, source="derived")), lead, TODAY
            )
        )
        self.assertTrue(
            evaluate.matches(
                _all_of(_cond("days_since_last_contact", "==", 9, source="derived")), lead, TODAY
            )
        )

    def test_a_never_contacted_lead_has_no_days_since_last_contact(self):
        payload = _all_of(_cond("days_since_last_contact", ">", 0, source="derived"))
        self.assertFalse(evaluate.matches(payload, _lead(last_contacted_date=None), TODAY))


class NotesSourceTests(unittest.TestCase):
    def test_contains_matches_a_literal_phrase_case_insensitively(self):
        payload = _all_of(_cond("hubspot_notes", "contains", "volume pricing", source="notes"))
        self.assertTrue(
            evaluate.matches(payload, _lead(hubspot_notes="Asked about VOLUME PRICING"), TODAY)
        )

    def test_contains_also_reads_event_notes_not_just_the_crm_field(self):
        payload = _all_of(_cond("hubspot_notes", "contains", "circle back", source="notes"))
        lead = _lead(events=[_event("call_logged", TODAY, notes="asked us to circle back in Q3")])
        self.assertTrue(evaluate.matches(payload, lead, TODAY))


class GroupTests(unittest.TestCase):
    def test_all_of_needs_every_child_and_any_of_needs_one(self):
        hit = _cond("deals_closed", ">", 2)
        miss = _cond("quotes_submitted", ">", 100)
        self.assertFalse(evaluate.matches(_all_of(hit, miss), _lead(), TODAY))
        self.assertTrue(
            evaluate.matches(
                {"version": utils.SCHEMA_VERSION, "operator": "any_of", "conditions": [hit, miss]},
                _lead(),
                TODAY,
            )
        )

    def test_a_nested_group_is_evaluated_as_its_own_branch(self):
        payload = {
            "version": utils.SCHEMA_VERSION,
            "operator": "all_of",
            "conditions": [
                _cond("deals_closed", ">", 2),
                {
                    "operator": "any_of",
                    "conditions": [
                        _cond("quotes_submitted", ">", 100),
                        _cond("quotes_created", ">", 1),
                    ],
                },
            ],
        }
        self.assertTrue(evaluate.matches(payload, _lead(), TODAY))

    def test_an_unknown_field_is_refused_rather_than_silently_missing(self):
        payload = _all_of(
            {"field": "favourite_colour", "operator": "==", "source": "lead", "threshold": "red"}
        )
        with self.assertRaises(evaluate.ConditionError):
            evaluate.matches(payload, _lead(), TODAY)

    def test_an_operator_the_engine_does_not_implement_is_refused(self):
        payload = _all_of(
            {"field": "deals_closed", "operator": "~=", "source": "lead", "threshold": 2}
        )
        with self.assertRaises(evaluate.ConditionError):
            evaluate.matches(payload, _lead(), TODAY)

    def test_an_unknown_group_operator_is_refused(self):
        payload = {
            "version": utils.SCHEMA_VERSION,
            "operator": "none_of",
            "conditions": [_cond("deals_closed", ">", 2)],
        }
        with self.assertRaises(evaluate.ConditionError):
            evaluate.matches(payload, _lead(), TODAY)

    def test_not_equal_reads_the_lead_field(self):
        payload = _all_of(_cond("stage", "!=", "churned"))
        self.assertTrue(evaluate.matches(payload, _lead(), TODAY))
        self.assertFalse(evaluate.matches(payload, _lead(stage="churned"), TODAY))

    def test_an_empty_payload_has_no_verdict(self):
        with self.assertRaises(evaluate.ConditionError):
            evaluate.matches({}, _lead(), TODAY)

    def test_an_empty_group_has_no_verdict_rather_than_firing_on_everything(self):
        payload = {"version": utils.SCHEMA_VERSION, "operator": "all_of", "conditions": []}
        with self.assertRaises(evaluate.ConditionError):
            evaluate.matches(payload, _lead(), TODAY)


if __name__ == "__main__":
    unittest.main()
