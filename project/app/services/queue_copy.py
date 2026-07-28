"""Copy normalization, verification snapshots and edit diffs (MUS-39).

Three jobs, all of them shared between ``plan_outreach()`` (a service) and the
triage queue views, which is why they live here rather than in either caller --
a service must not import from the API layer.

1. **Normalization.** An LLM may emit ``\\r\\n``; a ``<textarea>`` may submit
   ``\\r\\n``; Python counts ``\\r\\n`` as two characters. Every span offset in
   a verification report would then be off by one per preceding line break.
   The server normalizes *before* storing copy and *before* computing any
   offset (CONTRACT MUS-35 section 9.1c).

2. **Verification envelopes.** ``verify.verify_spans()`` lands with MUS-42,
   which merges before MUS-39. Until then this module returns a well-formed
   but empty v1 envelope, so the API shape is correct on either side of that
   merge and nothing downstream needs a null branch.

3. **Edit diffs.** The ``(suggested, edited)`` pair plus its opcodes is the
   copy eval corpus (MUS-21): every correction a human makes is labeled
   training data, and it is only capturable at the moment of editing.
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

    False as soon as the copy contains an astral character (an emoji echoed
    out of a HubSpot note), at which point the frontend must slice via
    ``Array.from()`` -- see CONTRACT section 9.1a.
    """
    return len(text) == len(text.encode("utf-16-le")) // 2


def _default_level() -> str:
    from django.conf import settings

    return getattr(settings, "COPY_VERIFY_LEVEL", verify.DEFAULT_LEVEL)


def empty_report(copy: str, level: str, today: datetime.date) -> dict:
    """A well-formed, claim-free v1 verification envelope (section 4.4)."""
    return {
        "version": 1,
        "level": level,
        "today": today.isoformat(),
        "copy": copy,
        "copy_length": len(copy),
        "is_astral_safe": is_astral_safe(copy),
        "verified_count": 0,
        "unverified_count": 0,
        "checked_count": 0,
        "summary": "0 of 0 claims verified",
        "can_approve": True,
        "claims": [],
    }


def build_verification(
    lead: Any,
    copy: str | None,
    action_type: str,
    *,
    level: str | None = None,
    today: datetime.date | None = None,
) -> dict:
    """Return the v1 verification report for ``copy``, normalized first.

    ``verification`` on an OutreachAction always describes ``effective_copy``
    (CONTRACT section 9.2), so this is called on every write that changes the
    copy in play -- planning, editing, reverting -- and never appended to.
    """
    normalized = normalize_copy(copy)
    level = level or _default_level()
    today = today or datetime.date.today()

    verify_spans = getattr(verify, "verify_spans", None)
    if verify_spans is None:  # MUS-42 not merged yet
        return empty_report(normalized, level, today)

    report = verify_spans(lead, normalized, action_type, level=level, today=today)
    return dict(report)


def can_approve(report: dict | None) -> bool:
    """Server-side approve gate. Absent/blank report => nothing contradicts."""
    if not report:
        return True
    return bool(report.get("can_approve", True))


def diff_edit(before: str, after: str) -> dict:
    """Summarize one reviewer edit for the eval corpus.

    ``diff_ops`` is schema v1: ``difflib.SequenceMatcher`` opcodes with the
    ``"equal"`` runs dropped, each carrying the before/after text so the corpus
    is readable without re-running the diff.
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
