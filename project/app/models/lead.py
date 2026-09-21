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
    """What one user's leads and events are: their columns, types and roles.

    ``lead_columns`` and ``event_columns`` are lists of ``{"name", "type"}``
    declarations; a lead column also declares ``lead_authored``, which decides
    whether its text is trusted anywhere. ``roles`` names the lead column that
    holds the contact and the one that holds the agency — the two the prompts
    and the verifier need by meaning rather than by name.
    """

    LEAD = "lead"
    EVENT = "event"

    # The declared types, on the class so a duck-typed reader that only holds a
    # shape (the verifier) needs no import to name one.
    NUMBER = NUMBER
    DATE = DATE
    TEXT = TEXT
    BOOL = BOOL

    ROLE_CONTACT_NAME = "contact_name"
    ROLE_AGENCY_NAME = "agency_name"
    ROLES = (ROLE_CONTACT_NAME, ROLE_AGENCY_NAME)

    owner = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="shape"
    )
    lead_columns = models.JSONField(default=list, blank=True)
    event_columns = models.JSONField(default=list, blank=True)
    roles = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def columns(self, kind=LEAD):
        """The declarations for one kind, as stored."""
        declared = self.lead_columns if kind == self.LEAD else self.event_columns
        return [column for column in declared or [] if isinstance(column, dict)]

    def types(self, kind=LEAD):
        """``{name: type}`` for one kind — the vocabulary's raw material."""
        return {
            column.get("name"): column.get("type")
            for column in self.columns(kind)
            if column.get("name")
        }

    def trusted(self):
        """Lead columns the lead did not author, so their text may be relied on."""
        return [column for column in self.columns() if not column.get("lead_authored")]

    def authored(self):
        """Lead columns the lead authored: untrusted everywhere, prompts included."""
        return [column for column in self.columns() if column.get("lead_authored")]

    def value(self, data, column, kind=LEAD):
        """``data[column]`` coerced by its declared type, ``None`` on a mismatch.

        An undeclared column has no type, so it has no value either — that is
        what keeps a stored blob from reaching a prompt or a rule unannounced.
        """
        column_type = self.types(kind).get(column)
        if column_type is None:
            return None
        return _coerce((data or {}).get(column), column_type)

    def role(self, role):
        """The lead column a role names, or ``""`` when the shape declares none."""
        return (self.roles or {}).get(role) or ""

    def role_value(self, data, role):
        """The value behind a role, as text — ``""`` when the role is unfilled."""
        column = self.role(role)
        if not column:
            return ""
        return self.value(data, column) or ""

    def clean(self):
        problems = {}
        for field, kind in (("lead_columns", self.LEAD), ("event_columns", self.EVENT)):
            messages = _column_problems(getattr(self, field), kind)
            if messages:
                problems[field] = messages
        messages = _role_problems(self.roles, self.columns())
        if messages:
            problems["roles"] = messages
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


def _role_problems(roles, lead_columns):
    """Both roles must name a declared text column the lead does not author.

    A role feeds the trusted region of every prompt and the verifier's greeting
    check, so a lead-authored column behind one would launder untrusted text
    into both.
    """
    if not isinstance(roles, dict):
        return ["Declare roles as an object."]
    unknown = set(roles) - set(Shape.ROLES)
    problems = [f"Unknown role(s): {_listed(unknown)}."] if unknown else []
    declared = {column.get("name"): column for column in lead_columns}
    for role in Shape.ROLES:
        column = declared.get(roles.get(role))
        if column is None:
            problems.append(
                f"Role {role!r} must name a declared lead column, got {roles.get(role)!r}."
            )
        elif column.get("type") != TEXT:
            problems.append(
                f"Role {role!r} must name a text column; {column['name']!r} is not one."
            )
        elif column.get("lead_authored"):
            problems.append(
                f"Role {role!r} must name a column the lead does not author; "
                f"{column['name']!r} would put lead-controlled text in the trusted record."
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
