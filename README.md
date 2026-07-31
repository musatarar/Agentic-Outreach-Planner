# Locked In — Agentic Outreach Planner

[![CI](https://github.com/musatarar/Agentic-Outreach-Planner/actions/workflows/ci.yml/badge.svg)](https://github.com/musatarar/Agentic-Outreach-Planner/actions/workflows/ci.yml)
![Coverage](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Fmusatarar%2FAgentic-Outreach-Planner%2Fbadges%2Fcoverage.json)

A Django + DRF backend that reads an agency's sales pipeline, decides which leads need
outreach *today* and *why*, and drafts personalized follow-up copy with an LLM — instead
of an account exec digging through HubSpot and Slack every morning.

## Why it's interesting

- **Rules decide, LLM writes.** Priority and action classification are deterministic,
  testable Python (book size, lifecycle stage, "gone quiet" detection, recency) — the LLM
  is only used for copywriting, never for the judgment call. See
  [`services/outreach.py`](project/app/services/outreach.py).
- **Provider-agnostic LLM layer.** Swap Claude / OpenAI / DeepSeek / Groq via the
  `/api/llm/config/` endpoint (backed by the `LLMConfiguration` model, encrypted key
  storage, no code changes, no vendor lock-in). See
  [`services/llm/`](project/app/services/llm/).
- **Safe default for the unknown.** Leads the rules can't classify are flagged
  `needs_human=True` and routed to a BD review queue instead of getting an
  auto-generated email.
- **Grounds copy against the record.** Before a generated email can be sent, a
  deterministic verifier (no LLM) checks every number, dollar figure, name, and date
  against the `Lead`: inflated deal counts, invented book sizes, the wrong contact, or an
  unauthorized discount all flip `needs_human=True` and land in the review queue with the
  specific problem spelled out. It **fails closed** — a wrong number in a sales email is
  more expensive than a delayed one — and strictness is configurable via `COPY_VERIFY_LEVEL`
  (`off | standard | strict`). See [`services/verify.py`](project/app/services/verify.py).
- **120 passing tests** across models, API, rule logic (LLM calls mocked), copy checks, and
  frontend — plus two eval harnesses that score classification accuracy and generated-copy
  quality against golden data (see [Evals](#evals)).

## Quickstart

Requires **Python 3.12+**. From a fresh clone:

```bash
# 1. Isolated environment + dependencies
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. Copy the env file: DJANGO_SECRET_KEY is required (a fresh local/demo key
#    is already filled in). Also set a provider key (default: groq — free
#    tier, no credit card at console.groq.com) if you want the LLM copy step;
#    it's picked up as a fallback until you save one via /api/llm/config/.
cp .env.example .env               # then edit .env and fill in the LLM key

# 3. Migrate, seed the demo pipeline + LLM catalog, and run
python manage.py migrate
python scripts/populate_demo_data.py   # loads the sample pipeline + LLM catalog
python manage.py runserver
```

No `DATABASE_URL` is needed for this path — it falls back to SQLite at `./db.sqlite3`.

Snoozed triage items return to the queue via `python manage.py unsnooze_due` (add `--dry-run`
to see what it would do). It is idempotent and cheap — two conditional `UPDATE`s — so run it
from cron every minute in anything long-lived.

Open **http://127.0.0.1:8000/**, click **"Run Outreach Plan"**, watch prioritized cards
with AI-drafted emails render in ~20–30s. Full walkthrough with sample results in
[DEMO.md](DEMO.md).

### Or with Docker

No local Python needed — just Docker:

```bash
cp .env.example .env   # required: DJANGO_SECRET_KEY and LOGIN_ALLOWED_EMAILS.
docker compose up      # optionally set a provider key too
```

This starts Postgres, builds the app image, applies migrations, seeds the demo pipeline, and
serves the app at **http://127.0.0.1:8000/**. The server starts even without an LLM provider
key — you just can't run the LLM copy step until one is set.

### Signing in

The API is authenticated by **magic link** — one operator, no passwords to store, reset or
rotate. Add yourself to the allowlist in `.env` (there is no signup flow, so the allowlist
is what decides who may request a link):

```bash
LOGIN_ALLOWED_EMAILS=you@example.com
DJANGO_DEBUG=True          # so the link comes back in the API response too
```

Then open **http://127.0.0.1:8000/signin**, enter that address, and the link is printed to
the server log:

```
INFO Magic sign-in link for you@example.com (expires in 900s):
     http://127.0.0.1:8000/auth/consume?token=nJ7yQwVh3kR2mLpX8sTcAeB1dGfHiKoZuYvNqW0xMjE
```

Paste it into the browser and you're in. Links are single-use and expire after 15 minutes
(`LOGIN_TOKEN_TTL_SECONDS`). **No SMTP is involved in the demo path** — delivery defaults to
`LOGIN_LINK_DELIVERY=console`; set it to `email` to use Django's email backend instead.

An address that isn't on the allowlist gets exactly the same response as one that is, so the
endpoint can't be used to find out who has an account. The one exception is the `dev_link`
field in the response body, which is populated only when `DJANGO_DEBUG=True` **and** delivery
is `console` **and** the address is allowlisted.

## Architecture, 30 seconds

| Layer | Where | What |
|---|---|---|
| Models | `project/app/models.py` | `Lead`, `Event`, `OutreachAction` (decision audit log), `ReviewDecision` |
| Logic | `project/app/services/outreach.py` | Priority scoring + action classification — pure Python, no LLM |
| LLM | `project/app/services/llm/` | Adapter per provider behind a common interface, selected via the DB-backed `LLMConfiguration` (see `/api/llm/config/`) |
| API | `project/app/views.py`, `urls.py` | DRF APIViews at `/api/*` |
| Frontend | `frontend/` (source), `project/app/static/frontend/` (built) | React + TS SPA: planner board, reports, BD dashboard — consumes the `/api/*` endpoints |

### Triage queue

`OutreachAction` carries a lifecycle — `pending → approved | snoozed | dismissed`, with a
short server-timed undo window — behind `/api/queue/*`. Three things in it are worth knowing:

- **`suggested_copy` is immutable, forever.** A reviewer's edits go in `edited_copy`, and every
  edit appends an `OutreachEdit` row holding the before/after pair and its diff. That diff is the
  quiet payoff of the whole product: every correction a human makes is labeled training data for
  the copy evals, and it only exists at the moment of editing. Dump it with
  `python manage.py dump_edit_corpus --committed-only > corpus.jsonl`.
- **Snooze is not skip.** It takes a judgement about *when* the lead should come back —
  `tomorrow`, `in_3_days`, `next_week`, a `custom` date, or `on_activity` ("come back when they
  actually do something"). `on_activity` records a watermark so historical events can't wake it,
  plus a 14-day backstop, because a lead that never acts would otherwise be indistinguishable
  from a dismiss nobody chose. `manage.py unsnooze_due` sweeps both kinds.
- **Dismiss is permanent.** It writes a suppression ledger row keyed on
  `sha256("v1|{lead_id}|{action_type}")`, which `plan_outreach()` consults *before* generating
  copy — so a re-run neither resurrects the recommendation nor pays for an LLM call to
  rediscover it. Undo inside the window revokes the suppression in the same transaction.

The React build is **committed** to `project/app/static/frontend/`, and Django still serves the three routes
(`/`, `/reports/`, `/next-actions/`) as thin shells (`templates/app/spa_base.html`). So `manage.py runserver`
alone runs the whole app — **no Node required** to demo or review.

## Stack

Python 3.12 · Django 4.2 · Django REST Framework · SQLite (local) / Postgres (Docker) · React 18 · TypeScript · Vite

## Tests

```bash
pip install -r requirements-dev.txt   # adds ruff, mypy, coverage on top of runtime deps
python manage.py test project.app
ruff check . && ruff format --check .
mypy project/app/services/
coverage run manage.py test project.app && coverage report
```

CI (`.github/workflows/ci.yml`) runs all of the above on every push and PR, across the
supported Python versions and against both SQLite and a Postgres service container.

## Evals

Two harnesses under [`evals/`](evals/) measure whether the product is actually *correct*,
not just that the code runs — see [`evals/README.md`](evals/README.md) for details.

- **Rules regression suite** (`run_rules_eval.py`) — scores the deterministic
  priority/action classifier against a hand-labeled golden set and gates on regression.
  Pure Python, no network.
- **Copy quality eval** (`run_copy_eval.py`, MUS-21) — scores the *generated email*: cheap
  deterministic checks (Subject line, length, no preamble, one CTA) first, then an LLM
  judge (concrete facts / peer tone / CTA-matches-action, 1–5 each, rubric in
  [`evals/rubrics/copy.md`](evals/rubrics/copy.md)). Built on Inspect, but generation *and*
  judging both run through the same provider-agnostic layer — nothing is hardcoded to a
  vendor, and a run only calls the provider you configured.

The copy eval makes real, paid LLM calls, so it runs as a **separate** job
(`.github/workflows/copy-eval.yml`) — nightly + manual, never on push/PR — and fails on a
quality regression against `evals/baselines/copy.json`.

### Provider comparison

Because the eval drives the real provider layer, running it per provider yields a
quality-vs-cost-vs-latency comparison (assembled from separate single-provider runs;
regenerate with `python evals/run_copy_eval.py --table`). Cost is estimated (the provider
interface returns text only); latency is measured wall-clock. Over the ~38 golden leads:

<!-- COPY-EVAL-TABLE -->
| Provider | Model | Judge (1-5) | facts | tone | CTA | Checks | est. $/email | Latency (med) |
|---|---|--:|--:|--:|--:|--:|--:|--:|
| groq | `llama-3.3-70b-versatile` | 4.68 | 4.1 | 5.0 | 5.0 | 89% | $0 | 1.87s |
<!-- /COPY-EVAL-TABLE -->

Add a row by running `--provider <name> --update-baseline` for any configured provider (needs
its API key), then `--table`. Notes on this run:

- **Judge = the same provider (self-grading) here**, which is lenient — tone and CTA saturate
  at 5.0. Pass `--judge-provider <name>` to `run_copy_eval.py` to grade with a different
  (stronger) configured provider for more discriminating scores; the harness stays agnostic
  either way.
- **Cost is $0** on Groq's free tier. Latency is the **median** API call time; a full 38-lead
  judge pass on the free tier is heavily rate-limited (the recorded run spent most of its
  wall-clock waiting on `Retry-After`), so use a paid tier or `--limit` for fast iteration.

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
