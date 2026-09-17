"""The decision payload a rules run produces, and the reply shape its inference half asks for."""

from pydantic import BaseModel

# Keys both engines report and the payload carries once, at the top level.
SHARED_KEYS = ("rules_evaluated", "unevaluable_rule_ids")


class Verdict(BaseModel):
    """One candidate rule's answer: whether its predicate holds, and on what words."""

    rule_id: int
    holds: bool
    # Nullable rather than defaulted: strict mode requires every property, so
    # "no quote" has to be a value the schema can express.
    evidence_quote: str | None


class InferenceVerdicts(BaseModel):
    """The whole structured reply: one verdict per candidate rule."""

    verdicts: list[Verdict]


def inference_section(rules_evaluated, matched, verdicts, unevaluable_rule_ids):
    """The inference half of the payload; ``verdicts`` are :class:`Verdict` instances."""
    return {
        "rules_evaluated": rules_evaluated,
        "matched_rule_ids": [rule.pk for rule in matched],
        "matched_rules": [rule.name for rule in matched],
        "verdicts": [verdict.model_dump() for verdict in verdicts],
        "unevaluable_rule_ids": list(unevaluable_rule_ids),
    }


def decision(owner_id, rules_evaluated, deterministic=None, inference=None):
    """One lead's decision: each engine's section nested, the keys they share lifted out."""
    sections = {"deterministic": deterministic or {}, "inference": inference or {}}
    unevaluable = {
        rule_id
        for section in sections.values()
        for rule_id in section.get("unevaluable_rule_ids", ())
    }
    return {
        "owner_id": owner_id,
        "rules_evaluated": rules_evaluated,
        **{
            name: {key: value for key, value in section.items() if key not in SHARED_KEYS}
            for name, section in sections.items()
        },
        "unevaluable_rule_ids": sorted(unevaluable),
    }
