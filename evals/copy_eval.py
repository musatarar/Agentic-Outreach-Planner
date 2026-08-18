#!/usr/bin/env python3
"""Inspect task for the outreach-copy quality eval (MUS-21).

Scores each generated email with cheap deterministic checks first, then an LLM
judge (three dimensions, 1-5). Inspect is scaffolding only: all real LLM traffic
flows through the repo's provider-agnostic layer, one configured provider per run.

Run via ``evals/run_copy_eval.py`` (adds baselines + the gate) or standalone:
``inspect eval evals/copy_eval.py -T provider=groq``.
"""

import json
import re
import sys
from pathlib import Path

# Make ``project.*`` and ``evals.*`` importable no matter where this is run from.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from inspect_ai import Task, task  # noqa: E402
from inspect_ai.dataset import MemoryDataset, Sample  # noqa: E402
from inspect_ai.model import ModelOutput  # noqa: E402
from inspect_ai.scorer import Score, Target, mean, scorer  # noqa: E402
from inspect_ai.solver import Generate, TaskState, solver  # noqa: E402

from evals import copy_checks  # noqa: E402
from evals.run_rules_eval import GOLDEN_PATH, build_lead, load_golden  # noqa: E402
from project.app.services import actions  # noqa: E402
from project.app.services.llm import build_client, retry  # noqa: E402
from project.app.services.llm import config as llm_config  # noqa: E402
from project.app.services.outreach import MAX_COPY_TOKENS, _build_copy_prompt  # noqa: E402
from project.app.services.telemetry import genai as telemetry_genai  # noqa: E402
from project.app.services.telemetry import setup as telemetry_setup  # noqa: E402

RUBRIC_PATH = REPO_ROOT / "evals" / "rubrics" / "copy.md"
JUDGE_MAX_TOKENS = 800
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
JUDGE_DIMENSIONS = ("concrete_facts", "tone", "cta_match")

# Fallback: regex-extract "dim": N (flat or nested) from malformed judge JSON
# rather than dropping the sample.
_DIM_SCORE_RE = {
    dim: re.compile(rf'"{dim}"\s*:\s*(?:\{{\s*"?score"?\s*:\s*)?([1-5])', re.IGNORECASE)
    for dim in JUDGE_DIMENSIONS
}


# ---------------------------------------------------------------------------
# provider resolution (agnostic; nothing hardcoded)
# ---------------------------------------------------------------------------


def active_provider():
    """The currently active, database-configured generation provider."""
    return llm_config.get_provider()


def resolve_judge_provider(gen_provider):
    """Judge provider fallback: config ``[llm.judge]``, else the generation
    provider -- a run never calls an unconfigured provider."""
    judge_cfg = llm_config.get_provider_config("judge")
    return judge_cfg.get("provider") or gen_provider


