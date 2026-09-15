# CLAUDE.md

Agentic outreach planner. Django 4.2 + DRF backend; React 18/TS frontend built by Vite into
a bundle committed at project/app/static/frontend/ and served by Django (no Node in the
runtime image). Rule functions in services/outreach.py select leads for outreach; provider
calls generate message text only; services/verify.py checks generated copy against stored
lead/event fields and blocks approval when checks fail; a human approval gate guards every
draft, and approved copy leaves via the reviewer's clipboard — the app sends nothing itself.

## Commands

```bash
# setup: venv + `pip install -r requirements-dev.txt`, then
python scripts/setup_env.py    # .env with a generated key; never overwrites an existing one
python manage.py migrate && python scripts/populate_demo_data.py && python manage.py runserver

# tests — Django's unittest runner. There is NO pytest, no conftest. Modules are
# tests_<subject>.py and test names are behavioral sentences: grep the behavior in English.
python manage.py test project.app                                   # full backend suite
python manage.py test project.app.tests.tests_review.Cls.test_name  # one test
DATABASE_URL=<postgres-url> python manage.py test project.app       # Postgres parity
coverage run manage.py test project.app && coverage report          # floor 90, pyproject

# lint / types / migrations — before any commit; this mirrors CI
ruff check . && ruff format --check .
mypy project/app/services/                          # CI typechecks this path, nothing more
python manage.py makemigrations --check --dry-run   # must be clean

# rules regression: pure Python, no DB, no network, frozen clock. Baselines change only by
# explicit human decision, never to make a run pass.
python evals/run_rules_eval.py

# frontend — only when frontend/ changed. NEVER hand-edit the built bundle.
cd frontend && npm ci && npm run typecheck && npm test && npm run build
git diff --exit-code -- project/app/static/frontend/   # CI fails on a stale bundle
```

## Where things are

services/outreach.py is the rules engine and the planner (numbered phases in comments);
services/llm/ is the provider-agnostic layer; services/verify.py is the grounding verifier.
Registries, to find anything: project/app/models/__init__.py, views/__init__.py,
serializers/__init__.py, frontend/src/api/endpoints.ts (every API call, one line each).
Constraint and index names appear verbatim in the model and its migration — grep the name.

## Database & migrations — hard rules

- NEVER edit a migration committed on the default branch. Additive follow-ups only.
- DDL that is instant on SQLite can stall Postgres: index adds take a SHARE lock, ALTER
  TABLE constraint adds take ACCESS EXCLUSIVE. Any index or constraint on outreachaction,
  lead or event is a hot-table change — prefer concurrent ops with `atomic = False`, and
  get human review.
- One concern per migration. Schema migrations carry no data operations; seeding lives in
  idempotent management commands (zero RunPython migrations — keep it so).
- New columns: nullable-or-constant-default first, backfill via a management command, then
  constrain. Do not write to the local SQLite file; the test runner makes a throwaway one.

## ORM, transactions, async seam

- Query budgets are pinned by tests_planner_perf.py, computed from
  `connection.ops.bulk_batch_size` so they hold on both backends. Raising one is a reviewed
  decision, not a test fix.
- No list endpoint serializes an unbounded table: pagination and a throttle scope for every
  new list or expensive endpoint (settings.py REST_FRAMEWORK block).
- Only `.all()` is served from a prefetch cache; a filtered call re-queries. Slice in Python.
- No Django signals; all writes go through explicit service functions.
- Race-sensitive logic gets a database-level guard (partial unique constraint or conditional
  UPDATE), never read-then-check. Copy the single-use login-token redemption or the
  reopen-of-dismiss revoke.
- ATOMIC_REQUESTS is off. Writes that must land together go in one explicit
  `transaction.atomic` block in the service function that owns them; `select_for_update`
  only inside one. Never hold a transaction open across the provider-call phase.
- plan_outreach is sync; only the provider-call phase runs on an event loop. NO ORM calls
  inside that phase — SynchronousOnlyOperation at runtime, and nothing static warns you.
  services/llm/ must not import Django at module level (runtime.py keeps its Django imports
  function-local). Preserve both.

## LLM layer

- Selection is environment-only (services/llm/config.py): LLM_PROVIDER (default groq),
  LLM_MODEL (blank = the adapter's DEFAULT_MODEL), the provider's key variable. Nothing is
  stored in the database or editable through the API.
- Adding a provider means two edits: the client registry (services/llm/__init__.py) and the
  env-var map (services/llm/config.py, also the set of values LLM_PROVIDER accepts).
- Retryability lives on the error class (services/llm/errors.py). Retry policy and timeouts
  are cost multipliers — flag any change as a spend change.
- Never log or persist raw prompts or completions.
- The stub provider is gated by OUTREACH_ALLOW_STUB_LLM=1, for tests only. Never weaken it.

## Security invariants — do not weaken, escalate instead

- Lead-controlled text (CRM notes, event payloads) is untrusted everywhere: sanitize before
  it enters any prompt, in the frontend too.
- `suggested_copy` is immutable once written; a reviewer's edit lands in `edited_copy`, so
  the two can always be diffed.
- The verifier fails closed: a missing or blank verification report blocks approval.
- COPY_VERIFY_LEVEL=off disables grounding checks silently. Never set it in committed
  config; treat any diff containing it as human-review-required.
- Magic-link auth stores only hashed tokens, single-use via conditional UPDATE, with
  timing-equalized failure paths and REMOTE_ADDR-only IP trust. Human-gated.

## Settings & env

- All configuration is environment variables; settings.py owns every default (docker-compose
  passes planner knobs through blank on purpose — do not restate defaults there).
- Use the `_env_int` / `_env_float` / `_env_number` / `_env_list` helpers for new variables:
  blank means unset, bad values raise ImproperlyConfigured naming the variable.
- Closed matrix: every variable the app reads has a `.env.example` entry and a
  docker-compose passthrough, and nothing is documented that is not read. Exceptions:
  Django's own DJANGO_SETTINGS_MODULE and the test-only OUTREACH_ALLOW_STUB_LLM gate.
- DJANGO_SECRET_KEY is mandatory (boot fails without it). Boot-time checks live in
  project/app/checks.py — add one when a misconfiguration should fail boot, not first use.

## Tests

- setUpTestData and factory helpers; pin dates to constants (the suites freeze TODAY) rather
  than reading the clock; patch `django.utils.timezone.now` only where the code under test
  reads it.
- Every LLM interaction is mocked — doubles subclass the real LLMClient so seam changes fail
  loudly. Never let a test reach a real provider.
- Do not edit or delete an existing test to make a change pass; that needs human sign-off
  first, for the test or the code.
- DRF throttle history persists across tests — clear it in setUp/tearDown as tests_auth.py does.

## What always needs a human before merge

Migrations; auth/session/throttle code; the approval gate; sanitization and verifier logic;
feature-flag default flips; anything changing provider spend (models, retries, concurrency,
prompt size); any retention or deletion touching LoginToken; any change to .claude/ or CI
workflow configuration.

## Deploy (placeholders — code does not determine these)

- Production server/WSGI setup: <not yet defined — the container runs the dev server>
- Production DATABASE_URL / migration execution window: <define with the deploy story>
