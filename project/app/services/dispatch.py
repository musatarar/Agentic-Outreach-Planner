"""The only send path (MUS-29): a hash-bound, one-shot approval gate.

Nothing sends without a live (``voided_at IS NULL``), resolved ``approve_send``
``ReviewDecision`` whose ``approved_body_sha256`` equals
``sha256(action.effective_copy)`` at send time; :func:`dispatch` hard-raises
otherwise. The ``approved → sent`` flip is a conditional-UPDATE CAS and the
``OutboundSend`` OneToOne is the DB backstop against a double send. Delivery is
console-only — the gate is the deliverable, not SMTP.

Skeleton: signature frozen by docs/contracts/agent-loop.md; the body lands in
the ``approval_gate`` component PR (typed against the agent_models schema).
"""

from __future__ import annotations

from typing import Any


class DispatchBlocked(RuntimeError):
    """A required link in the approval chain is missing; nothing was sent."""


def dispatch(action: Any) -> Any:
    """Send ``action``'s approved copy exactly once; return the ``OutboundSend``."""
    raise NotImplementedError("approval_gate component owns dispatch")
