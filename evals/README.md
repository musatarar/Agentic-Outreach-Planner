# evals/ — classifier evaluation harnesses

`determine_priority` and `determine_action` in
[`project/app/services/outreach.py`](../project/app/services/outreach.py) are the
actual product — the LLM only writes the outreach copy. They are tuned by
hand-picked threshold constants (`DORMANT_DAYS`, `POWER_USER_DEALS`,
`STALE_CONTACT_DAYS`, …). This directory measures whether that classification is
*correct*, not just that each branch executes.

## Rules regression suite (`run_rules_eval.py`)

Scores the classifier against a hand-labeled golden dataset of **ground-truth**
answers, prints a per-action-type score table + confusion matrix, and **fails
(exit 1) if any action type regresses below the committed baseline**.

```bash
python evals/run_rules_eval.py                    # score + gate vs baseline
python evals/run_rules_eval.py --update-baseline  # (re)write the baseline, exit 0
python evals/run_rules_eval.py --golden PATH      # score a different golden file
```

Pure Python — **no Django, no database, no network.** The classifier duck-types
on lead-like objects, so the harness feeds it `SimpleNamespace` stubs built from
`golden/leads.jsonl` (the same pattern as
[`project/app/tests_logic.py`](../project/app/tests_logic.py)) and the whole run
finishes in milliseconds. It only needs `anthropic` + `httpx` importable because
`outreach` imports the provider adapters at module load; nothing calls them.

### Files

| Path | What it is |
|------|------------|
| `golden/leads.jsonl` | 41 hand-labeled leads with the correct `(expected_action, expected_priority)`. |
| `baselines/rules.json` | Recorded per-action precision/recall/F1 the gate protects. |
| `run_rules_eval.py` | Loader + metrics + confusion matrix + regression gate. |

### Golden dataset format

One JSON object per line (blank lines and `//` comment lines are ignored):

```json
{"id": "gold_dormant_login21_active", "expected_action": "nudge_usage", "expected_priority": 3,
 "tags": ["boundary:DORMANT_DAYS"], "rationale": "login exactly 21 days ago; dormant needs > 21",
 "lead": {"contact_name": "…", "stage": "active_trial", "last_login_date": 21,
          "quotes_created": 3, "quotes_submitted": 0, "hubspot_notes": "…"}}
```

- `expected_action` ∈ the six `ACTION_TYPES`; `expected_priority` ∈ `{1, 2, 3}`.
- These labels are **ground truth** (what the classifier *should* say), so the
  baseline is legitimately below 1.0 where the rules are imperfect — that is the
  signal, not a bug.
- `lead` uses the real Lead field names (see `raw_data/leads.json`). **Date fields**
  (`signed_up_date`, `last_login_date`, `last_contacted_date`) accept an ISO
  string, `null`, or an **integer = that many days before the frozen `TODAY`**
  (`2026-06-12`), so threshold boundaries read directly:
  `"last_login_date": 21` means exactly `DORMANT_DAYS` days ago.
- Optional `events`: a list of `{"type", "meta"}` (event timestamps are only used
  by the copy path, not the classifier).

### What the golden set covers

All six action types; boundary trios at every threshold constant
(`DORMANT_DAYS`, `QUIET_CONTACT_DAYS`, `STALE_CONTACT_DAYS`, `TRIAL_AT_RISK_DAYS`,
`POWER_USER_DEALS`, `POWER_USER_SUBMISSIONS`, and the `$2M`/`$5M` book cutoffs);
adversarial notes that *look* like a hold/stall phrase but must not flip the
label; branch-precedence cases; and leads that should legitimately land on
`unknown`. Two leads (tagged `rule_gap`) document real gaps where a valuable
active user falls through to `unknown`.

### The regression gate

The gate is **action-type only** (priority is scored and printed but never fails
the run). For every action type it fails if `precision` or `recall` drops below
the baseline. To lock in an *intentional* change (e.g. after tuning a threshold),
re-run with `--update-baseline` and commit the updated `baselines/rules.json`.

### Prove it measures the classifier

Change a threshold constant in `outreach.py` (e.g. `DORMANT_DAYS = 21` → `30`) and
re-run: the numbers visibly move and the gate fails. Revert to restore green.

