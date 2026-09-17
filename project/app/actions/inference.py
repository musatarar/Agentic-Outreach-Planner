"""The inference pass -- a stub: it asks no model, so no candidate gets a verdict.

The pass around it is real: candidates arrive gated, the section returned here
is tallied with the deterministic matches, and a holding verdict reaches
``inferred_action_chosen``. Only the provider call is missing, so today every
candidate lands in ``unevaluable_rule_ids`` -- never asked is a different
answer from asked and refused.
"""

TODO = (
    "TODO: implement the inference pass. One structured-output call per lead "
    "(`LLMClient.generate_structured`): a prefix of the candidates' predicates, "
    "stable across leads, then the lead's record and its sanitized untrusted "
    "block; return one verdict per candidate, mapped back by rule pk."
)


def infer(candidates, lead, today):
    """One lead's inference-rule verdicts, as the decision payload's inference
    section: the candidates that hold, their verdicts, and the ones no usable
    verdict came back for. Stubbed -- nothing is asked, so nothing holds and
    nothing is evaluable."""
    candidates = list(candidates)
    return {
        "rules_evaluated": len(candidates),
        "matched_rule_ids": [],
        "matched_rules": [],
        "verdicts": [],
        "unevaluable_rule_ids": sorted(rule.pk for rule in candidates),
        "todo": TODO,
    }
