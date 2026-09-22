"""The ``conditions`` payload contract: schema, vocabulary, and the rule that a
rule may never fire on lead-controlled text alone.

Pure — no database. What is stored here is what the evaluator must resolve, so
anything this accepts is a promise and anything it rejects never reaches a row.
"""

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from project.app.models import Shape
from project.app.rules import utils
from project.app.tests.tests_shape_utils import shape

SHAPE = shape()


def _cond(field, operator, threshold=None, source=None):
    return utils._cond(field, operator, threshold, source=source, shape=SHAPE)


def _validate(payload, against=SHAPE):
    utils.validate_conditions(payload, against)


def _payload(*conditions, operator="all_of", version=utils.SCHEMA_VERSION):
    return {"version": version, "operator": operator, "conditions": list(conditions)}


LEAD = _cond("deals_closed", ">", 20)
DERIVED = _cond("days_since_last_contacted_date", ">=", 14, source="derived")
NOTES = _cond("hubspot_notes", "contains", "waiting on", source="notes")
EVENTS = _cond("type", "==", "email_sent", source="events")


class ValidPayloadTests(SimpleTestCase):
    def test_the_builders_produce_a_payload_the_validator_accepts(self):
        _validate(utils._all_of(LEAD))

    def test_notes_alongside_a_corroborator_is_accepted(self):
        _validate(_payload(NOTES, DERIVED))

    def test_every_seeded_shape_of_condition_is_evaluable(self):
        _validate(
            _payload(
                _cond("stage", "==", "demo_completed"),
                _cond("signed_up_date", "absent"),
                _cond("last_login_date", ">=", "2026-01-01"),
                _cond("state", "in", ["ID", "TX"]),
                _cond("days_since_last_login_date", "<=", 21, source="derived"),
                _cond("hubspot_notes", "contains", "waiting on", source="notes"),
            )
        )

    def test_one_level_of_grouping_is_allowed(self):
        _validate(_payload(LEAD, {"operator": "any_of", "conditions": [NOTES]}))


class CorroboratorTests(SimpleTestCase):
    """A conditions payload must not be satisfiable by CRM text on its own.

    An inference rule's predicate is judged separately and may stand alone;
    this is about the structured part.
    """

    def _refused(self, payload):
        with self.assertRaises(ValidationError) as ctx:
            _validate(payload)
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
        _validate(_payload(corroborated, corroborated, operator="any_of"))

    def test_hubspot_notes_cannot_be_read_as_a_lead_field(self):
        # Otherwise a notes-only payload would launder through a trusted source.
        with self.assertRaises(ValidationError):
            _validate(_payload(_cond("hubspot_notes", "contains", "budget", source="lead")))


class SchemaRejectionTests(SimpleTestCase):
    def _refused(self, payload):
        with self.assertRaises(ValidationError):
            _validate(payload)

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
        self._refused(_payload(_cond("favourite_colour", "==", "blue")))
        self._refused(_payload(_cond("deals_closed", ">", 1, source="vibes")))
        self._refused(_payload(_cond("deals_closed", ">", 1, source="derived")))

    def test_an_unknown_key_on_a_condition_is_refused(self):
        leaf = dict(LEAD, sneaky="payload")
        self._refused(_payload(leaf))

    def test_an_operator_that_does_not_apply_to_the_field_is_refused(self):
        self._refused(_payload(_cond("deals_closed", "contains", "20")))
        self._refused(_payload(_cond("signed_up_date", "contains", "2026")))

    def test_a_threshold_of_the_wrong_type_is_refused(self):
        self._refused(_payload(_cond("deals_closed", ">", "twenty")))
        self._refused(_payload(_cond("deals_closed", ">", True)))
        self._refused(_payload(_cond("signed_up_date", ">", "last tuesday")))
        self._refused(_payload(_cond("days_since_last_login_date", ">", "21", source="derived")))

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
        self._refused(_payload(_cond("hubspot_notes", "contains", "up", source="notes"), LEAD))
        self._refused(_payload(_cond("hubspot_notes", "contains", "   ", source="notes"), LEAD))

    def test_a_literal_phrase_is_accepted_alongside_a_corroborator(self):
        _validate(_payload(_cond("hubspot_notes", "contains", "budget", source="notes"), LEAD))


class PredicateTests(SimpleTestCase):
    def test_a_plain_predicate_passes(self):
        utils.validate_inference_predicate("the hubspot notes say they need help")

    def test_line_breaks_and_quotes_are_refused(self):
        for predicate in ('a ? "9"\nb ? "9"', "a\rb", 'they said "help"'):
            with self.subTest(predicate=predicate):
                with self.assertRaises(ValidationError):
                    utils.validate_inference_predicate(predicate)


