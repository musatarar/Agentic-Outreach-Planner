"""The inference pass -- a stub: it asks no model and matches no rule.

The pass around it is real: candidates arrive gated, the result is tallied with
the deterministic matches, and a match here reaches ``inferred_action_chosen``.
Only the provider call is missing, so a job that gets here today ends at
``no_action`` carrying :data:`TODO`.
"""

from dataclasses import dataclass

TODO = (
    "TODO: implement the inference pass. Build the one-prompt evaluation over "
    "`OutreachRule.build_inference_prompt` (labels assigned per call and mapped "
    "back server-side), run it through the LLM seam on sanitized, fenced lead "
    "data, and return the rules the model affirmed."
)


@dataclass(frozen=True, slots=True)
class InferenceResult:
    """The rules the pass affirmed, out of the candidates it was given."""

    matched: tuple
    candidates: tuple
    todo: str = ""


def infer(candidates, lead, today):
    """Ask the model which candidates hold. Stubbed: nothing is asked or matched."""
    return InferenceResult(matched=(), candidates=tuple(candidates), todo=TODO)
