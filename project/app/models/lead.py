"""The lead record, its ingested activity events, and the shape a user declares."""

import datetime
import re

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import models
from django.utils.functional import cached_property

NUMBER = "number"
DATE = "date"
TEXT = "text"
BOOL = "bool"
COLUMN_TYPES = (NUMBER, DATE, TEXT, BOOL)

# What a column declaration carries. `lead_authored` is lead columns only: an
# event is already untrusted whole.
COLUMN_KEYS = frozenset({"name", "type"})
LEAD_COLUMN_KEYS = COLUMN_KEYS | {"lead_authored"}

_COLUMN_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# `Event.timestamp` is a structural column, not a declared one, so no event
# column may claim the name.
EVENT_RESERVED_NAMES = frozenset({"timestamp"})

# The `derived` twin of a date column: how many days ago it was. A lead column
# of this name would shadow the twin, so the prefix is reserved.
DAYS_SINCE_PREFIX = "days_since_"


def _declared(column):
    """Whether one stored declaration carries the name and type readers index."""
    if not isinstance(column, dict):
        return False
    name = column.get("name")
    return isinstance(name, str) and bool(name) and column.get("type") in COLUMN_TYPES


def _coerce(value, column_type):
    """One stored value as its declared type, or ``None`` when it is not one."""
    if value is None:
        return None
    if column_type == BOOL:
        return value if isinstance(value, bool) else None
    if column_type == NUMBER:
        # bool is an int in Python; a flag is not a figure.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return value
    if column_type == DATE:
        return _as_date(value)
    return value if isinstance(value, str) else None


def _as_date(value):
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        pass
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


class Shape(models.Model):
    """What one user's leads and events are: their columns and types.

    ``lead_columns`` and ``event_columns`` are lists of ``{"name", "type"}``
    declarations; a lead column also declares ``lead_authored``, which decides
    whether its text is trusted anywhere. Nothing here names a column: the
    rules engine reads this declaration and nothing else.
    """

    LEAD = "lead"
    EVENT = "event"

    # The declared types, on the class so a duck-typed reader that only holds a
    # shape (the verifier) needs no import to name one.
    NUMBER = NUMBER
    DATE = DATE
    TEXT = TEXT
    BOOL = BOOL

    owner = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="shape"
    )
    lead_columns = models.JSONField(default=list, blank=True)
    event_columns = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def columns(self, kind=LEAD):
        """The usable declarations for one kind: every reader may index name and type.

        ``clean()`` is what refuses a malformed declaration on the way in; a row
        that reached the table another way is skipped here rather than raising
        out of whichever reader met it first.
        """
        declared = self.lead_columns if kind == self.LEAD else self.event_columns
        return [column for column in declared or [] if _declared(column)]

    @cached_property
    def _types(self):
        """``{kind: {name: type}}``, derived once — every value read is a lookup."""
        return {
            kind: {column["name"]: column["type"] for column in self.columns(kind)}
            for kind in (self.LEAD, self.EVENT)
        }

    def types(self, kind=LEAD):
        """``{name: type}`` for one kind — the vocabulary's raw material."""
        return self._types[kind]

    def trusted(self):
        """Lead columns the lead did not author, so their text may be relied on.

        Trusted is the narrow answer: only a column declaring ``lead_authored``
        false, so a declaration that never said trusts nothing.
        """
        return [column for column in self.columns() if column.get("lead_authored") is False]

    def authored(self):
        """Lead columns the lead may have authored: untrusted everywhere, prompts
        included — the inverse of :meth:`trusted`, so no column is neither."""
        return [column for column in self.columns() if column.get("lead_authored") is not False]

    def value(self, data, column, kind=LEAD):
        """``data[column]`` coerced by its declared type, ``None`` on a mismatch.

        An undeclared column has no type, so it has no value either — that is
        what keeps a stored blob from reaching a prompt or a rule unannounced.
        """
        column_type = self.types(kind).get(column)
        if column_type is None:
            return None
        return _coerce((data or {}).get(column), column_type)

    def trusted_value(self, data, column):
        """A lead column's value as text, or ``""`` when the lead authors it.

        The copy path names two columns directly; this is what keeps one the
        lead writes from reaching the trusted region of a prompt.
        """
        if column in {declared["name"] for declared in self.trusted() if declared.get("name")}:
            return self.value(data, column) or ""
        return ""

    def clean(self):
        problems = {}
        for field, kind in (("lead_columns", self.LEAD), ("event_columns", self.EVENT)):
            messages = _column_problems(getattr(self, field), kind)
            if messages:
                problems[field] = messages
        if problems:
            raise ValidationError(problems)

    def __str__(self):
        return f"shape of user {self.owner_id}"


