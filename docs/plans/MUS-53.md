# MUS-53 — Enforce the contract/skeleton/mini-PR workflow

## Context

MUS-53 turns the contract → skeleton → mini-PR workflow from prose in CLAUDE.md into a
machine-enforced system: a required `workflow-gate` check that classifies every PR by
its base/head refs and enforces file-map scope, marker accounting, contract lint, and
red-proof; a `ci-ok` summary check; GitHub rulesets on `master` and `feat/*` with **no
bypass**; and the docs that make the out-of-repo parts reproducible. Governing
principle from the ticket: *an instruction is a hope; a gate is a fact* — every rule is
either mechanically gated or explicitly marked review-only in this plan.

Verified repo facts this plan corrects the ticket against:

| Ticket says | Reality (verified 2026-07-30) |
|---|---|
| Default branch `main` | Default branch is **`master`** (GitHub API). **Decision: keep `master`**; every `main` in the ticket reads `master` below. |
| Branches `feat/x` + `feat/x/skeleton` | **Impossible in git** — ref directory/file conflict. **Decision: flat naming** (below). |
| "ci.yml currently targets main" | `ci.yml` pushes on `master`, and `pull_request:` is **unfiltered** — mini PRs already get full CI. No trigger change needed. |
| Mini PRs get "…and rules eval" | `evals/run_rules_eval.py` is **not in ci.yml today**. Plan adds a `rules-eval` job (pure Python, milliseconds). |
| — | `coverage-badge` job pushes directly to `master`; a no-bypass ruleset kills it. **Decision: badge moves to an unprotected `badges` branch** + shields.io endpoint URL in README. |
| "Replace stale CLAUDE.md with the new draft" | No draft exists (repo, Linear docs, local plans all searched). **Decision: author it fresh** from the ticket's workflow description; the two facts it must assert are verified: eval command `python evals/run_rules_eval.py`, mypy target `mypy project/app/services/` (ci.yml:39). |

### Branch naming (replaces the ticket's infeasible scheme)

| Branch | Role |
|---|---|
| `feat/<x>` | integration branch (from master) |
| `feat/<x>--skeleton` | skeleton PR head (base `feat/<x>`) |
| `feat/<x>--<component>` | mini PR head; segment after `--` must equal a file-map component key verbatim (e.g. `feat/run-composer--scope_engine`) |
| `feat/<x>--contract*` | contract-change PR head (reserved segment) |
| `meta/**` | gate-infra PRs to master |

All workflow branches are single-level, so **one ruleset pattern `feat/*` covers
everything** — this sidesteps the nested-glob (`feat/**`) uncertainty the ticket itself
flags. Classification always has both base and head, so the component is parsed by
stripping `<base>--` from the head; a head under a `feat/<x>` base that doesn't follow
the scheme is an **explicit gate failure** ("branch does not follow workflow naming"),
never a silent no-op. Stacked PRs (base = `feat/<x>--something`) are rejected —
components are disconnected services by design. Linear's auto-generated
`musansht/mus-NN-*` branch names fall outside `feat/*` and classify as Normal PRs
(gate no-ops, CI applies); workflow branches are named manually — new CLAUDE.md says so.

---

## 0. The bootstrap — how MUS-53 ships when every file it touches is protected

**Position: one `meta/**` PR, exempt from the contract/skeleton/mini-PR flow by
definition, decomposed and test-first internally.** Two refinements, not
disagreements:

1. **The exemption is narrower than it sounds.** The meta PR is exempt only from the
   gate that does not yet exist. Normal CI applies in full — and the meta PR itself
   *extends* CI to run `scripts/tests/` (new step in ci.yml), so the gate's own test
   suite runs green inside the very PR that introduces it. It is exempt from
   decomposition-by-contract, not from mechanical verification. The self-consistency
   is exact: once merged, the gate classifies its own PR (`meta/mus-53-bootstrap` →
   `master`) as the meta class and no-ops — the system retroactively endorses its own
   bootstrap. "Test-first internally" (tests committed before implementation within
   the PR) is **review-only** — commit ordering cannot be machine-checked; noted
   honestly per the governing principle.

2. **"Ships as a single meta PR" ≠ "the system turns on when it merges."** The PR
   ships code and docs. Rulesets — the actual no-merge enforcement — are created
   *after* merge, *after* the rehearsal proves the gate's verdicts on real PRs, first
   in Evaluate mode, then Active (§4). The bootstrap PR is deliberately inert.

