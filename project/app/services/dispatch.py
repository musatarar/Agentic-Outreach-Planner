"""The only send path (MUS-29): a hash-bound, one-shot approval gate.

Nothing sends without a live, resolved ``approve_send`` decision whose hash
matches ``effective_copy`` at send time, re-established from the database
inside the transaction that consumes it. The ``approved → sent`` flip is a
conditional-UPDATE CAS; the ``OutboundSend`` OneToOne backstops double sends.
Delivery is console-only.
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


def _live_approval(action: OutreachAction, *, lock: bool = False) -> tuple[ReviewDecision, str]:
    """The live approval authorizing ``action``'s copy *right now*, with the
    digest of the bytes it authorizes.

    Both halves are read together from the database, never from the caller's
    in-memory ``action``. ``lock`` takes the row for update (a no-op on SQLite,
    where the re-read carries the guarantee).
    """
    decisions = ReviewDecision.objects.filter(
        outreach_action=action,
        kind=ReviewDecision.KIND_APPROVE_SEND,
        status=ReviewDecision.STATUS_RESOLVED,
        voided_at__isnull=True,
    ).order_by("-created_at")
    decision = (decisions.select_for_update() if lock else decisions).first()
    if decision is None:
        raise DispatchBlocked("no live approve_send decision recorded for this action")

    body = (
        OutreachAction.objects.filter(pk=action.pk)
        .values_list("edited_copy", "suggested_copy")
        .first()
    )
    if body is None:
        raise DispatchBlocked("action no longer exists")
    effective_copy = body[0] or body[1]

    digest = hashlib.sha256(effective_copy.encode("utf-8")).hexdigest()
    if digest != decision.approved_body_sha256:
        raise DispatchBlocked("copy changed after approval; re-approve before sending")
    return decision, digest


def dispatch(action: OutreachAction) -> OutboundSend:
    """Send ``action``'s approved copy exactly once; return the ``OutboundSend``."""
    # Cheap pre-check; decides nothing — the re-read below is authoritative.
    _live_approval(action)

    with transaction.atomic():
        # The CAS picks the winner: exactly one concurrent dispatch flips
        # approved -> sent.
        updated = OutreachAction.objects.filter(
            pk=action.pk, status=OutreachAction.STATUS_APPROVED
        ).update(status=OutreachAction.STATUS_SENT, status_changed_at=timezone.now())
        if updated != 1:
            raise DispatchBlocked("action is not in an approved state")
        # Winning the CAS is not authorization: an undo + re-approve can leave
        # the action `approved` over different bytes. Re-check liveness and hash
        # equality inside the consuming transaction; raising rolls the CAS back.
        decision, digest = _live_approval(action, lock=True)
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
