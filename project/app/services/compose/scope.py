"""Scope engine (MUS-47 component 2): filter JSON -> queryset, through a whitelist.

The injection-sensitive half of the composer. ``scope`` arrives as user JSON and is
the only thing standing between a request body and ``QuerySet.filter()``, so it never
reaches ``.filter(**scope)``: every key is looked up in :data:`FILTERABLE`, coerced by
that entry's own coercer, and translated to an explicit predicate here.

Computed keys (contact recency, dormancy) are annotated querysets rather than Python
loops, so the query count stays constant in the number of leads.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class FilterSpec:
    """One filterable field: how to render it, and how to coerce its value."""

    label: str
    kind: str  # "select" | "int" | "days" | "bool"
    coerce: Callable[[Any], Any]
    choices: tuple[str, ...] = field(default=())


class ScopeError(ValueError):
    """A scope key that is unknown, or a value that will not coerce.

    Carries ``key`` so the view can name the offending filter in its 400 rather
    than returning a shrug.
    """

    def __init__(self, key: str, message: str) -> None:
        super().__init__(message)
        self.key = key


FILTERABLE: Mapping[str, FilterSpec] = {}


def validate_scope(scope: Mapping[str, Any]) -> dict[str, Any]:
    """Return the coerced scope, or raise :class:`ScopeError`."""
    raise NotImplementedError("scope component (MUS-47) owns validate_scope")


def apply_scope(queryset, scope: Mapping[str, Any], *, today: datetime.date):
    """Narrow ``queryset`` by an already-validated ``scope``."""
    raise NotImplementedError("scope component (MUS-47) owns apply_scope")


def scope_field_catalog() -> list[dict[str, Any]]:
    """The add-filter field list the composer's stage 01 renders."""
    raise NotImplementedError("scope component (MUS-47) owns scope_field_catalog")
