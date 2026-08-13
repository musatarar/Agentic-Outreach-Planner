"""The only send path (MUS-29): a hash-bound, one-shot approval gate.

Nothing sends without a live (``voided_at IS NULL``), resolved ``approve_send``
``ReviewDecision`` whose ``approved_body_sha256`` equals
``sha256(action.effective_copy)`` at send time; :func:`dispatch` hard-raises
otherwise. The ``approved → sent`` flip is a conditional-UPDATE CAS and the
``OutboundSend`` OneToOne is the DB backstop against a double send. Delivery is
console-only — the gate is the deliverable, not SMTP.
"""

from __future__ import annotations

import hashlib
import logging

from django.db import transaction
from django.utils import timezone

from project.app.models import OutboundSend, OutreachAction, ReviewDecision

logger = logging.getLogger(__name__)


class DispatchBlocked(RuntimeError):
    """A required link in the approval chain is missing; nothing was sent."""


def dispatch(action: OutreachAction) -> OutboundSend:
    """Send ``action``'s approved copy exactly once; return the ``OutboundSend``."""
    decision = (
        ReviewDecision.objects.filter(
            outreach_action=action,
            kind=ReviewDecision.KIND_APPROVE_SEND,
            status=ReviewDecision.STATUS_RESOLVED,
            voided_at__isnull=True,
        )
        .order_by("-created_at")
        .first()
    )
    if decision is None:
        raise DispatchBlocked("no live approve_send decision recorded for this action")

    body = action.effective_copy
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if digest != decision.approved_body_sha256:
        raise DispatchBlocked("copy changed after approval; re-approve before sending")

    with transaction.atomic():
        # The CAS is the winner-picker (same shape as the login-token consume):
        # exactly one concurrent dispatch sees approved and flips it to sent.
        updated = OutreachAction.objects.filter(
            pk=action.pk, status=OutreachAction.STATUS_APPROVED
        ).update(status=OutreachAction.STATUS_SENT, status_changed_at=timezone.now())
        if updated != 1:
            raise DispatchBlocked("action is not in an approved state")
        record = OutboundSend.objects.create(
            outreach_action=action,
            decision=decision,
            body_sha256=digest,
            channel="console",
        )

    logger.info(
        "console dispatch of action %s to %s (sha256 %s)",
        action.pk,
        action.lead.contact_email,
        digest,
    )
    action.refresh_from_db(fields=["status", "status_changed_at"])
    return record
