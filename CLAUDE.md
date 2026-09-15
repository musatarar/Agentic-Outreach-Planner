# CLAUDE.md

Agentic outreach planner. Django 4.2 + DRF backend; React 18/TS frontend built by Vite
into a bundle committed at project/app/static/frontend/ and served by Django (no Node in
the runtime image). Rule functions in services/outreach.py select leads for outreach;
provider calls generate message text only; services/verify.py checks generated copy
against stored lead/event fields and blocks approval when checks fail; a human approval
gate guards every draft, and approved copy leaves via the reviewer's clipboard — the app
sends nothing itself.

## Commands

```bash
# setup
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python scripts/setup_env.py   # writes .env from .env.example with a freshly generated
                              # DJANGO_SECRET_KEY; never overwrites an existing .env
```

```bash
# run
python manage.py migrate
python manage.py runserver                      # http://127.0.0.1:8000
python scripts/populate_demo_data.py            # demo data (ingest + LLM catalog seed)
```

```bash
# tests — Django's unittest runner. There is NO pytest, no conftest.
python manage.py test project.app                                  # full backend suite
python manage.py test project.app.tests.tests_queue                # one module
python manage.py test project.app.tests.tests_queue.Cls.test_name  # one test
# Test modules are tests_<subject>.py; test names are full behavioral sentences —
# grep for the behavior in plain English to find the right test.
DATABASE_URL=<postgres-url> python manage.py test project.app      # Postgres parity
                                                # (CI runs py3.12/3.13 x sqlite/postgres)
```

```bash
# lint / types / migrations — run before any commit; this mirrors CI exactly
ruff check . && ruff format --check .
mypy project/app/services/          # CI typechecks exactly this path, nothing more
python manage.py makemigrations --check --dry-run   # must be clean
```

```bash
# rules-engine regression (pure Python, no DB, no network, frozen clock)
python evals/run_rules_eval.py      # diffs against evals/baselines/ — baselines change
                                    # only by explicit human decision, never to make a run pass
```

```bash
# frontend — only when frontend/ changed
cd frontend && npm ci && npm run typecheck && npm test && npm run build
git diff --exit-code -- project/app/static/frontend/   # CI fails on a stale bundle
# NEVER hand-edit project/app/static/frontend/** — it is build output. Rebuild + commit.
```

## Architecture in 30 seconds

- project/app/services/outreach.py — rules engine + planner orchestrator (numbered
  phases in comments). project/app/services/llm/ — provider-agnostic LLM layer.
  services/verify.py — grounding verifier. services/agent/ — flag-gated tool-calling
  copy loop (OUTREACH_AGENT_ENABLED, default off).
- Registries (start here to find anything): project/app/models/__init__.py,
  project/app/views/__init__.py, project/app/serializers/__init__.py,
  frontend/src/api/endpoints.ts (every frontend API call, one line each).
- Constraint/index names (oa_queue_order, rd_one_live_send_per_action, ...) appear
  verbatim in model and migration — grep the name to get the whole story.
- Ticket IDs (MUS-nn) in comments remain as history pointers — grep one to find a
  feature's past.

## Database & migrations — hard rules

- NEVER edit a migration that is committed on the default branch. Additive follow-up
  migrations only.
- Run `python manage.py makemigrations --check --dry-run` after any model change.
- Dev is usually SQLite; production is Postgres (DATABASE_URL). DDL that is instant on
  SQLite can stall Postgres: index adds take a SHARE lock (writes blocked for the
  build); constraint adds via ALTER TABLE take ACCESS EXCLUSIVE (all access blocked).
  Either is a production stall on a hot table. Any index/constraint on outreachaction,
  lead, or event is a hot-table change: prefer concurrent operations with
  atomic = False, and get human review.
- One concern per migration. Schema migrations carry no data operations; seeding lives
  in idempotent management commands (the repo has zero RunPython migrations — keep it so).
- New columns: nullable-or-constant-default first (Postgres adds constant defaults
  instantly), backfill in batches via a management command, then constrain.
- Do not write to the local SQLite file; the Django test runner creates a
  throwaway database for each run.

## ORM & query discipline

- Query budgets are pinned by test (see tests_planner_perf.py — budgets computed from
  connection.ops.bulk_batch_size so they hold on both backends). A budget increase is a
  reviewed decision, not a test fix.
- List endpoints must not serialize unbounded tables; add pagination and a throttle
  scope to any new list/expensive endpoint (settings.py REST_FRAMEWORK block).
- Prefetch rule: only .all() is served from a prefetch cache — any filtered call
  re-queries. Slice prefetched collections in Python, not in the queryset.
- No Django signals anywhere; all writes go through explicit service functions. Do not
  introduce signals.
