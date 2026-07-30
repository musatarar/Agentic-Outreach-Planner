# CONTRACT.md — Locked In Agentic Outreach Planner

All agents build against this spec exactly. Do not deviate from names, shapes, or paths.

## Goal

Ingest `raw_data/leads.json` + `events.json`, decide which agency leads
need outreach today and why, generate suggested copy per lead with Claude
(`claude-sonnet-4-6`), display results on a single demo page.

## Models — `project/app/models.py`

```python
class Lead(models.Model):
    id = models.CharField(max_length=32, primary_key=True)  # "lead_001"
    agency_name = models.CharField(max_length=255)
    contact_name = models.CharField(max_length=255)
    contact_email = models.EmailField()
    contact_phone = models.CharField(max_length=32)
    state = models.CharField(max_length=2)
    num_producers = models.IntegerField()
    years_in_business = models.IntegerField()
    estimated_book_size_usd = models.BigIntegerField()
    stage = models.CharField(max_length=32)  # "active_trial" | "demo_completed"
    signed_up_date = models.DateField(null=True)
    last_login_date = models.DateField(null=True)
    quotes_created = models.IntegerField(default=0)
    quotes_submitted = models.IntegerField(default=0)
    deals_closed = models.IntegerField(default=0)
    last_contacted_date = models.DateField(null=True)
    hubspot_notes = models.TextField(blank=True)


class Event(models.Model):
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="events")
    type = models.CharField(max_length=32)  # login, quote_created, quote_submitted,
    # deal_closed, call_logged, email_sent,
    # demo_completed, onboarding_call
    timestamp = models.DateTimeField()
    meta = models.JSONField(default=dict, blank=True)


class OutreachAction(models.Model):  # what the planner decided/did
    lead = models.ForeignKey(
        Lead, on_delete=models.CASCADE, related_name="outreach_actions"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    priority = models.IntegerField()  # 1 highest, 3 lowest
    action_type = models.CharField(max_length=64)  # see ACTION_TYPES
    reason = models.TextField()  # why this lead, why now
    suggested_copy = models.TextField(blank=True)  # Claude-generated email/message
    needs_human = models.BooleanField(default=False)  # unknown action -> report to BD
    further_action = models.TextField(blank=True)  # what ops/AE should do next
```

## Action types — `project/app/services/actions.py`

```python
ACTION_TYPES = [
    "power_user_reward",  # near milestone, offer volume pricing/discount (medium)
    "follow_up_after_hold",  # asked to be contacted later; date passed (high)
    "reengage_dormant",  # onboarded but stopped using portal (high)
    "nudge_usage",  # active but underusing, needs encouragement (medium)
    "complete_onboarding",  # demo done but never signed up; weight by book size (high)
    "unknown",  # no pattern matched -> needs_human=True, report to BD
]
```

## Service — `project/app/services/outreach.py`

```python
def determine_priority(lead) -> int            # 1-3: book size + stage + notes + recency
def determine_action(lead) -> tuple[str, str]  # (action_type, reason) from lead + events
def generate_copy(lead, action_type, reason) -> str   # anthropic, model="claude-sonnet-4-6"
def plan_outreach() -> list[OutreachAction]    # all leads -> persist + return actions
```

- `generate_copy`: `anthropic.Anthropic()` (key already in env via settings); prompt from
  lead fields, hubspot_notes, recent events (especially call/email/demo notes), action_type.
  Returns a short personalized outreach email.
- `unknown` actions skip copy generation, set `needs_human=True`.

## API — `project/app/views.py`, `serializers.py`, `urls.py`

DRF APIViews + serializers (no router). Routes are included at the `api/` prefix
by `project/urls.py` (already wired).

- `POST /api/outreach/run/` — runs `plan_outreach()`, returns created actions ordered by
  priority. Item shape:
  `{id, lead: {id, agency_name, contact_name, contact_email}, priority, action_type,
    reason, suggested_copy, needs_human, further_action, created_at}`
- `GET /api/outreach/` — most recent action per lead, ordered by priority
- `GET /api/leads/` — all leads

## Frontend — `project/app/templates/app/index.html` + index view

The FE agent owns `project/app/views_frontend.py` (separate file so it never conflicts with
the API agent's `views.py`) and registers `path('', index)` in `project/urls.py` via
`from project.app.views_frontend import index`. Do not touch `project/app/urls.py`.

Single page, vanilla JS: "Run Outreach Plan" button -> `POST /api/outreach/run/` ->
render cards sorted by priority: who (agency/contact), priority badge (1=red/2=yellow/3=gray),
suggested copy, reason, needs_human/further_action flag. Loading state while Claude runs.
Minimal inline styles. Use `fetch` with `X-CSRFToken` from the `csrftoken` cookie.

## Ingestion — `project/app/management/commands/ingest_data.py` + `scripts/populate_demo_data.py`

Management command loads the two JSON files (defaults:
`raw_data/leads.json` and `raw_data/events.json`)
into Lead/Event, idempotent via `update_or_create` (clear+recreate events per lead is fine).
`scripts/populate_demo_data.py` bootstraps Django and calls the command.

## Rules for all agents

- Run Python inside the project virtualenv (`python -m venv .venv && pip install -r requirements.txt`; see README Quickstart). With it active, invoke commands as `python manage.py ...`.
- Only the models agent runs `makemigrations`.
- Never hardcode the API key; it comes from the environment (settings already map
  CLAUDE_API_KEY -> ANTHROPIC_API_KEY from `.env`).
- Tests in your area only; mock the anthropic client in tests.
- Your code may not run standalone until merge (cross-area imports) — that's expected.
