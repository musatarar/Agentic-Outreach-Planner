"""Scope engine (MUS-47 component 2): filter JSON -> queryset, through a whitelist.

The injection-sensitive half of the composer. ``scope`` arrives as user JSON and is
the only thing standing between a request body and ``QuerySet.filter()``, so it never
reaches ``.filter(**scope)``: every key is looked up in :data:`FILTERABLE`, coerced by
that entry's own coercer, and translated to a :class:`~django.db.models.Q` built here.
A key the whitelist does not know is refused *by name* rather than dropped -- dropping
it would silently run against a wider lead set than the operator was shown.

Computed keys (contact recency, dormancy) are annotated querysets rather than Python
loops, so the query count stays constant in the number of leads.

Three of the four computed filters are about *absence*, and they do not all mean the
same thing:

* never contacted is **maximally overdue** -- a NULL ``last_contacted_date`` is the
  lead most wanted, and an ``__lt`` comparison drops exactly that row;
* never logged in is **maximally dormant**, for the same reason;
* never signed up did **not** sign up inside the window, so it is excluded.

Getting any of them wrong makes the run quietly smaller with nothing to point at.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from django.db.models import Max, Q

# Lead.stage's two values. Static rather than a DISTINCT over the table: the catalog
# is served to a SimpleTestCase-clean function, and a dropdown that empties itself
# when the table is empty is a worse answer than a fixed one.
STAGES = ("active_trial", "demo_completed")

# Lead.state is a two-letter code. Also static, and for the same reason.
US_STATES = (
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO "
    "MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY"
).split()

# Every key that names a day window, so the view and the tests share one vocabulary.
DORMANT_DAYS = "dormant_days"


class ScopeError(ValueError):
    """A scope key that is unknown, or a value that will not coerce.

    Carries ``key`` so the view can name the offending filter in its 400 and the
    frontend can point at the chip, and ``code`` so the caller branches on a slug
    rather than on prose. ``ValueError`` subclass so a caller that only knows about
    ``ValueError`` still fails closed instead of passing the scope on.
    """

    CODE_UNKNOWN = "unknown_filter"
    CODE_INVALID = "invalid_filter"

    def __init__(self, key: str, message: str, code: str = CODE_INVALID) -> None:
        super().__init__(message)
        self.key = key
        self.code = code


class _Reject(ValueError):
    """Raised by a coercer, which knows the value but not the key it arrived under.

    :func:`validate_scope` catches it and re-raises the public :class:`ScopeError`
    with the key attached.
    """


@dataclass(frozen=True, slots=True)
class FilterSpec:
    """One filterable field: how to render it, how to coerce it, what it means.

    ``key`` is unique. ``label`` deliberately is **not**: ``book_min`` and ``book_max``
    both carry the noun ``"book"`` and the chip renderer composes label + bound into
    ``book >= $50,000``. A label of "book min" would render the bound twice and once
    badly.
    """

    key: str
    label: str
    bound: str  # "gte" | "lte" | "exact" | "days" | "bool"
    kind: str  # "select" | "int" | "days" | "bool"
    coerce: Callable[[Any], Any]
    # (value, today) -> Q. Built here rather than stored as a lookup string so a
    # computed filter and a plain column filter are the same kind of thing to the
    # caller, and so no part of a user key ever reaches a lookup keyword.
    predicate: Callable[[Any, datetime.date], Q] = field(repr=False, default=None)  # type: ignore[assignment]
    choices: tuple[str, ...] = ()


# --------------------------------------------------------------------------
# coercers
# --------------------------------------------------------------------------


def _count(value: Any) -> int:
    """A non-negative whole number, from an int or an all-ASCII-digits string.

    Query strings and HTML number inputs both arrive as text, so the digit string is a
    first-class input rather than a fallback. ``str.isdigit()`` rather than ``int()``
    is what rejects ``"50_000"``, ``"1e5"`` and ``"-1"`` -- CPython's ``int()`` accepts
    the first two and no JSON client emits either.

    ``bool`` is checked first because it is an ``int`` subclass: without that,
    ``book_min: true`` coerces to 1 and turns "book over a million" into "book over a
    dollar".
    """
    if isinstance(value, bool):
        raise _Reject("a boolean is not a count")
    if isinstance(value, int):
        if value < 0:
            raise _Reject("every numeric field in this catalog counts something")
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    raise _Reject(f"{value!r} is not a whole number")


def _flag(value: Any) -> bool:
    """A JSON boolean, or the two strings that unambiguously spell one.

    Everything else is refused rather than guessed at. ``"yes"`` read as falsey selects
    the exact complement of what was asked for -- a different set of leads, with nothing
    on screen to say so.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise _Reject(f"{value!r} is not true or false")