def _estimate_tokens(text):
    """Rough token estimate (~chars/4); the provider interface returns text only."""
    return max(1, len(text) // 4)


# Six attempts vs llm/retry.py's default four: an unattended eval run can
# afford to wait out a free-tier throttle where the planner cannot.
_EVAL_RETRY_POLICY = retry.RetryPolicy(max_attempts=6, initial_backoff_s=2.0, max_backoff_s=65.0)


def _attempt_scope(client, max_tokens):
    """A traced attempt scope for this eval's provider calls (MUS-25).

    ``configure_from_env()`` runs here because this harness never calls
    ``django.setup()``; it is idempotent and a no-op without an OTLP endpoint.
    """
    telemetry_setup.configure_from_env()
    return telemetry_genai.provider_call_scope(
        telemetry_genai.ProviderCall.from_client(client, max_tokens)
    )


async def _generate_with_retry(client, prompt, max_tokens):
    """Call the provider natively async, retrying only retryable failures.

    The returned ``LLMResult.latency_s`` times the successful API call alone --
    back-off sleeps are excluded, so it reflects the model, not rate limiting.
    """
    return await retry.acall_with_retry(
        lambda: client.agenerate(prompt, max_tokens=max_tokens),
        policy=_EVAL_RETRY_POLICY,
        # One CLIENT span per HTTP attempt (MUS-25); a no-op unless an OTLP
        # endpoint is configured.
        attempt_scope=_attempt_scope(client, max_tokens),
    )


# ---------------------------------------------------------------------------
# dataset
# ---------------------------------------------------------------------------


def build_dataset(golden_path=None):
    """Golden leads -> Inspect Samples, reusing the MUS-20 loader.

    Drops ``unknown`` leads (no copy is generated for those); each Sample's input
    is the exact production prompt, so copy is judged independently of rules bugs.
    """
    path = Path(golden_path) if golden_path else GOLDEN_PATH
    records = load_golden(path)
    samples = []
    for rec in records:
        action_type = rec["expected_action"]
        if action_type not in actions.SELECTABLE_ACTION_TYPES:
            continue  # e.g. "unknown" -> routed to a human, no copy
        lead = build_lead(rec)
        reason = rec.get("rationale", "") or ""
        prompt = _build_copy_prompt(lead, action_type, reason)
        meta = actions.ACTION_META.get(action_type, {})
        samples.append(
            Sample(
                input=prompt,
                id=rec.get("id"),
                metadata={
                    "action_type": action_type,
                    "action_label": meta.get("label", action_type),
                    "urgency": meta.get("urgency", "medium"),
                },
            )
        )
    if not samples:
        raise SystemExit(f"No selectable-action leads in golden set: {path}")
    return MemoryDataset(samples)


# ---------------------------------------------------------------------------
# solver: generate copy through the repo's provider layer
# ---------------------------------------------------------------------------


@solver
def generate_copy_solver(provider):
    """Generate the email via ``build_client(provider).complete(...)`` and stash
    latency + token estimates + model id in the sample store."""
    # Inspect gives a solver no teardown hook; the client's pooled transport is
    # released at process exit.
    client = build_client(provider)

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        prompt = state.input_text
        result = await _generate_with_retry(client, prompt, MAX_COPY_TOKENS)

        state.output = ModelOutput.from_content(model=provider, content=result.text)
        # None (not measured) stays distinct from 0.0 in the report.
        state.store.set(
            "latency_s", None if result.latency_s is None else round(result.latency_s, 4)
        )
        # The committed baselines were recorded against the estimate; switching
        # to the provider's real token counts is a follow-up.
        state.store.set("est_input_tokens", _estimate_tokens(prompt))
        state.store.set("est_output_tokens", _estimate_tokens(result.text))
        state.store.set("model", getattr(client, "model", provider))
        return state

    return solve


# ---------------------------------------------------------------------------
# scorer 1: deterministic checks (free, no LLM)
# ---------------------------------------------------------------------------


@scorer(metrics=[mean()])
def deterministic_scorer():
    """Score = fraction of the four structural checks that passed. The per-check
    booleans, length/CTA detail, and generation telemetry ride in metadata."""

    async def score(state: TaskState, target: Target) -> Score:
        email = state.output.completion or ""
        result = copy_checks.run_all(email)
        checks = {name: bool(result[name]) for name in copy_checks.CHECK_NAMES}
        passed = sum(checks.values())
        return Score(
            value=passed / len(copy_checks.CHECK_NAMES),
            answer=f"{passed}/{len(copy_checks.CHECK_NAMES)} checks",
            metadata={
                "checks": checks,
                "word_count": result["detail_word_count"],
                "cta_count": result["detail_cta_count"],
                "latency_s": state.store.get("latency_s"),
                "est_input_tokens": state.store.get("est_input_tokens"),
                "est_output_tokens": state.store.get("est_output_tokens"),
                "model": state.store.get("model"),
            },
        )

    return score


# ---------------------------------------------------------------------------
# scorer 2: LLM judge (through the agnostic layer; rubric from a file)
# ---------------------------------------------------------------------------


def _load_rubric():
    return RUBRIC_PATH.read_text(encoding="utf-8")


def _build_judge_prompt(rubric, state: TaskState, email):
    meta = state.metadata or {}
    return (
        f"{rubric}\n\n"
        f"---\n"
        f"PLANNED ACTION: {meta.get('action_type')} "
        f"({meta.get('action_label')}, urgency: {meta.get('urgency')})\n\n"
        f"CONTEXT THE WRITER WAS GIVEN (lead details + planned action):\n"
        f"<context>\n{state.input_text}\n</context>\n\n"
        f"EMAIL TO GRADE:\n<email>\n{email}\n</email>\n\n"
        f"Score the email per the rubric. Return only the JSON object."
    )


def _score_from_json(data):
    """Extract 1-5 scores from a parsed dict (flat ``4`` or nested
    ``{"score": 4}``). Returns ``(dims, notes)`` or ``None`` if incomplete."""
    dims = {}
    for key in JUDGE_DIMENSIONS:
        v = data.get(key)
        if isinstance(v, dict):
            v = v.get("score")
        try:
            score = int(round(float(v)))
        except (TypeError, ValueError):
            return None
        if not 1 <= score <= 5:
            return None
        dims[key] = score
    return dims, str(data.get("notes", ""))[:300]


def _parse_judge(raw):
    """Parse the judge output into ``(dims, notes)``, falling back to regex
    extraction when the JSON is malformed. Raises only when no score can be
    recovered for a dimension."""
    text = raw.strip()
    candidate = text
    m = _JSON_RE.search(text)
    if m:
        candidate = m.group(0)
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        parsed = _score_from_json(data)
        if parsed is not None:
            return parsed

    # Fallback: recover scores from malformed / non-JSON text.
    dims = {}
    for key in JUDGE_DIMENSIONS:
        hit = _DIM_SCORE_RE[key].search(text)
        if not hit:
            raise ValueError(f"could not extract score for '{key}'")
        dims[key] = int(hit.group(1))
    return dims, ""


@scorer(metrics=[mean()])
def judge_scorer(judge_provider):
    """Grade the email 1-5 on three dimensions using the configured judge
    provider. Score value = mean of the three dims (None -> 0.0 on failure);
    the per-dimension scores + parse_ok ride in metadata."""
    client = build_client(judge_provider)
    rubric = _load_rubric()

    async def score(state: TaskState, target: Target) -> Score:
        email = state.output.completion or ""
        prompt = _build_judge_prompt(rubric, state, email)
        try:
            raw = (await _generate_with_retry(client, prompt, JUDGE_MAX_TOKENS)).text
        except Exception as exc:  # noqa: BLE001 -- record, don't crash the run
            return Score(
                value=0.0,
                explanation=f"judge call failed: {exc}",
                metadata={"parse_ok": False, "error": str(exc), "judge_model": client.model},
            )
        try:
            dims, notes = _parse_judge(raw)
        except Exception as exc:  # noqa: BLE001 -- unparseable judge output
            return Score(
                value=0.0,
                explanation=f"unparseable judge output: {exc}",
                metadata={"parse_ok": False, "raw": raw[:800], "judge_model": client.model},
            )
        judge_mean = sum(dims.values()) / len(dims)
        return Score(
            value=round(judge_mean, 4),
            answer=json.dumps(dims),
            explanation=notes or json.dumps(dims),
            metadata={
                "parse_ok": True,
                "dims": dims,
                "notes": notes,
                "judge_model": client.model,
            },
        )

    return score


# ---------------------------------------------------------------------------
# task
# ---------------------------------------------------------------------------


@task
def copy_quality_task(provider=None, judge_provider=None, golden_path=None):
    """Copy-quality eval for one configured provider.

    provider       -- generation provider (default: active provider in config).
    judge_provider -- judge provider (default: config [llm.judge] or generation).
    """
    gen_provider = provider or active_provider()
    judge = judge_provider or resolve_judge_provider(gen_provider)
    return Task(
        dataset=build_dataset(golden_path),
        solver=[generate_copy_solver(gen_provider)],
        scorer=[deterministic_scorer(), judge_scorer(judge)],
        # Inspect requires a task model but nothing calls it; mock keeps Inspect key-free.
        model="mockllm/model",
        metadata={"gen_provider": gen_provider, "judge_provider": judge},
    )
