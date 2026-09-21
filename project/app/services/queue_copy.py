"""Copy normalization and verification snapshots.

Shared between the actions engine and the review views — a service must not
import from the API layer. Normalization happens *before* storing copy or
computing any offset; verification and approval each have one reading here.
"""

from __future__ import annotations

import datetime
from typing import Any

from project.app.services import verify


def normalize_copy(text: str | None) -> str:
    """Collapse CRLF/CR line endings to LF. ``None`` becomes ``""``."""
    if not text:
        return ""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def is_astral_safe(text: str) -> bool:
    """True when JS UTF-16 string indices equal Python code-point indices.

    False on astral characters (e.g. emoji), when the frontend must slice via
    ``Array.from()``.
    """
    return len(text) == len(text.encode("utf-16-le")) // 2


def _default_level() -> str:
    from django.conf import settings

    return getattr(settings, "COPY_VERIFY_LEVEL", verify.DEFAULT_LEVEL)


def build_verification(
    lead: Any,
    copy: str | None,
    action_type: str,
    *,
    level: str | None = None,
    today: datetime.date | None = None,
) -> dict:
    """Return the v1 verification report for ``copy``, normalized first.

    Called on every write that changes the copy in play; never appended to.
    """
    normalized = normalize_copy(copy)
    report = verify.verify_spans(
        lead,
        normalized,
        action_type,
        level=level or _default_level(),
        today=today or datetime.date.today(),
    )
    return dict(report)


def can_approve(report: dict | None) -> bool:
    """Server-side approve gate: the one reading of a verification report.

    Fails CLOSED on a missing or blank report — "we could not check this copy"
    blocks approval rather than waving it through.
    """
    if not report:
        return False
    return bool(report.get("can_approve", False))
