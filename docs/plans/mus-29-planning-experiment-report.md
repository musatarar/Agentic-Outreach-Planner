# MUS-29 Planning Experiment — Findings Report

**Question:** can a single Opus-max planning session, steered only by iterating the project
CLAUDE.md, produce a plan materially equivalent to a Fable multi-agent ("ultracode") gold
plan for the repo's largest ticket?

**Answer:** yes for everything you can name — architecture, ADR, scope, delivery, data
model, security posture all converged, and by the end Opus was *more* factually accurate
than gold twice. No for the unnamed tail: adversarial review kept finding real,
acceptance-relevant implementation defects one level finer each round, in both plans'
lineage. Planning guidance has a ceiling at "what you can articulate in advance"; the
layer below it belongs to adversarial review, not prompts.

## Method

- Two worktrees cut from `d8220fe`: `mus-29-fable` (gold) and `mus-29-opus` (candidate).
- **Gold**: 3 parallel codebase readers → 3-angle design panel (security-first,
  minimal-diff, state/ops-first) → judge (picked state/ops) → synthesis → adversarial
  critic (4 material fixes, incl. re-scoping Task 0 to the gate-retired master).
  952 lines.
- **Candidate**: fresh Opus-max agent per attempt, working only in its worktree,
  instructed to treat that worktree's CLAUDE.md as authoritative. After each failed
  round, an updater distilled the judge's gaps into a `## Planning discipline` CLAUDE.md
  section as *generalizable* principles (ticket-specific hints required: zero).
- **Judging**: a comparer rules material equivalence across 9 dimensions (ADR, delivery,
  data model, loop architecture, tools, crash-resume, approval gate, testing,
  scope/security), verifying disputed claims against the repo; any claimed match must
  survive an adversarial refuter. "Material" = would change code, schema, PRs, or tests.
- No execution of either plan has occurred.

## Round history

| Round | What ran | Gaps | Themes |
|---|---|---|---|
| 1 | Opus unaided | 7 | stale CI ground truth; non-native tool transport; default-path behavior change on merge; split trace/checkpoint stores; weak resume guarantees; edited a pinned test; missing calendar data source |
| 2 | attempt 2 under 48-line guidance | 2 | OneToOne relation semantics unhandled; missed the repo's delimiter-fencing injection idiom |
| 3 | attempt 3 | 4 | **non-monotonic re-roll**: closed round-2 gaps but regressed tool data access to live per-tool DB reads; reject-half as dead schema; no server-side arg binding; infeasible FE tests |
| 4 | attempt 4 | 1 | tool-derived facts unreconciled with the fail-closed verifier |
| 5 | attempt 5 | 2 | approval not byte-bound to content; **scope overcorrection** — shipped the verifier extension in-ticket where gold defers it (the guidance had allowed both; fixed with an explicit tiebreaker) |
| 6 | attempt 6 under full 15-principle guidance | compare: **match** → refute: 3 | per-step journal commits (dangling `tool_use` breaks resume on both wire formats); serializer `fields="__all__"` mass-assignment forges send approvals; read-then-act send/undo race |
| 7 | Opus patches its own plan (blind to gold) | compare: **match** → refute: 2 | stub's scripted tool-calls can't pass the `lru_cache`d factory (zero tool calls in real contexts; shared mutable state across 8 concurrent leads); resume's prior-state read violates the documented ORM-free phase contract |

Trajectory: 7 → 2 → 4 → 1 → 2 → 3 → 2. From round 6 on, the section-level judge cannot
distinguish the plans; only the refuter can, and each of its finds is a level narrower
than the last.

## What Opus did well

- **Full convergence on the categorical layer.** Same hand-rolled-over-LangGraph ADR
  with materially identical repo-specific rationale, same event-sourced
  trace-is-the-checkpoint data model, same four pure-function tools over a frozen
  snapshot, same provider-native tool-calling across every adapter including the stub,
  same testing seams, same injection posture.
- **Beat gold on facts, twice** (round 7 comparer, repo-verified): gold plans to add a
  `run_id` parameter that `genai.run_span` already accepts, and gold's delivery
  verification would halt on the committed conflict markers in `origin/master`'s
  CLAUDE.md that Opus's chore PR already grounds.
- **Honest self-scoping.** Named its non-goals with follow-up mechanisms, stated
  at-most-once (not exactly-once) send as an explicit risk trade-off, and during the
  round-7 patch flagged a pre-existing bug in its own plan and correctly left it out of
  scope.
- **Compliant plan mechanics from round 1**: template adherence, real test code, no
  placeholders, exact paths — the misses were always substance, never format.

## What steering fixed — and its two failure modes

