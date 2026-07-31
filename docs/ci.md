# CI + merge enforcement (MUS-53)

How the contract → skeleton → mini-PR workflow (see `CLAUDE.md`) is mechanically
enforced, and how to reproduce the out-of-repo parts. Governing principle: *an
instruction is a hope; a gate is a fact.*

## The two required checks

| Check | Workflow | What it summarizes |
|---|---|---|
| `ci-ok` | `.github/workflows/ci.yml` | lint, mypy, frontend, the py×db test matrix, rules-eval, gate-tests — one stable name over all matrix legs |
| `workflow-gate` | `.github/workflows/workflow-gate.yml` | classify → scope-gate (+ scoped mini tests, landing rules-eval) → red-proof (skeletons) → aggregate |

Both are aggregator jobs with `if: always()`; they can be red but never absent.
`workflow-gate` has **no branch/path filters** — it runs on every PR and no-ops with
success for classes the workflow doesn't apply to (meta/normal). This closes the classic
trap: a required check that never triggers blocks its PR forever.

The coupling between these job names and the ruleset JSON below is pinned by
`scripts/tests/test_check_scope.py::ConsistencyTests` — renaming a job without updating
`docs/rulesets/*.json` breaks CI before it can break the repo.

## Rulesets

Rulesets (not classic branch protection: they take wildcard patterns and are free on
public repos) enforce, on `master` (exact) and `feat/*` (one pattern — all workflow
branches are single-level by construction):

- require a pull request before merging (this is also what rejects direct pushes),
- required status checks `ci-ok` + `workflow-gate`,
- block force pushes,
- **zero bypass actors** — "forced to pass" includes the admin, or it's theater.

The configuration is committed as `docs/rulesets/master.json` and
`docs/rulesets/feat.json` (enforcement deliberately starts at `evaluate`). Create both:

```bash
gh api repos/musatarar/Agentic-Outreach-Planner/rulesets --input docs/rulesets/master.json
gh api repos/musatarar/Agentic-Outreach-Planner/rulesets --input docs/rulesets/feat.json
```

Read back and diff against the committed intent (the API adds server-side defaults; the
fields that matter are `enforcement`, `conditions`, `rules`, `bypass_actors`):

```bash
gh api repos/musatarar/Agentic-Outreach-Planner/rulesets   # note the ids
gh api repos/musatarar/Agentic-Outreach-Planner/rulesets/<id>
```

### Evaluate → Active procedure

1. Create both rulesets with `"enforcement": "evaluate"` (as committed).
2. Exercise one red PR and one green PR per protected pattern; confirm the ruleset
   **Insights** (Settings → Rules → Insights, or
   `gh api repos/{owner}/{repo}/rulesets/rule-suites`) log would-block / would-pass
   respectively, and that the `feat/*` pattern actually matched the branches.
3. Flip to active:

   ```bash
   gh api -X PUT repos/musatarar/Agentic-Outreach-Planner/rulesets/<id> \
     -f enforcement=active
   ```

4. Immediately verify both directions again: a red PR shows a dead merge button; a green
   PR merges; a direct push to a protected branch is rejected.

Never activate before the gate code has passed three layers: its tier-1 unit suite, an
advisory rehearsal on real PRs, and Evaluate-mode insights. Activating a gate that always
fails is the lockout scenario below.

### Recovery path (lockout)

Failure mode: rulesets active + no bypass + a gate bug that always fails ⇒ the fix PR for
the gate cannot merge, for anyone, including the admin.

Ruleset enforcement is settings-plane state, not gated by itself. Recovery: a repo admin
sets enforcement to Disabled — Settings → Rules → (ruleset) → Enforcement, or

```bash
gh api -X PUT repos/musatarar/Agentic-Outreach-Planner/rulesets/<id> -f enforcement=disabled
```

— then lands the fix via a `meta/**` PR under plain CI, re-enables Evaluate, re-verifies
with a red/green scenario pair, and flips Active again.

This is honest, not a loophole: "no admin bypass" binds the **merge/push plane** only. The
**settings plane** (rulesets' continued existence, repo settings) necessarily remains
admin-editable — that is exactly why lockout is recoverable, and its trust model is
deliberately out of scope here (MUS-54).

## Reverts and undo

- **On `master`**: GitHub's revert button works — `revert-*` heads classify as Normal PRs,
  the gate no-ops, plain CI still applies.
- **On `feat/<x>`**: revert PRs do *not* work, by design. A GitHub-generated `revert-…`
  head fails workflow naming, and reverting a merged mini PR would re-add markers, which
  mini-class accounting forbids. The undo path on a feature branch is a corrected mini PR
  for the same component (or recreating the feature branch from `master`).

## Operational notes

- **PRs opened before the gate existed** don't show the new required checks until their
  head moves — push any commit or use "Update branch"; the checks then appear and apply.
- **Coverage badge**: the badge job pushes a shields.io endpoint JSON to the unprotected
  `badges` branch (rulesets cover only `master` and `feat/*`); the README badge reads
  `https://raw.githubusercontent.com/musatarar/Agentic-Outreach-Planner/badges/coverage.json`.
  Direct README pushes to master from CI are gone — a no-bypass ruleset forbids them.
- **Protected paths** are hardcoded in `scripts/check_scope.py` (list reproduced in
  `CLAUDE.md`). They include `evals/golden/**`, `evals/baselines/**`, and
  `evals/run_rules_eval.py`, so a contract cannot claim the very baseline the landing gate
  checks; the cost is that a deliberate baseline update rides a `meta/**` PR, separate
  from the product change that motivated it.
- **Local self-check** before pushing a mini PR:
  `python scripts/check_scope.py --base origin/feat/<x> --component <component>`.
