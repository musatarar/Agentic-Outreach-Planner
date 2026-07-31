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

## The enforced workflow: contract → skeleton → mini PRs

Multi-component features are built as disconnected services against a frozen contract.
**Every rule below is machine-enforced** by the `workflow-gate` required check
(`.github/workflows/workflow-gate.yml` → `scripts/check_scope.py`) plus GitHub rulesets on
`master` and `feat/*` with no bypass actors — a violating PR cannot merge, and direct
pushes to protected branches are rejected. `docs/ci.md` documents the enforcement setup.

### Branch naming (manual — never let Linear auto-name these)

| Branch | Role |
|---|---|
| `feat/<x>` | feature integration branch, cut from `master` |
| `feat/<x>--skeleton` | skeleton PR head (base `feat/<x>`) |
| `feat/<x>--<component>` | mini PR head; `<component>` must equal a file-map key verbatim |
| `feat/<x>--contract*` | contract-change PR head (reserved prefix) |
| `meta/**` | gate-infrastructure PRs to `master` (the only class allowed to touch protected paths) |

All workflow branches are single-level (`--` separator, never `/`). A head under a
`feat/<x>` base that doesn't follow this scheme is an explicit gate failure, and stacked
PRs (base = `feat/<x>--something`) are rejected. Branches outside this scheme (e.g.
Linear's `musansht/mus-NN-*`) classify as Normal PRs: the gate no-ops, ordinary CI still
applies.

### Phase 0 — contract

Write `docs/contracts/<x>.md` from `docs/contracts/TEMPLATE.md`. It must contain the
required sections and the fenced `# file-map` YAML block: every owned file in exactly one
component, an `assembly` component, `shared` files owned by nobody. The gate lints it
(`python scripts/check_scope.py --validate-contract docs/contracts/<x>.md`).

### Phase 1 — skeleton PR (`feat/<x>--skeleton` → `feat/<x>`)

Commit the contract, stub files raising `NotImplementedError`, and real failing tests for
every component behind `@unittest.expectedFailure`. Scope check is skipped (skeletons may
create anything non-protected); markers may be added. The **red-proof gate** proves each
marked test fails for the right reason (AssertionError or NotImplementedError once its
marker is stripped) — do not strip markers manually; the gate does it and posts a
module → red-count table in the job log.

### Phase 2 — mini PRs (`feat/<x>--<component>` → `feat/<x>`)

One PR per component. The gate enforces: diff ⊆ the component's `files` + `tests`; no
shared file, no protected path; the component's own marker count is **zero** after the PR;
every other test module's marker count is **unchanged** (sweep over all
`project/app/tests*.py`); the scoped suite passes for real
(`python manage.py test project.app.<tests_module>`). Self-check before pushing:

```bash
python scripts/check_scope.py --base origin/feat/<x> --component <component>
```

Renaming or deleting a mapped test module fails the gate ("file map out of date") — update
the map via a contract-change PR instead. Contract-change PRs (`feat/<x>--contract*`) may
touch only the contract and test modules, and may add markers only alongside a contract
diff.

### Phase 3 — landing (`feat/<x>` → `master`)

Zero markers across the feature's test modules, contract present and clean, rules-eval
baseline holds, full CI green. On feature branches the undo path is a corrected mini PR
for the same component — GitHub revert PRs only work on `master` (their generated branch
names fail workflow naming, and re-adding markers violates mini accounting).

### Invariants (all machine-checked unless marked review-only)

- An instruction is a hope; a gate is a fact. `ci-ok` + `workflow-gate` are required
  checks on `master` and `feat/*`; there are no bypass actors.
- Protected paths (hardcoded in `scripts/check_scope.py`, not in any contract):
  `.github/workflows/**`, `scripts/check_scope.py`, `scripts/red_proof.py`,
  `scripts/tests/**`, `CLAUDE.md`, `docs/contracts/TEMPLATE.md`, `docs/ci.md`,
  `docs/rulesets/**`, `evals/golden/**`, `evals/baselines/**`, `evals/run_rules_eval.py`.
  They change only via `meta/**` → `master` PRs (normal CI still applies to those).
- Marker detection is AST-based (both `@unittest.expectedFailure` and bare
  `@expectedFailure`), immune to comments and strings.
- Review-only (cannot be machine-checked, so reviewers own it): contract prose being
  meaningful (lint checks section presence, not quality); tests-before-implementation
  commit ordering; a marked test failing for a plausible-but-wrong reason.

## Testing & database

- **Fixtures, not live edits**: tests use `setUpTestData()`/fixtures. Never modify
  `db.sqlite3` directly.
- `python scripts/populate_demo_data.py` is the single source of truth for demo state.
- `python manage.py test` builds a fresh test database every run; the suite mocks every
  LLM provider call. Real-provider evals are separate, gated workflows (never on push/PR).

## Git workflow

- **Always use git worktrees** — one per branch/task (`.claude/worktrees/<name>`), so
  parallel agents never collide. Never switch branches in the main checkout.
- **One PR per ticket**; track work in Linear (`to-issues` skill converts plans). Workflow
  branches above are named manually; Linear-generated branches are for Normal PRs only.
- CI (`.github/workflows/ci.yml`) runs lint, mypy, frontend, the backend matrix
  (py3.12/3.13 × sqlite/postgres), the rules eval, and the gate's own tests on every push
  and PR; `ci-ok` summarizes them under one required name.
