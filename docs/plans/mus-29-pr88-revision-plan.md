# PR 88 pre-merge revision plan (MUS-29)

**Status: awaiting Musa's approval. No code has been written.**

Scope: fix the three verified merge-blockers on `feat/agent-loop` before PR 88 lands, and
file the rest as follow-up tickets. Evidence and severity reasoning are in
`mus-29-pr88-triage.md`; raw candidates in `mus-29-pr88-review-findings.md`.

## Ground truth (verified 2026-08-15, not assumed)

- `origin/master` = `902cf42`. PR 88 head = `origin/feat/agent-loop` = `554dccf`. Merge
  base = `34d3183`; master advanced past it via PR 80 (the planning-discipline skill,
  `.claude/skills/` only). GitHub reports the PR `MERGEABLE` — no rebase needed.
- PR 88 CI is fully green at `554dccf` (ci-ok, four test legs, frontend, ruff, mypy, rules
  eval).
- The local `feat/agent-loop` ref inside `.claude/worktrees/agent-loop-reports-trace`
  points at `902cf42`, **not** the PR head. Any work must start from a worktree cut from
  `origin/feat/agent-loop`, not that stale ref.
- Toolchain: backend `python manage.py test project.app`; frontend `node --test` over
  `frontend/tests/*.test.ts` (no Vitest/jsdom). Verified against `CLAUDE.md` and CI.

## Invariants that must survive every task

Restated in full because a re-roll re-decides everything, not just the deltas:

1. **The flag-off path stays byte-identical.** `OUTREACH_AGENT_ENABLED` defaults off.
   Flag-off runs must issue zero new queries — `tests_planner_perf` pins the counts with
   `assertNumQueries`. Every fix below either sits inside the `if runtime.agent_enabled:`
   block or on a request shape (`resume_run_id`) that flag-off callers never send.
2. **No pinned test is edited.** The verifiers checked each fix against the tests that
   cover its site and found none requiring edits; that is a per-task acceptance condition,
   not an assumption. If a fix turns out to need one, stop and get authorization.
3. **Phase 3 stays ORM-free.** New DB reads belong in phase 2b (synchronous, ORM-land), not
   threaded into the async phase via `sync_to_async`.
4. **The single-winner claim survives.** The `claim_epoch` CAS is what makes concurrent
   resumers safe; nothing here may weaken it, and the two-claimer race stays tested.
5. **Red first.** Each PR's first commit is the failing test, with the failing output pasted
   into the PR description.

## Task 1 — MB1: re-verify the approval inside the send transaction

`project/app/services/dispatch.py`. Today the live-decision lookup (lines 30-39) and the
digest comparison (43-46) run before `with transaction.atomic():` (48); the CAS conditions
only on `status=approved`, and `OutboundSend` is written against the pre-fetched decision
and pre-computed digest. An undo → edit → re-approve cycle committing inside that window
binds a send to a voided decision with a stale digest.

**Red**: a test that interleaves the cycle deterministically — take the pre-transaction
snapshot, then void D1 / edit the copy / create D2 before entering the atomic block, and
assert `dispatch()` raises `DispatchBlocked` and writes no `OutboundSend`. The clean seam is
a hook the test patches between the check and the transaction (or a
`transaction.on_commit`-free helper the test can drive); pick whichever avoids threading —
the point is determinism, not real concurrency.

**Green**: inside the atomic block, after the CAS wins, re-select the live resolved
`approve_send` decision with `select_for_update()` and recompute the digest from a freshly
read `effective_copy`; raise `DispatchBlocked` on any mismatch so the rollback undoes the
CAS. Bind `OutboundSend.decision` to the re-read row.

**Acceptance**: new test green; the four pinned approval-gate tests
(`tests_agent_loop_approval_gate.py`) green unmodified; full backend suite at its current
count with 1 expected failure (the deliberate red-team marker); mypy and ruff clean.

**Blast radius**: `dispatch.py` only. Note for the reviewer: `dispatch()` has no production
caller yet, so this is hardening the gate before anything depends on it.

## Task 2 — MB2: let a terminal-but-unfinalized run be resumed

`project/app/services/agent/state.py` + `outreach.py` phase 2b. `NON_TERMINAL_STATUSES` is
`(pending, claimed, gathering, drafting)`, so `_claim_sync`'s UPDATE matches zero rows for a
run checkpointed `failed`/`exhausted`; that surfaces as `AgentClaimLost`, which phase 5
filters out of the rows to write. Nothing ever resets a terminal run. A lead whose run
failed before the crash therefore gets no `OutreachAction` row on this or any later resume.

