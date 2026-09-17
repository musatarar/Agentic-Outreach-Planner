"""The deterministic pass: one stored ``conditions`` payload against one lead.

:mod:`project.app.rules.utils` owns the vocabulary and refuses on write
anything this module cannot evaluate; every field it lists is resolved here.
Pure Python and duck-typed like the planner's rule functions -- no database, no
provider call. Lead-controlled text is only ever read through the planner's
sanitized notes blob.
"""

import datetime

from project.app.rules import utils
from project.app.services import outreach


class ConditionError(Exception):
    """A payload this engine cannot evaluate -- it names something unknown."""


# Named phrase sets a `contains` threshold may reference, resolved to the
# planner's own lists so the two can never drift.
PHRASE_SETS = {
    "HOLD_PHRASES": outreach.HOLD_PHRASES,
    "STALL_PHRASES": outreach.STALL_PHRASES,
}


def _days_since_signup(lead, today):
    return outreach._days_since(getattr(lead, "signed_up_date", None), today)


def _days_since_last_login(lead, today):
    return outreach._days_since(getattr(lead, "last_login_date", None), today)


def _days_since_last_contact(lead, today):
    return outreach._days_since(getattr(lead, "last_contacted_date", None), today)


def _deals_below_milestone(lead, today):
    milestone = outreach._milestone_from_notes(lead)
    if milestone is None:
        return False
    return (getattr(lead, "deals_closed", 0) or 0) < milestone


# Every non-`lead` field in the vocabulary, resolved from the lead + its events.
RESOLVERS = {
    utils.SOURCE_DERIVED: {
        "days_since_signup": _days_since_signup,
        "days_since_last_login": _days_since_last_login,
        "days_since_last_contact": _days_since_last_contact,
        "gone_quiet": outreach._gone_quiet,
    },
    utils.SOURCE_NOTES: {
        # The sanitized, lowercased blob -- never the raw CRM field.
        "hubspot_notes": lambda lead, today: outreach._notes_blob(lead),
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
    field_type = utils.FIELDS.get(source, {}).get(field)
    if field_type is None:
        raise ConditionError(f"Unknown field {field!r} on source {source!r}.")
    return _compare(
        _value(source, field, lead, today), leaf.get("operator"), leaf.get("threshold"), field_type
    )


def _value(source, field, lead, today):
    if source == utils.SOURCE_LEAD:
        # `_as_date` only narrows datetimes; every other type passes through.
        return outreach._as_date(getattr(lead, field, None))
    return RESOLVERS[source][field](lead, today)


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
    text = str(value or "").lower()
    phrases = PHRASE_SETS.get(threshold)
    if phrases is not None:
        return outreach._matched_phrase(text, phrases) is not None
    if threshold in utils.PHRASE_SETS:
        raise ConditionError(f"Phrase set {threshold!r} has no phrases behind it.")
    return threshold.strip().lower() in text


def _coerce(threshold, field_type):
    if field_type == utils.DATE and isinstance(threshold, str):
        return datetime.date.fromisoformat(threshold)
    return threshold
