"""The ``conditions`` payload: its vocabulary, its builders and its validator.

A rule's structured predicate is data, so what it may name is a contract, not a
convention: the evaluator has to resolve exactly this vocabulary, and a payload
naming anything else would be stored happily and then never fire.
:func:`validate_conditions` is that contract, and every write runs it.

The vocabulary is read off the models rather than restated here: a ``Lead``
column is named under ``lead`` and an ``Event`` column under ``events``, so a
schema change moves the vocabulary with it instead of leaving the two to
drift. Figures the engine computes have no column behind them, so those stay
declared in :data:`COMPUTED_FIELDS`.

A payload may name more than the evaluator resolves: an unresolved field is
refused at evaluation rather than quietly firing.

Sources split by who controls the value. ``lead`` and ``derived`` are the
agency's own record and figures computed from it; ``notes`` and ``events``
carry free text a lead can write. A conditions payload may read the untrusted
ones, but is never satisfiable by them alone — see
:data:`CORROBORATING_SOURCES`.
"""

import datetime
import functools

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import models

SCHEMA_VERSION = 1

SOURCE_LEAD = "lead"
SOURCE_DERIVED = "derived"
SOURCE_NOTES = "notes"
SOURCE_EVENTS = "events"

# Resolution order for a condition that does not name its source, and the order
# error messages list sources in.
SOURCES = (SOURCE_LEAD, SOURCE_DERIVED, SOURCE_NOTES, SOURCE_EVENTS)

# Sources whose values the lead cannot author, so a condition reading one is
# enough to corroborate a branch that also reads CRM text. (An inference
# rule's predicate is judged separately, and may stand alone.)
CORROBORATING_SOURCES = frozenset({SOURCE_LEAD, SOURCE_DERIVED})

NUMBER = "number"
DATE = "date"
TEXT = "text"
BOOL = "bool"

# The model behind each column-sourced source, and where that source sends the
# columns its subject authors. `lead` is corroborating, so a lead-authored
# column lands in `notes` instead; `events` is already untrusted, so it keeps
# its own.
COLUMN_SOURCES = {
    SOURCE_LEAD: ("app.Lead", SOURCE_NOTES),
    SOURCE_EVENTS: ("app.Event", SOURCE_EVENTS),
}

# Django column class -> condition type, most specific first. A column whose
# class is absent here has no comparison vocabulary, so it stays out of the
# vocabulary rather than being guessed at.
COLUMN_TYPES = (
    ((models.BooleanField,), BOOL),
    ((models.DateField,), DATE),  # DateTimeField subclasses it; both compare as dates
    ((models.IntegerField, models.FloatField, models.DecimalField), NUMBER),
    ((models.CharField, models.TextField), TEXT),
)

# Figures the engine computes per lead. No column carries them, so unlike the
# model-sourced fields these are declared.
COMPUTED_FIELDS = {
    SOURCE_DERIVED: {
        "days_since_signup": NUMBER,
        "days_since_last_login": NUMBER,
        "days_since_last_contact": NUMBER,
    },
}

GROUP_OPERATORS = frozenset({"all_of", "any_of"})
NO_THRESHOLD_OPERATORS = frozenset({"exists", "absent"})

OPERATORS_BY_TYPE = {
    NUMBER: frozenset({"==", "!=", ">", ">=", "<", "<=", "in", "exists", "absent"}),
    DATE: frozenset({"==", "!=", ">", ">=", "<", "<=", "exists", "absent"}),
    TEXT: frozenset({"==", "!=", "in", "contains", "exists", "absent"}),
    BOOL: frozenset({"==", "!=", "exists", "absent"}),
}

# A `contains` threshold is one literal phrase; several go in an `any_of` group.
MIN_LITERAL_PHRASE_CHARS = 3

LEAF_KEYS = frozenset({"field", "operator", "source", "threshold"})
GROUP_KEYS = frozenset({"operator", "conditions"})
ROOT_KEYS = frozenset({"version", "operator", "conditions"})

