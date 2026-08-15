"""Run lifecycle (MUS-47 component 4): create, classify, close, discard.

One active run at a time, and the database decides it. ``create_run`` inserts and lets
the ``pr_one_active_run`` partial unique index reject the second one -- a read-then-write
"is there an active run?" check would let two concurrent POSTs both win.

``classify_run`` is the free stage: deterministic rules over the scoped queryset, writing
``RunLead`` rows with the schema-v1 trace. It constructs no LLM client and makes no
provider call, which is pinned by a test rather than left as an intention.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class RunConflict(RuntimeError):
    """A run already occupies the single active slot."""

    def __init__(self, active_run_id: int) -> None:
        super().__init__(f"run {active_run_id} is already active")
        self.active_run_id = active_run_id


class InvalidRunTransition(RuntimeError):
    """The requested stage is not reachable from the run's current status."""


class RunNotFound(LookupError):
    """No run with that id."""


def create_run(*, scope: Mapping[str, Any], created_by: str):
    raise NotImplementedError("lifecycle component (MUS-47) owns create_run")


def active_run():
    raise NotImplementedError("lifecycle component (MUS-47) owns active_run")


def classify_run(run):
    raise NotImplementedError("lifecycle component (MUS-47) owns classify_run")


def close_run(run, *, actor: str):
    raise NotImplementedError("lifecycle component (MUS-47) owns close_run")


def discard_run(run, *, actor: str):
    raise NotImplementedError("lifecycle component (MUS-47) owns discard_run")
