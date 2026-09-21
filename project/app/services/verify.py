"""Deterministic grounding verifier for generated outreach copy.

Pure regex/string logic, no LLM, duck-typed on lead attributes. Checks every
concrete claim in the copy against the lead's stored data, read through its
owner's declared shape: the number and date columns ground figures and dates,
and the two roles ground the greeting and the agency mention. The actions
engine fails closed on any :class:`Violation` (see SECURITY.md). Must not
import ``outreach`` — that module imports this one.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, replace
from typing import Any

from project.app.services import actions

# Strictness levels, surfaced to ops via the COPY_VERIFY_LEVEL setting.
#   off      -> disabled (escape hatch); returns no violations.
#   standard -> high-confidence contradictions only (the default).
#   strict   -> standard plus omission/loose signals — higher recall, lower precision.
LEVEL_OFF = "off"
LEVEL_STANDARD = "standard"
LEVEL_STRICT = "strict"
DEFAULT_LEVEL = LEVEL_STANDARD
LEVELS = (LEVEL_OFF, LEVEL_STANDARD, LEVEL_STRICT)


@dataclass(frozen=True)
class Violation:
    """A single grounding problem found in generated copy.

    ``start``/``end``/``field`` are defaulted so positional two-argument
    construction still works (``tests_verify.py`` does exactly that).
    """

    kind: str
    message: str
    start: int | None = None
    end: int | None = None
    field: str = ""


@dataclass(frozen=True)
class Claim:
    """One assertion the copy makes, checked against the lead record.

    Unlike :class:`Violation` this records *passes* too — the source of the
    reviewer's green underlines and the "N of M claims verified" summary.
    """

    id: str
    kind: str
    start: int | None
    end: int | None
    text: str
    verified: bool | None  # True=grounded  False=contradicted  None=not a claim
    field: str
    expected: Any
    claimed: Any
    message: str
    counts_toward_summary: bool

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "verified": self.verified,
            "field": self.field,
            "expected": _jsonable(self.expected),
            "claimed": _jsonable(self.claimed),
            "message": self.message,
            "counts_toward_summary": self.counts_toward_summary,
        }


# Claim kind -> `Violation.kind` slug. The slugs are frozen output written into
# `further_action`, so they are deliberately NOT the claim kinds.
_VIOLATION_KIND = {
    "amount": "unsupported_amount",
    "count": "wrong_count",
    "contact_name": "wrong_contact_name",
    "iso_date": "unsupported_date",
    "unauthorized_offer": "unauthorized_offer",
    "unsupported_year": "unsupported_year",
}
# `omission` covers two distinct violation slugs; the checked field picks one.
_OMISSION_VIOLATION_KIND = {
    "contact_name": "contact_name_absent",
    "agency_name": "agency_name_absent",
}

# Claim kinds excluded from the "N of M" ratio: not assertions about the record.
_UNCOUNTED_KINDS = frozenset(
    {"goal_reference", "future_date", "unauthorized_offer", "omission", "unsupported_year"}
)

# Claim kinds that block approval on their own, whatever the "N of M" ratio says.
# Drafting already fails closed on these; the approve gate must agree.
BLOCKING_KINDS = frozenset({"unauthorized_offer"})

# The two roles a shape fills. Spelled out rather than imported: this module
# stays Django-free, and `_OMISSION_VIOLATION_KIND` already keys on them.
ROLE_CONTACT_NAME = "contact_name"
ROLE_AGENCY_NAME = "agency_name"

VERIFICATION_SCHEMA_VERSION = 1

_GOAL_REFERENCE_MESSAGE = "Framed as a target, not a claim about the record."
_FUTURE_DATE_MESSAGE = "Scheduling language: a future date is not grounded against the record."


# --------------------------------------------------------------------------
# patterns
# --------------------------------------------------------------------------

# Currency: "$5,000,000", "$5M", "$500K", "$5.2M", "$5 million". The optional
# suffix must end on a word boundary so "$5 monthly" reads as $5, not $5M.
_CURRENCY_RE = re.compile(
    r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*(million|billion|thousand|[kmb])?\b",
    re.IGNORECASE,
)
_MULTIPLIERS = {
    "k": 1e3,
    "thousand": 1e3,
    "m": 1e6,
    "million": 1e6,
    "b": 1e9,
    "billion": 1e9,
}
# A cited figure passes if it is within this fraction of a grounded value, so
# clean roundings ("$5M" for 4,800,000 or 5,200,000) are accepted.
_AMOUNT_TOLERANCE = 0.10

# A bare integer. Without a noun per column the verifier cannot bind "14 quotes"
# to a column, so every standalone integer is checked against every number
# column and an unmatched one fails closed: noisier than the old per-noun
# patterns, not weaker.
_INTEGER_RE = re.compile(r"\b\d+\b")

# Goal framing that turns a count into a *target* rather than a claim about the
# record ("once you hit 20 closed deals"). Deliberately excludes the ambiguous
# "hit"/"reach", which also describe achievements ("congrats on hitting 47").
_GOAL_MARKER = (
    r"if|once|when|whenever|until|till|toward|towards|nearing|approaching|"
    r"goals?|targets?|milestones?|aiming|aim|en route|on track|short of|"
    r"shy of|away from|close to|closing in|get to|up to"
)
_GOAL_BEFORE_RE = re.compile(rf"\b(?:{_GOAL_MARKER})\b[^.!?\n]{{0,20}}$", re.IGNORECASE)
_GOAL_AFTER_RE = re.compile(
    r"^[^.!?\n]{0,12}\b(?:milestone|target|goal|mark|threshold)\b", re.IGNORECASE
)

# Greeting line only ("Hi Priya,"), so a mid-body "say hi to Dan" is ignored.
_SALUTATION_RE = re.compile(
    r"^[ \t]*(?:hi|hello|hey|dear)\s+([^\n,]+?)\s*(?:[,\n]|$)",
    re.IGNORECASE | re.MULTILINE,
)
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")

# Unauthorized commercial promises. Anchored so ordinary words ("feel free",
# a bare "20%") don't trigger; only concrete give-aways do.
_OFFER_RES = (
    re.compile(r"\d+\s*(?:%|percent)\s*(?:off|discount)", re.IGNORECASE),
    re.compile(r"\bdiscount(?:s|ed|ing)?\b", re.IGNORECASE),
    re.compile(r"\bfree\s+months?\b", re.IGNORECASE),
    re.compile(r"\bmonths?\s+free\b", re.IGNORECASE),
    re.compile(r"\bwaiv(?:e|ed|es|ing|er)\b", re.IGNORECASE),
    re.compile(r"\bcomplimentary\b", re.IGNORECASE),
    re.compile(r"\bno\s+(?:cost|charge)\b", re.IGNORECASE),
    re.compile(r"\brebate\b", re.IGNORECASE),
    re.compile(r"\bcoupon\b", re.IGNORECASE),
    re.compile(r"\bpromo(?:tion(?:al)?|s)?\b", re.IGNORECASE),
    re.compile(
        r"\b(?:special|preferred|discounted|volume)\s+(?:pricing|rate|rates)\b",
        re.IGNORECASE,
    ),
)

_GENERIC_SALUTATIONS = {
    "there",
    "team",
    "folks",
    "all",
    "everyone",
    "yall",
    "y'all",
    "friend",
    "friends",
}
_HONORIFICS = {"mr", "mrs", "ms", "miss", "dr", "prof", "sir", "madam", "mx"}
# Dropped when reducing an agency name to its distinctive tokens (strict only).
_AGENCY_STOPWORDS = {
    "insurance",
    "agency",
    "agencies",
    "advisors",
    "advisor",
    "group",
    "groups",
    "risk",
    "partners",
    "partner",
    "associates",
    "brokers",
    "broker",
    "brokerage",
    "llc",
    "inc",
    "co",
    "company",
    "services",
    "service",
    "solutions",
    "insurers",
    "underwriters",
    "financial",
    "and",
    "the",
    "of",
}


# --------------------------------------------------------------------------
# helpers (re-implemented locally to avoid importing outreach)
# --------------------------------------------------------------------------


def _events(lead: Any) -> list:
    events = getattr(lead, "events", None)
    if events is None:
        return []
    if hasattr(events, "all"):
        return list(events.all())
    return list(events)


def _as_date(value: Any) -> datetime.date | None:
    """A stored date, however it was stored — ingest keeps them as ISO text."""
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


def _shape(lead: Any) -> Any:
    return getattr(lead, "shape", None)


def _stored(lead: Any, column_type: str) -> list:
    """Every raw value the lead and its events keep in a column the shape
    declares as ``column_type``.

    Raw rather than coerced: each check knows how loosely to read its own
    values — an amount tolerates ``"$12,000"``, a plain date does not.
    ``column_type`` is read off the shape itself, so this module stays free of
    Django imports.
    """
    shape = _shape(lead)
    if shape is None:
        return []
    values = []
    data = getattr(lead, "data", None) or {}
    for name, declared in shape.types().items():
        if declared == column_type:
            values.append(data.get(name))
    event_columns = [
        name for name, declared in shape.types(shape.EVENT).items() if declared == column_type
    ]
    for event in _events(lead):
        event_data = getattr(event, "data", None) or {}
        values.extend(event_data.get(name) for name in event_columns)
    return values


def _role(lead: Any, role: str) -> str:
    """The value behind one of the shape's two roles, as text."""
    shape = _shape(lead)
    if shape is None:
        return ""
    return str(shape.role_value(getattr(lead, "data", None), role) or "").strip()


