# Agentic Outreach Planner

[![CI](https://github.com/musatarar/Agentic-Outreach-Planner/actions/workflows/ci.yml/badge.svg)](https://github.com/musatarar/Agentic-Outreach-Planner/actions/workflows/ci.yml)

Sales/BD/Ops track their clients through CRM data, LinkedIn conversations, emails, handwritten notes, and more creating a tangled web of who they are following up with and what the next best action with their lead is. Locked In turns that into a reviewed queue, where users can define what their data looks like and what conditions correspond to what action. 

```
e.g.
Sat through the demo, never actually signed up -> Complete onboarding 
Signed up, then went dark -> Re-engage dormant account
"Circle back after budget approval" — written in a note 6 weeks ago and forgotten -> Follow up
Customer is building quotes but never submitting one -> Nudge usage
```

<img width="1437" height="666" alt="Screenshot 2026-09-22 at 11 28 48 AM" src="https://github.com/user-attachments/assets/1d6aa46a-ddab-465d-a301-02ffac54cde0" />

## Demo Quickstart
### Docker

```bash
python scripts/setup_env.py   # then set LOGIN_ALLOWED_EMAILS in .env
docker compose up
```

Starts Postgres, builds the image, migrates, seeds the demo pipeline and serves on
**http://127.0.0.1:8000/**, with the actions engine's cron ticking beside it. It runs
Django's development server, not a production stack.

### Python 3.12+.
```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
python scripts/setup_env.py     # writes .env with a generated DJANGO_SECRET_KEY
python manage.py migrate
python scripts/populate_demo_data.py         # seeds demo data (--reset empties first)
python manage.py runserver      # http://127.0.0.1:8000
```

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
`LLM_PROVIDER` and that provider's key in `.env`, then open a lead on the leads page and use
the **Generate email** button on the action the engine proposed for it. `groq` is the
default and has a free tier (https://console.groq.com).



`up` reuses the image it already built, so pass `--build` after pulling code. On a column
that does not exist, the database predates a regenerated migration: `docker compose down -v`
drops the `postgres_data` volume and the next `up` rebuilds it from scratch. That volume
holds only demo data, which is reseeded on every start.

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
| `OUTREACH_MAX_COPY_TOKENS` | Token budget for one copy call; a reasoning model's hidden reasoning is billed against it. |
| `ACTIONS_LLM_DRY_RUN` | Whether an actions-engine run may call the provider. `True` (the default) skips it: every inference candidate comes back unevaluable and the tick costs nothing. Exactly `False` turns the dry run off; anything else leaves it on. |
| `ACTIONS_CRON_INTERVAL_SECONDS` | Seconds between `run_action_jobs` ticks in the compose `cron` service. Read by `docker/cron.sh`, not by Django. Default 300. |
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

# actions engine: queue every lead without an open job, then run the batch.
# `docker compose up` runs this on a loop in the `cron` service; this is the
# same entry point by hand. Runs are dry by default: ACTIONS_LLM_DRY_RUN=False spends.
python manage.py run_action_jobs [--limit N] [--no-enqueue]

# frontend -- only when frontend/ changed
cd frontend && npm ci && npm run typecheck && npm test && npm run build
git diff --exit-code -- project/app/static/frontend/   # CI fails on a stale bundle
```

## Stack

Python 3.12 · Django 4.2 · Django REST Framework · SQLite (local) / Postgres (Docker) ·
React 18 · TypeScript · Vite

## Next Steps
1. Dynamic frontend lead display
2. Interface for user signup and rules creation
3. Hubspot OAuth
