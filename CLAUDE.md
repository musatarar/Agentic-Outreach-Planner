# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this
repository.

## Context

**Locked In — Agentic Outreach Planner**: a Django 4.2 + DRF backend with a committed
React/TS frontend bundle. Rules decide which leads need outreach (deterministic Python in
`project/app/services/outreach.py`); a provider-agnostic LLM layer
(`project/app/services/llm/`, selected via `config.toml`) only writes copy; a deterministic
verifier (`services/verify.py`) grounds generated copy against the record and fails closed.
See `README.md` for the product tour and `SECURITY.md` for the injection-hardening posture.

## Commands

Set up once from a clean clone (Python 3.12+):

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt   # runtime deps + ruff, mypy, coverage, pyyaml
cp .env.example .env                  # DJANGO_SECRET_KEY is required
```

With the venv active:

```bash
python manage.py runserver            # dev server (http://127.0.0.1:8000)
python manage.py migrate              # apply migrations
python scripts/populate_demo_data.py  # seed the demo pipeline (single source of demo state)
python manage.py test project.app     # full backend suite
python manage.py test project.app.tests_logic.SomeCase.test_name  # single test
ruff check . && ruff format --check . # lint + format check
mypy project/app/services/            # type check (CI runs exactly this target)
python evals/run_rules_eval.py        # rules regression eval vs committed baseline
python -m unittest discover -s scripts/tests   # the workflow gate's own test suite
```

Frontend source lives in `frontend/`; the built bundle is committed to
`project/app/static/frontend/` (CI fails if it goes stale — run `npm run build` and commit
the bundle after frontend changes). Database defaults to SQLite; `DATABASE_URL` switches to
Postgres; `docker compose up` runs the full stack.

## The enforced workflow: Linear-driven skeleton → component PRs

Multi-component features are decomposed **in Linear**, not in the repo: one self-contained
sub-issue per component, no plan files and no contract documents. Linear owns the plan;
the repo owns the proof. A skeleton PR plants real failing tests per component, and each
component PR must take exactly its own tests green while every other test file's red count
stays frozen. Those rules are machine-enforced by the `workflow-gate` required check
(`.github/workflows/workflow-gate.yml` → `scripts/check_scope.py`) plus GitHub rulesets on
`master` and `feat/*` with no bypass actors — a violating PR cannot merge, and direct
pushes to protected branches are rejected. `docs/ci.md` documents the enforcement setup.

### Operating rules (behavioral — not machine-enforceable, so they bind Claude directly)

- **Claude never merges a PR — ever, and absolutely never into `master`.** Claude opens
  the PR, reports the link, and stops. Musa reviews, merges, and says "continue".
- **Stop and wait at every phase boundary**: after decomposition, after the skeleton PR,
  after *each* component PR, after the integration review.
- **Every checkpoint is a Linear comment** on the relevant issue — at each completed
  step/todo boundary and on every stop. It states what changed, the verification evidence,
  the commit ids, and the exact next step, self-contained enough to resume from with zero
  chat context. A checkpoint that exists only in chat is not a checkpoint.

The two halves of that last rule are skills committed to this repo, so a clean clone
carries them and neither side of the loop depends on a personal setup:

| Skill | Use it when |
|---|---|
| [`.claude/skills/checkpointing-work`](.claude/skills/checkpointing-work/SKILL.md) | **Writing** a checkpoint — a step finished, you are stopping for review or a merge, context is about to compact, or something was deliberately left undone. It pins the six required slots (headline + SHA, what changed, evidence with real numbers, deliberate leftovers, repo coordinates, exact next step) and the Linear WAF workarounds. |
| [`.claude/skills/resuming-from-checkpoint`](.claude/skills/resuming-from-checkpoint/SKILL.md) | **Reading** one — picking up tracked work with no chat context: a bare ticket id or URL, "continue", "restart from step N", or the first turn after a compaction. Ticket, then newest checkpoint **ordered by `createdAt`, not `updatedAt`**, then verify against the repo before touching anything. |

Invoke the skill rather than working from memory of it. The failure they exist to prevent
is asymmetric: a missing checkpoint costs the next agent a full re-exploration, and a
checkpoint read in the wrong order resumes from the wrong step.

This closes the loop across the phases below. Every **STOP** marker means the same two
things in order: post the checkpoint, then wait — do not open the next phase's PR, and do
not merge. Every "continue" that arrives without chat context starts the other way round:
resume from the checkpoint first, verify the coordinates against the repo, *then* work.

### Branch naming (manual — never let Linear auto-name these)

| Branch | Role |
|---|---|
| `feat/<x>` | feature integration branch, cut from `master`; name it `feat/mus-<N>` |
| `feat/<x>--skeleton` | skeleton PR head (base `feat/<x>`); repeatable mid-feature |
| `feat/<x>--<component>` | component PR head; `<component>` matches `[a-z0-9_]+` and names the test artifacts |
| `meta/**` | gate-infrastructure PRs to `master` (the only class allowed to touch protected paths) |

All workflow branches are single-level (`--` separator, never `/`). A head under a
`feat/<x>` base that doesn't follow this scheme is an explicit gate failure, and stacked
PRs (base = `feat/<x>--something`) are rejected. Branches outside this scheme (e.g.
Linear's `musansht/mus-NN-*`) classify as Normal PRs: the gate no-ops, ordinary CI still
applies.

`feat/mus-<N>` is a recommendation with teeth. The feature slug `<fslug>` (the feature name
lowercased, dashes → underscores) prefixes every test artifact, so a semantic name like
`feat/llm` with a `retry` component would address `tests_llm_retry.py` — a path that could
collide with a real product module. A ticket-numbered feature name makes that impossible.

### Phase 1 — decompose in Linear

One sub-issue per component, written so an agent with zero other context can execute it.
Required template:

```markdown
**Title**: <feature>: <component> — <one-line deliverable>
**Branch**: `feat/<x>--<component>`
**Test artifacts**: `project/app/tests_<fslug>_<component>.py` and/or
`frontend/tests/<fslug>_<component>.test.ts`

## Context
What exists today, which files matter, why this component is needed. No "see the parent
issue" — restate it.

## Deliverable
What to build, and an explicit **out of scope** list.

## Passing criteria
The behavior the component's tests assert, concretely enough to write them from.

## Edge cases
Failure modes, boundaries, and what must keep working.

## Considerations
Ordering constraints, gotchas, prior decisions that apply.
```

Then **STOP** — Musa reviews the decomposition before any code.

### Phase 2 — skeleton PR (`feat/<x>--skeleton` → `feat/<x>`)

Stubs raising `NotImplementedError` plus real failing tests for every component:

- **BE**: `project/app/tests_<fslug>_<component>.py`, tests behind `@unittest.expectedFailure`.
- **FE**: `frontend/tests/<fslug>_<component>.test.ts`, tests declared as node:test
  `todo`. Todo failures don't fail `npm test`, so `ci-ok` stays green while the feature
  is red.

The gate requires the skeleton to move the total red count (markers + FE todos) by at
least one — `|delta| ≥ 1` across both stacks, base ∪ head. It is deliberately *not* "some
markers exist": `master` carries a permanent, documented `@unittest.expectedFailure` in
`project/app/tests_redteam.py` (a KNOWN GAP), so only a *change* proves a skeleton. A
marker-neutral stub fix rides a component PR instead. The skeleton branch is repeatable
mid-feature when a later component needs its tests planted.

The **red-proof gate** then proves each BE marked test fails for the right reason
(AssertionError or NotImplementedError once its marker is stripped) and posts a
module → red-count table in the job log — do not strip markers manually. No markers
anywhere is a pass no-op (an FE-only skeleton has nothing here to prove). Then **STOP** —
Musa reviews and merges.

### Phase 3 — component PRs (`feat/<x>--<component>` → `feat/<x>`), sequential

One PR per sub-issue, one at a time. The gate enforces:

- no protected path in the diff;
- the component's artifacts **exist at the base ref** (a PR cannot self-create the
  artifact it is measured against) and still exist at head — renaming or deleting one
  fails;
- its **own** BE markers → 0 and its **own** FE todos → 0;
- every *other* test file's red count is unchanged, swept across both stacks
  (`project/app/tests*.py` and `frontend/tests/*.test.ts`);
- the scoped BE suite passes for real (`python manage.py test project.app.<tests_module>`),
  run only when the BE artifact exists. FE has no scoped step by design — `npm test` under
  `ci-ok` is the FE real-run enforcement.

Markers > 0 at base is *not* required, so a repeat PR for the same component — the fix and
undo path on a feature branch — stays legal. Self-check before pushing:

```bash
python scripts/check_scope.py --base origin/feat/<x> --component <component>
```

Then **STOP** after each component PR — Musa merges and says "continue".

### Phase 4 — integration review and landing (`feat/<x>` → `master`)

Ask first, then spin a **fresh review agent** with no session context over the full feature
diff; findings are fixed via ordinary component PRs, never by a direct push. The landing
gate applies a per-file delta rule — red count at head ≤ red count at base, over every
`project/app/tests*.py` and `frontend/tests/*.test.ts` — so the feature can never land
carrying more red than `master` already had, while the redteam baseline is tolerated. The
rules-eval baseline must hold and full CI must be green. A stale branch whose base moved
may need "Update branch" before the delta reads true. **Musa merges the feature to
`master`. Claude does not.**

### Invariants (all machine-checked unless marked review-only)

- An instruction is a hope; a gate is a fact. `ci-ok` + `workflow-gate` are required
  checks on `master` and `feat/*`; there are no bypass actors.
- Protected paths (hardcoded in `scripts/check_scope.py`): `.github/workflows/**`,
  `scripts/check_scope.py`, `scripts/red_proof.py`, `scripts/tests/**`, `CLAUDE.md`,
  `docs/ci.md`, `docs/rulesets/**`, `.claude/skills/**`, `evals/golden/**`,
  `evals/baselines/**`, `evals/run_rules_eval.py`. They change only via `meta/**` →
  `master` PRs (normal CI still applies to those). The skills are in that list for the
  same reason `CLAUDE.md` is — they encode the workflow's own stop-and-resume protocol, so
  a component PR must not be able to rewrite the rules it is being held to. Note the scope:
  `.claude/skills/**`, not `.claude/**`, so `.claude/settings.json` stays freely editable.
- Marker detection is AST-based (both `@unittest.expectedFailure` and bare
  `@expectedFailure`), immune to comments and strings. FE todo counting is textual (it
  must work on `git show` blobs) and deliberately over-approximate — a todo-looking token
  in a comment counts and fails loudly, because undercounting is the dangerous direction.
- All red accounting is delta-based, never absolute zero, so the permanent redteam
  expected-failure never blocks a landing.
- Review-only — cannot be machine-checked, so reviewers own it:
  - **File scope.** A component PR may touch any non-protected file; there is no file map
    any more. Mitigations are structural, not mechanical: sequential components, a human
    merging every PR, and sibling test counts frozen by the gate.
  - **FE fail-reason.** node:test has no "unexpected pass" for a todo test, so a vacuous
    FE skeleton test is caught only by review (BE fail-reason is proved by red-proof).
  - **Skeleton honesty.** Markers must be stripped by red-proof, not by hand, and a BE
    test that fails for a plausible-but-wrong reason still passes red-proof.
  - Tests-before-implementation commit ordering, and whether a Linear sub-issue is really
    self-contained.

## Testing & database

- **Fixtures, not live edits**: tests use `setUpTestData()`/fixtures. Never modify
  `db.sqlite3` directly.
- `python scripts/populate_demo_data.py` is the single source of truth for demo state.
- `python manage.py test` builds a fresh test database every run; the suite mocks every
  LLM provider call. Real-provider evals are separate, gated workflows (never on push/PR).

## Git workflow

- **Always use git worktrees** — one per branch/task (`.claude/worktrees/<name>`), so
  parallel agents never collide. Never switch branches in the main checkout.
- **One PR per sub-issue**; track work in Linear, which owns the plan (`to-issues` skill
  converts plans into issues). Workflow branches above are named manually; Linear-generated
  branches are for Normal PRs only.
- CI (`.github/workflows/ci.yml`) runs lint, mypy, frontend, the backend matrix
  (py3.12/3.13 × sqlite/postgres), the rules eval, and the gate's own tests on every push
  and PR; `ci-ok` summarizes them under one required name.
