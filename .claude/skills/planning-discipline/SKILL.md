---
name: planning-discipline
description: Use when writing, reviewing, or steering an implementation plan for this repository — a ticket plan, multi-PR feature breakdown, ADR or design decision, or task list — before any code is written. Also use when judging whether an existing plan is executable as written, and whenever a plan touches state/resume logic, approval gates, LLM tool-calling, migrations, or new untrusted-text channels.
---

# Planning Discipline

## Overview

Rules for writing implementation plans in this repo. A plan that violates these is wrong
even if it is internally coherent: most planning failures here are not incoherence but
contradiction with repo ground truth — assuming state instead of verifying it, routing
around an abstraction instead of extending it, or designing a mechanism whose guarantees
don't survive a crash, a race, or an attacker-supplied string.

Each rule below was distilled from an observed planning failure during the MUS-29
dual-planner experiment (findings: `docs/plans/mus-29-planning-experiment-report.md`,
checkpoint log on Linear MUS-29). No rule, once written, was violated in later planning
rounds — but rules only cover what they name. These converge the categorical decisions;
they do not replace adversarial review of a finished plan for implementation-depth
defects (transaction boundaries, serializer surfaces, check-then-act races).

## Ground truth

- **Plan against origin, not your worktree.** Before deciding delivery order, `git fetch`
  and inspect current `origin/master` — recent log plus the exact files your plan depends
  on. The worktree's checked-out base is often stale and is never evidence of repo state.
  If the plan would fix repo/CI infrastructure, first check whether that fix already
  landed upstream; if it did, Task 0 is a rebase onto `origin/master` plus verification,
  not a remediation PR that duplicates merged work.
- **Verify the actual test toolchain before planning any tests.** Check what runner the
  repo really uses — `package.json` scripts, CI workflow steps, and at least one existing
  test file cited by real path as the pattern to follow — and write new tests inside that
  established runner (the frontend here uses `node --test` with `frontend/tests/*.test.ts`;
  there is no Vitest, jsdom, or Testing Library). Never attribute a conventional stack to
  the repo from memory: a plan citing test files or frameworks that don't exist cannot be
  executed as written. Introducing a new test framework is its own explicitly scoped
  decision (dependencies + CI changes), never an implicit side effect of a feature plan.

## Architecture & scope

- **Extend abstractions; don't route around them.** When a feature needs a capability the
  existing abstraction layer lacks (e.g. structured tool-calling on the provider-agnostic
  LLM layer), the plan's scope is to add that capability to the abstraction itself — a
  typed interface implemented in **every** configured adapter plus the test stub, using
  each provider's native structured API. Never invent a bespoke text protocol over the
  existing plain-string seam as a workaround, and never declare the missing capability a
  non-goal when it is the mechanism the ticket asks for.
- **Behavioral rewrites land behind a default-off flag.** When replacing or wrapping an
  existing core path, gate the new path behind a settings flag that defaults to off, so
  the merged default path — its behavior, persistence, query counts, mock seams, and
  every test pinned to them — stays byte-identical to today. Flag-off runs must create
  zero new rows and issue zero new queries. Flipping the default on is a separate, later
  decision with its own evidence, never part of the initial plan.
- **A new fact source upstream of a fail-closed output gate must be reconciled with
  that gate.** When the plan introduces a new source of facts feeding a generator —
  tool results, retrieved documents, computed lookups — and the generator's output
  already passes through a deterministic gate that fails closed against a fixed
  grounding corpus (e.g. `services/verify.py` checking figures/dates against the lead
  record, with approval blocked while anything is unverified), the plan must state
  explicitly how *legitimate* content derived from the new source fares at that gate.
  "The old gate still runs" is not defense-in-depth when the new source exists
  precisely to surface facts the gate's corpus lacks — that is the gate systematically
  rejecting the feature's headline output, i.e. unplanned degradation. Two acceptable
  positions, chosen explicitly: (a) extend the gate's corpus to cover the new source,
  reading the *persisted, sanitized* source data (e.g. stored per-step tool results),
  never the model's restatement of it; or (b) scope the extension out as a named
  non-goal with that same follow-up specified, and mitigate in code — constrain new
  first-party fact surfaces to restate only facts the gate's corpus already asserts,
  instruct the generator to ground checkable claims (figures, dates) in the gate's
  corpus, list the fail-closed spike as a risk, and make the *measured* fail-closed /
  needs-human rate an explicit input to the flag-flip or rollout decision (see the
  default-off-flag rule above — this is part of that "separate decision's evidence").
  Tiebreaker: on a ticket explicitly flagged as scope-risky, or when the acceptance
  criteria do not require the gate to pass the new facts, choose (b). Plan only to the
  acceptance criteria — never extend a fail-closed safety gate in the same ticket as
  the feature that stresses it.
- **Documented phase/layer invariants are design constraints, not obstacles.** When a
  module documents an invariant about a phase or layer (e.g. "the async/concurrent phase
  holds no ORM access and is never handed model objects" — see the phase contract spelled
  out in `services/outreach.py`), the plan must design *within* it: bulk-prefetch
  everything the constrained phase will need into an immutable, plain-data snapshot
  (a frozen context object built in the preceding synchronous phase) and hand tools that
  snapshot, so tools are pure functions over it. Threading DB reads into the constrained
  phase via `sync_to_async` per call is a violation, not a technique — it also silently
  escalates the whole surface's test base class (TestCase → TransactionTestCase). New DB
  access inside such a phase is acceptable only for the single narrowest *write* seam the
  design explicitly sanctions (e.g. a checkpoint append), never for reads a prior phase
  could have batched.

## State, crash-resume & migrations