class VocabularyTests(SimpleTestCase):
    """The nameable fields come off the owner's declared shape, not a list
    restated in utils that a re-declaration could leave behind."""

    def test_every_trusted_lead_column_is_nameable_under_the_lead_source(self):
        trusted = {column["name"] for column in SHAPE.trusted()}
        self.assertEqual(trusted, set(utils.fields_by_source(SHAPE)[utils.SOURCE_LEAD]))

    def test_event_columns_are_nameable_under_the_events_source(self):
        events = utils.fields_by_source(SHAPE)[utils.SOURCE_EVENTS]
        self.assertEqual(events["type"], utils.TEXT)
        self.assertEqual(events["premium"], utils.NUMBER)

    def test_the_structural_event_timestamp_is_nameable_though_undeclared(self):
        events = utils.fields_by_source(SHAPE)[utils.SOURCE_EVENTS]
        self.assertEqual(events[utils.EVENT_TIMESTAMP], utils.DATE)

    def test_a_lead_authored_column_lands_in_notes_and_never_in_lead(self):
        fields = utils.fields_by_source(SHAPE)
        authored = {column["name"] for column in SHAPE.authored()}
        self.assertTrue(authored)
        self.assertEqual(authored, set(fields[utils.SOURCE_NOTES]))
        self.assertFalse(authored & set(fields[utils.SOURCE_LEAD]))

    def test_every_trusted_date_column_gets_a_days_since_twin(self):
        derived = utils.fields_by_source(SHAPE)[utils.SOURCE_DERIVED]
        dates = [c["name"] for c in SHAPE.trusted() if c["type"] == utils.DATE]
        self.assertEqual(set(derived), {f"{utils.DAYS_SINCE_PREFIX}{name}" for name in dates})
        self.assertTrue(all(field_type == utils.NUMBER for field_type in derived.values()))

    def test_column_types_follow_the_declaration(self):
        lead = utils.fields_by_source(SHAPE)[utils.SOURCE_LEAD]
        self.assertEqual(lead["state"], utils.TEXT)
        self.assertEqual(lead["estimated_book_size_usd"], utils.NUMBER)
        self.assertEqual(lead["signed_up_date"], utils.DATE)

    def test_an_undeclared_name_is_in_no_source(self):
        fields = utils.fields_by_source(SHAPE)
        for source in utils.SOURCES:
            with self.subTest(source=source):
                self.assertNotIn("owner", fields[source])
                self.assertNotIn("id", fields[source])

    def test_renaming_a_column_takes_the_old_name_out_of_the_vocabulary(self):
        renamed = shape(
            lead_columns=[
                {"name": "closed_deals", "type": "number", "lead_authored": False},
                {"name": "agency_name", "type": "text", "lead_authored": False},
                {"name": "contact_name", "type": "text", "lead_authored": False},
            ]
        )
        fields = utils.fields_by_source(renamed)[utils.SOURCE_LEAD]
        self.assertIn("closed_deals", fields)
        self.assertNotIn("deals_closed", fields)
        with self.assertRaises(ValidationError):
            _validate(_payload(LEAD), renamed)

    def test_an_authored_column_is_nameable_at_the_type_it_was_declared(self):
        typed = shape(
            lead_columns=[
                {"name": "deals_closed", "type": "number", "lead_authored": False},
                {"name": "self_reported_seats", "type": "number", "lead_authored": True},
            ]
        )
        notes = utils.fields_by_source(typed)[utils.SOURCE_NOTES]
        self.assertEqual(notes["self_reported_seats"], utils.NUMBER)
        _validate(
            _payload(
                _cond("deals_closed", ">", 1),
                {
                    "field": "self_reported_seats",
                    "operator": ">",
                    "source": utils.SOURCE_NOTES,
                    "threshold": 5,
                },
            ),
            typed,
        )

    def test_a_stored_column_missing_its_name_or_type_names_nothing(self):
        malformed = shape(
            lead_columns=[{"type": "text", "lead_authored": False}, {"name": "stage"}],
            event_columns=[{"name": "premium"}],
        )
        fields = utils.fields_by_source(malformed)
        self.assertEqual(fields[utils.SOURCE_LEAD], {})
        self.assertEqual(fields[utils.SOURCE_NOTES], {})
        self.assertEqual(fields[utils.SOURCE_EVENTS], {utils.EVENT_TIMESTAMP: utils.DATE})

    def test_a_shape_that_declares_nothing_names_only_the_event_timestamp(self):
        empty = Shape()
        fields = utils.fields_by_source(empty)
        self.assertEqual(fields[utils.SOURCE_LEAD], {})
        self.assertEqual(fields[utils.SOURCE_NOTES], {})
        self.assertEqual(fields[utils.SOURCE_DERIVED], {})
        self.assertEqual(fields[utils.SOURCE_EVENTS], {utils.EVENT_TIMESTAMP: utils.DATE})

    def test_no_field_name_is_claimed_by_two_sources(self):
        fields = utils.fields_by_source(SHAPE)
        names = [name for source in utils.SOURCES for name in fields[source]]
        self.assertEqual(len(names), len(set(names)))


class SourceResolutionTests(SimpleTestCase):
    """A condition that does not name its source gets it from the field."""

    def test_a_lead_column_resolves_to_the_lead_source(self):
        self.assertEqual(_cond("deals_closed", ">", 1)["source"], utils.SOURCE_LEAD)

    def test_without_a_shape_every_name_falls_through_to_lead(self):
        # The builder has no vocabulary to consult; the validator refuses it.
        self.assertEqual(
            utils._cond("hubspot_notes", "contains", "budget")["source"], utils.SOURCE_LEAD
        )

    def test_an_event_column_resolves_to_the_events_source(self):
        self.assertEqual(_cond("type", "==", "login")["source"], utils.SOURCE_EVENTS)

    def test_a_lead_authored_column_resolves_to_notes(self):
        self.assertEqual(_cond("hubspot_notes", "contains", "budget")["source"], utils.SOURCE_NOTES)

    def test_a_computed_figure_resolves_to_its_declared_source(self):
        self.assertEqual(
            _cond("days_since_last_contacted_date", ">=", 14)["source"], utils.SOURCE_DERIVED
        )

    def test_an_explicit_source_is_never_overridden(self):
        # Including a wrong one -- validate_conditions is what refuses it.
        self.assertEqual(
            _cond("hubspot_notes", "contains", "budget", source="lead")["source"], "lead"
        )

    def test_an_unclaimed_name_falls_through_to_lead_for_the_validator_to_refuse(self):
        self.assertEqual(_cond("favourite_colour", "==", "blue")["source"], "lead")
        with self.assertRaises(ValidationError):
            _validate(_payload(_cond("favourite_colour", "==", "blue")))
