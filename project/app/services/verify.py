"""Deterministic grounding verifier for generated outreach copy (MUS-22).

The LLM writes the email; these *rules* decide whether it may be sent. We
extract every concrete claim the model made about the lead — dollar figures,
counts, the contact/agency name, dates — and check each against the ``Lead``
record. Anything the record does not support is a :class:`Violation`.

Design notes:

- **No LLM.** Verification is pure regex/string logic, so it adds no provider
  calls and stays unit-testable without a database — duck-typed on the lead
  attributes from ``CONTRACT.md`` (plus ``lead.events``), exactly like
  :func:`project.app.services.outreach.determine_action`.
- **Fail closed.** ``plan_outreach`` turns any violation into
  ``needs_human=True`` and routes the (still-populated) draft to the BD review
  queue. A wrong number in a sales email is more expensive than a delayed one.
- **Precision first.** Every check is anchored to a keyword ("47 closed deals",
  not a bare "47"), so we flag genuine contradictions rather than good copy —
  over-flagging would defeat the "rules decide, the model writes" split. Recall
  is tunable via ``level`` (``off | standard | strict``); see below.

This module must **not** import ``outreach`` — that module imports this one, so
the trivial event/date helpers are re-implemented here to avoid a cycle.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, replace
from typing import Any

from project.app.services import actions

# Strictness levels, surfaced to ops via the COPY_VERIFY_LEVEL setting.
#   off      -> verification disabled (escape hatch); returns no violations.
#   standard -> high-confidence contradictions only (the default; the four
#               acceptance cases all fire here).
#   strict   -> standard plus omission/loose signals (personalization checks,
#               loose year grounding) — higher recall, lower precision.
LEVEL_OFF = "off"
LEVEL_STANDARD = "standard"
LEVEL_STRICT = "strict"
DEFAULT_LEVEL = LEVEL_STANDARD
LEVELS = (LEVEL_OFF, LEVEL_STANDARD, LEVEL_STRICT)


@dataclass(frozen=True)
class Violation:
    """A single grounding problem found in generated copy.

    ``kind`` is a machine-readable slug (e.g. ``"wrong_count"``); ``message``
    is the human-readable explanation shown to the BD reviewer.

    ``start`` / ``end`` / ``field`` are defaulted so positional two-argument
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

    Unlike :class:`Violation` this records *passes* too — "which claims were
    checked and survived" is what the reviewer's green underlines are drawn
    from, and it is the only thing "N of M claims verified" can be computed
    from. See CONTRACT-MUS-35.md §4.3.
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