def _choice(choices: tuple[str, ...]) -> Callable[[Any], str]:
    """Exact membership. An unmatched value is an error, not an empty result.

    An empty run for a typo'd stage is indistinguishable from "no leads match", which
    is the wrong thing for the operator to conclude.
    """

    def coerce(value: Any) -> str:
        if isinstance(value, str) and value in choices:
            return value
        raise _Reject(f"{value!r} is not one of {', '.join(choices)}")

    return coerce


def _state(value: Any) -> str:
    """``Lead.state`` stores ``"CA"`` and someone will type ``"ca"``."""
    if not isinstance(value, str):
        raise _Reject(f"{value!r} is not a state code")
    normalized = value.strip().upper()
    if normalized not in US_STATES:
        raise _Reject(f"{value!r} is not a US state code")
    return normalized


# --------------------------------------------------------------------------
# predicates
# --------------------------------------------------------------------------


def _column(name: str, lookup: str) -> Callable[[Any, datetime.date], Q]:
    """A plain comparison against one Lead column.

    The lookup is a constant from this module, never assembled from a user key.
    """

    def predicate(value: Any, today: datetime.date) -> Q:
        return Q(**{f"{name}__{lookup}": value})

    return predicate


def _cutoff_date(today: datetime.date, days: int) -> datetime.date:
    return today - datetime.timedelta(days=days)


def _cutoff_dt(today: datetime.date, days: int) -> datetime.datetime:
    """Midnight UTC on the cutoff day.

    Events are ``DateTimeField``s and the window is a date, so the boundary has to be
    pinned to something. Midnight means "logged in on the cutoff day at all" counts as
    active, which is what "dormant for more than N days" says.
    """
    return datetime.datetime.combine(
        _cutoff_date(today, days), datetime.time.min, tzinfo=datetime.timezone.utc
    )


def _last_contacted(value: int, today: datetime.date) -> Q:
    # The NULL is unconditional and deliberate: a lead nobody has ever contacted is
    # overdue at every window, including one no dated lead can satisfy.
    return Q(last_contacted_date__lt=_cutoff_date(today, value)) | Q(
        last_contacted_date__isnull=True
    )


def _signed_up_within(value: int, today: datetime.date) -> Q:
    # The opposite NULL rule to the two above, and the reason they are worth separate
    # functions: a lead with no signup date did not sign up inside the window.
    return Q(signed_up_date__gte=_cutoff_date(today, value))


def _dormant(value: int, today: datetime.date) -> Q:
    # Reads `_last_login`, the annotation apply_scope attaches -- NOT
    # `Lead.last_login_date`, which is a denormalized CRM column that disagrees with the
    # event log in both directions. The events are the record.
    return Q(_last_login__lt=_cutoff_dt(today, value)) | Q(_last_login__isnull=True)


def _has_notes(value: bool, today: datetime.date) -> Q:
    # `\S` rather than a TRIM comparison: neither SQLite's nor Postgres' default TRIM
    # strips a newline, so "   \n  " would come back as a note on both backends. The
    # two sides must partition the table exactly, or "has notes" plus "has none" adds
    # up to fewer leads than exist.
    written = Q(hubspot_notes__regex=r"\S")
    return written if value else ~written


# --------------------------------------------------------------------------
# the whitelist
# --------------------------------------------------------------------------


def _spec(key, label, bound, kind, coerce, predicate, choices=()):
    return FilterSpec(
        key=key,
        label=label,
        bound=bound,
        kind=kind,
        coerce=coerce,
        predicate=predicate,
        choices=choices,
    )


