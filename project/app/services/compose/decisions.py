"""Suggestion decisions (MUS-47 component 8): accept and reject, symmetrically.

Accepting a suggestion is a logged human decision, per lead, and the UI shows the delta
(``P2 -> P1 . accepted``) so the audit trail is visible rather than buried.

Two invariants:

* ``rules_priority`` / ``rules_action`` are never written here. The deterministic verdict
  is immutable; accept moves ``effective_*`` and nothing else.
* Both outcomes are real. Reject is not "do nothing" -- it restores ``effective_*`` to the
  rules values, stamps actor and timestamp, and is as re-runnable as accept. Accept ->
  reject -> accept is legal and the last decision wins.

An accepted ``action_change`` also recomputes ``dedupe_key`` (the identity of the
recommendation genuinely changed) and rebuilds the reason from
:data:`ACCEPTED_ACTION_REASON` -- a first-party template. The model's own ``rationale`` is
untrusted-derived prose and never becomes a generation-prompt input.
"""

from __future__ import annotations

ACCEPTED_ACTION_REASON = (
    "Reviewer accepted an agent suggestion to change the action from {old} to {new}."
)


def accept_suggestion(run, lead_id: str, *, actor: str):
    raise NotImplementedError("decisions component (MUS-47) owns accept_suggestion")


def reject_suggestion(run, lead_id: str, *, actor: str):
    raise NotImplementedError("decisions component (MUS-47) owns reject_suggestion")
