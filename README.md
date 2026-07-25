# Agentic Outreach Planner

A Django + DRF backend that reads an agency's sales pipeline, decides which leads need
outreach *today* and *why*, and drafts personalized follow-up copy with an LLM — instead
of an account exec digging through HubSpot and Slack every morning.

## Why it's interesting

- **Rules decide, LLM writes.** Priority and action classification are deterministic,
  testable Python (book size, lifecycle stage, "gone quiet" detection, recency) — the LLM
  is only used for copywriting, never for the judgment call. See
  [`services/outreach.py`](project/app/services/outreach.py).
- **Provider-agnostic LLM layer.** Swap Claude / OpenAI / DeepSeek / Groq via one line in
  [`config.toml`](config.toml) — no code changes, no vendor lock-in. See
  [`services/llm/`](project/app/services/llm/).
- **Safe default for the unknown.** Leads the rules can't classify are flagged
  `needs_human=True` and routed to a BD review queue instead of getting an
  auto-generated email.
- **51 passing tests** across models, API, rule logic (LLM calls mocked), and frontend.

## Quickstart

Requires **Python 3.9+**. From a fresh clone:

```bash
# 1. Isolated environment + dependencies
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. API key for whichever provider config.toml selects (default: groq — free
#    tier, no credit card at console.groq.com). Only one key is needed.
cp .env.example .env               # then edit .env and fill in the key

# 3. Migrate, seed the demo pipeline, and run
python manage.py migrate
python scripts/populate_demo_data.py   # loads the sample pipeline
python manage.py runserver
```

Open **http://127.0.0.1:8000/**, click **"Run Outreach Plan"**, watch prioritized cards
with AI-drafted emails render in ~20–30s. Full walkthrough with sample results in
[DEMO.md](DEMO.md).

### Or with Docker

No local Python needed — just Docker:

```bash
cp .env.example .env   # optional: set a provider key to enable the LLM copy step
docker compose up
```

This builds the image, applies migrations, seeds the demo pipeline, and serves the app at
**http://127.0.0.1:8000/**. The server starts even without a key — you just can't run the
LLM copy step until one is set.

## Architecture, 30 seconds

| Layer | Where | What |
|---|---|---|
| Models | `project/app/models.py` | `Lead`, `Event`, `OutreachAction` (decision audit log), `ReviewDecision` |
| Logic | `project/app/services/outreach.py` | Priority scoring + action classification — pure Python, no LLM |
| LLM | `project/app/services/llm/` | Adapter per provider behind a common interface, selected via `config.toml` |
| API | `project/app/views.py`, `urls.py` | DRF APIViews at `/api/*` |
| Frontend | `frontend/` (source), `project/app/static/frontend/` (built) | React + TS SPA: planner board, reports, BD dashboard — consumes the `/api/*` endpoints |

The React build is **committed** to `project/app/static/frontend/`, and Django still serves the three routes
(`/`, `/reports/`, `/next-actions/`) as thin shells (`templates/app/spa_base.html`). So `manage.py runserver`
alone runs the whole app — **no Node required** to demo or review.

## Stack

Python 3.9 · Django 4.2 · Django REST Framework · SQLite · React 18 · TypeScript · Vite

## Tests

```bash
python manage.py test project.app
```

## Frontend development

Node is only needed to change the frontend. The source lives in `frontend/`; the Django shells load the
committed bundle via `{% static %}`.

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173 — hot reload, proxies /api to Django on :8000
npm run typecheck  # tsc --noEmit
npm run build      # emits the committed bundle into project/app/static/frontend/
```

Run `npm run dev` alongside `manage.py runserver` (port 8000) for local development, then `npm run build`
and commit `project/app/static/frontend/` before opening a PR so the plain-Django path stays current.
