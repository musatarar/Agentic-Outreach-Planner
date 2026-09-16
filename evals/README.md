# evals/ — rules regression gate

`determine_action` and `determine_priority` in
[`project/app/services/outreach.py`](../project/app/services/outreach.py) decide who gets
outreach and why. The LLM only writes the copy. This directory measures whether that
classification is correct, and fails when it regresses.

## Running it

```bash
python evals/run_rules_eval.py                    # score + gate against the baseline
python evals/run_rules_eval.py --update-baseline  # rewrite the baseline, exit 0
python evals/run_rules_eval.py --golden PATH      # score a different golden file
```

Pure Python: no Django, no database, no network, frozen clock (`TODAY = 2026-06-12`).
The classifier duck-types on lead-like objects, so the harness feeds it `SimpleNamespace`
stubs built from `golden/leads.jsonl`. The run finishes in milliseconds. CI runs it as its
own job on every push and pull request.

## Files

| Path | What it is |
|---|---|
| `run_rules_eval.py` | Loader, metrics, confusion matrix, regression gate. |
| `golden/leads.jsonl` | Hand-labeled leads with the correct `(expected_action, expected_priority)`. |
| `baselines/rules.json` | Recorded per-action precision/recall/F1 that the gate protects. |

That is the whole directory: everything here serves the rules gate and nothing else.
The planner's deterministic shape checks on generated copy, which used to live here, are
application code and ship as `project/app/services/copy_checks.py`.

Golden records are one JSON object per line; `//` and blank lines are ignored. Date fields
accept an ISO string, `null`, or an integer meaning that many days before `TODAY`, so
threshold boundaries read directly (`"last_login_date": 21` is exactly `DORMANT_DAYS`).
The labels are ground truth — what the classifier *should* say — so a baseline below 1.0
is the signal, not a bug.

## The gate

Action type only; priority is scored and printed but never fails the run. For each action
type the run fails if precision or recall drops below the baseline.

**Baselines change only by an explicit human decision, never to make a run pass.** A red
gate means the rules moved. Decide whether that move was intended; if it was, re-run with
`--update-baseline` and commit the new `baselines/rules.json` as its own reviewed change.
