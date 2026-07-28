#!/usr/bin/env python3
"""LLM-judge copy quality eval runner + regression gate (MUS-21).

Sibling of ``run_rules_eval.py``. Where that harness scores the *classifier*,
this one scores the *generated copy*: it runs the Inspect task in
``evals/copy_eval.py`` for **one configured provider**, aggregates deterministic
checks + LLM-judge scores, writes a per-provider result artifact, and fails
(exit 1) if quality regressed below the committed baseline.

Key properties (see evals/README.md and the plan):

* **LLM-agnostic, single provider per run.** Generation and judging both flow
  through the repo's database-backed provider layer. Nothing is hardcoded, and a
  run only ever calls the provider you configured (active, or ``--provider``).
* **The comparison table is assembled from separate runs.** Run once per
  provider; ``--table`` renders the Markdown table from whatever
  ``evals/results/copy-*.json`` artifacts exist.

Usage::

    python evals/run_copy_eval.py                       # gate active provider vs baseline
    python evals/run_copy_eval.py --provider claude     # gate a specific configured provider
    python evals/run_copy_eval.py --update-baseline     # (re)write this provider's baseline
    python evals/run_copy_eval.py --limit 6             # quick smoke test (fewer leads)
    python evals/run_copy_eval.py --table               # print the README comparison table

Requires the provider's API key in the environment (e.g. GROQ_API_KEY); a
provider whose key is missing fails fast with a clear message.
"""

import argparse
import datetime
import json
import statistics
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from inspect_ai import eval as inspect_eval  # noqa: E402

from evals import copy_checks  # noqa: E402
from evals.copy_eval import active_provider, copy_quality_task, resolve_judge_provider  # noqa: E402

EVALS_DIR = REPO_ROOT / "evals"
BASELINE_PATH = EVALS_DIR / "baselines" / "copy.json"
RESULTS_DIR = EVALS_DIR / "results"
PRICING_PATH = EVALS_DIR / "pricing.toml"
LOG_DIR = EVALS_DIR / ".inspect-logs"
TODAY = "2026-06-12"  # frozen date the golden leads are relative to (see run_rules_eval)

# Regression tolerances. Generation is non-deterministic (every run produces
# different emails), so exact-match gating would flap -- these bands absorb
# run-to-run noise while still catching a real drop.
DET_TOLERANCE = 0.10  # deterministic pass-rate points (0-1)
JUDGE_TOLERANCE = 0.5  # judge score points (1-5 scale)
MIN_VALID_FRACTION = 0.8  # need this fraction of samples to actually produce copy
ROUND = 4

DET_SCORER = "deterministic_scorer"
JUDGE_SCORER = "judge_scorer"


# ---------------------------------------------------------------------------
# pricing (reference metadata only)
# ---------------------------------------------------------------------------


def _load_pricing():
    if not PRICING_PATH.exists():
        return {}
    with open(PRICING_PATH, "rb") as fh:
        return tomllib.load(fh).get("models", {})


# ---------------------------------------------------------------------------
# run one provider through Inspect and aggregate
# ---------------------------------------------------------------------------


def _find_score(sample, name):
    scores = getattr(sample, "scores", None) or {}
    return scores.get(name)


def run_provider(provider, judge_provider, golden_path, limit, max_samples):
    """Run the Inspect copy task for one provider and return aggregates."""
    task = copy_quality_task(
        provider=provider, judge_provider=judge_provider, golden_path=golden_path
    )
    logs = inspect_eval(
        task,
        model="mockllm/model",  # Inspect's own model is unused; real calls go via repo layer
        limit=limit,
        max_samples=max_samples,
        log_dir=str(LOG_DIR),
        display="plain",
        log_level="warning",
        fail_on_error=False,  # a bad sample shouldn't sink the run; we count them
    )
    log = logs[0]
    if log.status == "error" or log.samples is None:
        err = getattr(log, "error", None)
        raise SystemExit(f"Inspect eval failed for provider '{provider}': {err}")
    return aggregate(log, provider, judge_provider)