def _is_goal_context(copy: str, start: int, end: int) -> bool:
    """True when the count at ``copy[start:end]`` is framed as a target/goal
    (goal cue just before, or milestone noun just after)."""
    before = copy[max(0, start - 30) : start]
    after = copy[end : end + 16]
    return bool(_GOAL_BEFORE_RE.search(before) or _GOAL_AFTER_RE.search(after))


def _coerce_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):  # bool is an int subclass; not a real figure
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^\d.]", "", str(value))
    if not cleaned or cleaned == ".":
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _money_to_number(digits: str, suffix: str | None) -> float:
    number = float(digits.replace(",", ""))
    return number * _MULTIPLIERS.get((suffix or "").lower(), 1.0)


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def _jsonable(value: Any) -> Any:
    """The report is persisted to a JSONField."""
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def normalize_copy(copy: str) -> str:
    """Collapse ``\\r\\n`` / ``\\r`` to ``\\n`` before any offset is computed.

    Python counts ``\\r\\n`` as two characters, which would skew every span.
    """
    if not copy:
        return copy
    return copy.replace("\r\n", "\n").replace("\r", "\n")


def _is_astral_safe(copy: str) -> bool:
    """True when JS string indices equal Unicode code-point indices."""
    return len(copy) == len(copy.encode("utf-16-le")) // 2


