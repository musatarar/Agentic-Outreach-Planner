"""The bounded tool-calling loop for one lead (MUS-29).

Budgets (steps, tool calls, wall clock) come from ``PlannerRuntime``; every
provider response and its consequent step records are persisted through the
checkpoint before the loop continues, which is what bounds crash re-billing at
one call per in-flight lead.

Skeleton: signatures frozen by docs/contracts/agent-loop.md; the body lands in
the ``loop`` component PR.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from project.app.services.agent.state import Checkpoint, StepRecord
from project.app.services.agent.tools import ToolContext
from project.app.services.llm.base import LLMClient
from project.app.services.llm.runtime import PlannerRuntime


@dataclass(frozen=True, slots=True)
class AgentOutcome:
    """What the loop hands back to phase 3.

    Deliberately carries no ``action_type``, ``priority`` or ``needs_human``:
    the rules engine keeps sole authority over classification, and this package
    never imports the send-gate module — both facts are pinned by
    ``tests_agent_loop_assembly.py``.
    """

    draft_text: str = ""
    error: Exception | None = None
    steps_used: int = 0
    tool_calls_used: int = 0


async def run_agent_lead(
    *,
    prompt: str,
    lead_run_pk: int,
    prior_steps: Sequence[StepRecord],
    context: ToolContext,
    client: LLMClient,
    runtime: PlannerRuntime,
    checkpoint: Checkpoint,
) -> AgentOutcome:
    """Drive one lead's loop: claim, fold, call, execute tools, checkpoint."""
    raise NotImplementedError("loop component owns run_agent_lead")