def _bounded(stem, label, column):
    """The `<stem>_min` / `<stem>_max` pair, which share a label by design."""
    return (
        _spec(f"{stem}_min", label, "gte", "int", _count, _column(column, "gte")),
        _spec(f"{stem}_max", label, "lte", "int", _count, _column(column, "lte")),
    )


FILTERABLE: Mapping[str, FilterSpec] = {
    spec.key: spec
    for spec in (
        _spec(
            "stage", "stage", "exact", "select", _choice(STAGES), _column("stage", "exact"), STAGES
        ),
        _spec(
            "state", "state", "exact", "select", _state, _column("state", "exact"), tuple(US_STATES)
        ),
        *_bounded("book", "book", "estimated_book_size_usd"),
        *_bounded("producers", "producers", "num_producers"),
        _spec(
            "years_min",
            "years in business",
            "gte",
            "int",
            _count,
            _column("years_in_business", "gte"),
        ),
        *_bounded("quotes_created", "quotes created", "quotes_created"),
        *_bounded("quotes_submitted", "quotes submitted", "quotes_submitted"),
        *_bounded("deals", "deals closed", "deals_closed"),
        _spec("last_contacted_gt_days", "last contacted", "days", "days", _count, _last_contacted),
        _spec("signed_up_within_days", "signed up", "days", "days", _count, _signed_up_within),
        _spec(DORMANT_DAYS, "dormant", "days", "days", _count, _dormant),
        _spec("has_notes", "has notes", "bool", "bool", _flag, _has_notes),
    )
}


# --------------------------------------------------------------------------
# the public surface
# --------------------------------------------------------------------------


def validate_scope(scope: Mapping[str, Any]) -> dict[str, Any]:
    """Return a fresh, coerced scope, or raise :class:`ScopeError`.

    Fresh because ``SavedScope.filters`` stores the return value while the view still
    owns the raw request body; mutating the caller's dict would make those two the same
    object.

    One bad key fails the whole scope rather than being dropped: a run that quietly
    ignored a filter would go out against a wider lead set than the operator approved.
    """
    validated: dict[str, Any] = {}
    for key, raw in scope.items():
        spec = FILTERABLE.get(key)
        if spec is None:
            raise ScopeError(key, f"Unknown filter {key!r}.", code=ScopeError.CODE_UNKNOWN)
        try:
            validated[key] = spec.coerce(raw)
        except _Reject as exc:
            raise ScopeError(key, f"Invalid value for {key!r}: {exc}") from None
    return validated


def apply_scope(queryset, scope: Mapping[str, Any], *, today: datetime.date):
    """Narrow ``queryset`` by an already-validated ``scope``.

    The whitelist is re-checked here rather than trusted. "Already validated" is a
    comment, and the refusal has to happen at the call rather than at queryset
    evaluation, so a bad scope never becomes a lazy queryset that someone passes on to
    a caller with no idea where it came from.
    """
    for key in scope:
        if key not in FILTERABLE:
            raise ScopeError(key, f"Unknown filter {key!r}.", code=ScopeError.CODE_UNKNOWN)

    # Attached only when asked for: the join and GROUP BY cost nothing to a scope that
    # never mentions dormancy. `filter=` on the aggregate is load-bearing -- a lead's
    # most recent *event* is not its most recent *login*, and sc_ghost in the fixture
    # exists to catch exactly that.
    if DORMANT_DAYS in scope:
        queryset = queryset.annotate(
            _last_login=Max("events__timestamp", filter=Q(events__type="login"))
        )

    for key, value in scope.items():
        queryset = queryset.filter(FILTERABLE[key].predicate(value, today))
    return queryset


def scope_field_catalog() -> list[dict[str, Any]]:
    """The add-filter field list stage 01 renders, projected from :data:`FILTERABLE`.

    Projected rather than maintained beside it: a second list starts lying the first
    time one of them moves. ``choices`` is a list because this crosses the wire as
    JSON, where a tuple is not a thing.
    """
    return [
        {
            "key": spec.key,
            "label": spec.label,
            "bound": spec.bound,
            "kind": spec.kind,
            "choices": list(spec.choices),
        }
        for spec in FILTERABLE.values()
    ]
