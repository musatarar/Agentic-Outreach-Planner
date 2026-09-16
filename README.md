# Agentic Outreach Planner

[![CI](https://github.com/musatarar/Agentic-Outreach-Planner/actions/workflows/ci.yml/badge.svg)](https://github.com/musatarar/Agentic-Outreach-Planner/actions/workflows/ci.yml)

Leads come in from a CRM export. Deterministic rules pick who needs outreach today and
why. An LLM drafts the copy for those leads and nothing else. A grounding verifier checks
every claim in the draft against the stored record. A human reviews each draft in an
inbox, edits it, and approves or dismisses it. Approved copy leaves via the reviewer's
clipboard — **the app sends nothing itself**, and there is no send machinery.

The judgement is deterministic Python you can read and test. The model only writes prose.

## Quickstart

Python 3.12+.

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
python scripts/setup_env.py     # writes .env with a generated DJANGO_SECRET_KEY
python manage.py migrate
python scripts/populate_demo_data.py
python manage.py runserver      # http://127.0.0.1:8000
```

Updating a checkout from before the migration squash: `rm db.sqlite3`, then re-run
`migrate` and `populate_demo_data` above. Demo data is regenerated, not migrated.

Put your address in `LOGIN_ALLOWED_EMAILS` in `.env`, open
**http://127.0.0.1:8000/signin**, enter it, and the sign-in link is printed to the server
log:

```
INFO Magic sign-in link for you@example.com (expires in 900s):
     http://127.0.0.1:8000/auth/consume?token=...
```

Paste it into the browser. Links are single use. No SMTP is involved: delivery defaults to
`console`. An address not on the allowlist gets exactly the same response as one that is.

The demo runs without an LLM key — you just cannot generate copy. For real drafts, set
`LLM_PROVIDER` and that provider's key in `.env`, then use **Generate all**, or the per-lead
generate button, on the leads page. `groq` is the default and has a free tier
(https://console.groq.com).

### Docker

```bash
python scripts/setup_env.py   # then set LOGIN_ALLOWED_EMAILS in .env
docker compose up
```

Starts Postgres, builds the image, migrates, seeds the demo pipeline and serves on
**http://127.0.0.1:8000/**. It runs Django's development server, not a production stack.

## Workspaces

Leads and their events belong to a **workspace** (a tenant). A signed-in user sees exactly
one workspace's book: the one their membership row names. A user with no membership can
sign in, but every data endpoint answers `403 {"code": "no_tenant"}` — there is no
unassigned book to fall back on.

`scripts/populate_demo_data.py` seeds one workspace, `demo`, ingests the sample leads and
events into it, and enrols every address in `LOGIN_ALLOWED_EMAILS`, so the quickstart above
signs you in to a book you can see.

Three commands manage this by hand:

```bash
python manage.py create_tenant acme --name "Acme Insurance"   # idempotent
python manage.py add_tenant_member acme you@example.com       # creates the user if needed
python manage.py ingest_data --tenant acme                    # --tenant is required
```

`backfill_tenant <slug>` exists for the upgrade path: it assigns leads and events written
before workspaces existed (`tenant IS NULL`) to one workspace, and each event follows its
own lead. It is idempotent.

Two limits worth knowing, both deliberate and both in the follow-up list:

- **One workspace per user.** Membership is a one-to-one row; switching workspaces needs a
  selector in the UI and a per-request choice on the API.
- **Lead ids are global.** The lead primary key is the CRM id (`lead_001`), so two
  workspaces cannot both own that id. `ingest_data` refuses such a collision by name rather
  than moving the lead.

## Configuration

Everything is environment variables. `.env.example` is the full list in two sections;
`project/settings.py` holds every default.

**Basic — the lines you edit**

| Variable | What it is |
|---|---|
| `DJANGO_SECRET_KEY` | Required; the app refuses to boot without it. `scripts/setup_env.py` generates one. |
| `LLM_PROVIDER` | `groq` (default) \| `claude` \| `chatgpt` \| `deepseek` |
| `GROQ_API_KEY`, `ANTHROPIC_API_KEY` / `CLAUDE_API_KEY`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY` | The key for whichever provider you chose. |
| `LOGIN_ALLOWED_EMAILS` | Comma-separated addresses allowed to sign in. There is no signup flow. |

**Advanced — defaults are fine**

| Variable | What it bounds |
|---|---|
| `DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS` | Standard Django deployment settings. |
| `DATABASE_URL` | Postgres; blank uses the SQLite file `./db.sqlite3`. |
| `DJANGO_LOG_LEVEL` | Level for the `project.app` logger. The sign-in link is logged at INFO. |
| `COPY_VERIFY_LEVEL` | Grounding strictness: `off` \| `standard` \| `strict`. |
| `LLM_MODEL` | Model id; blank uses the adapter's own default. |
| `OUTREACH_MAX_IN_FLIGHT`, `OUTREACH_MAX_ATTEMPTS`, `OUTREACH_INITIAL_BACKOFF_S`, `OUTREACH_MAX_BACKOFF_S`, `OUTREACH_BACKOFF_MULTIPLIER`, `OUTREACH_REQUEST_TIMEOUT_S`, `OUTREACH_PER_LEAD_TIMEOUT_S` | How hard a planner run drives the provider. Bad values fail at boot with the variable named. |
| `LOGIN_LINK_DELIVERY`, `LOGIN_TOKEN_TTL_SECONDS`, `LOGIN_LINK_BASE_URL`, `LOGIN_RATE_LIMIT_EMAIL`, `LOGIN_RATE_LIMIT_IP`, `LOGIN_RESEND_COOLDOWN_SECONDS` | Magic-link delivery, expiry and rate limits. |

## Commands

```bash
# tests (Django's unittest runner; there is no pytest)
python manage.py test project.app
python manage.py test project.app.tests.tests_review          # one module
DATABASE_URL=<postgres-url> python manage.py test project.app # Postgres parity
coverage run manage.py test project.app && coverage report    # floor is 90

# lint, types, migrations -- this is what CI runs
ruff check . && ruff format --check .
mypy project/app/services/
python manage.py makemigrations --check --dry-run

# rules regression gate (pure Python, no DB, no network)
python evals/run_rules_eval.py

# frontend -- only when frontend/ changed
cd frontend && npm ci && npm run typecheck && npm test && npm run build
git diff --exit-code -- project/app/static/frontend/   # CI fails on a stale bundle
```

CI runs the backend suite on Python 3.12 and 3.13 against both SQLite and Postgres, plus
lint, mypy, the migration check, the rules eval and the frontend build.

## Architecture

- **Models** (`project/app/models/`): `Tenant`, `TenantMembership`, `Lead`, `Event`,
  `OutreachAction`, `DismissedOutreachKey`, `LoginToken`.
- **Tenancy** (`services/tenancy.py`, `permissions.py`): a caller's workspace is resolved
  onto `request.tenant` by the `HasTenant` permission, and every lead, event and outreach
  query filters on it. Outreach rows carry no tenant column — they are scoped through
  `lead__tenant`.
- **Rules + planner** (`services/outreach.py`): `determine_action` / `determine_priority`
  are pure functions over a lead and its events. `plan_outreach` runs in numbered phases —
  read, classify and build prompts, call the provider, run the two output gates, write.
- **LLM layer** (`services/llm/`): one adapter per provider behind a shared interface,
  selected by `LLM_PROVIDER`. Imports no Django, stores nothing.
- **Verifier** (`services/verify.py`): deterministic, no LLM. Checks numbers, names, dates
  and unauthorized commercial promises against the record. Fails closed — a missing or
  blank report blocks approval.
- **Frontend** (`frontend/`, built into `project/app/static/frontend/`): React 18 + TS,
  four pages — sign in, consume link, leads, review inbox. The bundle is committed and
  served by Django, so `manage.py runserver` alone runs the whole app with no Node.

Registries — start here to find anything: `project/app/models/__init__.py`,
`project/app/views/__init__.py`, `project/app/serializers/__init__.py`,
`frontend/src/api/endpoints.ts` (every frontend API call, one line each).

### Review flow

`OutreachAction` moves `pending → approved | dismissed`, and either state reopens back to
`pending`, behind `/api/outreach/<id>/{edit,verify,approve,dismiss,reopen}/`.

- **`suggested_copy` is immutable.** A reviewer's edit lands in `edited_copy`, so what the
  model wrote and what a human sent can always be diffed.
- **Approval is a judgement, not a send.** The server re-verifies the copy in play and
  refuses approval when a claim contradicts the record.
- **Dismiss is permanent.** It writes a suppression row keyed on
  `sha256("v1|{lead_id}|{action_type}")`, which `plan_outreach` reads *before* generating,
  so a re-run neither resurrects the recommendation nor pays for a call to rediscover it.
  Reopening a dismissal revokes the suppression in the same transaction.

## Stack

Python 3.12 · Django 4.2 · Django REST Framework · SQLite (local) / Postgres (Docker) ·
React 18 · TypeScript · Vite

See [SECURITY.md](SECURITY.md) for the threat model, and [CLAUDE.md](CLAUDE.md) for the
working rules this repo enforces.
