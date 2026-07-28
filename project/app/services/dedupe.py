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

    Deliberately scoped to (lead, action_type) and NOT to the reason text or
    the rule trace. The product promise is "dismiss is permanent": if a
    reviewer says "stop asking me to nudge this lead's usage", a re-run that
    computes a marginally different reason string must not resurrect it.

    It is scoped to action_type (rather than lead alone) so a genuinely
    different situation still surfaces: dismissing `nudge_usage` for lead_007
    does not suppress a later `reengage_dormant` for the same lead.

    Bumping DEDUPE_VERSION intentionally un-suppresses everything and is a
    deliberate, reviewed act -- never a side effect.
    """
    raw = f"{DEDUPE_VERSION}|{lead_id}|{action_type}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