def aggregate(log, provider, judge_provider):
    samples = log.samples or []
    n = len(samples)

    det_checks = {name: [] for name in copy_checks.CHECK_NAMES}
    det_overall = []
    judge_dims = {dim: [] for dim in ("concrete_facts", "tone", "cta_match")}
    judge_overall = []
    latencies = []
    in_tokens = []
    out_tokens = []
    model = None
    gen_errors = 0
    judge_parse_failures = 0

    for sample in samples:
        det = _find_score(sample, DET_SCORER)
        if det is None:
            gen_errors += 1  # solver errored -> no scores for this sample
            continue
        meta = det.metadata or {}
        checks = meta.get("checks", {})
        for name in copy_checks.CHECK_NAMES:
            if name in checks:
                det_checks[name].append(1.0 if checks[name] else 0.0)
        if det.value is not None:
            det_overall.append(float(det.value))
        if meta.get("latency_s") is not None:
            latencies.append(float(meta["latency_s"]))
        if meta.get("est_input_tokens") is not None:
            in_tokens.append(int(meta["est_input_tokens"]))
        if meta.get("est_output_tokens") is not None:
            out_tokens.append(int(meta["est_output_tokens"]))
        model = model or meta.get("model")

        judge = _find_score(sample, JUDGE_SCORER)
        jmeta = (judge.metadata or {}) if judge else {}
        if judge is None or not jmeta.get("parse_ok"):
            judge_parse_failures += 1
            continue
        dims = jmeta.get("dims", {})
        for dim in judge_dims:
            if dim in dims:
                judge_dims[dim].append(float(dims[dim]))
        if dims:
            judge_overall.append(sum(dims.values()) / len(dims))

    def _mean(xs):
        return round(statistics.mean(xs), ROUND) if xs else None

    n_valid = n - gen_errors
    pricing = _load_pricing().get(model or "", {})
    in_rate = pricing.get("input_per_1m")
    out_rate = pricing.get("output_per_1m")
    cost = _estimate_cost(in_tokens, out_tokens, in_rate, out_rate, n_valid)

    return {
        "provider": provider,
        "judge_provider": judge_provider,
        "model": model,
        "generated_at": datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "today": TODAY,
        "n": n,
        "n_valid": n_valid,
        "n_generation_errors": gen_errors,
        "n_judge_parse_failures": judge_parse_failures,
        "deterministic": {
            **{name: _mean(det_checks[name]) for name in copy_checks.CHECK_NAMES},
            "overall": _mean(det_overall),
        },
        "judge": {
            **{dim: _mean(judge_dims[dim]) for dim in judge_dims},
            "overall": _mean(judge_overall),
        },
        "latency_s": {
            "median": round(statistics.median(latencies), ROUND) if latencies else None,
            "p90": _percentile(latencies, 90),
            "mean": _mean(latencies),
        },
        "cost_est_usd": cost,
    }


def _estimate_cost(in_tokens, out_tokens, in_rate, out_rate, n_valid):
    if in_rate is None or out_rate is None:
        return {
            "per_email": None,
            "total": None,
            "input_per_1m": in_rate,
            "output_per_1m": out_rate,
        }
    total = sum(in_tokens) / 1e6 * in_rate + sum(out_tokens) / 1e6 * out_rate
    return {
        "per_email": round(total / n_valid, 6) if n_valid else None,
        "total": round(total, 6),
        "input_per_1m": in_rate,
        "output_per_1m": out_rate,
    }


def _percentile(xs, pct):
    if not xs:
        return None
    ordered = sorted(xs)
    k = max(0, min(len(ordered) - 1, int(round((pct / 100) * (len(ordered) - 1)))))
    return round(ordered[k], ROUND)


# ---------------------------------------------------------------------------
# result artifacts + baseline
# ---------------------------------------------------------------------------


def write_result_artifact(results):
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"copy-{results['provider']}.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)
        fh.write("\n")
    return path


