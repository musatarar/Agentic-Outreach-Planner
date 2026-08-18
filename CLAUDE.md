# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

## Context

**Locked In — Agentic Outreach Planner**: Django 4.2 + DRF backend with a committed
React/TS frontend bundle. Deterministic rules decide which leads need outreach
(`project/app/services/outreach.py`); a provider-agnostic LLM layer
(`project/app/services/llm/`, selected via `config.toml`) only writes copy; a
deterministic verifier (`services/verify.py`) grounds generated copy against the record
and fails closed. See `README.md` for the product tour and `SECURITY.md` for the
injection-hardening posture.

## Commands

Setup from a clean clone (Python 3.12+):

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env                  # DJANGO_SECRET_KEY is required
```

Day to day:

```bash
python manage.py runserver            # dev server (http://127.0.0.1:8000)
python manage.py migrate
python scripts/populate_demo_data.py  # single source of demo state
python manage.py test project.app     # full backend suite
python manage.py test project.app.tests.tests_logic.SomeCase.test_name  # single test
ruff check . && ruff format --check .
mypy project/app/services/            # CI runs exactly this target
python evals/run_rules_eval.py        # rules regression vs committed baseline
```

Frontend source lives in `frontend/`; the built bundle is committed to
`project/app/static/frontend/`. After frontend changes: `npm run build` and commit the
bundle (CI fails if it goes stale). SQLite by default; `DATABASE_URL` switches to
Postgres; `docker compose up` runs the full stack.

## Workflow

An instruction is a hope; a gate is a fact. Every rule below is marked **[gate]**
(machine-enforced) or **[convention]** (discipline + review) — don't confuse the two.

1. **Green CI or no merge** [gate]. 
2. **Red first** [convention]. The first commit of a PR is failing tests that specify the
   behavior; implementation comes after. Paste the failing test output into the PR
   description as the receipt.
3. **One ticket, one PR** [convention]. Break work into Linear tickets before writing
   code (`to-issues` skill converts plans). Keep PRs to one concern, roughly ≤400 changed
   lines — split rather than grow.
4. **Big features get an integration branch** [convention]. Cut `feat/<x>` from
   `master`, stack small PRs into it, land it when green. Required checks apply on
   `feat/*` too, so the branch can't rot.
5. **Squash merge only** [gate — repo setting]. One PR = one commit on `master`;
   `git revert` is the undo path.
6. **Plans follow the planning discipline** [convention]. Before writing any
   implementation plan (ticket plan, multi-PR breakdown, ADR), invoke the
   `planning-discipline` skill and hold the plan to it.

## Comments & docstrings

- **Verbosity != clarity** [convention]. Assume the reader understands the code
  generally. Module/class docstrings state purpose in 1–3 lines; test docstrings pin
  the behavior in one line; non-obvious or security-critical facts survive as
  compressed one-liners (point at SECURITY.md rather than re-arguing it). No design
  history, no alternatives considered, no restating what the code shows.

## Testing & database

- **Fixtures, not live edits**: tests use `setUpTestData()`/fixtures. Never modify
  `db.sqlite3` directly.
- `python scripts/populate_demo_data.py` is the single source of truth for demo state.
- `python manage.py test` builds a fresh test database every run and mocks every LLM
  provider call. Real-provider evals are separate, manually gated workflows — never on
  push/PR.
- **Existing tests are pinned**: needing to edit one to land a change is a design smell —
  redesign (usually flag-gating) or get explicit authorization first (details in the
  `planning-discipline` skill).
- **Frontend testing uses the `webapp-testing` skill**

## Git

- One worktree per branch/task (`.claude/worktrees/<name>`); never switch branches in
  the main checkout. Merge conflicts are the collision detector between parallel agents.
- Linear auto-named branches (`musansht/mus-NN-*`) are fine everywhere.