# An inference predicate renders into one line of a larger prompt. These
# characters would let a stored predicate forge a second line or a second
# answer slot, so they never reach the prompt.
PREDICATE_FORBIDDEN = ('"', "\n", "\r")


@functools.cache
def fields_by_source():
    """Every field a condition may name, by source and type — read-only.

    Model columns first: each source in :data:`COLUMN_SOURCES` takes its
    model's concrete columns, minus the primary key, the relations and any
    column whose type has no comparison vocabulary. A column its subject
    authors goes to that source's untrusted sink, so ``hubspot_notes`` is
    reachable as ``notes`` and never as ``lead``.
    """
    fields = {source: {} for source in SOURCES}
    for source, names in COMPUTED_FIELDS.items():
        fields[source].update(names)
    for source, (label, untrusted_sink) in COLUMN_SOURCES.items():
        model = apps.get_model(label)
        for column in model._meta.concrete_fields:
            if column.primary_key or column.is_relation:
                continue
            field_type = _column_type(column)
            if field_type is None:
                continue
            owner = untrusted_sink if column.name in model.UNTRUSTED_FIELDS else source
            fields[owner][column.name] = field_type
    return fields


def source_for(field):
    """The source that owns ``field``, first match in :data:`SOURCES` order.

    An unclaimed name answers ``lead`` so that :func:`validate_conditions`
    stays the one place an unknown field is refused.
    """
    for source in SOURCES:
        if field in fields_by_source()[source]:
            return source
    return SOURCE_LEAD


def _column_type(column):
    for classes, field_type in COLUMN_TYPES:
        if isinstance(column, classes):
            return field_type
    return None


def _cond(field, operator, threshold=None, source=None):
    condition = {"field": field, "operator": operator, "source": source or source_for(field)}
    if threshold is not None:
        condition["threshold"] = threshold
    return condition


def _all_of(*conditions):
    return {
        "version": SCHEMA_VERSION,
        "operator": "all_of",
        "conditions": list(conditions),
    }


def _any_of(*conditions):
    """A nested group, so no ``version`` — only the root payload carries one."""
    return {"operator": "any_of", "conditions": list(conditions)}


def validate_conditions(payload):
    """Check a ``conditions`` payload against the schema and the vocabulary.

    Raises ``ValidationError``; returns None when the payload is evaluable.
    """
    if not isinstance(payload, dict):
        raise ValidationError("conditions must be an object.")
    unknown = set(payload) - ROOT_KEYS
    if unknown:
        raise ValidationError(f"conditions has unknown key(s): {_listed(unknown)}.")
    version = payload.get("version")
    if version != SCHEMA_VERSION:
        raise ValidationError(f"conditions.version must be {SCHEMA_VERSION}, got {version!r}.")

    operator = payload.get("operator")
    children = payload.get("conditions")
    _validate_group(operator, children, "conditions", nested=False)

    if not _branch_corroborated({"operator": operator, "conditions": children}):
        raise ValidationError(
            "These conditions can be satisfied by lead-controlled text alone: "
            "every branch needs at least one 'lead' or 'derived' condition."
        )


def validate_inference_predicate(text):
    """Check a predicate can render as exactly one prompt line that names one
    answer slot. Raises ``ValidationError``."""
    for char in PREDICATE_FORBIDDEN:
        if char in text:
            raise ValidationError(
                "An inference predicate must be a single line and cannot contain "
                "a double quote — those would let it forge extra prompt lines or "
                "a second answer."
            )


def _validate_group(operator, children, path, *, nested):
    if operator not in GROUP_OPERATORS:
        raise ValidationError(f"{path}.operator must be 'all_of' or 'any_of', got {operator!r}.")
    if not isinstance(children, list) or not children:
        raise ValidationError(f"{path}.conditions must be a non-empty list.")
    for index, child in enumerate(children):
        child_path = f"{path}[{index}]"
        if not isinstance(child, dict):
            raise ValidationError(f"{child_path} must be an object.")
        if "field" in child:
            _validate_leaf(child, child_path)
        elif "operator" in child:
            if nested:
                raise ValidationError(f"{child_path}: groups nest one level only.")
            unknown = set(child) - GROUP_KEYS
            if unknown:
                raise ValidationError(f"{child_path} has unknown key(s): {_listed(unknown)}.")
            _validate_group(child.get("operator"), child.get("conditions"), child_path, nested=True)
        else:
            raise ValidationError(f"{child_path} must be a condition or a group.")