def load_baseline(path=BASELINE_PATH):
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _quality_section(results):
    """The gated subset of a run's aggregates (quality only; no latency/cost)."""
    return {
        "model": results["model"],
        "n": results["n"],
        "deterministic": results["deterministic"],
        "judge": results["judge"],
    }


def update_baseline(results, path=BASELINE_PATH):
    baseline = load_baseline(path) or {
        "_comment": (
            "Baseline for evals/run_copy_eval.py (MUS-21), keyed by provider. Quality "
            "only -- latency/cost live in evals/results/ and are not gated. LLM output is "
            "non-deterministic, so the gate uses tolerance bands (see run_copy_eval.py). "
            "Regenerate a provider with `python evals/run_copy_eval.py --provider <p> "
            "--update-baseline` after an intended change."
        ),
        "providers": {},
    }
    baseline["generated_at"] = results["generated_at"]
    baseline["today"] = results["today"]
    baseline.setdefault("providers", {})[results["provider"]] = _quality_section(results)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(baseline, fh, indent=2)
        fh.write("\n")


def check_gate(results, baseline):
    """Return a list of failure strings (empty = PASS)."""
    failures = []
    provider = results["provider"]

    valid_fraction = (results["n_valid"] / results["n"]) if results["n"] else 0.0
    if valid_fraction < MIN_VALID_FRACTION:
        failures.append(
            f"only {results['n_valid']}/{results['n']} samples produced copy "
            f"(< {MIN_VALID_FRACTION:.0%}); too many generation errors to measure"
        )

    prov_base = (baseline or {}).get("providers", {}).get(provider)
    if prov_base is None:
        failures.append(
            f"no baseline for provider '{provider}'. Run with --update-baseline to record one."
        )
        return failures

    for name in list(copy_checks.CHECK_NAMES) + ["overall"]:
        base = prov_base.get("deterministic", {}).get(name)
        cur = results["deterministic"].get(name)
        if base is None or cur is None:
            continue
        if cur < base - DET_TOLERANCE:
            failures.append(
                f"deterministic.{name}: {cur:.3f} < baseline {base:.3f} - {DET_TOLERANCE}"
            )

    for name in ("concrete_facts", "tone", "cta_match", "overall"):
        base = prov_base.get("judge", {}).get(name)
        cur = results["judge"].get(name)
        if base is None or cur is None:
            continue
        if cur < base - JUDGE_TOLERANCE:
            failures.append(f"judge.{name}: {cur:.3f} < baseline {base:.3f} - {JUDGE_TOLERANCE}")
    return failures


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


def _fmt(v, spec=".3f"):
    return "  n/a" if v is None else format(v, spec)


def print_report(results):
    p = results
    print()
    print(f"Copy quality eval  —  provider: {p['provider']}  (judge: {p['judge_provider']})")
    print(f"model: {p['model']}   golden: {p['n']} leads   today={p['today']}")
    print("=" * 74)
    print(
        f"valid: {p['n_valid']}/{p['n']}   "
        f"generation errors: {p['n_generation_errors']}   "
        f"judge parse failures: {p['n_judge_parse_failures']}"
    )

    print("\nDeterministic checks (pass rate)")
    print("-" * 40)
    for name in copy_checks.CHECK_NAMES:
        print(f"  {name:<14}{_fmt(p['deterministic'][name])}")
    print(f"  {'overall':<14}{_fmt(p['deterministic']['overall'])}")

    print("\nLLM-judge scores (mean, 1-5)")
    print("-" * 40)
    for dim in ("concrete_facts", "tone", "cta_match"):
        print(f"  {dim:<14}{_fmt(p['judge'][dim], '.2f')}")
    print(f"  {'overall':<14}{_fmt(p['judge']['overall'], '.2f')}")

    lat = p["latency_s"]
    cost = p["cost_est_usd"]
    print("\nLatency / cost")
    print("-" * 40)
    print(f"  latency median : {_fmt(lat['median'], '.2f')}s   p90 {_fmt(lat['p90'], '.2f')}s")
    if cost["per_email"] is None:
        print("  est. cost      : n/a (no pricing for this model in evals/pricing.toml)")
    else:
        print(f"  est. cost      : ${cost['per_email']:.6f}/email  (${cost['total']:.4f} total)")