**Red**: a resume test that checkpoints a run `failed` (and a second `exhausted`) with no
`OutreachAction` row, then resumes and asserts the lead comes back with a row rather than
vanishing. Model it on the existing crash-resume tests in `tests_agent_loop_assembly.py`.

**Green**: a new `state.py` helper called from phase 2b, before `load_prior_steps` — a
conditional UPDATE resetting to `STATUS_PENDING` every `AgentLeadRun` for **this**
`trace_run_id` whose status is `failed`/`exhausted` and which has no `OutreachAction` row.
Failed runs then genuinely retry; exhausted runs re-claim, find the budget already spent
(`steps_used` is counted from prior steps), and fall to the exhausted append at zero
provider calls.

**Do not** touch `_claim_sync`'s status filter: `tests_agent_loop_loop.py` pins that terminal
runs refuse claims and that a refused claim surfaces as `AgentClaimLost`. The reset lives one
layer up, so both pinned tests stay green.

**Decision to confirm before implementing** — for `failed` runs, reset-and-retry (proposed)
spends one lead's provider calls again on an operator-initiated resume, which is what resume
is for when the failure was a transient `LLMError`. The alternative is to finalize the failed
run into a `needs_human` row without re-running: zero calls, preserves the recorded failure,
but never recovers from a transient fault. `exhausted` converges to the same outcome either
way. **I propose reset-and-retry** — one mechanism for both statuses, and resume is explicit.
Say if you want the cheaper one.

**Acceptance**: new tests green; the two pinned claim tests and both crash-resume tests green
unmodified; `tests_planner_perf` untouched (the new query is inside the agent-enabled block);
full suite, mypy, ruff clean.

## Task 3 — MB3 + FU-A: validate resume where the mechanism lives

`project/app/views.py:41-49` gates resume only on `AgentLeadRun` rows existing — never on the
flag — while all resume machinery in `plan_outreach` sits inside `if runtime.agent_enabled:`.
Flag-off, a resume therefore re-runs every lead single-shot under the old run id, ignoring
every checkpointed step, and the trace endpoint then serves the crashed agent run's step log
as the provenance of a single-shot draft. FU-A is the same defect one layer down:
`plan_outreach` validates nothing at all, so a typo'd id mints fresh rows under it.

**Red**: two tests — flag-off + `resume_run_id` is rejected rather than re-planning (assert no
provider calls and no new rows), and `plan_outreach(resume_run_id=<unknown>)` raises the typed
error rather than running.

**Green**: raise a typed `UnknownRun` (and an agent-disabled error) inside `plan_outreach`;
`OutreachRunView` translates both to 400 with the existing `{"error": "unknown_run"}` envelope
plus a new code for the disabled case. The view keeps its early check or delegates — either
way validation now also exists at the mechanism.

**Acceptance**: new tests green; `tests_agent_loop_assembly.py:302-310` (the unknown-id 400)
and the `inspect.signature` pin green unmodified; flag-off query counts untouched; full suite,
mypy, ruff clean. Update `docs/contracts/agent-loop.md` with the new error case.

## Delivery

Three stacked PRs into `feat/agent-loop`, one per task, each red-first and well under the
~400-line guideline, in a fresh worktree cut from `origin/feat/agent-loop` (**not** the stale
local ref). Required checks apply to `feat/*`, so each is gated. When all three are green and
merged, PR 88 gets a final CI pass and the squash-merge — on your explicit go, per the
standing rule that merges wait for you.

Tasks are independent (different files, no shared seam), so they can also go out in parallel
if you prefer throughput over a linear stack.

## Follow-ups to file as tickets (not this PR)

From the triage: FU-B queue-view concurrency and audit (the largest — CAS the three queue
mutations, hoist the reviewer guard, extract the decision-recording service seam, lock down
the admin); FU-C adapter tool-call parsing (blank arguments, dropped entries, Claude's
tool_result message shape, plus the missing adapter tests that mask all three); FU-D loop
budget overshoot and the timeout-overwrites-done race (note: the naive per-call break is
unsound — see the triage); FU-E verifier grounding vs. tool facts, which is the explicit
scoped-out non-goal whose measured needs-human rate should gate any flag flip; FU-F exhaustion
mislabeled as a shape-gate failure; FU-G demo AE slots permanently in the past; FU-H sticky
trace error; plus the quality cleanups worth doing (S1, S2, S3R, S4, S7A, S8, R3, EF1-EF4).

FU-D's timeout race and FU-G are both one-liners and could ride along with Task 2 if you want
them in before the merge; everything else is genuinely post-merge.