def _column_problems(declared, kind):
    """Every way one list of column declarations can be unusable."""
    if not isinstance(declared, list):
        return ["Declare columns as a list."]
    allowed = LEAD_COLUMN_KEYS if kind == Shape.LEAD else COLUMN_KEYS
    problems, seen = [], set()
    for index, column in enumerate(declared):
        if not isinstance(column, dict):
            problems.append(f"Column {index} must be an object.")
            continue
        unknown = set(column) - allowed
        if unknown:
            problems.append(f"Column {index} has unknown key(s): {_listed(unknown)}.")
        name = column.get("name")
        if not isinstance(name, str) or not _COLUMN_NAME_RE.match(name):
            problems.append(
                f"Column {index} needs a snake_case name: lowercase letters, digits "
                "and underscores, starting with a letter."
            )
        elif name in seen:
            problems.append(f"Column {name!r} is declared twice.")
        elif kind == Shape.EVENT and name in EVENT_RESERVED_NAMES:
            problems.append(f"Column {name!r} is a structural event column and cannot be declared.")
        elif kind == Shape.LEAD and name.startswith(DAYS_SINCE_PREFIX):
            problems.append(
                f"Column {name!r} would shadow the {DAYS_SINCE_PREFIX!r} figure a date "
                "column of that name is read through, so the prefix is reserved."
            )
        else:
            seen.add(name)
        if column.get("type") not in COLUMN_TYPES:
            problems.append(
                f"Column {index} needs a type from {_listed(COLUMN_TYPES)}, "
                f"got {column.get('type')!r}."
            )
        if kind == Shape.LEAD and not isinstance(column.get("lead_authored"), bool):
            problems.append(
                f"Column {index} needs 'lead_authored': true or false — whether the "
                "lead writes this column decides whether its text can be trusted."
            )
    return problems


def _listed(values):
    return ", ".join(repr(value) for value in sorted(values))


class Lead(models.Model):
    """One agency, as its owner's shape says a lead is shaped.

    ``data`` holds whatever was ingested; the owner's :class:`Shape` is what
    decides which of it is a column, what type it has, and whether the lead
    wrote it. A lead with no owner has no shape, and so no readable columns.
    """

    id = models.CharField(max_length=32, primary_key=True)  # "lead_001"
    # Whose book this lead sits in: the user whose rules an engine runs for it.
    # NULL where ingestion named nobody, and an unowned lead has no rules.
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="leads",
    )
    data = models.JSONField(default=dict, blank=True)

    @cached_property
    def shape(self):
        """The owner's declared shape, or ``None``.

        Read through the relations so ``select_related("owner__shape")`` serves
        it — the engine asks once per job.
        """
        if self.owner_id is None:
            return None
        try:
            return self.owner.shape
        except ObjectDoesNotExist:
            return None

    def __str__(self):
        return self.id


class Event(models.Model):
    """One activity record. ``timestamp`` is structural — the queue orders on
    it and ingest knows the raw key — and everything else is declared."""

    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="events")
    timestamp = models.DateTimeField()
    data = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"{self.lead_id} @ {self.timestamp:%Y-%m-%d}"