# Claim kind -> the `Violation.kind` slug a contradicted claim reports as. The
# violation slugs are part of the frozen output `plan_outreach()` writes into
# `further_action`, so they are deliberately NOT the claim kinds.
_VIOLATION_KIND = {
    "amount": "unsupported_amount",
    "deals_count": "wrong_count",
    "quotes_count": "wrong_count",
    "producers_count": "wrong_count",
    "years_count": "wrong_count",
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

# Claim kinds that are deliberately excluded from the "N of M" ratio: targets
# and scheduling language are not assertions about the record, and a prohibition
# is not a claim about it either.
_UNCOUNTED_KINDS = frozenset(
    {"goal_reference", "future_date", "unauthorized_offer", "omission", "unsupported_year"}
)

# Claim kinds that block approval on their own, whatever the "N of M" ratio says.
# The summary ratio and the approve gate answer two different questions: "how
# much of this copy did we grade against the record?" and "may a reviewer send
# it?". An unauthorized commercial promise is the single most consequential
# thing generated copy can contain — `plan_outreach()` already fails closed on
# it via `needs_human=True`, and the approve gate must agree.
BLOCKING_KINDS = frozenset({"unauthorized_offer"})

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

# Counts are anchored to their noun *and* an achievement qualifier so goals and
# incidental numbers ("close 5 more deals", "a 15-minute call", "5-deal
# milestone") are ignored — only claims about the record are checked.
_DEALS_RE = re.compile(
    r"(?:(\d+)\s+closed\s+deals?|(\d+)\s+deals?\s+closed|closed\s+(\d+)\s+deals?)",
    re.IGNORECASE,
)
_QUOTES_RE = re.compile(
    r"(?:(\d+)\s+quotes?\s+(created|submitted)|(created|submitted)\s+(\d+)\s+quotes?)",
    re.IGNORECASE,
)
# Producer counts are anchored to the *lead's own team* (a possessive / second-
# person cue) so comparisons ("agencies with 50 producers") and hypotheticals
# ("as you add 2 producers") — which are not claims about this record — are not
# flagged. Up to three words may sit between the cue and the count.
_PRODUCERS_RE = re.compile(
    r"(?:your|team of|team's|roster of|staff of|you've|you have|you employ)\s+"
    r"(?:[\w'&/-]+\s+){0,3}?(\d+)\s+producers?\b",
    re.IGNORECASE,
)
_YEARS_RE = re.compile(
    r"(?:(\d+)[\s-]+years?\s+in\s+business|(\d+)-year-old)",
    re.IGNORECASE,
)

# Aspiration / goal framing that turns a count into a *target* rather than a
# claim about the record — e.g. "once you hit 20 closed deals", "toward 20
# closed deals", "your goal of 20 closed deals", "the 20 closed deals
# milestone". Milestones like this appear verbatim in HubSpot notes (lead_001:
# "if she hits 20 closed deals") and are exactly what a power-user email should
# echo, so they must not be flagged. Deliberately excludes the ambiguous
# "hit"/"reach" (they also describe achievements: "congrats on hitting 47"),
# relying on the conditional/directional words that actually precede them.
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
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    return None


def _int_attr(lead: Any, name: str) -> int | None:
    value = getattr(lead, name, None)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_group_int(match: re.Match) -> int | None:
    for group in match.groups():
        if group is not None and group.isdigit():
            return int(group)
    return None


def _is_goal_context(copy: str, start: int, end: int) -> bool:
    """True when the count at ``copy[start:end]`` is framed as a target/goal
    rather than a claim about the record. Checks the same clause just before the
    count for a goal cue ("once you hit 20 ...") and just after it for a
    milestone noun ("20 closed deals milestone")."""
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
    """MUS-39 persists the report to a JSONField."""
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def normalize_copy(copy: str) -> str:
    """Collapse ``\\r\\n`` / ``\\r`` to ``\\n`` before any offset is computed.

    An LLM may emit ``\\r\\n`` and a ``<textarea>`` may submit it; Python counts
    ``\\r\\n`` as two characters, so every span after the first line break would
    be off by one per preceding line (CONTRACT-MUS-35.md §9.1(c)).
    """
    if not copy:
        return copy
    return copy.replace("\r\n", "\n").replace("\r", "\n")


def _is_astral_safe(copy: str) -> bool:
    """True when JS string indices equal Unicode code-point indices (§9.1(a))."""
    return len(copy) == len(copy.encode("utf-16-le")) // 2


def _trim_span(copy: str, start: int, end: int) -> tuple[int, int]:
    """Trim surrounding whitespace off a match span (§9.1(b)).

    ``_CURRENCY_RE`` matches ``"$1,400,000 "`` because its optional magnitude
    suffix is ``\\b``-terminated; an untrimmed span underlines into the next word.
    """
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

    ``id`` is assigned later, once the report is sorted by offset, so it is
    stable within a report. De-duplication is keyed on
    ``(kind, start, end, message)``: keying on ``message`` alone (which is what
    ``verify_copy`` still does for its ``Violation`` list) collapses two
    genuinely different offsets that happen to read the same (§4.7).
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
    """Dollar figures the model is allowed to cite: the book size and any event
    premiums (both are shown to the model in the copy prompt)."""
    grounded: list[float] = []
    book = _coerce_number(getattr(lead, "estimated_book_size_usd", None))
    if book is not None:
        grounded.append(book)
    for event in _events(lead):
        meta = getattr(event, "meta", None) or {}
        premium = _coerce_number(meta.get("premium"))
        if premium is not None:
            grounded.append(premium)
    return grounded


def _is_grounded_amount(value: float, grounded: list[float]) -> bool:
    for target in grounded:
        if value == target:
            return True
        if target and abs(value - target) <= _AMOUNT_TOLERANCE * abs(target):
            return True
    return False


def _check_amounts(lead: Any, copy: str, claims: list | None = None) -> None:
    grounded = _grounded_amounts(lead)
    if not grounded:  # nothing to check against (real leads always have a book)
        return
    book = _coerce_number(getattr(lead, "estimated_book_size_usd", None))
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
            field="estimated_book_size_usd",
            expected=book,
            claimed=value,
            message=""
            if ok
            else (
                f"Copy cites {match.group(0).strip()} but no matching dollar "
                f"figure is in the lead record."
            ),
        )