All 15 principles held once written; no principle, once named, was violated in a later
round. The final artifact is the 140-line `## Planning discipline` section in the opus
worktree's CLAUDE.md (uncommitted diff). Its themes: verify origin ground truth; extend
abstractions rather than route around them; default-off flags; existing-test edits are
design smells; one append-only log for trace+checkpoint; explicit resume with
single-winner claims; relation-cardinality audits before "additive" migrations; the full
untrusted-text pipeline for new channels; fail-closed gate reconciliation (with
tiebreaker); phase invariants as constraints; both decision outcomes reachable and
tested; server-side arg binding; verify the test toolchain; record-like data in migrated
models; approvals authorize exact bytes.

Two failure modes showed up and are worth remembering:

1. **Re-roll variance (round 3).** A fresh attempt re-decides *everything*, not just the
   flagged items — it closed both named gaps and regressed an unnamed dimension.
   Guidance must pin architecture invariants, not just correct misses.
2. **Ambiguity resolves the wrong way (round 5).** The gate-reconciliation principle
   allowed "extend the gate" or "defer it"; Opus heard *address X* as *build X* on a
   ticket whose own notes warned against scope. Principles that permit two positions
   need an explicit tiebreaker.

## What steering could not fix

The refuter's finds at rounds 6–7 were genuinely material — three of them violated
acceptance criteria outright — but they live at a depth prose guidance doesn't reach
reliably: transaction boundaries between two appends, a serializer's field-exposure
surface, check-then-act windows, factory-signature compatibility, a single read's
phase-contract conformance. Fixing them via feedback worked perfectly (the round-7 patch
was thorough and consistent), but each review pass then found new defects one level
down. The tail did not run dry in seven rounds and plausibly never does at this plan
size — the gold plan's own lineage needed a critic pass and still carried two factual
errors.

## Recommendations

1. **Adopt a curated Planning discipline section into the real CLAUDE.md.** Most of the
   15 principles are repo-general, cheap to keep, and empirically effective. Trim the
   two most MUS-29-flavored if desired.
2. **Institutionalize the adversarial plan review.** The single highest-value component
   of this experiment was the refuter: max-effort, primed with failure classes
   (atomicity, races, serializer surface, factory/DI constructibility, phase contracts),
   verifying claims against the repo. Run it on any large plan — including
   Fable-authored ones — before execution. A `plan-review` skill wrapping that prompt
   would capture it.
3. **Pin invariants when steering a re-roll.** When re-running a planner after feedback,
   restate the architecture invariants that must survive, not only the deltas.
4. **Treat compare-level equivalence as necessary, never sufficient.** The section-level
   judge passed plans that still contained approval-gate bypasses. Match verdicts
   without an adversarial pass are unsafe for consequential work.
5. **Repo hygiene item (independent):** `origin/master`'s CLAUDE.md carries committed
   merge-conflict markers from `843c24d` — needs a small fix PR.

## Open items

- Two unpatched refute-r7 gaps in the Opus plan (stub/factory design; phase-contract
  read), documented above — chase only if the Opus plan is ever executed.
- Gold's two factual errors (run_span parameter; halting delivery check) are uncorrected
  in the fable worktree plan.
- Execution of either plan: deliberately not started; a separate decision.
- Worktrees remain at `d8220fe`; both plans specify rebase onto `origin/master` as
  Task 0.

## Operational notes (orchestration lessons from this session)

- The Workflow `args` channel silently dropped values twice (ticket text, then worktree
  paths); hardcoding all values in the script body was the reliable fix.
- macOS purged the `/tmp` scratchpad mid-experiment (multi-day session); durable
  artifacts belong under the session directory or the repo. Round plans remain
  reconstructable from agent transcripts and workflow journals.
- Resume-from-cache after an API outage worked exactly as designed (the 756k-token
  attempt 6 replayed free); mid-response connection drops cost only the dead agent.
- Rough scale: 7 workflow runs, ~35 agents, ~4M subagent tokens across the experiment.

## Artifact index

- Gold plan: `.claude/worktrees/mus-29-fable/docs/plans/mus-29-plan.md` (952 lines)
- Opus plan (post-patch): `.claude/worktrees/mus-29-opus/docs/plans/mus-29-plan.md` (2,488 lines)
- Steering artifact: `.claude/worktrees/mus-29-opus/CLAUDE.md` (+140 lines, uncommitted)
- Round snapshots: session dir `mus29-rounds/` (rounds 5–6; 1–4 reconstructable from transcripts)
- Verdicts with full gap detail: workflow journals `wf_de57452e`, `wf_6365fe61`, `wf_c2cf8cba`, `wf_ce410978`
- Checkpoint log: Linear MUS-29 comments (5 checkpoints)
