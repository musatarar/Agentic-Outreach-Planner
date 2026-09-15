"""Builders for the versioned ``OutreachRule.conditions`` payload."""

from project.app.rules.models import OutreachRule


def _cond(field, operator, threshold=None, source="lead"):
    condition = {"field": field, "operator": operator, "source": source}
    if threshold is not None:
        condition["threshold"] = threshold
    return condition


def _all_of(*conditions):
    return {
        "version": OutreachRule.CONDITIONS_SCHEMA_VERSION,
        "operator": "all_of",
        "conditions": list(conditions),
    }
