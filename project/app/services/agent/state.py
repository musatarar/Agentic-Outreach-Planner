"""Event-sourced agent run state (MUS-29).

State is a fold over append-only ``AgentStep`` rows: the crash checkpoint and
the reports-page trace are the same artifact, so they cannot drift. Steps
persist the *sanitized, unwrapped* tool result; ``wrap_untrusted()`` delimiters
are applied exactly once, at fold time — the persisted trace stays greppable
and the conversation is always fenced.

This module holds phase 3's one sanctioned ORM exception: checkpoint writes go
through :class:`Checkpoint`, each a short ``transaction.atomic()`` via
``sync_to_async(thread_sensitive=True)`` under one ``asyncio.Lock``. Never
``select_for_update`` (whole-DB lock on SQLite); the claim is an epoch-CAS
conditional UPDATE, the ``LoginToken`` single-use pattern.

Skeleton: signatures frozen by docs/contracts/agent-loop.md; bodies land in the
``loop`` component PR.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from project.app.services.llm.chat_types import Message

KIND_LLM_CALL = "llm_call"
KIND_TOOL_RESULT = "tool_result"
KIND_FINAL = "final"


@dataclass(frozen=True, slots=True)
class StepRecord:
    """One persisted step, detached from the ORM so phase 3 stays ORM-free."""

    seq: int
    kind: str
    payload: Mapping[str, Any]


class AgentClaimLost(RuntimeError):
    """Another worker won the epoch-CAS claim for this lead run.

    The loser writes nothing and produces no ``OutreachAction`` row.
    """


def fold_messages(
    prompt: str, steps: Sequence[StepRecord], *, force_final: bool = False
) -> list[Message]:
    """Rebuild the conversation from the step log (the trace-to-messages fold)."""
    raise NotImplementedError("loop component owns fold_messages")


def create_lead_runs(trace_run_id: str, lead_ids: Sequence[str]) -> dict[str, int]:
    """Idempotently ensure one ``AgentLeadRun`` per lead; return lead_id → pk.

    Sync, called from phase 2 (get_or_create backed by the
    ``alr_one_run_per_lead_per_trace`` constraint).
    """
    raise NotImplementedError("loop component owns create_lead_runs")


def load_prior_steps(lead_run_pk: int) -> tuple[StepRecord, ...]:
    """Read a run's persisted steps in ``seq`` order. Sync, called from phase 2."""
    raise NotImplementedError("loop component owns load_prior_steps")


class Checkpoint:
    """Owns one worker's claim token and the lock serializing its DB writes."""

    def __init__(self) -> None:
        # Worker token (uuid4 hex) and asyncio.Lock arrive with the loop
        # component PR; the skeleton keeps construction side-effect free.
        pass

    async def claim(self, lead_run_pk: int) -> bool:
        """Epoch-CAS claim of one run; ``False`` means another worker owns it."""
        raise NotImplementedError("loop component owns Checkpoint.claim")

    async def append(
        self,
        lead_run_pk: int,
        records: Sequence[StepRecord],
        *,
        status: str,
        steps_used: int,
        tool_calls_used: int,
    ) -> None:
        """Persist step records + counters in one short transaction (write-ahead unit)."""
        raise NotImplementedError("loop component owns Checkpoint.append")
