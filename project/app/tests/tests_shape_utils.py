"""The demo shape, for suites that need a lead to mean something.

Imported from the seed command rather than restated, so the declaration the
demo runs on is the declaration the tests hold the engine to.
"""

from project.app.management.commands.seed_rules_catalog import (
    EVENT_COLUMNS,
    LEAD_COLUMNS,
)
from project.app.models import Shape


def shape(**overrides):
    """An unsaved demo shape — all the vocabulary and the evaluator need."""
    fields = {"lead_columns": LEAD_COLUMNS, "event_columns": EVENT_COLUMNS}
    fields.update(overrides)
    return Shape(**fields)


def shape_for(owner, **overrides):
    """The demo shape, stored for ``owner``."""
    stored = shape(owner=owner, **overrides)
    stored.save()
    return stored
