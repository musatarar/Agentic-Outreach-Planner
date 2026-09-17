"""The inference pass -- a stub today: it asks no model, so nothing gets a verdict.

The pass around it is real: candidates arrive gated, the section returned here
is tallied with the deterministic matches, and a holding verdict reaches
``inferred_action_chosen``. Only the provider call is missing, and a dry run
skips it on purpose -- either way every candidate lands in
``unevaluable_rule_ids``, because never asked is a different answer from asked
and refused.
"""

TODO = (
    "TODO: implement the inference pass. One structured-output call per lead "
    "(`LLMClient.generate_structured`): a prefix of the candidates' predicates, "
    "stable across leads, then the lead's record and its sanitized untrusted "
    "block; return one verdict per candidate, mapped back by rule pk."
)

DRY_RUN = "ACTIONS_LLM_DRY_RUN is set: this run made no provider call."


def not_asked(candidates, reason):
    """The inference section for a pass that asked nothing.

    No verdict came back for any candidate, so each one is unevaluable rather
    than a non-match, and ``reason`` records which silence this was.
    """
    candidates = list(candidates)
    return {
        "rules_evaluated": len(candidates),
        "matched_rule_ids": [],
        "matched_rules": [],
        "verdicts": [],
        "unevaluable_rule_ids": sorted(rule.pk for rule in candidates),
        "reason": reason,
    }


def infer(candidates, lead, today):
    """One lead's inference-rule verdicts, as the decision payload's inference
    section. Stubbed: nothing is asked, so nothing holds."""
    return not_asked(candidates, TODO)
