"""The deterministic pass: one stored ``conditions`` payload against one lead.

:mod:`project.app.rules.utils` owns the vocabulary, reading it off the Lead and
Event columns. A field it names that nothing here resolves is refused at
evaluation rather than quietly firing. Pure Python and duck-typed like the planner's rule functions -- no database, no
provider call. A lead-authored column is sanitized as it is read, so no rule
ever matches against raw CRM text.
"""

import datetime

from project.app.rules import utils
from project.app.services import outreach, sanitize


class ConditionError(Exception):
    """A payload this engine cannot evaluate -- it names something unknown."""


def _deals_below_milestone(lead, today):
    milestone = outreach._milestone_from_notes(lead)
    if milestone is None:
        return False
    return (getattr(lead, "deals_closed", 0) or 0) < milestone


# Every computed figure in the vocabulary. Columns and their `days_since_`
# twins resolve off the lead row itself, in `_value`.
RESOLVERS = {
    utils.SOURCE_DERIVED: {
        "gone_quiet": outreach._gone_quiet,
    },
    utils.SOURCE_NOTES: {
        "milestone_from_notes": lambda lead, today: outreach._milestone_from_notes(lead),
        "deals_below_milestone": _deals_below_milestone,
    },
    utils.SOURCE_EVENTS: {
        "has_no_reply_email": lambda lead, today: outreach._had_no_reply_email(lead),
    },
}


def matches(payload, lead, today):
    """Whether ``lead`` satisfies a validated ``conditions`` payload.

    Raises :class:`ConditionError` on a payload the vocabulary no longer covers;
    the caller records that rather than letting it fire or silently pass.
    """
    if not payload:
        raise ConditionError("An empty conditions payload has no verdict.")
    return _group(payload.get("operator"), payload.get("conditions"), lead, today)


def _group(operator, children, lead, today):
    if not children:
        raise ConditionError(f"A {operator!r} group with no conditions has no verdict.")
    if operator not in utils.GROUP_OPERATORS:
        raise ConditionError(f"Unknown group operator {operator!r}.")
    check = all if operator == "all_of" else any
    return check(
        _group(child.get("operator"), child.get("conditions"), lead, today)
        if "field" not in child
        else _leaf(child, lead, today)
        for child in children
    )


def _leaf(leaf, lead, today):
    source = leaf.get("source")
    field = leaf.get("field")
    field_type = utils.fields_by_source().get(source, {}).get(field)
    if field_type is None:
        raise ConditionError(f"Unknown field {field!r} on source {source!r}.")
    return _compare(
        _value(source, field, lead, today), leaf.get("operator"), leaf.get("threshold"), field_type
    )


def _value(source, field, lead, today):
    if source == utils.SOURCE_LEAD:
        # `_as_date` only narrows datetimes; every other type passes through.
        return outreach._as_date(getattr(lead, field, None))
    resolver = RESOLVERS.get(source, {}).get(field)
    if resolver is not None:
        return resolver(lead, today)
    if source == utils.SOURCE_DERIVED and field.startswith(utils.DAYS_SINCE_PREFIX):
        column = field[len(utils.DAYS_SINCE_PREFIX) :]
        return outreach._days_since(getattr(lead, column, None), today)
    if source == utils.SOURCE_NOTES and hasattr(lead, field):
        return sanitize.sanitize_untrusted(getattr(lead, field) or "")
    # In the vocabulary, but nothing computes it yet -- see the Event columns,
    # which need an "any event where..." semantic first.
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
    """True when any of the author's phrases appears in ``value``."""
    phrases = threshold if isinstance(threshold, list) else [threshold]
    for phrase in phrases:
        if not isinstance(phrase, str) or utils.reads_as_phrase_set(phrase):
            raise ConditionError(f"{phrase!r} is not a phrase to look for.")
    text = str(value or "").lower()
    return outreach._matched_phrase(text, [p.strip().lower() for p in phrases]) is not None


def _coerce(threshold, field_type):
    if field_type == utils.DATE and isinstance(threshold, str):
        return datetime.date.fromisoformat(threshold)
    return threshold