Steelman for splitting into several meta PRs (checker first, workflows second, docs
third): smaller reviews. Rejected: every intermediate state is a repo with half an
enforcement system and docs describing gates that don't exist, and since nothing is
enforced until rulesets activate post-rehearsal, splitting buys no safety — only more
states to reason about.

---

## 1. Decomposition

Each component is a disconnected service: no two edit the same file.

### C1 — Workflow doc + contract format
- **Owns:** `CLAUDE.md` (full rewrite), `docs/contracts/TEMPLATE.md`
- **Inputs:** ticket's workflow description; the two verified facts (eval command,
  mypy target). Phase 1's "strip markers locally and confirm red" ritual is replaced
  by a pointer to the red-proof gate (§8 of ticket).
- **Outputs:** committed docs; TEMPLATE contains the fenced `# file-map` YAML block
  exactly as specced (components → files/tests, `shared`).
- **Proof:** `scripts/tests/test_check_scope.py::test_lint_template_example_passes`
  runs contract lint over TEMPLATE's example map — doc/format drift breaks CI.
  **Cost:** milliseconds, every push.

### C2 — `scripts/check_scope.py` + its tests (the brain)
- **Owns:** `scripts/check_scope.py`, `scripts/tests/` (checker tests), `pyyaml` pin
  in `requirements-dev.txt`
- **Inputs:** CLI args / `GITHUB_BASE_REF` + `GITHUB_HEAD_REF`; local git history
  (`git diff --name-only origin/$BASE...HEAD`, `git show ref:path` for base-side
  marker counts); contract files.
- **Outputs:** exit 0/1 with messages that name the offending file and the rule
  broken (readable by a Claude session mid-loop). Modes:
  - `--classify` → class/feature/component (written to `GITHUB_OUTPUT`)
  - `--gate` → full per-class enforcement (diff-set ⊆ file map; shared-file and
    protected-path rejection; AST marker accounting: own module → 0, every other
    test module unchanged, sweep over *all* `project/app/tests*.py`)
  - `--validate-contract PATH` → lint: map parses; every owned file in exactly one
    component; `shared` ∩ owned = ∅; an `assembly` component exists; no protected
    path claimed by any component or `shared`
  - local self-check per ticket: `python scripts/check_scope.py --base origin/feat/x
    --component scope_engine`
- Protected-path set hardcoded **here**: `.github/workflows/**`,
  `scripts/check_scope.py`, `scripts/red_proof.py`, `scripts/tests/**`, `CLAUDE.md`,
  `docs/contracts/TEMPLATE.md`, `docs/ci.md`, `docs/rulesets/**` (ticket's list +
  the two files the ticket couldn't name because it hadn't invented them; evals
  additions → open question 7c).
- **Proof:** the §6 unit suite — pure Python + throwaway `git init` repos in tmpdirs;
  no GitHub, no network. **Cost:** a few seconds, every push.

### C3 — `scripts/red_proof.py` + its tests
- **Owns:** `scripts/red_proof.py`, its tests in `scripts/tests/`
- **Inputs:** the feature's test-module list (from the file map); the checkout.
- **Behavior:** AST-transform the working copy (uncommitted) to strip both
  `@unittest.expectedFailure` and bare `@expectedFailure`; run each marked module via
  `manage.py test`; classify per-test: AssertionError → red (good);
  NotImplementedError → red (good); test **passes** → vacuous → fail naming the test;
  ImportError/collection error → broken scaffolding → fail naming the module; any
  other error type → fail naming the test (conservative). Emits the module →
  red-count summary table to the job log.
- **Proof:** fixture test modules (one per outcome class) run through the real
  subprocess path. **Cost:** ~10–30 s, every push.

### C4 — `ci.yml` evolution
- **Owns:** `.github/workflows/ci.yml`, README badge line
- **Changes:** (a) `ci-ok` summary job — `needs:` every job, `if: always()`, fails on
  any failed/cancelled/skipped dependency (rulesets need one stable name over matrix
  legs); (b) `concurrency` per ref, `cancel-in-progress` for PR events only; (c) new
  `rules-eval` job: `python evals/run_rules_eval.py`; (d) new step running
  `python -m unittest discover -s scripts/tests` (without this the gate's own tests
  never run in CI); (e) `coverage-badge` job rewritten to push a coverage JSON to the
  unprotected `badges` branch; README badge becomes a shields.io endpoint URL. The
  `pull_request` trigger stays **unfiltered** — already a superset of the ticket's ask.