- Race-sensitive logic gets a database-level guard (partial unique constraint or
  conditional UPDATE), never a read-then-check. Existing patterns to copy:
  single-use login-token redemption, the agent-run epoch-CAS claim,
  rd_one_live_send_per_action.

## Transactions

- ATOMIC_REQUESTS is off (settings.py does not set it): no view is wrapped in a
  transaction automatically. Writes that must land together go in one explicit
  transaction.atomic block in the service function that owns them — never spread
  across helpers each committing separately.
- select_for_update only inside an explicit transaction.atomic block (it errors
  outside one).
- Never hold a transaction open across the async provider-call phase: collect inputs,
  close the transaction, await, then write results in a new atomic block. A
  transaction spanning the event-loop hop pins a connection for the full provider
  latency and can deadlock against the planner's own writes.

## Async seam — do not cross it

- plan_outreach is sync; only the provider-call phase runs on an event loop. NO ORM
  calls inside that async phase — it raises SynchronousOnlyOperation at runtime and
  nothing static will warn you. The agent checkpoint writer is the single, documented
  exception; do not add a second.
- services/llm/ must not import Django at module level (runtime.py keeps Django imports
  function-local so the package imports without Django). Preserve this.

## LLM layer

- Adding a provider currently requires edits in three places: the client registry
  (services/llm/__init__.py), the env-var map (services/llm/config.py), and the
  telemetry provider-name map (telemetry/genai.py). Missing the third silently drops
  telemetry attribution. Edit all three or consolidate first.
- Retryability lives on the error class (services/llm/errors.py). Retry policy and
  timeouts are cost multipliers — flag any change as a spend change in the PR.
- Never log or persist raw prompts/completions outside the ProviderTrace content path;
  telemetry spans carry sha256 hashes only. Do not add content keys to spans.
- The stub provider is gated by OUTREACH_ALLOW_STUB_LLM=1 and exists for benchmarks and
  tests only. Never weaken that gate.

## Security invariants — do not weaken, escalate instead

- Lead-controlled text (CRM notes, event payloads) is untrusted input everywhere:
  sanitize before it enters any prompt; tool results are sanitized, length-capped, and
  server-bound to the lead id. The same rule applies in the frontend: never interpolate
  lead-controlled fields into anything prompt-bound.
- suggested_copy on OutreachAction is immutable once written (the eval corpus diffs it);
  reviewer edits create OutreachEdit rows.
- The verifier fails closed: a missing/blank verification report blocks approval.
- COPY_VERIFY_LEVEL=off disables grounding checks silently. Never set it in committed
  config; treat any diff containing it as human-review-required.
- Magic-link auth stores only hashed tokens, single-use via conditional UPDATE, with
  timing-equalized failure paths and REMOTE_ADDR-only IP trust. Changes here are
  human-gated.

## Settings & env conventions

- All configuration is environment variables; settings.py is the single source of
  defaults (docker-compose passes planner knobs through blank on purpose — do not
  restate defaults elsewhere).
- Use the _env_int/_env_number/_env_list helpers in settings.py for new variables:
  blank means unset, and bad values must raise ImproperlyConfigured naming the variable.
  (Some older vars use bare int() — fix opportunistically, never imitate.)
- Every new setting gets a .env.example entry and a docker-compose passthrough.
- DJANGO_SECRET_KEY is mandatory (boot fails without it). Boot-time system checks live
  in project/app/checks.py — add one when a misconfiguration should fail boot rather
  than fail at first use.

## Tests

- Fixtures via setUpTestData and factory helpers; pin dates to constants (the suites
  freeze TODAY) rather than reading the clock; patch django.utils.timezone.now only
  where the view under test reads it.
- Every LLM provider interaction is mocked — doubles subclass the real LLMClient so
  seam changes fail loudly. Never let a test reach a real provider.
- Do not edit or delete an existing test to make a change pass; a failing existing
  test requires human sign-off before either the test or the code changes.
- Two suites assert repo-infrastructure facts via git grep / git check-ignore; if one
  fails after a legitimate change, update its manifest — that is the intended workflow,
  not a defect.
- Coverage floor is 90 (pyproject.toml); DRF throttle history persists across tests —
  clear it in setUp/tearDown as tests_auth.py does.

## What always needs a human before merge

Migrations; auth/session/throttle code; the approval gate; sanitization and verifier
logic; feature-flag default flips; anything changing provider spend (models, retries,
concurrency, prompt size); any retention/deletion touching audit tables (ProviderTrace*,
AgentStep, OutreachEdit, LoginToken); any change to .claude/ or CI workflow
configuration.

## Deploy (placeholders — code does not determine these)

- Production server/WSGI setup: <not yet defined — the container currently runs the dev
  server>
- Production DATABASE_URL / migration execution window: <define with the deploy story>