def _trim_span(copy: str, start: int, end: int) -> tuple[int, int]:
    """Trim surrounding whitespace off a match span (``_CURRENCY_RE`` can match
    a trailing space; an untrimmed span underlines into the next word)."""
    text = copy[start:end]
    start += len(text) - len(text.lstrip())
    end -= len(text) - len(text.rstrip())
    return start, max(start, end)


def _claim(
    claims: list | None,
    *,
    kind: str,
    copy: str,
    start: int | None,
    end: int | None,
    verified: bool | None,
    field: str,
    expected: Any = None,
    claimed: Any = None,
    message: str = "",
) -> Claim:
    """Record one inspected claim — passing, failing, or not-a-claim.

    De-duplication is keyed on ``(kind, start, end, message)``, not message
    alone, so identical text at different offsets survives.
    """
    if start is not None and end is not None:
        start, end = _trim_span(copy, start, end)
        text = copy[start:end]
    else:
        start = end = None
        text = ""
    claim = Claim(
        id="",
        kind=kind,
        start=start,
        end=end,
        text=text,
        verified=verified,
        field=field,
        expected=expected,
        claimed=claimed,
        message=message,
        counts_toward_summary=kind not in _UNCOUNTED_KINDS,
    )
    if claims is not None:
        key = (claim.kind, claim.start, claim.end, claim.message)
        if key not in {(c.kind, c.start, c.end, c.message) for c in claims}:
            claims.append(claim)
    return claim


def _violation_kind(claim: Claim) -> str:
    if claim.kind == "omission":
        return _OMISSION_VIOLATION_KIND[claim.field]
    return _VIOLATION_KIND[claim.kind]


