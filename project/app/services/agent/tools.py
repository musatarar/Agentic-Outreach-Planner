"""Four read-only tools the agent loop may call (MUS-29).

Skeleton: interfaces and data shapes are frozen by docs/contracts/agent-loop.md;
bodies land in the ``agent_tools`` component PR.

Tools execute as pure functions over a phase-2-built :class:`ToolContext`
snapshot — they never touch the ORM, and ``lead_id`` is never a model-supplied
argument: the executor is bound server-side to the current lead's context, so a
model-chosen ``lead_id`` has no lever. Every free-text field is
``sanitize_untrusted()``-cleaned at snapshot time and every rendered result is
capped at :data:`MAX_TOOL_RESULT_CHARS` at execution time.
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from project.app.services.llm.chat_types import ToolSpec

#: Hard cap applied to every rendered tool result at execution time, on top of
#: the per-field sanitization already applied at context build.
MAX_TOOL_RESULT_CHARS = 2000

#: The four read-only tools: get_lead_history, get_similar_won_deals,
#: get_product_details, check_ae_calendar. Names are frozen in the contract;
#: the JSON schemas land with the agent_tools component PR.
TOOL_SPECS: tuple[ToolSpec, ...] = ()


class UnknownTool(ValueError):
    """A tool name outside :data:`TOOL_SPECS` reached :func:`execute_tool`."""


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Per-lead snapshot every tool reads from, built in phase 2.

    Frozen because phase 3 shares it across the whole loop for one lead: a tool
    reading yesterday's snapshot is fine, a tool mutating it is not.
    """

    lead_id: str
    history: tuple[Mapping[str, Any], ...]
    similar_won_deals: tuple[Mapping[str, Any], ...]
    product_details: Mapping[str, Any]
    ae_slots: tuple[Mapping[str, Any], ...]


def similar_won_deals_for(lead: Any, all_leads: Sequence[Any]) -> tuple[dict[str, Any], ...]:
    """Deterministic top-3 closed-deal look-alikes for ``lead``."""
    raise NotImplementedError("agent_tools component owns similar_won_deals_for")


def build_tool_context(
    lead: Any,
    prior_actions: Sequence[Mapping[str, Any]],
    similar: tuple[Mapping[str, Any], ...],
    ae_slots: Sequence[Mapping[str, Any]],
    today: datetime.date | None,
) -> ToolContext:
    """Snapshot one lead's tool-visible state, sanitizing every free-text field."""
    raise NotImplementedError("agent_tools component owns build_tool_context")


def execute_tool(name: str, arguments: Mapping[str, Any], context: ToolContext) -> str:
    """Run one tool as a pure function over ``context`` and render the result."""
    raise NotImplementedError("agent_tools component owns execute_tool")
