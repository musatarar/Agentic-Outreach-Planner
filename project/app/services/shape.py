"""The write path for one user's declared shape.

A shape is the vocabulary every rule of its owner is written against, so a
column that moves, changes type or starts being lead-authored is a write
against those rules too. Validation at rule save only ever saw the shape of
that moment, so the check runs again here: a declaration that would leave a
stored rule unevaluable is refused rather than discovered by the engine a tick
later, with nothing surfaced to the user.

Django-only, as in ``rules/services.py`` — the HTTP layer translates these
exceptions.
"""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from project.app.models import Shape
from project.app.rules import services as rules_services


def declare(owner, fields):
    """Store ``owner``'s declaration; returns the stored :class:`Shape`.

    Raises ``django.core.exceptions.ValidationError`` — the model's verdict on
    the columns, or this module's on the rules they would strand.
    """
    shape = Shape.objects.filter(owner=owner).first() or Shape(owner=owner)
    for field, value in fields.items():
        setattr(shape, field, value)
    shape.full_clean()
    refused = rules_services.rules_refused_by(owner, shape)
    if refused:
        raise ValidationError({"rules": [_stranded(refused)]})
    return _store(shape)


def _stranded(refused):
    """Which rules this declaration would strand, and on what."""
    named = " ".join(
        f"Rule {rule.name!r} (id {rule.pk}): {messages[0]}" for rule, messages in refused
    )
    return (
        "This declaration would leave stored rules unevaluable, so the engine would refuse "
        f"them on every run: {named} Edit or delete them first."
    )


def _store(shape):
    """Save the declaration, re-reading when a concurrent first write won the row.

    One shape per user is a database constraint rather than a read-then-check,
    so the loser of that race updates the row the winner inserted instead of
    answering with a 500.
    """
    try:
        with transaction.atomic():
            shape.save()
    except IntegrityError:
        stored = Shape.objects.get(owner_id=shape.owner_id)
        stored.lead_columns = shape.lead_columns
        stored.event_columns = shape.event_columns
        stored.save()
        return stored
    return shape