- **Proof:** tier 2 only — the meta PR's own checks show `ci-ok` aggregating; badge
  proof is one push to master post-merge. A tier-1 consistency test (C2's suite)
  YAML-parses ci.yml and asserts the `ci-ok` job exists under that exact name.

### C5 — `.github/workflows/workflow-gate.yml`
- **Owns:** that one file. Thin YAML, zero logic: jobs `classify` (runs
  `check_scope.py --classify`) → `scope-gate` (runs `--gate`; for the landing class
  also runs the scoped marker sweep + `python evals/run_rules_eval.py`; for mini PRs
  also `python manage.py test project.app.<tests_module>`) → `red-proof`
  (`if: classify.outputs.class == 'skeleton'`) → final aggregator job **named
  `workflow-gate`** (`needs:` all, `if: always()`, fails unless every needed job
  succeeded or was legitimately skipped for the class). One required check name;
  red-proof is enforced transitively without becoming a second required name that
  would block non-skeleton PRs.
- Triggers: `on: pull_request` with **no branches/paths filter** — the check always
  appears, classification decides behavior; the "required check that never triggers"
  trap is closed structurally. Checkout with `fetch-depth: 0`.
- **Proof:** tier 2 rehearsal (§5). Tier-1 consistency test asserts the file parses
  and the aggregator job name is `workflow-gate`.

### C6 — Rulesets + `docs/ci.md`
- **Owns:** `docs/ci.md`, `docs/rulesets/master.json`, `docs/rulesets/feat.json`
- Ruleset content (both): require PR before merge; required checks `ci-ok` +
  `workflow-gate`; block force pushes; no direct pushes; **zero bypass actors**.
  Patterns: `master` exact; `feat/*`.
- Created from the committed JSON via `gh api repos/{owner}/{repo}/rulesets --input
  docs/rulesets/<f>.json`, read back and diffed — the config is out-of-repo state,
  but scripted creation + API read-back demotes most of tier 3 to tier 2.
- `docs/ci.md` documents: creation commands, Evaluate → Active procedure, the
  recovery path (§4), the revert-PR story (corrected — see open question 7a), the
  stale-open-PR note (PRs opened before the gate existed need a push/"Update branch"
  to grow the new required checks), and the honest caveat that no-bypass binds the
  merge/push plane only — the settings plane remains admin-editable (that trust model
  is MUS-54, out of scope).
- **Proof:** tier 2 (Evaluate-mode insights, dead merge button) + tier-1 consistency
  test: parse the two JSONs and assert their required-check names equal the job names
  in the two workflow files — the cross-file string coupling becomes a CI fact.

### C7 — Demo contract + rehearsal kit
- **Owns on master:** nothing permanent. `docs/contracts/demo.md`, stubs
  `project/app/services/demo_{alpha,beta,assembly}.py`, tests
  `project/app/tests_demo_{alpha,beta,assembly}.py` are created on rehearsal branches
  exactly as the real workflow would, land via scenario 8, and are removed by the
  cleanup PR (§5 step 18).
- **Proof:** it *is* the proof — the §5 playbook.

**Same-file collisions:** none. **Couplings flagged:** C1's TEMPLATE ↔ C2's lint
rules (resolved into tier 1 by `test_lint_template_example_passes`); C4/C5 job names ↔
C6 ruleset JSON strings (resolved into tier 1 by the consistency test; final binding
still tier 2).

---

## 2. Verifiability tiers — the honest split

**Tier 1 — unit-testable in CI, every push:**
all of C2 (file-map parsing, contract lint, classification, diff-set validation via
tmpdir git repos, AST marker accounting, message content); C3's stripping and outcome
classification via fixtures; the consistency tests (TEMPLATE lints clean; workflow
YAMLs parse; ruleset JSON check names == workflow job names); the rules eval itself.

**Tier 2 — verifiable only against real GitHub:**
trigger firing and the check *appearing* on every PR class; `GITHUB_BASE_REF`/
`HEAD_REF` plumbing; `fetch-depth: 0` + `origin/$BASE` resolution inside Actions;
`ci-ok` aggregation under a real matrix failure/cancel; concurrency cancellation;
ruleset pattern actually covering `feat/*` branches; required-check-by-name binding;
merge-button state; Evaluate-mode insights; direct-push rejection.

**Tier 3 — not testable, only documented:**
the rulesets' continued existence and out-of-band edit resistance (settings plane —
MUS-54's trust model); repo admin/visibility settings. Everything else that started
tier 3 has been pushed down: ruleset *content* → committed JSON + scripted
create/read-back (tier 2); required-name coupling → consistency test (tier 1).

**How the plan maximizes tier 1:** classification lives in Python, not workflow `if:`
expressions; every verdict, message, and count is a `check_scope.py` function; YAML
contains only sequencing. **Where the thin-YAML principle breaks down, named:**
`on:` triggers, job-level `if:`, `needs` + `if: always()` aggregation semantics,
checkout depth, and env plumbing are irreducibly YAML and only rehearsable; the
required-check name binding lives in GitHub's config, not in any testable artifact;
and red-proof's "run the tests and watch them fail" step inherently needs the Django
test runner in a subprocess — tier 1 still, but the slow end of it.

**Review-only residue (mechanical enforcement impossible, said explicitly):**
test-first commit ordering inside the meta PR; contract prose being *meaningful*
(lint checks section presence, not quality); a marked test failing for a
wrong-but-plausible reason (red-proof verifies failure class, not intent); settings-
plane integrity; badge endpoint visual correctness.

---

## 3. Build order, justified by proof

All of 1–5 are commits inside the single meta PR (branch `meta/mus-53-bootstrap`),
in this order; 6–8 are post-merge operations.

1. **C2 checker, tests first.** The §6 inventory is written as failing tests, then
   `check_scope.py` makes them pass. *If built after the workflows*, its correctness
   would only ever be observed through live workflow runs — unverifiable in CI, which
   is the exact disease this ticket cures.
2. **C3 red-proof, tests first.** Same argument.
3. **C1 docs.** TEMPLATE is written only once lint exists to validate it — *wrong
   order leaves the contract format a hope, not a fact.* CLAUDE.md's two asserted
   facts are already verified against ci.yml.
4. **C4 ci.yml.** After C2/C3 exist, so the new `scripts/tests` step actually runs
   them; `ci-ok` must exist *before* any ruleset JSON names it — *wrong order = a
   required check that never reports = every PR on master permanently blocked.*
5. **C5 gate workflow.** A thin caller written last, after both scripts it invokes
   are green locally.
6. **Meta PR opens and merges.** Its own run is the first tier-2 datum: proves both
   workflows trigger, `ci-ok` aggregates, and the gate classifies a `meta/**` head as
   the meta class and no-ops. Rulesets do not exist yet; nothing can lock.
7. **C7 rehearsal phase A** (§5) — all eight scenario verdicts proven while the merge
   button is still unconstrained.
8. **C6 rulesets:** Evaluate → verify insights → Active → rehearsal phase B (merge-
   button proofs + the screenshot). *Enabling rulesets any earlier than step 8 is the
   lockout scenario* (§4).

---

## 4. The lockout risk

The failure mode: rulesets active + no bypass + a gate bug that always fails ⇒ the
fix PR for the gate cannot merge, for anyone, including the admin.

**Exact order of enablement relative to landing the gate code:**
1. Gate code merges to master with **no rulesets in existence** (build-order step 6).
2. **Phase A rehearsal (advisory):** all 8 scenarios run as real PRs; the checks
   report red/green exactly as predicted, but nothing blocks. This proves both
   directions — the gate goes red on violations *and green on compliant PRs*. The
   "gate always fails" bug is caught here, before anything is required.
3. Rulesets created in **Evaluate mode** (available free on public repos — this repo
   is public; playbook step 12 confirms availability before relying on it, fallback
   in open question 7b). Re-run one red and one green scenario; the ruleset Insights
   log must show would-block / would-pass respectively, and must show the `feat/*`
   pattern matching the rehearsal branches — this is the explicit pattern-
   verification the ticket demands.
4. Flip to **Active**. Immediately verify both directions again: a red mini PR shows
   a dead merge button; the corrected PR merges (phase B).

**How the gate is verified correct before it becomes required:** three independent
layers, in order of increasing realism — the tier-1 suite (every verdict function),
phase A (real PRs, advisory), Evaluate mode (real enforcement engine, dry-run). The
gate becomes required only after passing all three.

**The structural anti-trap:** `workflow-gate` has no branch/path filters, a single
always-reporting aggregator job, and classification in Python that maps *every*
(base, head) shape to an explicit verdict — the check can be red, never absent. The
residual "workflow file has a YAML syntax error → check never appears" case is
covered by the tier-1 parse test and, ultimately, by recovery below.

**Documented recovery path (goes verbatim into `docs/ci.md`):** ruleset enforcement
is settings-plane state, not gated by itself. Recovery = repo admin sets enforcement
to Disabled — Settings → Rules → ruleset → Enforcement, or
`gh api -X PUT repos/{owner}/{repo}/rulesets/{id} -f enforcement=disabled` — then
lands the fix via a `meta/**` PR under plain CI, re-enables Evaluate, re-verifies with
a scenario re-run, and flips Active again. This is honest, not a loophole: "no admin
bypass" binds the merge/push plane; the settings plane always remains, is exactly why
lockout is recoverable, and its trust model is deliberately MUS-54's problem.

---

## 5. The rehearsal playbook

Feature `demo`; components `alpha`, `beta`, `assembly`. Executable top-to-bottom
without rereading the ticket. Scenario numbers map to the ticket's acceptance list.

**Preconditions:** meta PR merged; master CI green; `gh` authenticated as admin; work
in a dedicated worktree per CLAUDE.md. No rulesets exist yet.

### Phase A — verdicts (rulesets off; merge button intentionally alive)

| # | Action | Expect | Capture |
|---|---|---|---|
| 1 | `git push origin master:feat/demo` | branch exists | — |
| 2 | Branch `feat/demo--skeleton` from `feat/demo`. Commit: `docs/contracts/demo.md` whose file map **claims `scripts/check_scope.py`** under `alpha` (otherwise normal: alpha/beta/assembly components, their stub service files `project/app/services/demo_*.py`, tests modules `project/app/tests_demo_*.py`, empty `shared`); skeleton tests behind `@unittest.expectedFailure` calling stubs that raise `NotImplementedError`. Open PR → base `feat/demo`. | **`workflow-gate` FAIL** at contract lint, message names `scripts/check_scope.py` as protected. **[Scenario 3]** | Screenshot: failed check annotation |
| 3 | Push: remove the protected-path claim; add one **vacuous** test to `tests_demo_alpha.py` (`@expectedFailure` over `assert True`). | Contract lint green; **`red-proof` FAIL** naming `tests_demo_alpha.<Case>.<test>`. **[Scenario 2]** | Screenshot: red-proof log + summary table |
| 4 | Push: make that test real (assert against contract output; genuinely fails). | `red-proof` green with module → red-count table; `workflow-gate` green; `ci-ok` green. **[Scenario 1]** Merge into `feat/demo`. | Log link: red-count table |
| 5 | Branch `feat/demo--alpha` from `feat/demo`. Implement alpha, remove **all** alpha markers — but also add a one-line edit to `project/app/services/demo_beta.py`. Open PR → base `feat/demo`. | **FAIL**: "diff outside component file map: project/app/services/demo_beta.py". **[Scenario 4]** | Screenshot: check annotation |
| 6 | Push: revert the beta-file edit; deliberately leave **one** alpha marker. | **FAIL**: marker accounting — "1 expectedFailure remaining in project/app/tests_demo_alpha.py". **[Scenario 5]** | — |
| 7 | Push: remove the last alpha marker but also delete one marker in `tests_demo_beta.py`. | **FAIL**: "marker count changed in sibling module tests_demo_beta.py". **[Scenario 6]** | — |
| 8 | Push: restore `tests_demo_beta.py` byte-identical to `feat/demo`. | **Green** — gate log shows the scoped run `manage.py test project.app.tests_demo_alpha` passing. **[Scenario 7]** Merge. | — |
| 9 | Clean mini flow for beta: `feat/demo--beta`, implement, markers → 0, green, merge. | green | — |
| 10 | Open landing PR: base `master`, head `feat/demo` — assembly markers still present. | **FAIL**: "markers remain across feature test modules: tests_demo_assembly.py". **[Scenario 8a]** Leave PR open. | — |
| 11 | `feat/demo--assembly`: implement wiring, markers → 0, merge into `feat/demo`. Landing PR re-runs. | **Green** (zero markers; contract exists; rules-eval baseline holds). **[Scenario 8b]** **Do not merge yet.** | — |

### Phase B — enforcement (rulesets on)

| # | Action | Expect | Capture |
|---|---|---|---|
| 12 | `gh api repos/musatarar/Agentic-Outreach-Planner/rulesets --input docs/rulesets/master.json` and `…/feat.json`, both with `"enforcement": "evaluate"`. Read back via `gh api …/rulesets` and diff against the committed JSON. | created; content matches | — |
| 13 | Recreate a red mini PR (branch `feat/demo--alpha` again with an out-of-map edit). Also push one trivial direct commit to `feat/demo`. | Ruleset **Insights** show: merge would-block; direct push would-block; the `feat/*` pattern listed as matching `feat/demo` — **pattern verification done here**. | Screenshot: insights entries |
| 14 | Flip both rulesets to `"enforcement": "active"` (`gh api -X PUT`). | active | — |
| 15 | View the red PR from step 13. | **Merge button dead** — "Required check workflow-gate has failed". | **Screenshot — the README image** |
| 16 | Fix the PR to green → button revives → close it. Then `git commit --allow-empty && git push origin feat/demo` and the same against `master`. | Both pushes **rejected** by the ruleset. | Screenshot (optional): rejection message |
| 17 | Merge the landing PR from step 11 through live enforcement. | merges; `feat/demo` history lands on master | — |
| 18 | Cleanup PR to master (head `chore/demo-cleanup`): delete `docs/contracts/demo.md`, `project/app/services/demo_*.py`, `project/app/tests_demo_*.py`; add the screenshot + section to README. | Classified Normal → gate no-op success; CI green; merges. Delete all `feat/demo*` branches. | — |

Estimated wall clock: 45–75 minutes, dominated by Actions queue time (~8–10 workflow
runs at 2–3 min each).

---

## 6. Test inventory for `scripts/check_scope.py` (and red-proof) — written first

`scripts/tests/test_check_scope.py` — fixtures build throwaway git repos
(`git init` in tmpdir, scripted commits/branches) so diff logic is proven offline.

**File-map parsing**
- `test_parse_file_map_happy_path`
- `test_parse_missing_file_map_block`
- `test_parse_invalid_yaml_names_line`

**Contract lint (`--validate-contract`)**
- `test_lint_valid_contract_passes`
- `test_lint_template_example_passes` *(C1↔C2 coupling lock)*
- `test_lint_file_owned_by_two_components_rejected`
- `test_lint_shared_overlaps_owned_rejected`
- `test_lint_missing_assembly_component_rejected`
- `test_lint_protected_path_claimed_by_component_rejected`
- `test_lint_protected_path_in_shared_rejected`

**Classification**
- `test_classify_skeleton_pr`
- `test_classify_mini_pr_extracts_component`
- `test_classify_feature_landing`
- `test_classify_meta_pr`
- `test_classify_normal_pr_noop`
- `test_classify_malformed_head_under_feat_base_fails`
- `test_classify_stacked_base_rejected`

**Mini-PR enforcement**
- `test_mini_diff_within_map_passes`
- `test_mini_diff_outside_map_rejected_names_file`
- `test_mini_touches_shared_file_rejected`
- `test_mini_touches_protected_path_rejected`
- `test_mini_component_not_declared_rejected`
- `test_mini_contract_missing_rejected`
- `test_mini_test_module_rename_rejected_file_map_out_of_date`
- `test_mini_test_module_delete_rejected_file_map_out_of_date`

**Marker accounting (AST)**
- `test_marker_count_qualified_decorator`
- `test_marker_count_bare_decorator`
- `test_marker_count_immune_to_comments_and_strings`
- `test_mini_own_module_zero_after_passes`
- `test_mini_own_module_marker_remaining_rejected`
- `test_mini_sibling_marker_removed_rejected`
- `test_mini_marker_added_anywhere_rejected`

**Skeleton exemption**
- `test_skeleton_may_add_markers`
- `test_skeleton_scope_check_skipped_contract_still_required`
- `test_skeleton_protected_path_still_rejected`

**Feature landing**
- `test_landing_zero_markers_passes`
- `test_landing_surviving_marker_rejected_names_module`

**Contract-change PRs**
- `test_contract_change_shape_allowed`
- `test_contract_change_marker_add_with_contract_diff_allowed`
- `test_contract_change_marker_add_without_contract_diff_rejected`
- `test_contract_change_touching_implementation_rejected`

**Edges + messages**
- `test_empty_diff_noop_success`
- `test_docs_only_pr_to_master_noop_success`
- `test_failure_messages_name_file_and_rule`

**Consistency (cross-file couplings pulled into tier 1)**
- `test_workflow_yamls_parse`
- `test_ruleset_json_check_names_match_workflow_job_names`

`scripts/tests/test_red_proof.py` — fixture modules, real subprocess runs:
- `test_strip_removes_both_marker_forms`
- `test_assertion_failure_counts_red`
- `test_notimplementederror_counts_red`
- `test_vacuous_test_fails_naming_test`
- `test_import_error_fails_naming_module`
- `test_unexpected_error_type_fails`
- `test_summary_table_lists_module_red_counts`

---

## 7. Open questions and disagreements

Resolved with Musa (2026-07-30): keep `master`; flat `feat/<x>--<component>` naming;
badge → `badges` branch; CLAUDE.md authored fresh.

Remaining — surfaced, not papered over:

- **(a) The ticket's revert-escape claim is wrong on feature branches.** §5 says "a
  revert PR still goes through the gates, which is fine because reverts pass tests by
  construction." True for master (a `revert-*` head classifies Normal → gate no-ops,
  CI applies). False on `feat/<x>`: a GitHub-generated `revert-…` head fails naming
  classification, and reverting a merged mini PR *re-adds markers*, which mini-class
  accounting forbids. Correction to document in `docs/ci.md`: on feature branches the
  undo path is a corrected mini PR for the same component (or recreating the feature
  branch); reverts as a mechanism exist only on master. **This is a ticket bug; the
  plan implements the corrected version.**
