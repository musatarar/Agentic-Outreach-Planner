"""The deterministic pass: one stored ``conditions`` payload against one lead.

:mod:`project.app.rules.utils` owns the vocabulary, reading it off the Lead and
Event columns. A field it names that nothing here resolves is refused at
evaluation rather than quietly firing. Pure Python and duck-typed -- no
database, no provider call. Lead-controlled text is only ever read through the
sanitized notes blob.
"""

import datetime

from project.app.rules import utils
from project.app.services import outreach, sanitize


class ConditionError(Exception):
    """A payload this engine cannot evaluate -- it names something unknown."""


def _days_since_signup(lead, today):
    return outreach._days_since(getattr(lead, "signed_up_date", None), today)


def _days_since_last_login(lead, today):
    return outreach._days_since(getattr(lead, "last_login_date", None), today)


def _days_since_last_contact(lead, today):
    return outreach._days_since(getattr(lead, "last_contacted_date", None), today)


def _notes_blob(lead, today):
    """Combined lowercase text of hubspot notes + event notes/outcomes.

    Attacker-controlled free-text, sanitized before it is matched against; a
    phrase match is only a SIGNAL, and ``validate_conditions`` is what keeps it
    from satisfying a rule on its own (see SECURITY.md).
    """
    parts = [getattr(lead, "hubspot_notes", "") or ""]
    for event in outreach._events_list(lead):
        meta = getattr(event, "meta", None) or {}
        for key in ("notes", "subject", "outcome"):
            if meta.get(key):
                parts.append(str(meta[key]))
    return " ".join(sanitize.sanitize_untrusted(p) for p in parts).lower()


# Every non-`lead` field in the vocabulary, resolved from the lead + its events.
RESOLVERS = {
    utils.SOURCE_DERIVED: {
        "days_since_signup": _days_since_signup,
        "days_since_last_login": _days_since_last_login,
        "days_since_last_contact": _days_since_last_contact,
    },
    utils.SOURCE_NOTES: {
        # The sanitized, lowercased blob -- never the raw CRM field.
        "hubspot_notes": _notes_blob,
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
    if resolver is None:
        # In the vocabulary, but nothing computes it yet -- see the Event
        # columns, which need an "any event where..." semantic first.
        raise ConditionError(f"Nothing resolves {field!r} on source {source!r} yet.")
    return resolver(lead, today)


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
