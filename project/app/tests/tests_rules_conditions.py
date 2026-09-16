"""The ``conditions`` payload contract: schema, vocabulary, and the rule that a
rule may never fire on lead-controlled text alone.

Pure — no database. What is stored here is what the planner must evaluate, so
anything this accepts is a promise and anything it rejects never reaches a row.
"""

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from project.app.rules import utils


def _payload(*conditions, operator="all_of", version=utils.SCHEMA_VERSION):
    return {"version": version, "operator": operator, "conditions": list(conditions)}


LEAD = utils._cond("deals_closed", ">", 20)
DERIVED = utils._cond("gone_quiet", "==", True, source="derived")
NOTES = utils._cond("hubspot_notes", "contains", "HOLD_PHRASES", source="notes")
EVENTS = utils._cond("has_no_reply_email", "==", True, source="events")


class ValidPayloadTests(SimpleTestCase):
    def test_the_builders_produce_a_payload_the_validator_accepts(self):
        utils.validate_conditions(utils._all_of(LEAD))

    def test_notes_alongside_a_corroborator_is_accepted(self):
        utils.validate_conditions(_payload(NOTES, DERIVED))

    def test_every_seeded_shape_of_condition_is_evaluable(self):
        utils.validate_conditions(
            _payload(
                utils._cond("stage", "==", "demo_completed"),
                utils._cond("signed_up_date", "absent"),
                utils._cond("last_login_date", ">=", "2026-01-01"),
                utils._cond("state", "in", ["ID", "TX"]),
                utils._cond("days_since_last_login", "<=", 21, source="derived"),
                utils._cond("milestone_from_notes", "exists", source="notes"),
            )
        )

    def test_one_level_of_grouping_is_allowed(self):
        utils.validate_conditions(_payload(LEAD, {"operator": "any_of", "conditions": [NOTES]}))


class CorroboratorTests(SimpleTestCase):
    """A rule must not be satisfiable by CRM text on its own."""

    def _refused(self, payload):
        with self.assertRaises(ValidationError) as ctx:
            utils.validate_conditions(payload)
        self.assertIn("lead-controlled text alone", str(ctx.exception))

    def test_a_notes_only_rule_is_refused(self):
        self._refused(_payload(NOTES))

    def test_an_events_only_rule_is_refused(self):
        self._refused(_payload(EVENTS))

    def test_an_any_of_branch_that_notes_alone_could_satisfy_is_refused(self):
        # `any_of` means the notes branch fires the rule by itself, so the
        # sibling lead condition corroborates nothing.
        self._refused(_payload(NOTES, LEAD, operator="any_of"))

    def test_an_any_of_of_groups_needs_a_corroborator_in_every_branch(self):
        corroborated = {"operator": "all_of", "conditions": [NOTES, LEAD]}
        self._refused(
            _payload(corroborated, {"operator": "all_of", "conditions": [NOTES]}, operator="any_of")
        )
        utils.validate_conditions(_payload(corroborated, corroborated, operator="any_of"))

    def test_hubspot_notes_cannot_be_read_as_a_lead_field(self):
        # Otherwise notes-only rules would launder through a trusted source.
        with self.assertRaises(ValidationError):
            utils.validate_conditions(
                _payload(utils._cond("hubspot_notes", "contains", "budget", source="lead"))
            )


class SchemaRejectionTests(SimpleTestCase):
    def _refused(self, payload):
        with self.assertRaises(ValidationError):
            utils.validate_conditions(payload)

    def test_payloads_that_are_not_a_versioned_object_are_refused(self):
        for payload in ("yes", [1, 2, 3], 42, None, {}, {"lol": 1}):
            with self.subTest(payload=payload):
                self._refused(payload)

    def test_a_future_schema_version_is_refused(self):
        self._refused(_payload(LEAD, version=utils.SCHEMA_VERSION + 1))

    def test_an_unknown_group_operator_is_refused(self):
        self._refused(_payload(LEAD, operator="xor"))

    def test_an_empty_condition_list_is_refused(self):
        self._refused(_payload())

    def test_groups_nest_one_level_only(self):
        self._refused(
            _payload(
                LEAD,
                {
                    "operator": "any_of",
                    "conditions": [{"operator": "all_of", "conditions": [LEAD]}],
                },
            )
        )

    def test_an_unknown_field_or_source_is_refused(self):
        self._refused(_payload(utils._cond("favourite_colour", "==", "blue")))
        self._refused(_payload(utils._cond("deals_closed", ">", 1, source="vibes")))
        self._refused(_payload(utils._cond("deals_closed", ">", 1, source="derived")))

    def test_an_unknown_key_on_a_condition_is_refused(self):
        leaf = dict(LEAD, sneaky="payload")
        self._refused(_payload(leaf))

    def test_an_operator_that_does_not_apply_to_the_field_is_refused(self):
        self._refused(_payload(utils._cond("deals_closed", "contains", "20")))
        self._refused(_payload(utils._cond("gone_quiet", ">", True, source="derived")))

    def test_a_threshold_of_the_wrong_type_is_refused(self):
        self._refused(_payload(utils._cond("deals_closed", ">", "twenty")))
        self._refused(_payload(utils._cond("deals_closed", ">", True)))
        self._refused(_payload(utils._cond("signed_up_date", ">", "last tuesday")))
        self._refused(_payload(utils._cond("gone_quiet", "==", "yes", source="derived")))

    def test_a_missing_or_surplus_threshold_is_refused(self):
        self._refused(_payload({"field": "deals_closed", "operator": ">", "source": "lead"}))
        self._refused(
            _payload(
                {
                    "field": "signed_up_date",
                    "operator": "exists",
                    "source": "lead",
                    "threshold": "2026-01-01",
                }
            )
        )

    def test_an_unknown_phrase_set_is_refused(self):
        self._refused(
            _payload(utils._cond("hubspot_notes", "contains", "HOLD_PHRSES", source="notes"), LEAD)
        )

    def test_a_literal_phrase_is_accepted_alongside_a_corroborator(self):
        utils.validate_conditions(
            _payload(utils._cond("hubspot_notes", "contains", "budget", source="notes"), LEAD)
        )


class PredicateTests(SimpleTestCase):
    def test_a_plain_predicate_passes(self):
        utils.validate_inference_predicate("the hubspot notes say they need help")

    def test_line_breaks_and_quotes_are_refused(self):
        for predicate in ('a ? "9"\nb ? "9"', "a\rb", 'they said "help"'):
            with self.subTest(predicate=predicate):
                with self.assertRaises(ValidationError):
                    utils.validate_inference_predicate(predicate)
