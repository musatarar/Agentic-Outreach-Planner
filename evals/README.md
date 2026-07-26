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

## Roadmap

MUS-21 (LLM-judge eval for the generated copy) will add a sibling harness here,
reusing the same `golden/` + `baselines/` layout.
