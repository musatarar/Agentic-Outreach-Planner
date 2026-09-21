"""The deterministic pass: one stored ``conditions`` payload against one lead.

:mod:`project.app.rules.utils` owns the vocabulary, reading it off the lead
owner's declared shape. A field it names that nothing here resolves is refused
at evaluation rather than quietly firing. Pure Python and duck-typed -- no
database, no provider call. Lead-authored text is only ever read sanitized.
"""

import datetime

from project.app.rules import utils
from project.app.services import outreach, sanitize


class ConditionError(Exception):
    """A payload this engine cannot evaluate -- it names something unknown."""


def matches(payload, lead, today):
    """Whether ``lead`` satisfies a validated ``conditions`` payload.

    Raises :class:`ConditionError` on a payload the lead's shape no longer
    covers; the caller records that rather than letting it fire or silently
    pass.
    """
    if not payload:
        raise ConditionError("An empty conditions payload has no verdict.")
    shape = getattr(lead, "shape", None)
    if shape is None:
        raise ConditionError("This lead's owner declares no shape, so nothing resolves.")
    return _group(payload.get("operator"), payload.get("conditions"), lead, shape, today)


def _group(operator, children, lead, shape, today):
    if not children:
        raise ConditionError(f"A {operator!r} group with no conditions has no verdict.")
    if operator not in utils.GROUP_OPERATORS:
        raise ConditionError(f"Unknown group operator {operator!r}.")
    check = all if operator == "all_of" else any
    return check(
        _group(child.get("operator"), child.get("conditions"), lead, shape, today)
        if "field" not in child
        else _leaf(child, lead, shape, today)
        for child in children
    )


def _leaf(leaf, lead, shape, today):
    source = leaf.get("source")
    field = leaf.get("field")
    field_type = utils.fields_by_source(shape).get(source, {}).get(field)
    if field_type is None:
        raise ConditionError(f"Unknown field {field!r} on source {source!r}.")
    return _compare(
        _value(source, field, lead, shape, today),
        leaf.get("operator"),
        leaf.get("threshold"),
        field_type,
    )


def _value(source, field, lead, shape, today):
    data = getattr(lead, "data", None)
    if source == utils.SOURCE_LEAD:
        return shape.value(data, field)
    if source == utils.SOURCE_NOTES:
        # Attacker-controlled free text, sanitized before it is matched against;
        # a phrase match is only a SIGNAL, and `validate_conditions` is what
        # keeps it from satisfying a rule on its own (see SECURITY.md).
        return sanitize.sanitize_untrusted(str(shape.value(data, field) or "")).lower()
    if source == utils.SOURCE_DERIVED:
        column = field[len(utils.DAYS_SINCE_PREFIX) :]
        return outreach._days_since(shape.value(data, column), today)
    # In the vocabulary, but nothing computes it yet -- the event columns need
    # an "any event where..." semantic first.
    raise ConditionError(f"Nothing resolves {field!r} on source {source!r} yet.")


def _blank(value):
    """Absent for `exists`/`absent`. ``False`` and ``0`` are present values."""
    return value is None or value == ""


def _compare(value, operator, threshold, field_type):
    if operator == "exists":
        return not _blank(value)
    if operator == "absent":
        return _blank(value)
    if operator == "contains":
        return _contains(value, threshold)
    if _blank(value):
        return False
    if operator == "in":
        return value in [_coerce(item, field_type) for item in threshold]
    threshold = _coerce(threshold, field_type)
    if operator == "==":
        return value == threshold
    if operator == "!=":
        return value != threshold
    if operator == ">":
        return value > threshold
    if operator == ">=":
        return value >= threshold
    if operator == "<":
        return value < threshold
    if operator == "<=":
        return value <= threshold
    raise ConditionError(f"Unknown operator {operator!r}.")


def _contains(value, threshold):
    return threshold.strip().lower() in str(value or "").lower()


def _coerce(threshold, field_type):
    if field_type == utils.DATE and isinstance(threshold, str):
        return datetime.date.fromisoformat(threshold)
    return threshold
