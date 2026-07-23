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

```bash
git clone <repo-url> && cd "Eventual Technical Challenge"

# .env at repo root (gitignored) needs one key for whichever provider config.toml selects:
#   GROQ_API_KEY=...      (default provider — free tier, no credit card: console.groq.com)
#   ANTHROPIC_API_KEY=... (or CLAUDE_API_KEY, for provider = "claude")

./venv/bin/python manage.py migrate
./venv/bin/python scripts/populate_demo_data.py   # loads the sample pipeline
./venv/bin/python manage.py runserver
```

Open **http://127.0.0.1:8000/**, click **"Run Outreach Plan"**, watch prioritized cards
with AI-drafted emails render in ~20–30s. Full walkthrough with sample results in
[DEMO.md](DEMO.md).

## Architecture, 30 seconds

| Layer | Where | What |
|---|---|---|
| Models | `project/app/models.py` | `Lead`, `Event`, `OutreachAction` (decision audit log), `ReviewDecision` |
| Logic | `project/app/services/outreach.py` | Priority scoring + action classification — pure Python, no LLM |
| LLM | `project/app/services/llm/` | Adapter per provider behind a common interface, selected via `config.toml` |
| API | `project/app/views.py`, `urls.py` | DRF APIViews at `/api/*` |
| Frontend | `project/app/templates/app/`, `views_frontend.py` | Vanilla-JS pages: outreach board, reports, next-actions queue |

## Stack

Python 3.9 · Django 4.2 · Django REST Framework · SQLite · vanilla JS (no frontend build step)

## Tests

```bash
./venv/bin/python manage.py test project.app
```