# ---------------------------------------------------------------------------
# comparison table (from result artifacts)
# ---------------------------------------------------------------------------


def render_table():
    artifacts = sorted(RESULTS_DIR.glob("copy-*.json")) if RESULTS_DIR.exists() else []
    if not artifacts:
        return "_No copy-eval results yet. Run `python evals/run_copy_eval.py` first._"
    rows = [json.loads(a.read_text(encoding="utf-8")) for a in artifacts]

    header = (
        "| Provider | Model | Judge (1-5) | facts | tone | CTA | Checks | est. $/email | Latency (med) |\n"
        "|---|---|--:|--:|--:|--:|--:|--:|--:|"
    )
    lines = [header]
    for r in rows:
        j, d, c, lat = r["judge"], r["deterministic"], r["cost_est_usd"], r["latency_s"]
        cost = (
            "$0"
            if c["per_email"] == 0
            else (f"${c['per_email']:.5f}" if c["per_email"] is not None else "n/a")
        )
        lines.append(
            f"| {r['provider']} | `{r['model']}` | "
            f"{_tbl(j['overall'], '.2f')} | {_tbl(j['concrete_facts'], '.1f')} | "
            f"{_tbl(j['tone'], '.1f')} | {_tbl(j['cta_match'], '.1f')} | "
            f"{_tbl_pct(d['overall'])} | {cost} | "
            f"{_tbl(lat['median'], '.2f')}s |"
        )
    return "\n".join(lines)


def _tbl(v, spec):
    return "n/a" if v is None else format(v, spec)


def _tbl_pct(v):
    return "n/a" if v is None else f"{v * 100:.0f}%"


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(description="LLM-judge copy quality eval + gate.")
    parser.add_argument(
        "--provider", help="Generation provider (default: the active database-configured one)."
    )
    parser.add_argument(
        "--judge-provider", help="Judge provider (default: [llm.judge] or generation)."
    )
    parser.add_argument(
        "--golden", help="Path to a golden JSONL set (default: evals/golden/leads.jsonl)."
    )
    parser.add_argument("--baseline", type=Path, default=BASELINE_PATH, help="Baseline JSON path.")
    parser.add_argument(
        "--limit", type=int, default=None, help="Only run the first N leads (smoke test)."
    )
    parser.add_argument(
        "--max-samples", type=int, default=4, help="Concurrent samples (rate-limit control)."
    )
    parser.add_argument(
        "--update-baseline", action="store_true", help="(Re)write this provider's baseline, exit 0."
    )
    parser.add_argument(
        "--table", action="store_true", help="Print the README comparison table and exit."
    )
    args = parser.parse_args(argv)

    if args.table:
        print(render_table())
        return 0

    baseline_path = args.baseline
    gen_provider = args.provider or active_provider()
    judge_provider = args.judge_provider or resolve_judge_provider(gen_provider)

    results = run_provider(
        provider=gen_provider,
        judge_provider=judge_provider,
        golden_path=args.golden,
        limit=args.limit,
        max_samples=args.max_samples,
    )
    artifact = write_result_artifact(results)
    print_report(results)
    print(f"\nResult artifact: {artifact}")

    if args.update_baseline:
        update_baseline(results, baseline_path)
        print(f"Baseline updated for provider '{gen_provider}' at {baseline_path}")
        return 0

    baseline = load_baseline(baseline_path)
    failures = check_gate(results, baseline)
    print()
    if failures:
        print(f"Gate: FAIL — {len(failures)} regression(s) for provider '{gen_provider}':")
        for f in failures:
            print(f"  - {f}")
        print("If this change is intentional, rerun with --update-baseline.")
        return 1
    print(
        f"Gate: PASS — provider '{gen_provider}' met baseline (tolerances: "
        f"det ±{DET_TOLERANCE}, judge ±{JUDGE_TOLERANCE})."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