def _check_counts(lead: Any, copy: str, claims: list | None = None) -> None:
    deals = _int_attr(lead, "deals_closed")
    if deals is not None:
        for match in _DEALS_RE.finditer(copy):
            claimed = _first_group_int(match)
            if _is_goal_context(copy, match.start(), match.end()):
                _goal_claim(claims, copy, match, "deals_closed", deals, claimed)
                continue
            if claimed is None:
                continue
            _count_claim(
                claims,
                copy,
                match,
                kind="deals_count",
                field="deals_closed",
                expected=deals,
                claimed=claimed,
                message=f"Copy claims {claimed} closed deals but the record shows {deals}.",
            )

    created = _int_attr(lead, "quotes_created")
    submitted = _int_attr(lead, "quotes_submitted")
    for match in _QUOTES_RE.finditer(copy):
        if match.group(1) is not None:
            claimed, qualifier = int(match.group(1)), match.group(2).lower()
        else:
            claimed, qualifier = int(match.group(4)), match.group(3).lower()
        field = "quotes_created" if qualifier == "created" else "quotes_submitted"
        expected = created if qualifier == "created" else submitted
        if _is_goal_context(copy, match.start(), match.end()):
            _goal_claim(claims, copy, match, field, expected, claimed)
            continue
        if expected is None:
            continue
        _count_claim(
            claims,
            copy,
            match,
            kind="quotes_count",
            field=field,
            expected=expected,
            claimed=claimed,
            message=f"Copy claims {claimed} quotes {qualifier} but the record shows {expected}.",
        )

    producers = _int_attr(lead, "num_producers")
    if producers is not None:
        for match in _PRODUCERS_RE.finditer(copy):
            claimed = int(match.group(1))
            if _is_goal_context(copy, match.start(), match.end()):
                _goal_claim(claims, copy, match, "num_producers", producers, claimed)
                continue
            _count_claim(
                claims,
                copy,
                match,
                kind="producers_count",
                field="num_producers",
                expected=producers,
                claimed=claimed,
                message=f"Copy claims {claimed} producers but the record shows {producers}.",
            )

    years = _int_attr(lead, "years_in_business")
    if years is not None:
        for match in _YEARS_RE.finditer(copy):
            claimed = _first_group_int(match)
            if _is_goal_context(copy, match.start(), match.end()):
                _goal_claim(claims, copy, match, "years_in_business", years, claimed)
                continue
            if claimed is None:
                continue
            _count_claim(
                claims,
                copy,
                match,
                kind="years_count",
                field="years_in_business",
                expected=years,
                claimed=claimed,
                message=f"Copy claims {claimed} years in business but the record shows {years}.",
            )


def _count_claim(claims, copy, match, *, kind, field, expected, claimed, message) -> None:
    ok = claimed == expected
    _claim(
        claims,
        kind=kind,
        copy=copy,
        start=match.start(),
        end=match.end(),
        verified=ok,
        field=field,
        expected=expected,
        claimed=claimed,
        message="" if ok else message,
    )


def _goal_claim(claims, copy, match, field, expected, claimed) -> None:
    """A count framed as a target ("on track for the 20 closed deals mark").

    Not an assertion about the record, so it is neither a violation nor part of
    the "N of M" ratio — but the reviewer still wants to see it was inspected.
    """
    _claim(
        claims,
        kind="goal_reference",
        copy=copy,
        start=match.start(),
        end=match.end(),
        verified=None,
        field=field,
        expected=expected,
        claimed=claimed,
        message=_GOAL_REFERENCE_MESSAGE,
    )


def _check_contact_name(lead: Any, copy: str, claims: list | None = None) -> None:
    contact = (getattr(lead, "contact_name", "") or "").strip()
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
    dates: set[datetime.date] = set()
    for attr in ("signed_up_date", "last_login_date", "last_contacted_date"):
        value = _as_date(getattr(lead, attr, None))
        if value is not None:
            dates.add(value)
    for event in _events(lead):
        value = _as_date(getattr(event, "timestamp", None))
        if value is not None:
            dates.add(value)
    return dates


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


def _agency_tokens(lead: Any) -> list[str]:
    name = getattr(lead, "agency_name", "") or ""
    return [t for t in _tokens(name) if len(t) >= 3 and t not in _AGENCY_STOPWORDS]


def _check_strict(lead: Any, copy: str, today: datetime.date, claims: list | None = None) -> None:
    """Omission / loose-grounding checks layered on top of ``standard``."""
    low = copy.lower()

    contact = (getattr(lead, "contact_name", "") or "").strip()
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

    agency_tokens = _agency_tokens(lead)
    if agency_tokens and not any(re.search(rf"\b{re.escape(t)}\b", low) for t in agency_tokens):
        agency = getattr(lead, "agency_name", "")
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

    Check order is load-bearing: ``verify_copy`` derives its ``Violation`` list
    from this list, and the order of the messages a BD reviewer reads must not
    change.
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

    Returns a list of :class:`Violation` (empty means the copy is grounded).
    ``level`` selects strictness (``off | standard | strict``); ``action_type``
    gates the unauthorized-offer check (volume pricing is fine for a reward).
    Pure and deterministic: no database, no LLM.

    ``claims`` is a keyword-only *out*-parameter: pass a list to also receive
    every inspected :class:`Claim`, including the ones that passed. The return
    value, its order and its de-duplication are unchanged either way — see
    ``tests_verify_spans.py``.
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
    """Build the v1 verification report (CONTRACT-MUS-35.md §4.4).

    The report is what the reviewer's underlines and the
    "N of M claims verified" summary are rendered from. ``copy`` is normalized
    (§9.1(c)) and echoed back: offsets index into ``report["copy"]``, never into
    whatever the client currently has in its textarea (§9.2).

    ``can_approve`` has **two independent causes** and is false if either holds:
    a contradicted claim about the record, or a claim of a
    :data:`BLOCKING_KINDS` kind. The two compose — neither overrides the other —
    so a reviewer's warning state may have to point at an offer span rather than
    at a mismatched number.

    Pure: no database, no LLM, duck-typed on the same lead attributes.
    """
    today = today or datetime.date.today()
    copy = normalize_copy(copy)
    claims = _collect_claims(lead, copy, action_type, level, today)

    # Offsets order the report; ids are assigned afterwards so they are stable
    # within it. Omission claims have no span and sort last.
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