## Copy quality eval (`run_copy_eval.py`, LLM judge)

Where the rules eval scores the *classifier*, this one scores the *generated
copy*. `_build_copy_prompt` asks the model for six things — a Subject line, a
~120-word body, concrete lead detail, a helpful-peer voice, exactly one CTA
matching the planned action, and no commentary — and nothing verified any of
them. This harness does: **cheap deterministic checks first, then an LLM judge.**

Built on [Inspect](https://inspect.aisi.org.uk/) (`inspect-ai`, in
`requirements-dev.txt`) — but Inspect is only scaffolding (dataset, scorers,
report, `inspect view` logs). **Every real LLM call — generation *and* judging —
goes through the repo's own provider-agnostic layer** (`get_llm_client` /
`build_client`), never Inspect's model providers. Inspect's task model is
`mockllm/model`, so the harness needs no key for Inspect itself.

```bash
python evals/run_copy_eval.py                     # gate the active provider vs baseline
python evals/run_copy_eval.py --provider claude   # gate a specific configured provider
python evals/run_copy_eval.py --update-baseline   # (re)write this provider's baseline
python evals/run_copy_eval.py --limit 6           # quick smoke test (fewer leads)
python evals/run_copy_eval.py --table             # print the README comparison table
```

The provider's API key must be in the environment (e.g. `GROQ_API_KEY`).

### Files

| Path | What it is |
|------|------------|
| `run_copy_eval.py` | CLI runner: runs the Inspect task for one provider, aggregates, gates, renders the table. |
| `copy_eval.py` | The Inspect task — dataset, generation solver, deterministic + judge scorers. |
| `copy_checks.py` | Pure-Python deterministic checks (no LLM, no deps; unit-tested in `project/app/tests_copy_scorers.py`). |
| `rubrics/copy.md` | The LLM-judge rubric — committed as a file, not buried in a prompt string. |
| `pricing.toml` | Per-model reference rates for the est. cost column (runs no AI). |
| `baselines/copy.json` | Per-provider quality baseline the gate protects. |
| `results/copy-<provider>.json` | Full per-run aggregates (quality + latency + est. cost) feeding the table. |

### LLM-agnostic, one provider per run

The harness only ever calls the provider you configured — the active,
database-configured provider (see `/api/llm/config/`), or an explicit
`--provider` that must name a provider in the seeded catalog. It never fans
out to providers you didn't configure. The judge is equally agnostic: pass
`--judge-provider` to grade with a different configured provider (handy to
avoid a model grading its own output), otherwise it uses the generation
provider. Nothing is hardcoded to any vendor.

The provider **comparison table** is therefore assembled from *separate*
single-provider runs — run once per provider you want to compare, then
`--table` renders the Markdown from whatever `results/copy-*.json` artifacts
exist.

### What it scores

Reuses the same `golden/leads.jsonl` (via the rules harness's `load_golden` /
`build_lead`), dropping `unknown` leads (no copy is generated for those) — ~38
leads. Each lead's ground-truth `expected_action` is the planned action and its
`rationale` is the "why now", so copy quality is judged independently of any
rules bug.

- **Deterministic (pass/fail, free):** has a `Subject:` line; body word count in
  band; no preamble/commentary; exactly one CTA-shaped sentence. Coarse by
  design — they catch gross violations; the judge handles nuance.
- **LLM judge (1–5 each, rubric in `rubrics/copy.md`):** references ≥2 concrete
  lead facts; tone is a helpful peer, not salesy; the CTA matches the planned
  `action_type`.

### The regression gate

Generation is non-deterministic (every run yields different emails), so the gate
uses **tolerance bands** rather than exact match: a provider fails if a
deterministic pass-rate drops more than 0.10 below its baseline, or a judge
dimension drops more than 0.5 (on the 1–5 scale), or fewer than 80% of leads
produced copy. Baselines are **per provider**; only quality is gated —
latency/cost live in `results/` and the README table, and are reported, not
gated. Lock in an intended change with `--provider <p> --update-baseline`.

### Gating in CI

`.github/workflows/copy-eval.yml` runs this as a **separate** job — nightly plus
manual (`workflow_dispatch`), never on push/PR — because it makes real, paid LLM
calls. Provider keys come from GitHub secrets; a regression fails the job.
