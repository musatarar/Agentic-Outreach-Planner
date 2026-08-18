"""Assess the next action for one lead (MUS-70).

Answers "what should happen with this client, and why" without composing mail.
The rules decide; this module only surfaces and persists their answer. Its own
module rather than part of the planner: assess writes no outreach row and
consults no suppression, so it neither suppresses nor is suppressed.
"""

from __future__ import annotations

import datetime

from project.app.models import DismissedOutreachKey, Lead, LeadAssessment, OutreachAction
from project.app.services import dedupe as dedupe_service
from project.app.services.outreach import determine_action, explain, failed_generation_filter

# Statuses in which a recommendation is still the AE's to act on. Same pair the
# planner treats as open, read here only to report it back.
OPEN_STATUSES = (OutreachAction.STATUS_PENDING, OutreachAction.STATUS_SNOOZED)


def _open_action(lead_id: str, key: str) -> OutreachAction | None:
    """The newest open outreach row for this exact recommendation, if any.

    Excludes failed-generation rows for the same reason the planner does: a
    recorded failure is not something the AE has queued.
    """
    return (
        OutreachAction.objects.filter(lead_id=lead_id, dedupe_key=key, status__in=OPEN_STATUSES)
        .exclude(failed_generation_filter())
        .order_by("-created_at", "-id")
        .first()
    )


def _is_dismissed(key: str) -> bool:
    """Whether this recommendation carries a live dismissal."""
    return DismissedOutreachKey.objects.filter(dedupe_key=key, revoked_at__isnull=True).exists()


def assess_lead(lead: Lead, today: datetime.date | None = None) -> LeadAssessment:
    """Assess ``lead`` and persist the result. Always answers, never refuses.

    Deterministic only: no provider is called and no advisory is produced, so
    ``advisory_status`` reports ``disabled``. The advisory half lands in the
    follow-up PR and may never change ``action_type`` or ``priority``.
    """
    today = today or datetime.date.today()

    # Two pure calls over the same rules: `explain` for the envelope, which has
    # no reason text, and `determine_action` for the reason the AE reads.
    envelope = explain(lead, today)
    action_type, reason = determine_action(lead, today)

    key = dedupe_service.dedupe_key(lead.id, action_type)
    return LeadAssessment.objects.create(
        lead=lead,
        action_type=action_type,
        priority=envelope["priority"]["value"],
        reason=reason,
        rule_trace=envelope,
        advisory_status=LeadAssessment.ADVISORY_DISABLED,
        open_outreach_action=_open_action(lead.id, key),
        dismissed=_is_dismissed(key),
    )