def _validate_leaf(leaf, path):
    unknown = set(leaf) - LEAF_KEYS
    if unknown:
        raise ValidationError(f"{path} has unknown key(s): {_listed(unknown)}.")
    fields = fields_by_source()
    source = leaf.get("source")
    if source not in fields:
        raise ValidationError(f"{path}.source must be one of {_listed(fields)}, got {source!r}.")
    field = leaf.get("field")
    if field not in fields[source]:
        raise ValidationError(
            f"{path}: {source!r} has no field {field!r}; known: {_listed(fields[source])}."
        )
    field_type = fields[source][field]
    operator = leaf.get("operator")
    if operator not in OPERATORS_BY_TYPE[field_type]:
        raise ValidationError(
            f"{path}: {operator!r} does not apply to {field!r} ({field_type}); "
            f"known: {_listed(OPERATORS_BY_TYPE[field_type])}."
        )
    _validate_threshold(leaf, operator, field_type, path)


def _validate_threshold(leaf, operator, field_type, path):
    threshold = leaf.get("threshold")
    if operator in NO_THRESHOLD_OPERATORS:
        if threshold is not None:
            raise ValidationError(f"{path}: {operator!r} takes no threshold.")
        return
    if "threshold" not in leaf or threshold is None:
        raise ValidationError(f"{path}: {operator!r} needs a threshold.")
    if operator == "contains":
        _validate_phrase(threshold, path)
        return
    if operator == "in":
        if not isinstance(threshold, list) or not threshold:
            raise ValidationError(f"{path}: 'in' needs a non-empty list threshold.")
        for item in threshold:
            _validate_scalar(item, field_type, path)
        return
    _validate_scalar(threshold, field_type, path)


def _validate_phrase(threshold, path):
    if not isinstance(threshold, str) or not threshold.strip():
        raise ValidationError(f"{path}: 'contains' needs a phrase.")
    if len(threshold.strip()) < MIN_LITERAL_PHRASE_CHARS:
        raise ValidationError(
            f"{path}: a literal phrase needs at least {MIN_LITERAL_PHRASE_CHARS} characters."
        )


def _validate_scalar(value, field_type, path):
    if field_type == BOOL:
        if not isinstance(value, bool):
            raise ValidationError(f"{path}: expected true or false, got {value!r}.")
        return
    # bool is an int in Python; a boolean threshold on a count is a mistake.
    if field_type == NUMBER:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(f"{path}: expected a number, got {value!r}.")
        return
    if field_type == DATE:
        if not isinstance(value, str):
            raise ValidationError(f"{path}: expected an ISO date string, got {value!r}.")
        try:
            datetime.date.fromisoformat(value)
        except ValueError:
            raise ValidationError(f"{path}: expected an ISO date (YYYY-MM-DD), got {value!r}.")
        return
    if not isinstance(value, str):
        raise ValidationError(f"{path}: expected text, got {value!r}.")


def _branch_corroborated(node):
    """Whether every way of satisfying ``node`` involves a corroborating source.

    A leaf corroborates only if its own source does. An ``all_of`` holds only
    when all its children hold, so one corroborated child is enough; an
    ``any_of`` can be satisfied by any single child, so every child must carry
    its own corroborator.
    """
    if "field" in node:
        return node.get("source") in CORROBORATING_SOURCES
    children = node.get("conditions") or []
    check = any if node.get("operator") == "all_of" else all
    return check(_branch_corroborated(child) for child in children)


def _listed(values):
    return ", ".join(repr(value) for value in sorted(values))
