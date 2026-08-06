# CI + merge enforcement (MUS-53, reworked in MUS-64)

How the Linear-driven skeleton → component-PR workflow (see `CLAUDE.md`) is mechanically
enforced, and how to reproduce the out-of-repo parts. Governing principle: *an
instruction is a hope; a gate is a fact.*

## The two required checks

| Check | Workflow | What it summarizes |
|---|---|---|
| `ci-ok` | `.github/workflows/ci.yml` | lint, mypy, frontend, the py×db test matrix, rules-eval, gate-tests — one stable name over all matrix legs |
| `workflow-gate` | `.github/workflows/workflow-gate.yml` | classify → scope-gate (+ scoped component tests, landing rules-eval) → red-proof (skeletons) → aggregate |

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

The `feat/*` ruleset **excludes `refs/heads/feat/*--*`** — the skeleton and component PR
*head* branches. Those are where development pushes happen; putting a pull-request rule on
them would reject every push and kill the workflow. The enforcement boundary is the
integration branch `feat/<x>` (and `master`): a head branch only becomes code via a gated
PR into a protected base, so nothing is lost by leaving heads pushable.

The configuration is committed as `docs/rulesets/master.json` and
`docs/rulesets/feat.json`. Create both:

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

### Activation procedure

GitHub's **Evaluate** (dry-run) enforcement is not available on this plan — the API
returns `422: Enforcement evaluate option is not supported on this plan. Please upgrade
to Enterprise` (verified 2026-07-31; it is an Enterprise feature, not a public-repo
freebie). The committed JSON therefore carries `"enforcement": "active"`, and activation
uses the pre-staged-verdicts fallback:

1. With **no rulesets in existence**, rehearse the gate on real PRs (the full playbook:
   red and green verdicts for every class) while the merge button is still unconstrained.
2. Pre-stage one **red** PR and one **green** PR against a protected base.
3. Create both rulesets (already `active`, quiet moment) with the commands above.
4. Immediately verify both directions: the red PR's merge button is dead ("Required
   check workflow-gate has failed"), the green PR merges, and a direct push to `master`
   or `feat/<x>` is rejected. This also confirms the patterns matched real branches —
   the proof Evaluate-mode insights would have given, with a smaller window.

Never activate before the gate code has passed both prior layers: its tier-1 unit suite
and the advisory rehearsal on real PRs. Activating a gate that always fails is the
lockout scenario below.

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
- **On `feat/<x>`**: revert PRs do *not* work, by design — a GitHub-generated `revert-…`
  head fails workflow naming. The undo path on a feature branch is a corrected **component
  PR for the same component** (or recreating the feature branch from `master`). That path
  is deliberately open: the component gate does not require any red at base, so a repeat
  PR with nothing left to clear passes.

## Operational notes

- **PRs opened before the gate existed** don't show the new required checks until their
  head moves — push any commit or use "Update branch"; the checks then appear and apply.
- **Coverage badge**: the badge job pushes a shields.io endpoint JSON to the unprotected
  `badges` branch (rulesets cover only `master` and `feat/*`); the README badge reads
  `https://raw.githubusercontent.com/musatarar/Agentic-Outreach-Planner/badges/coverage.json`.
  Direct README pushes to master from CI are gone — a no-bypass ruleset forbids them.
- **Protected paths** are hardcoded in `scripts/check_scope.py` (list reproduced in
  `CLAUDE.md`). They include `evals/golden/**`, `evals/baselines/**`, and
  `evals/run_rules_eval.py`, so a component PR cannot edit the very baseline the landing
  gate checks; the cost is that a deliberate baseline update rides a `meta/**` PR, separate
  from the product change that motivated it.
- **No FE scoped-test step, on purpose.** The gate runs the scoped *backend* suite for a
  component PR (`manage.py test project.app.<module>`) but nothing equivalent for the
  frontend: `npm test` under `ci-ok` already runs every `frontend/tests/*.test.ts` on
  every PR, so a second run inside `workflow-gate` would buy nothing. The gate's frontend
  job is accounting — todo counts to zero for the component, frozen everywhere else —
  while `ci-ok` is the real-run enforcement. Consequence: the gate emits `test_targets`
  only when the component has a backend artifact, and an FE-only component PR passes the
  scope gate with no scoped step at all.
- **Landing staleness.** The landing gate compares red counts per file between
  `origin/master` and the feature head. If `master`'s baseline moved after the branch was
  cut (a `meta/**` or normal PR can legitimately change marker counts), a stale feature
  branch can read as an increase it never made. "Update branch" on the PR — or a merge
  from `master` — re-bases the comparison and the delta reads true.
- **Local self-check** before pushing a component PR:
  `python scripts/check_scope.py --base origin/feat/<x> --component <component>`.
