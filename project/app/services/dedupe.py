"""Stable identity for an outreach recommendation (MUS-39).

Pure: no Django, no database. ``plan_outreach()`` and the triage views both
key off this, so it lives on its own rather than inside either of them.
"""

from __future__ import annotations

import hashlib

DEDUPE_VERSION = "v1"


def dedupe_key(lead_id: str, action_type: str) -> str:
    """Stable identity of a recommendation.

    KEY = sha256("v1|{lead_id}|{action_type}").hexdigest()

    Deliberately scoped to (lead, action_type), never the reason text: "dismiss
    is permanent" must survive re-runs, while a different action type for the
    same lead still surfaces. Bumping DEDUPE_VERSION un-suppresses everything —
    a deliberate act, never a side effect.
    """
    raw = f"{DEDUPE_VERSION}|{lead_id}|{action_type}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
