"""Selective generation (MUS-47 component 9): copy for the leads actually chosen.

The expensive stage, and the only one that writes ``OutreachAction`` rows. It generates
for ``selected=True, already_queued=False`` rows only -- a lead with an open
recommendation sharing its dedupe key is flagged in the selection payload rather than
silently double-generated.

It reuses the planner's machinery rather than reimplementing it: ``classify_lead`` built
the prompt, ``_agenerate_all`` runs the bounded pool, and ``review_outcome`` plus
``snapshot_for`` apply the same two output gates. A composer draft is therefore subject
to exactly the grounding verifier a planner draft is -- that equivalence is the point,
and it is why no model-derived text from stage 03 is allowed anywhere near the prompt.

A failed lead records its ``failure_kind`` and stays selectable for retry. One dead
provider call does not sink the run.
"""

from __future__ import annotations

from collections.abc import Sequence


def select_leads(run, lead_ids: Sequence[str], selected: bool) -> int:
    """Bulk-toggle selection. Returns the number of rows changed."""
    raise NotImplementedError("generate component (MUS-47) owns select_leads")


def generate_for_selection(run, *, provider: str, model: str, actor: str) -> dict:
    """Generate copy for the selected rows.

    Returns ``{"generated", "failed", "skipped", "actual_usd"}``.
    """
    raise NotImplementedError("generate component (MUS-47) owns generate_for_selection")
