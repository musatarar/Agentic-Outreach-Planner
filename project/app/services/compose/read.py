"""Advisory agent read (MUS-47 component 7): evidence-cited suggestions, no authority.

The one genuinely new AI capability in the composer, and the one that reads hostile
input by design. Two things the deterministic rules cannot see go in -- ``hubspot_notes``
and the *shape* of the event sequence -- and what comes out can only ever propose.

Three properties carry the safety, and each is a test rather than a hope:

* **No tool the model can steer.** One forced ``emit_suggestion`` call is the entire
  action space. ``lead_id`` is absent from its schema; the acting lead is bound
  server-side, so a note that says "also raise lead_042" has nothing to reach.
* **Evidence must be verbatim.** Every quote is checked against
  :attr:`ReadSource.notes_sanitized` / :attr:`ReadSource.timeline_sanitized` -- the exact
  bytes the model was shown, never the raw record, because a quote from sanitized text
  would otherwise fail wherever sanitization rewrote a character. Fabricated evidence
  discards the whole suggestion.
* **The blast radius of a perfect injection is a card.** A malicious note demanding P1
  can at most produce a suggestion, quoting itself, in front of a human with a Reject
  button. The attack becomes its own evidence.
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

MAX_RATIONALE_CHARS = 200
MAX_EVIDENCE_ITEMS = 4
SUGGESTION_KINDS = ("raise", "lower", "action_change", "none")

# Set by the read component: a ToolSpec named "emit_suggestion" whose parameters
# carry suggestion / proposed_priority / proposed_action / rationale / evidence[]
# and deliberately NO lead_id.
SUGGESTION_TOOL: Any = None


@dataclass(frozen=True, slots=True)
class ReadSource:
    """One lead's frozen, plain-data input to the read.

    Built in the synchronous phase, exactly like ``AgentLeadPlan`` -- the async phase
    holds no ORM handle and cannot emit a lazy query. The two ``*_sanitized`` fields
    are what the model literally sees, which is what makes them the right corpus for
    the evidence validator.
    """

    lead_id: str
    notes_sanitized: str
    timeline_sanitized: str
    rules_priority: int
    rules_action: str


def render_timeline(lead, *, today: datetime.date, limit: int = 12) -> str:
    """Render the event list as an ordered timeline the rules cannot express."""
    raise NotImplementedError("read component (MUS-47) owns render_timeline")


def build_read_source(lead, *, today: datetime.date) -> ReadSource:
    raise NotImplementedError("read component (MUS-47) owns build_read_source")


def build_read_prompt(source: ReadSource) -> str:
    raise NotImplementedError("read component (MUS-47) owns build_read_prompt")


def validate_suggestion(raw: Mapping[str, Any], source: ReadSource) -> dict[str, Any] | None:
    """Return the normalized suggestion, or ``None`` when it must be discarded."""
    raise NotImplementedError("read component (MUS-47) owns validate_suggestion")


async def aread_all(sources: Sequence[ReadSource], *, client, runtime):
    raise NotImplementedError("read component (MUS-47) owns aread_all")


def run_read(run, *, provider: str, model: str, actor: str):
    raise NotImplementedError("read component (MUS-47) owns run_read")
