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
from dataclasses import dataclass
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
    """

    kind: str
    message: str


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
_PRODUCERS_RE = re.compile(r"(\d+)\s+producers?\b", re.IGNORECASE)
_YEARS_RE = re.compile(
    r"(?:(\d+)[\s-]+years?\s+in\s+business|(\d+)-year-old)",
    re.IGNORECASE,
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


def _check_amounts(lead: Any, copy: str) -> list[Violation]:
    grounded = _grounded_amounts(lead)
    if not grounded:  # nothing to check against (real leads always have a book)
        return []
    violations = []
    for match in _CURRENCY_RE.finditer(copy):
        value = _money_to_number(match.group(1), match.group(2))
        if not _is_grounded_amount(value, grounded):
            violations.append(
                Violation(
                    "unsupported_amount",
                    f"Copy cites {match.group(0).strip()} but no matching dollar "
                    f"figure is in the lead record.",
                )
            )
    return violations


def _check_counts(lead: Any, copy: str) -> list[Violation]:
    violations = []

    deals = _int_attr(lead, "deals_closed")
    if deals is not None:
        for match in _DEALS_RE.finditer(copy):
            claimed = _first_group_int(match)
            if claimed is not None and claimed != deals:
                violations.append(
                    Violation(
                        "wrong_count",
                        f"Copy claims {claimed} closed deals but the record shows {deals}.",
                    )
                )

    created = _int_attr(lead, "quotes_created")
    submitted = _int_attr(lead, "quotes_submitted")
    for match in _QUOTES_RE.finditer(copy):
        if match.group(1) is not None:
            claimed, qualifier = int(match.group(1)), match.group(2).lower()
        else:
            claimed, qualifier = int(match.group(4)), match.group(3).lower()
        expected = created if qualifier == "created" else submitted
        if expected is not None and claimed != expected:
            violations.append(
                Violation(
                    "wrong_count",
                    f"Copy claims {claimed} quotes {qualifier} but the record shows {expected}.",
                )
            )

    producers = _int_attr(lead, "num_producers")
    if producers is not None:
        for match in _PRODUCERS_RE.finditer(copy):
            claimed = int(match.group(1))
            if claimed != producers:
                violations.append(
                    Violation(
                        "wrong_count",
                        f"Copy claims {claimed} producers but the record shows {producers}.",
                    )
                )

    years = _int_attr(lead, "years_in_business")
    if years is not None:
        for match in _YEARS_RE.finditer(copy):
            claimed = _first_group_int(match)
            if claimed is not None and claimed != years:
                violations.append(
                    Violation(
                        "wrong_count",
                        f"Copy claims {claimed} years in business but the record shows {years}.",
                    )
                )

    return violations


def _check_contact_name(lead: Any, copy: str) -> Violation | None:
    contact = (getattr(lead, "contact_name", "") or "").strip()
    if not contact:
        return None
    contact_tokens = set(_tokens(contact))
    match = _SALUTATION_RE.search(copy)
    if not match:
        return None
    greeted = match.group(1).strip()
    greeted_tokens = [t for t in _tokens(greeted) if t not in _HONORIFICS]
    if not greeted_tokens:  # e.g. "Dear Sir," — nothing to contradict
        return None
    if contact_tokens.intersection(greeted_tokens):
        return None
    if set(greeted_tokens) <= _GENERIC_SALUTATIONS:
        return None
    return Violation(
        "wrong_contact_name",
        f'Copy greets "{greeted}" but the lead contact is {contact}.',
    )


def _check_offer(lead: Any, copy: str, action_type: str) -> list[Violation]:
    # Volume pricing / discounts are authorized only for the reward action.
    if action_type == actions.POWER_USER_REWARD:
        return []
    for pattern in _OFFER_RES:
        match = pattern.search(copy)
        if match:
            return [
                Violation(
                    "unauthorized_offer",
                    f'Copy makes a commercial promise ("{match.group(0).strip()}") that '
                    f"is not authorized for a {action_type} action.",
                )
            ]
    return []


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


def _check_iso_dates(lead: Any, copy: str, today: datetime.date) -> list[Violation]:
    record = _record_dates(lead)
    violations = []
    for match in _ISO_DATE_RE.finditer(copy):
        try:
            cited = datetime.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            continue  # not a real calendar date
        if cited in record:
            continue
        # A past/today ISO date in prose is a copied record fact; a future one
        # is scheduling language ("let's talk on ..."), which we don't ground.
        if cited <= today:
            violations.append(
                Violation(
                    "unsupported_date",
                    f"Copy cites the date {match.group(0)}, which is not in the lead record.",
                )
            )
    return violations


def _agency_tokens(lead: Any) -> list[str]:
    name = getattr(lead, "agency_name", "") or ""
    return [t for t in _tokens(name) if len(t) >= 3 and t not in _AGENCY_STOPWORDS]


def _check_strict(lead: Any, copy: str, today: datetime.date) -> list[Violation]:
    """Omission / loose-grounding checks layered on top of ``standard``."""
    violations = []
    low = copy.lower()

    contact = (getattr(lead, "contact_name", "") or "").strip()
    if contact:
        first = _tokens(contact)[0] if _tokens(contact) else ""
        if len(first) >= 2 and not re.search(rf"\b{re.escape(first)}\b", low):
            violations.append(
                Violation(
                    "contact_name_absent",
                    f"Copy never addresses the contact by name ({contact}).",
                )
            )

    agency_tokens = _agency_tokens(lead)
    if agency_tokens and not any(re.search(rf"\b{re.escape(t)}\b", low) for t in agency_tokens):
        violations.append(
            Violation(
                "agency_name_absent",
                f'Copy never names the agency ("{getattr(lead, "agency_name", "")}").',
            )
        )

    allowed_years = {today.year, today.year + 1}
    allowed_years.update(d.year for d in _record_dates(lead))
    for match in _YEAR_RE.finditer(copy):
        year = int(match.group(0))
        if year not in allowed_years:
            violations.append(
                Violation(
                    "unsupported_year",
                    f"Copy mentions the year {year}, which is not tied to any record date.",
                )
            )

    return violations


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------


def verify_copy(
    lead: Any,
    copy: str,
    action_type: str,
    *,
    level: str = DEFAULT_LEVEL,
    today: datetime.date | None = None,
) -> list[Violation]:
    """Check generated ``copy`` against the ``lead`` record.

    Returns a list of :class:`Violation` (empty means the copy is grounded).
    ``level`` selects strictness (``off | standard | strict``); ``action_type``
    gates the unauthorized-offer check (volume pricing is fine for a reward).
    Pure and deterministic: no database, no LLM.
    """
    if level == LEVEL_OFF or not copy:
        return []
    today = today or datetime.date.today()

    violations: list[Violation] = []
    violations += _check_amounts(lead, copy)
    violations += _check_counts(lead, copy)
    name_violation = _check_contact_name(lead, copy)
    if name_violation is not None:
        violations.append(name_violation)
    violations += _check_offer(lead, copy, action_type)
    violations += _check_iso_dates(lead, copy, today)
    if level == LEVEL_STRICT:
        violations += _check_strict(lead, copy, today)

    # Overlapping patterns can surface the same problem twice; keep first seen.
    seen: set[str] = set()
    unique: list[Violation] = []
    for violation in violations:
        if violation.message not in seen:
            seen.add(violation.message)
            unique.append(violation)
    return unique


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
