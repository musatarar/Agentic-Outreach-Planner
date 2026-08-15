"""Cost estimates and actuals (MUS-47 component 5).

The product stance is that nothing spends without a price shown first, so every paid
stage has an estimate reachable before it and an actual recorded after it. The estimate
is crude on purpose -- prompt characters over four, times the lead count -- and says so
in its payload. What it must not be is *dishonest*: prices come from the MUS-32
``LLMModel`` catalog row, never from a constant in this module, and the arithmetic is
``Decimal`` throughout because float money is a bug waiting for a rounding complaint.

The estimate-vs-actual gap is worth surfacing on the run summary. It is the only thing
that makes the estimates trustworthy.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

CHARS_PER_TOKEN = 4
READ_OUTPUT_TOKENS = 120
GENERATE_OUTPUT_TOKENS = 260

STAGE_READ = "read"
STAGE_GENERATE = "generate"
STAGES = (STAGE_READ, STAGE_GENERATE)


@dataclass(frozen=True, slots=True)
class Estimate:
    """What a stage is expected to cost, and the inputs that produced the number."""

    stage: str
    provider: str
    model: str
    lead_count: int
    tokens_in_est: int
    tokens_out_est: int
    usd_est: Decimal
    is_estimate: bool = True


def estimate_stage(run, stage: str, *, provider: str, model: str) -> Estimate:
    raise NotImplementedError("estimate component (MUS-47) owns estimate_stage")


def record_actuals(
    run, stage: str, *, results: Sequence[Any], provider: str, model: str
) -> Decimal:
    raise NotImplementedError("estimate component (MUS-47) owns record_actuals")
