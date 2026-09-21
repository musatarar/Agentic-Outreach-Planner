"""The ``conditions`` payload contract: schema, vocabulary, and the rule that a
rule may never fire on lead-controlled text alone.

Pure — no database. What is stored here is what the evaluator must resolve, so
anything this accepts is a promise and anything it rejects never reaches a row.
"""

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from project.app.models import Lead
from project.app.rules import utils


def _payload(*conditions, operator="all_of", version=utils.SCHEMA_VERSION):
    return {"version": version, "operator": operator, "conditions": list(conditions)}


LEAD = utils._cond("deals_closed", ">", 20)
DERIVED = utils._cond("days_since_last_contact", ">=", 14, source="derived")
NOTES = utils._cond("hubspot_notes", "contains", "waiting on", source="notes")
EVENTS = utils._cond("type", "==", "email_sent", source="events")


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
                utils._cond("hubspot_notes", "contains", "waiting on", source="notes"),
            )
        )

    def test_one_level_of_grouping_is_allowed(self):
        utils.validate_conditions(_payload(LEAD, {"operator": "any_of", "conditions": [NOTES]}))


class CorroboratorTests(SimpleTestCase):
    """A conditions payload must not be satisfiable by CRM text on its own.

    An inference rule's predicate is judged separately and may stand alone;
    this is about the structured part.
    """

    def _refused(self, payload):
        with self.assertRaises(ValidationError) as ctx:
            utils.validate_conditions(payload)
        self.assertIn("lead-controlled text alone", str(ctx.exception))

    def test_a_notes_only_payload_is_refused(self):
        self._refused(_payload(NOTES))

    def test_an_events_only_payload_is_refused(self):
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
        # Otherwise a notes-only payload would launder through a trusted source.
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
        self._refused(_payload(utils._cond("signed_up_date", "contains", "2026")))

    def test_a_threshold_of_the_wrong_type_is_refused(self):
        self._refused(_payload(utils._cond("deals_closed", ">", "twenty")))
        self._refused(_payload(utils._cond("deals_closed", ">", True)))
        self._refused(_payload(utils._cond("signed_up_date", ">", "last tuesday")))
        self._refused(_payload(utils._cond("days_since_last_login", ">", "21", source="derived")))

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

    def test_a_phrase_too_short_to_mean_anything_is_refused(self):
        self._refused(
            _payload(utils._cond("hubspot_notes", "contains", "up", source="notes"), LEAD)
        )
        self._refused(
            _payload(utils._cond("hubspot_notes", "contains", "   ", source="notes"), LEAD)
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


class VocabularyTests(SimpleTestCase):
    """The nameable fields come off the Lead and Event columns, not a list
    restated in utils that the schema could leave behind."""

    def test_every_comparable_lead_column_is_nameable_under_the_lead_source(self):
        columns = {
            column.name
            for column in Lead._meta.concrete_fields
            if not column.primary_key
            and not column.is_relation
            and column.name not in Lead.UNTRUSTED_FIELDS
        }
        self.assertEqual(columns, set(utils.fields_by_source()[utils.SOURCE_LEAD]))

    def test_a_relation_is_not_a_comparable_column_so_no_rule_can_name_it(self):
        fields = utils.fields_by_source()
        for source in utils.SOURCES:
            with self.subTest(source=source):
                self.assertNotIn("owner", fields[source])

    def test_event_columns_are_nameable_under_the_events_source(self):
        events = utils.fields_by_source()[utils.SOURCE_EVENTS]
        self.assertEqual(events["type"], utils.TEXT)
        self.assertEqual(events["timestamp"], utils.DATE)

    def test_a_lead_authored_column_lands_in_notes_and_never_in_lead(self):
        fields = utils.fields_by_source()
        for column in Lead.UNTRUSTED_FIELDS:
            with self.subTest(column=column):
                self.assertIn(column, fields[utils.SOURCE_NOTES])
                self.assertNotIn(column, fields[utils.SOURCE_LEAD])
        self.assertFalse(Lead.UNTRUSTED_FIELDS & set(fields[utils.SOURCE_LEAD]))

    def test_column_types_follow_the_django_field_class(self):
        lead = utils.fields_by_source()[utils.SOURCE_LEAD]
        self.assertEqual(lead["state"], utils.TEXT)
        self.assertEqual(lead["estimated_book_size_usd"], utils.NUMBER)
        self.assertEqual(lead["signed_up_date"], utils.DATE)

    def test_keys_relations_and_uncomparable_columns_stay_out(self):
        fields = utils.fields_by_source()
        self.assertNotIn("id", fields[utils.SOURCE_LEAD])
        self.assertNotIn("lead", fields[utils.SOURCE_EVENTS])
        self.assertNotIn("id", fields[utils.SOURCE_EVENTS])
        # JSON has no comparison vocabulary here, so the payload is not nameable.
        self.assertNotIn("meta", fields[utils.SOURCE_EVENTS])

    def test_the_engines_computed_figures_keep_their_declared_sources(self):
        fields = utils.fields_by_source()
        for source, names in utils.COMPUTED_FIELDS.items():
            with self.subTest(source=source):
                self.assertLessEqual(set(names), set(fields[source]))

    def test_no_field_name_is_claimed_by_two_sources(self):
        names = [name for source in utils.SOURCES for name in utils.fields_by_source()[source]]
        self.assertEqual(len(names), len(set(names)))


class SourceResolutionTests(SimpleTestCase):
    """A condition that does not name its source gets it from the field."""

    def test_a_lead_column_resolves_to_the_lead_source(self):
        self.assertEqual(utils._cond("deals_closed", ">", 1)["source"], utils.SOURCE_LEAD)

    def test_an_event_column_resolves_to_the_events_source(self):
        self.assertEqual(utils._cond("type", "==", "login")["source"], utils.SOURCE_EVENTS)

    def test_a_lead_authored_column_resolves_to_notes(self):
        self.assertEqual(
            utils._cond("hubspot_notes", "contains", "budget")["source"], utils.SOURCE_NOTES
        )

    def test_a_computed_figure_resolves_to_its_declared_source(self):
        self.assertEqual(
            utils._cond("days_since_last_contact", ">=", 14)["source"], utils.SOURCE_DERIVED
        )

    def test_an_explicit_source_is_never_overridden(self):
        # Including a wrong one -- validate_conditions is what refuses it.
        self.assertEqual(
            utils._cond("hubspot_notes", "contains", "budget", source="lead")["source"], "lead"
        )

    def test_an_unclaimed_name_falls_through_to_lead_for_the_validator_to_refuse(self):
        self.assertEqual(utils._cond("favourite_colour", "==", "blue")["source"], "lead")
        with self.assertRaises(ValidationError):
            utils.validate_conditions(_payload(utils._cond("favourite_colour", "==", "blue")))
