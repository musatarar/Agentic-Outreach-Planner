"""The inference half of a rules run: one lead's candidate predicates, judged by the model."""

from project.app.rules import schema
from project.app.services import outreach
from project.app.services.llm import LLMMalformedResponseError, get_llm_client

# Lead-independent, so these bytes repeat exactly and a prefix cache can serve them.
_PREFIX_HEADER = """You are evaluating outreach rules for Locked In. Locked In sells Sure Lock — insurance premium protection for homeowners — through independent insurance agencies.

Below are numbered predicates about one insurance agency lead, each written by \
the Locked In user whose rules these are, in the form `<predicate> ? <rule id>`. \
Answer every one of them, and only them, with a verdict naming that rule id: \
`holds` is whether the predicate is true of the lead, and `evidence_quote` is \
the shortest span of the lead's own data that shows it — null when nothing in \
the data shows it.

Predicates:"""


def build_prefix(candidates):
    """The lead-independent head of the prompt: the task, then one line per candidate."""
    lines = "\n".join(rule.build_inference_prompt() for rule in candidates)
    return f"{_PREFIX_HEADER}\n{lines}\n"


def build_prompt(candidates, lead, today):
    """The prefix, then the lead — trusted record first, lead-authored text fenced after it."""
    return f"""{build_prefix(candidates)}
Today is {today}.

Trusted lead record (system fields — safe to rely on):
{outreach.build_trusted_block(lead)}

{outreach.UNTRUSTED_STANDING_INSTRUCTION}

{outreach.build_untrusted_block(lead)}"""


def infer(candidates, lead, today, *, client=None):
    """Judge each candidate inference rule against ``lead``, as the payload's inference section.

    One provider call, asking about every candidate at once. A candidate the
    reply answers unusably — no verdict, two verdicts, or a reply that does not
    fit the schema at all — is unevaluable rather than a non-match, so a model
    that answers half the question still gets the other half used.
    """
    candidates = sorted(candidates, key=lambda rule: rule.pk)
    if not candidates:
        return schema.inference_section(0, (), (), ())

    prompt = build_prompt(candidates, lead, today)
    client = client or get_llm_client()
    try:
        reply = client.generate_structured(prompt, schema.InferenceVerdicts).parsed
    except LLMMalformedResponseError:
        # Nothing was answered; other LLMErrors are the job's to retry, not a verdict.
        return schema.inference_section(len(candidates), (), (), [rule.pk for rule in candidates])
    return _section(candidates, reply)


def _section(candidates, reply):
    # Verdicts are looked up per candidate, so one naming any other id is dropped.
    answered, duplicated = {}, set()
    for verdict in reply.verdicts:
        if verdict.rule_id in answered:
            duplicated.add(verdict.rule_id)
            continue
        answered[verdict.rule_id] = verdict

    matched, verdicts, unevaluable = [], [], []
    for rule in candidates:
        verdict = answered.get(rule.pk)
        if verdict is None or rule.pk in duplicated:
            unevaluable.append(rule.pk)
            continue
        verdicts.append(verdict)
        if verdict.holds:
            matched.append(rule)
    return schema.inference_section(len(candidates), matched, verdicts, unevaluable)