- **(b) Evaluate mode availability** is believed free on public repos; playbook step
  12 verifies before relying on it. Fallback if absent: pre-stage a green scenario
  and a red scenario, flip Active during a quiet moment, verify both immediately —
  same proof, smaller window, recovery path (§4) already in hand.
- **(c) `evals/` is not on the ticket's protected-path list.** A contract could claim
  `evals/golden/leads.jsonl` or `evals/baselines/rules.json` and a "compliant" mini PR
  could quietly weaken the very baseline the landing gate checks. **Recommend adding
  `evals/golden/**`, `evals/baselines/**`, `evals/run_rules_eval.py` to the protected
  set** (cost: threshold-tuning changes become a two-PR dance — product code via the
  normal flow, baseline update via `meta/**`). Plan includes this unless vetoed; it
  slightly expands the ticket's hardcoded list.
- **(d) Scenario 8 lands throwaway demo code on master** before the cleanup PR
  removes it (playbook step 18). Two commits of noise in master history — accepted as
  the cost of rehearsing the real flow end-to-end; flagged in case stopping scenario
  8 at the green check without merging is preferred (weaker proof: merge-ability
  shown, actual landing not exercised).
- **(e) Marker sweep breadth:** §3's "no adding new ones anywhere" is implemented as
  a sweep over *all* `project/app/tests*.py` modules, not just the feature's — the
  literal reading. Existing repo test modules currently carry no markers, so this is
  free today, and a future unrelated feature's in-flight skeleton on another branch is
  unaffected (accounting is per-PR-diff, not repo-global state). Stating the
  interpretation so it isn't discovered later.
- **(f) Priority context:** MUS-25/26 work is in flight on `musansht/*` branches.
  Once rulesets go Active, those PRs also need `ci-ok` + `workflow-gate` (they'll
  no-op the gate and pass CI if green), and any PR opened before the gate existed
  needs a push or "Update branch" for the new checks to appear. Sequencing MUS-53's
  activation around in-flight merges is an operational choice; the plan itself
  doesn't depend on it.

---

## Verification

1. **Tier 1, locally before the meta PR:** `python -m unittest discover -s
   scripts/tests` green; `ruff check . && ruff format --check .` green; existing
   suite `python manage.py test project.app` untouched and green.
2. **The meta PR itself:** `ci-ok` and `workflow-gate` both appear and pass on it
   (first live proof of triggers + meta-class no-op).
3. **The playbook (§5)** — all 8 scenarios with predicted verdicts, then Evaluate
   insights, then the dead merge button under Active. Screenshots collected per the
   capture column; the dead-button shot goes into the README via the cleanup PR.
4. **Post-activation:** one push to master confirms the badge pipeline writes to the
   `badges` branch and the README badge renders.
