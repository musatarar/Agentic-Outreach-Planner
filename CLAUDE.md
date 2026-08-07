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
```

Frontend source lives in `frontend/`; the built bundle is committed to
`project/app/static/frontend/` (CI fails if it goes stale — run `npm run build` and commit
the bundle after frontend changes). Database defaults to SQLite; `DATABASE_URL` switches to
Postgres; `docker compose up` runs the full stack.

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