- **Audit trail + crash-resume = one append-only event log.** When a feature needs both
  an inspectable trace and restartable state, persist a single append-only log of
  per-step rows (unique `(parent, seq)`, committed as a write-ahead unit per external
  call) and derive current state as a fold over it. Do not design a mutable
  snapshot/JSON-blob checkpoint alongside a trace: two artifacts can drift, and
  batch-boundary snapshots lose everything in-flight since the last barrier.
- **Crash recovery is an explicit resume, not a staleness heuristic.** Expose resume as
  an operation keyed by the durable run id (parameter/endpoint), replaying finished work
  from the log at zero external calls. Serialize concurrent resumers with a
  per-work-unit single-winner claim — conditional UPDATE, `rowcount == 1` (the existing
  `LoginToken` single-use pattern) — and test the two-claimer race. Time-based
  stale-run adoption both delays recovery and leaves a duplicate-work window.
- **"Additive-only" is not a virtue when the old schema encoded an assumption.** Before
  extending an existing model to store a new category of row against the same parent,
  read the relation's actual cardinality and uniqueness in `models.py` (OneToOne? unique
  constraint? unique_together?) and grep every reader of that relation. A OneToOne or
  uniqueness rule that encoded the old one-purpose-per-parent semantics must be relaxed
  in the same migration — FK plus per-category conditional `UniqueConstraint`s (and a
  void/supersede lifecycle if a category can be redone) — and every reader that assumed
  one-row-per-parent must be updated to filter by category, or new-category rows will
  silently corrupt old queries. The plan must include a test that creates both the old
  and the new category against a single parent; if that test would raise
  IntegrityError, the "additive" migration was actually breaking.

## Human decisions & approval gates

- **Both outcomes of a human decision must be reachable, recorded, and tested.** If the
  ticket asks to record a decision with two outcomes (approve AND reject, accept AND
  dismiss), map *each* outcome to a concrete existing user action whose view writes an
  auditable row (actor from the session + timestamp) inside that action's existing
  transaction, keep undo/void symmetric across both, and test each end-to-end through
  its endpoint. An enum value or schema column that no code path can ever create is
  unshipped scope masquerading as a data-model detail — if the plan cannot name the view
  that writes the row, the outcome is not planned.
- **A human approval authorizes exact bytes, not an entity.** When an approval gates a
  consequential action on specific content (an outbound send, a publish), bind the
  decision to the content itself: persist a content hash (sha256 of the effective copy)
  on the decision row in the same transaction as the status flip, re-compute and compare
  it at execution time, and fail closed on any mismatch. Make execution a terminal
  one-shot per-entity state transition (conditional-UPDATE CAS into a sent-style state
  with no undo past it), backed by a durable unique-per-entity execution record as the
  double-execution backstop. Re-running upstream validators at execution time checks
  groundedness, not what the human approved — it is never a substitute: the gate must
  prove the thing executed is byte-identical to the thing approved. Include a test that
  tampers with the content after approval and asserts execution hard-fails.

## Untrusted input

- **New third-party-text channels inherit the full untrusted-input pipeline.** Any new
  path that feeds attacker-reachable text to the model — tool results, retrieved
  documents, external API payloads — gets the repo's *complete* existing treatment, not
  a subset: sanitization, length caps, **and** the delimiter fencing / standing-
  instruction wrapper the current prompts use (`wrap_untrusted()` and the
  `UNTRUSTED_*` markers in `services/llm/sanitize.py`), applied exactly once at the
  point the text enters the conversation and never in the instruction region. Claiming
  an existing prompt's fencing "covers" the new channel is wrong unless that channel's
  bytes actually flow through the wrapper. The plan must include a red-team test
  asserting a planted injection payload surfaces in the final message list redacted
  *and* inside the untrusted markers — sanitization tests alone do not prove fencing.
- **The model never supplies entity-identifying or scope-widening tool arguments.** Bind
  the acting entity (lead, user, account) server-side from the run context when
  constructing tool executors; keep identifiers out of the tool's argument schema
  entirely where possible, and where a schema must accept extra keys, drop unknown ones
  and ignore any model-supplied identifier rather than validating and honoring it.
  Model-chosen arguments are attacker-reachable via injected CRM text, so the plan must
  include a test proving a foreign identifier in tool arguments cannot change what data
  the tool returns (no cross-entity read widening).

## Tests & data

- **Editing an existing test is a design smell, not a task.** If the plan requires
  changing any existing test (including pinned query/perf ledgers), first redesign so it
  doesn't — flag-gating off the default path usually achieves this. Only if genuinely
  unavoidable: isolate the edit in its own commit, justify every changed line, and get
  explicit authorization before writing it into the plan as sanctioned.
- **Record-like synthetic data lives in a migrated model, seeded via the demo pipeline.**
  If a tool or feature reads per-entity state (availability slots, schedules, inventory),
  back it with a real migrated model seeded idempotently through
  `scripts/populate_demo_data.py` — the single source of demo truth — so operators can
  edit it as data. Reserve computed-on-the-fly values and in-code constants for genuinely
  static first-party facts (e.g. a product catalog).

## When steering or re-running a planner

Two failure modes observed even with these rules present:

- **Re-roll variance**: a fresh planning attempt re-decides *everything*, not just the
  items feedback named — it can close the flagged gaps and regress an unflagged
  dimension. When re-running a planner after feedback, restate the architecture
  invariants that must survive, not only the deltas.
- **Ambiguity resolves the wrong way**: a rule that permits two positions (extend the
  gate vs defer it) will sometimes get the costlier one under scope pressure. Rules
  here carry explicit tiebreakers; if you add a rule that allows alternatives, add the
  tiebreaker too.