# --------------------------------------------------------------------------
# individual checks
# --------------------------------------------------------------------------


def _grounded_amounts(lead: Any) -> list[float]:
    """Dollar figures the model may cite: every number column, lead and events."""
    shape = _shape(lead)
    if shape is None:
        return []
    grounded = [_coerce_number(value) for value in _stored(lead, shape.NUMBER)]
    return [value for value in grounded if value is not None]


def _is_grounded_amount(value: float, grounded: list[float]) -> bool:
    for target in grounded:
        if value == target:
            return True
        if target and abs(value - target) <= _AMOUNT_TOLERANCE * abs(target):
            return True
    return False


def _check_amounts(lead: Any, copy: str, claims: list | None = None) -> None:
    grounded = _grounded_amounts(lead)
    if not grounded:  # nothing to check against (real leads always have figures)
        return
    for match in _CURRENCY_RE.finditer(copy):
        value = _money_to_number(match.group(1), match.group(2))
        ok = _is_grounded_amount(value, grounded)
        _claim(
            claims,
            kind="amount",
            copy=copy,
            start=match.start(),
            end=match.end(),
            verified=ok,
            field="",
            expected=sorted(set(grounded)),
            claimed=value,
            message=""
            if ok
            else (
                f"Copy cites {match.group(0).strip()} but no matching dollar "
                f"figure is in the lead record."
            ),
        )


def _spanned(copy: str, *patterns: re.Pattern) -> list[tuple[int, int]]:
    """Where a figure is already accounted for by another check."""
    return [m.span() for pattern in patterns for m in pattern.finditer(copy)]


