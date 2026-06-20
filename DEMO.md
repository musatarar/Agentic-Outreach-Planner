# Outreach Planner — Demo Guide

A tool that reads the agency pipeline (`leads.json` + `events.json`), decides who needs
outreach today and why, and uses Claude (`claude-sonnet-4-6`) to draft personalized copy
per lead. AEs open one page each morning instead of digging through HubSpot and Slack.

## Setup (once)

```bash
# 1. API key — .env at repo root (already present, gitignored):
#    CLAUDE_API_KEY=sk-ant-...

# 2. Migrate and load the pipeline data
./venv/bin/python manage.py migrate
./venv/bin/python scripts/populate_demo_data.py   # ingests 5 leads, 28 events
```

## Run the demo

```bash
./venv/bin/python manage.py runserver
```

1. Open **http://127.0.0.1:8000/**
2. Click **"Run Outreach Plan"** — takes ~20–30s (one Claude call per lead).
3. Results render as cards sorted by priority:
   - **P1 (red)** — reach out today
   - **P2 (yellow)** — this week
   - **P3 (gray)** — low urgency
   - Each card: who (agency/contact), why now (reason citing real signals), the
     suggested email (with copy-to-clipboard), and a **"Needs human review"** banner
     when the planner can't classify a lead (reported to BD instead of auto-drafting).

## What to point out in the demo

| Lead | Result | Why |
|---|---|---|
| Tom Kladis (Meridian, $5.6M) | **P1 · follow_up_after_hold** | Said he was waiting on Q2 budget; budget approved, then went quiet + no-reply email |
| Dana Mosely (Bluegrass, $8.1M) | **P1 · complete_onboarding** | Biggest book, demo'd, promised May follow-up, never signed up |
| Priya Nair (Summit) | **P2 · power_user_reward** | 6 deals closed, notes flag the 20-deal volume-pricing milestone |
| Derek Sohn (Highline) | **P2 · nudge_usage** | Logs in, creates quotes, has never submitted one |
| Susan Lakewood (Lakewood) | **P2 · nudge_usage** | Steady user, 3 deals short of her self-declared 5-deal commitment target |

The generated emails cite real specifics from call/demo notes (Ray's buy-in, the Q2
budget, named clients like Pacific Rim Imports) — not generic templates.

## Reports & Next Actions

- **http://127.0.0.1:8000/reports/** — per-lead audit trail: every run's detected issue,
  how it was handled (collapsible suggested copy, or "Reported to BD" for unclassified
  leads), and any next action. Run the planner twice to show history accumulating.
- **http://127.0.0.1:8000/next-actions/** — open follow-ups (needs-human items +
  further_action notes) as a prioritized todo queue. Assign/Done/Snooze buttons are
  intentionally stubbed ("coming soon") — talking point: this is where AE workflow
  management lands next.

## API (the page is just a viewer over these)

```bash
curl -X POST http://127.0.0.1:8000/api/outreach/run/   # run planner + generate copy
curl http://127.0.0.1:8000/api/outreach/               # latest plan (no Claude calls)
curl http://127.0.0.1:8000/api/leads/                  # raw pipeline
curl http://127.0.0.1:8000/api/reports/                # full action history, newest first
```

## Tests

```bash
./venv/bin/python manage.py test project.app   # 27 tests: models/ingestion, API, logic (mocked Claude), frontend
```

## Architecture (60-second version)

- **Models** (`project/app/models.py`): `Lead`, `Event`, `OutreachAction` (audit log of
  what the planner decided — priority, action, reason, copy, needs_human).
- **Logic** (`project/app/services/outreach.py`): rule-based priority scoring (book size,
  stage, gone-quiet detection, recency) and ordered action classification — deterministic
  and testable; Claude is used only for the copywriting, with rules deciding *who* and *why*.
- **API** (`project/app/views.py`): thin DRF layer over the service.
- **Frontend** (`project/app/templates/app/index.html`): single vanilla-JS page.
- Unclassifiable leads become `needs_human=True` reports for BD review instead of
  auto-generated emails — the safe default for situations the rules don't yet know.
