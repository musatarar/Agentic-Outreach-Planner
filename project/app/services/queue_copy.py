"""Copy normalization, verification snapshots and edit diffs (MUS-39).

Shared between ``plan_outreach()`` and the triage queue views — a service must
not import from the API layer. Normalization happens *before* storing copy or
computing any offset; verification and approval each have one reading here.
"""

from __future__ import annotations

import datetime
import difflib
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


def diff_edit(before: str, after: str) -> dict:
    """Summarize one reviewer edit for the eval corpus (MUS-21).

    ``diff_ops`` is schema v1: ``SequenceMatcher`` opcodes minus ``"equal"``
    runs, each carrying its before/after text.
    """
    matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
    ops: list[dict] = []
    chars_added = 0
    chars_removed = 0
    for op, a0, a1, b0, b1 in matcher.get_opcodes():
        if op == "equal":
            continue
        ops.append(
            {
                "op": op,
                "a0": a0,
                "a1": a1,
                "b0": b0,
                "b1": b1,
                "before": before[a0:a1],
                "after": after[b0:b1],
            }
        )
        chars_added += b1 - b0
        chars_removed += a1 - a0
    return {
        "diff_ops": ops,
        "chars_added": chars_added,
        "chars_removed": chars_removed,
        "similarity": matcher.ratio(),
    }