def _inside(span: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
    return any(start <= span[0] and span[1] <= end for start, end in spans)


def _check_counts(lead: Any, copy: str, claims: list | None = None) -> None:
    """Every standalone integer against every number column.

    A currency amount, an ISO date and a year are each another check's, so
    those are skipped here. What is left is a figure about the record with no
    noun to bind it to a column, so it is grounded if it equals any number the
    record holds and a contradiction if it equals none.
    """
    grounded = [value for value in _grounded_amounts(lead) if float(value).is_integer()]
    if not grounded:
        return
    expected = sorted({int(value) for value in grounded})
    taken = _spanned(copy, _CURRENCY_RE, _ISO_DATE_RE, _YEAR_RE)
    for match in _INTEGER_RE.finditer(copy):
        if _inside(match.span(), taken):
            continue
        claimed = int(match.group(0))
        if _is_goal_context(copy, match.start(), match.end()):
            _goal_claim(claims, copy, match, expected, claimed)
            continue
        ok = claimed in expected
        _claim(
            claims,
            kind="count",
            copy=copy,
            start=match.start(),
            end=match.end(),
            verified=ok,
            field="",
            expected=expected,
            claimed=claimed,
            message=""
            if ok
            else f"Copy claims {claimed}, which is not a figure in the lead record.",
        )


def _goal_claim(claims, copy, match, expected, claimed) -> None:
    """A count framed as a target: neither a violation nor part of the "N of M"
    ratio, but still shown to the reviewer as inspected."""
    _claim(
        claims,
        kind="goal_reference",
        copy=copy,
        start=match.start(),
        end=match.end(),
        verified=None,
        field="",
        expected=expected,
        claimed=claimed,
        message=_GOAL_REFERENCE_MESSAGE,
    )


def _check_contact_name(lead: Any, copy: str, claims: list | None = None) -> None:
    contact = _role(lead, ROLE_CONTACT_NAME)
    if not contact:
        return
    contact_tokens = set(_tokens(contact))
    match = _SALUTATION_RE.search(copy)
    if not match:
        return
    greeted = match.group(1).strip()
    greeted_tokens = [t for t in _tokens(greeted) if t not in _HONORIFICS]
    if not greeted_tokens:  # e.g. "Dear Sir," — nothing to contradict
        return
    if set(greeted_tokens) <= _GENERIC_SALUTATIONS:  # "Hi there," — no assertion
        return
    ok = bool(contact_tokens.intersection(greeted_tokens))
    _claim(
        claims,
        kind="contact_name",
        copy=copy,
        start=match.start(1),
        end=match.end(1),
        verified=ok,
        field="contact_name",
        expected=contact,
        claimed=greeted,
        message="" if ok else f'Copy greets "{greeted}" but the lead contact is {contact}.',
    )


def _check_offer(lead: Any, copy: str, action_type: str, claims: list | None = None) -> None:
    # Volume pricing / discounts are authorized only for the reward action.
    if action_type == actions.POWER_USER_REWARD:
        return
    for pattern in _OFFER_RES:
        match = pattern.search(copy)
        if match:
            _claim(
                claims,
                kind="unauthorized_offer",
                copy=copy,
                start=match.start(),
                end=match.end(),
                verified=False,
                field="action_type",
                expected=action_type,
                claimed=match.group(0).strip(),
                message=(
                    f'Copy makes a commercial promise ("{match.group(0).strip()}") that '
                    f"is not authorized for a {action_type} action."
                ),
            )
            return


def _record_dates(lead: Any) -> set[datetime.date]:
    """Every date the record holds: each declared date column, plus the
    structural event timestamps."""
    shape = _shape(lead)
    dates = {_as_date(value) for value in _stored(lead, shape.DATE)} if shape else set()
    dates.update(_as_date(getattr(event, "timestamp", None)) for event in _events(lead))
    return {value for value in dates if value is not None}


def _check_iso_dates(
    lead: Any, copy: str, today: datetime.date, claims: list | None = None
) -> None:
    record = _record_dates(lead)
    for match in _ISO_DATE_RE.finditer(copy):
        try:
            cited = datetime.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            continue  # not a real calendar date
        grounded = cited in record
        # A past/today ISO date in prose is a copied record fact; a future one
        # is scheduling language ("let's talk on ..."), which we don't ground.
        future = not grounded and cited > today
        _claim(
            claims,
            kind="future_date" if future else "iso_date",
            copy=copy,
            start=match.start(),
            end=match.end(),
            verified=None if future else grounded,
            field="record_dates",
            expected=sorted(d.isoformat() for d in record),
            claimed=cited.isoformat(),
            message=_FUTURE_DATE_MESSAGE
            if future
            else (
                ""
                if grounded
                else f"Copy cites the date {match.group(0)}, which is not in the lead record."
            ),
        )


def _agency_tokens(name: str) -> list[str]:
    return [t for t in _tokens(name) if len(t) >= 3 and t not in _AGENCY_STOPWORDS]


def _check_strict(lead: Any, copy: str, today: datetime.date, claims: list | None = None) -> None:
    """Omission / loose-grounding checks layered on top of ``standard``."""
    low = copy.lower()

    contact = _role(lead, ROLE_CONTACT_NAME)
    if contact:
        first = _tokens(contact)[0] if _tokens(contact) else ""
        if len(first) >= 2 and not re.search(rf"\b{re.escape(first)}\b", low):
            # An omission has no span: there is nothing in the copy to underline.
            _claim(
                claims,
                kind="omission",
                copy=copy,
                start=None,
                end=None,
                verified=False,
                field="contact_name",
                expected=contact,
                claimed=None,
                message=f"Copy never addresses the contact by name ({contact}).",
            )

    agency = _role(lead, ROLE_AGENCY_NAME)
    agency_tokens = _agency_tokens(agency)
    if agency_tokens and not any(re.search(rf"\b{re.escape(t)}\b", low) for t in agency_tokens):
        _claim(
            claims,
            kind="omission",
            copy=copy,
            start=None,
            end=None,
            verified=False,
            field="agency_name",
            expected=agency,
            claimed=None,
            message=f'Copy never names the agency ("{agency}").',
        )

    allowed_years = {today.year, today.year + 1}
    allowed_years.update(d.year for d in _record_dates(lead))
    for match in _YEAR_RE.finditer(copy):
        year = int(match.group(0))
        if year not in allowed_years:
            _claim(
                claims,
                kind="unsupported_year",
                copy=copy,
                start=match.start(),
                end=match.end(),
                verified=False,
                field="record_dates",
                expected=sorted(allowed_years),
                claimed=year,
                message=f"Copy mentions the year {year}, which is not tied to any record date.",
            )


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------


def _collect_claims(
    lead: Any, copy: str, action_type: str, level: str, today: datetime.date
) -> list[Claim]:
    """Run every check over ``copy``, recording passes as well as failures.

    Check order is load-bearing: it fixes the ``Violation`` message order.
    """
    claims: list[Claim] = []
    if level == LEVEL_OFF or not copy:
        return claims
    _check_amounts(lead, copy, claims)
    _check_counts(lead, copy, claims)
    _check_contact_name(lead, copy, claims)
    _check_offer(lead, copy, action_type, claims)
    _check_iso_dates(lead, copy, today, claims)
    if level == LEVEL_STRICT:
        _check_strict(lead, copy, today, claims)
    return claims


def verify_copy(
    lead: Any,
    copy: str,
    action_type: str,
    *,
    level: str = DEFAULT_LEVEL,
    today: datetime.date | None = None,
    claims: list | None = None,
) -> list[Violation]:
    """Check generated ``copy`` against the ``lead`` record.

    Returns :class:`Violation` s (empty means grounded). Pure and
    deterministic: no database, no LLM. ``claims`` is a keyword-only
    *out*-parameter: pass a list to also receive every inspected
    :class:`Claim`, passes included; the return value is unchanged either way.
    """
    today = today or datetime.date.today()
    collected = _collect_claims(lead, normalize_copy(copy), action_type, level, today)
    if claims is not None:
        claims.extend(collected)

    violations = [
        Violation(_violation_kind(c), c.message, c.start, c.end, c.field)
        for c in collected
        if c.verified is False
    ]

    # Overlapping patterns can surface the same problem twice; keep first seen.
    seen: set[str] = set()
    unique: list[Violation] = []
    for violation in violations:
        if violation.message not in seen:
            seen.add(violation.message)
            unique.append(violation)
    return unique


def verify_spans(
    lead: Any,
    copy: str,
    action_type: str,
    *,
    level: str = DEFAULT_LEVEL,
    today: datetime.date | None = None,
) -> dict:
    """Build the v1 verification report (reviewer underlines + "N of M" summary).

    ``copy`` is normalized and echoed back: offsets index into
    ``report["copy"]``, never the client's textarea. ``can_approve`` is false
    on either a contradicted claim or a :data:`BLOCKING_KINDS` claim. Pure: no
    database, no LLM.
    """
    today = today or datetime.date.today()
    copy = normalize_copy(copy)
    claims = _collect_claims(lead, copy, action_type, level, today)

    # Sort by offset, then assign ids. Omission claims have no span and sort last.
    claims.sort(key=lambda c: (c.start is None, c.start or 0, c.end or 0, c.kind))
    ordered = [
        replace(claim, id=f"claim-{index:04d}") for index, claim in enumerate(claims, start=1)
    ]

    counted = [c for c in ordered if c.counts_toward_summary]
    verified_count = sum(1 for c in counted if c.verified is True)
    unverified_count = sum(1 for c in counted if c.verified is False)
    checked_count = verified_count + unverified_count
    # A prohibition stays out of the ratio (it is not a claim about the record)
    # but still blocks approval on its own.
    blocked = any(c.kind in BLOCKING_KINDS for c in ordered)
    return {
        "version": VERIFICATION_SCHEMA_VERSION,
        "level": level,
        "today": today.isoformat(),
        "copy": copy,
        "copy_length": len(copy),
        "is_astral_safe": _is_astral_safe(copy),
        "verified_count": verified_count,
        "unverified_count": unverified_count,
        "checked_count": checked_count,
        "summary": f"{verified_count} of {checked_count} claims verified",
        "can_approve": unverified_count == 0 and not blocked,
        "claims": [c.to_dict() for c in ordered],
    }


def format_violations(violations: list[Violation]) -> str:
    """Render violations as the ``further_action`` text a BD reviewer reads."""
    if not violations:
        return ""
    lines = "\n".join(f"- {v.message}" for v in violations)
    return (
        "Grounding check failed — the generated copy contradicts the lead record:\n"
        f"{lines}\n\n"
        "The draft has been kept for reference; a human should correct these "
        "issues before the email is sent."
    )
