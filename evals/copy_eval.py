#!/usr/bin/env python3
"""Inspect task for the outreach-copy quality eval (MUS-21).

`generate_copy` (project/app/services/outreach.py) asks the model for six
properties -- Subject line, ~120-word body, concrete lead detail, helpful-peer
voice, exactly one CTA matching the planned action, no commentary -- and nothing
verifies any of them. This task does: it scores each generated email with cheap
deterministic checks first, then an LLM judge (three dimensions, 1-5).

Design constraints (see evals/README.md):

* **Inspect is scaffolding only.** All real LLM traffic -- both generation and
  judging -- flows through the repo's provider-agnostic layer
  (``get_llm_client`` / ``build_client``), never Inspect's own model providers.
  The Inspect task model is ``mockllm/model`` so the harness needs no key for
  Inspect itself.
* **LLM-agnostic.** No provider is hardcoded anywhere. Generation uses the
  provider selected in config.toml (or an explicit ``--provider``); the judge
  uses the same layer (config ``[llm.judge]`` or a fallback to the generation
  provider).
* **Only the configured provider runs.** One provider per invocation.

Run standalone via Inspect (``inspect eval evals/copy_eval.py -T provider=groq``)
or, preferably, via ``evals/run_copy_eval.py`` which adds baselines + the gate.
"""

import asyncio
import json
import re
import sys
import time
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
from project.app.services.llm import build_client  # noqa: E402
from project.app.services.llm import config as llm_config  # noqa: E402
from project.app.services.outreach import MAX_COPY_TOKENS, _build_copy_prompt  # noqa: E402

RUBRIC_PATH = REPO_ROOT / "evals" / "rubrics" / "copy.md"
JUDGE_MAX_TOKENS = 800
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
JUDGE_DIMENSIONS = ("concrete_facts", "tone", "cta_match")

# Fallback extractor: pull "dim": N (flat) or "dim": {"score": N} (nested) out of
# text even when the surrounding JSON is malformed -- weak judges (e.g. llama)
# occasionally mis-close a brace. Recovers the score rather than dropping the sample.
_DIM_SCORE_RE = {
    dim: re.compile(rf'"{dim}"\s*:\s*(?:\{{\s*"?score"?\s*:\s*)?([1-5])', re.IGNORECASE)
    for dim in JUDGE_DIMENSIONS
}


# ---------------------------------------------------------------------------
# provider resolution (agnostic; nothing hardcoded)
# ---------------------------------------------------------------------------


def active_provider():
    """The generation provider selected in config.toml."""
    return llm_config.get_provider()


def resolve_judge_provider(gen_provider):
    """Judge provider: config ``[llm.judge].provider`` if set, else the
    generation provider (so a run never calls an unconfigured provider)."""
    judge_cfg = llm_config.get_provider_config("judge")
    return judge_cfg.get("provider") or gen_provider


def _estimate_tokens(text):
    """Rough token estimate (~chars/4). The provider interface returns text
    only, so exact usage isn't available; this feeds the est. cost column."""
    return max(1, len(text) // 4)


def _retry_after_seconds(exc, default):
    """Honor a provider's ``Retry-After`` header when present (free tiers send a
    real wait on 429/token-per-minute throttle), else fall back to ``default``."""
    resp = getattr(exc, "response", None)
    header = resp.headers.get("retry-after") if resp is not None else None
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    return default


async def _complete_with_retry(client, prompt, max_tokens, attempts=6):
    """Call the sync client off the event loop, retrying transient failures
    (rate limits, blips). Respects ``Retry-After`` so full runs self-pace under
    free-tier per-minute limits instead of failing.

    Returns ``(text, call_seconds)`` where ``call_seconds`` times only the
    successful API call -- back-off/throttle sleeps are excluded so reported
    latency reflects the model, not the free tier's rate limiting.
    """
    last_exc = None
    for i in range(attempts):
        try:
            t0 = time.perf_counter()
            text = await asyncio.to_thread(client.complete, prompt, max_tokens=max_tokens)
            return text, time.perf_counter() - t0
        except Exception as exc:  # noqa: BLE001 -- provider errors vary; retry then surface
            last_exc = exc
            if i < attempts - 1:
                delay = _retry_after_seconds(exc, default=2.0 * (i + 1))
                await asyncio.sleep(min(delay, 65.0))
    raise last_exc


# ---------------------------------------------------------------------------
# dataset
# ---------------------------------------------------------------------------


def build_dataset(golden_path=None):
    """Golden leads -> Inspect Samples, reusing the MUS-20 loader.

    Drops ``unknown`` leads (no copy is generated for those). Each Sample's
    input is the exact production prompt; ground-truth ``expected_action`` is the
    planned action and ``rationale`` is the "why now" reason, so copy quality is
    judged independently of any rules bug.
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
    client = build_client(provider)

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        prompt = state.input_text
        text, latency = await _complete_with_retry(client, prompt, MAX_COPY_TOKENS)

        state.output = ModelOutput.from_content(model=provider, content=text)
        state.store.set("latency_s", round(latency, 4))
        state.store.set("est_input_tokens", _estimate_tokens(prompt))
        state.store.set("est_output_tokens", _estimate_tokens(text))
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
    """Parse the judge output into ``(dims, notes)``, robustly.

    Prefers clean JSON (flat ``{"tone": 5}`` or nested ``{"tone": {"score": 5}}``);
    if the JSON is malformed, falls back to regex-extracting each dimension's
    score. Raises only when no score can be recovered for a dimension.
    """
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
            raw, _latency = await _complete_with_retry(client, prompt, JUDGE_MAX_TOKENS)
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
        # Inspect requires a task model, but our solver/scorer never call it --
        # all real LLM traffic goes through the repo layer. Mock keeps Inspect
        # key-free.
        model="mockllm/model",
        metadata={"gen_provider": gen_provider, "judge_provider": judge},
    )
