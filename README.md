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

### Planner run

`plan_outreach()` runs as five phases: read the leads → classify them and build their prompts →
**call the provider** → run the two output gates → write the rows. Only phase 3 is network I/O,
and only phase 3 is concurrent: every lead is submitted to one `asyncio.gather` behind a
semaphore, so up to `OUTREACH_MAX_IN_FLIGHT` calls are in flight and the next lead starts the
instant a slot frees. (A chunked loop would be the obvious way to bound concurrency and the
wrong one — it waits for the slowest call in each batch, so one 30-second lead idles seven
workers.)

The function itself stays **synchronous**. Phases 1, 2, 4 and 5 are all ORM work, which cannot
run inside an event loop; phase 3 gets a loop of its own and hands back plain values. Prompt
construction is deliberately hoisted into phase 2 for the same reason — it walks `lead.events`,
and a lazy query inside `gather` raises `SynchronousOnlyOperation`.

How hard a run drives the provider is deployment configuration, not product configuration, so
it lives in the environment rather than in the DB-backed `LLMConfiguration` that selects the
provider. All seven are optional; the defaults below are what you get with none of them set.

| Variable | Default | What it bounds |
|---|--:|---|
| `OUTREACH_MAX_IN_FLIGHT` | `8` | Provider calls outstanding at once |
| `OUTREACH_MAX_ATTEMPTS` | `4` | Total attempts per lead (`1` = no retry) |
| `OUTREACH_INITIAL_BACKOFF_S` | `0.5` | First backoff ceiling |
| `OUTREACH_MAX_BACKOFF_S` | `30.0` | Backoff ceiling, and the cap on an honoured `Retry-After` |
| `OUTREACH_BACKOFF_MULTIPLIER` | `2.0` | Growth per attempt (must be ≥ 1) |
| `OUTREACH_REQUEST_TIMEOUT_S` | `60.0` | One HTTP attempt |
| `OUTREACH_PER_LEAD_TIMEOUT_S` | `150.0` | The whole retry loop for one lead |

Backoff is AWS **full jitter** — `uniform(0, min(cap, initial × multiplier^n))` — not an
exponential sequence with a small random nudge. Under concurrency the distinction is the whole
point: with a fixed schedule, N workers that fail at the same moment retry at the same moment,
forever, in lockstep. The same argument applies to a provider-supplied `Retry-After`, which is
why it wins on magnitude but still gets proportional jitter added on top.

The two timeouts are nested deliberately. The per-lead bound is the one that matters under
concurrency: a worker is holding 1/N of the run's throughput, so a lead that keeps drawing
retryable failures has to be given up on rather than waited out.

### Database cost

A run's **read** cost is 11 queries, flat in lead count: two dedupe-ledger reads, the leads,
their events (one prefetch), four to resolve the provider, and the transaction plus the
supersede sweep. On top of that come the INSERTs, which are *not* flat and are the backend's
decision rather than ours — Django's SQLite backend caps a batch at
`max_query_params (999) ÷ fields`, which is **55 rows** for `OutreachAction`, while Postgres
sends one statement at any size. So a 200-lead run is 14 queries on the SQLite CI leg and 11 on
the Postgres one.

Before `prefetch_related("events")` the read cost was `10 + 11N` — every lead's events were
re-read by the classifier, the prompt builder, the grounding verifier and the trace snapshot in
turn. Measured by reverting the prefetch and re-running the assertion:

| 12 leads | `COPY_VERIFY_LEVEL=off` | `standard` (the default) |
|---|--:|--:|
| Before | 95 | 143 |
| After | 12 | 12 |

At the 200-lead benchmark size that is roughly 2,200 queries before, 14 after.

`project/app/tests_planner_perf.py` is the regression lock, and it is three assertions rather
than one: a fixed count at 12 leads, the *same* read cost at 3 leads and at 60 (a constant on
its own could be updated past a reintroduced N+1; two equal counts at different sizes could
not), and the INSERT count computed from `connection.ops.bulk_batch_size` so it is right on
both backends. One case runs at `COPY_VERIFY_LEVEL=standard`, because `off` is exactly the
level that skips the verifier's event walk — the bigger half of what the prefetch buys.

`_events_list` still accepts a related manager *or* a plain list, because
`evals/run_rules_eval.py` feeds the classifier `SimpleNamespace` leads and its whole point is
running without a database. Prefetch is compatible: `.all()` on a prefetched instance is served
from `_prefetched_objects_cache`. Calling `.filter()` or `.count()` there instead would bypass
the cache and quietly restore the N+1.

### What the review queue says when there is no copy

The review queue is a *finite* list of things a human has to decide, and its whole value is
that everything in it is work. A 429 from a free tier used to land there looking exactly like
"no automated outreach pattern matched" — same `needs_human`, same shape of sentence about the
lead. A reviewer with no way to tell a provider's bad thirty seconds from a real judgement call
learns to skim the queue, which costs far more than the rate limit did.

So a rate limit is now **retried rather than escalated**, and when retries genuinely run out
the row says whose problem it is:

| Situation | What the row says |
|---|---|
| No rule matched | *BD review needed … no automated outreach pattern matched.* Real work: read the notes, decide. |
| Retryable error, budget spent | *Gave up after 4 attempt(s) over 31.2s — the groq API kept returning rate limits (HTTP 429). **This is a transient provider failure, not a problem with this lead.*** Re-run later. |
| Non-retryable error | *Failed and was not retryable (an authentication failure: …) — a configuration or provider-contract problem an engineer should look at.* |
| Anything else | The pre-existing catch-all, for a prompt that wouldn't build or a client that wouldn't resolve. |

There is deliberately **no `failure_kind` column**. The ticket asks for the distinction in
`further_action`, the frontend renders that field as prose, and an enum column would mean a
migration, a serializer field and a frontend change for something nobody queries.

A failed row does not hold the recommendation's dedupe slot, which is what makes *"re-run the
planner"* an instruction that works. Without that, the "an open item wins" rule would skip
exactly the lead the message named, and the row would sit in a finite queue for ever — clearable
only by dismissing the recommendation permanently or approving an empty draft. The next
successful run supersedes it. Unmatched-classification rows *do* keep their slot: those are real
decisions, and raising them twice a day is the duplicate-inbox bug the rule exists to prevent.

**A run has no overall deadline, and that is a known bound rather than an oversight.** Worst case
is `ceil(leads / OUTREACH_MAX_IN_FLIGHT) × OUTREACH_PER_LEAD_TIMEOUT_S` — with the defaults and
200 leads, about 62 minutes inside one synchronous `POST /api/outreach/run/`, and because phase 5
is a single transaction at the end, a proxy timing out at minute 30 writes nothing. Size
`OUTREACH_PER_LEAD_TIMEOUT_S` against your own proxy's limit and lead count. A run-level deadline
that preserves partial results needs per-task bookkeeping (the outer timeout would otherwise
cancel the gather and discard completed leads), which is more than this ticket should carry.

Bad values are rejected **at boot**, by a Django system check, with the offending environment
variable named in the message — `OUTREACH_BACKOFF_MULTIPLIER must be at least 1, got 0.5.`
rather than a `ValueError` about a dataclass field an operator has never heard of, and long
before it becomes a 500 for whoever clicked "Run Outreach Plan". `manage.py check`, and
therefore every `manage.py` command, catches it. The check calls the same accessor the planner
does, so the two can never disagree about what is valid.

Two relations are enforced as well as the individual ranges: `OUTREACH_PER_LEAD_TIMEOUT_S` must
be at least `OUTREACH_REQUEST_TIMEOUT_S` (two individually-plausible numbers the wrong way
round give a 100% failure rate), and `OUTREACH_MAX_IN_FLIGHT` has a ceiling of 256 — a typo
guard, not a capacity limit.

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

## Merge enforcement

The contract → skeleton → mini-PR workflow in [`CLAUDE.md`](CLAUDE.md) is not advisory:
GitHub rulesets on `master` and `feat/*` require two status checks — `ci-ok` (the whole
CI matrix under one name) and `workflow-gate` (classify → scope check → red-proof) — with
**zero bypass actors**. Setup, activation, and the lockout recovery path are documented in
[`docs/ci.md`](docs/ci.md); the ruleset JSON is committed under
[`docs/rulesets/`](docs/rulesets/).

Verified live on 2026-07-31 (API evidence, since a dead merge button doesn't screenshot):

- an out-of-scope mini PR ([#60](https://github.com/musatarar/Agentic-Outreach-Planner/pull/60))
  sat `BLOCKED`; plain merge was refused, and admin merge (`gh pr merge --admin`) was
  refused with *"Repository rule violations found … 2 of 2 required status checks are
  failing"* — no bypass includes the repository admin;
- direct pushes to `master` and `feat/demo` were rejected with `GH013: Changes must be
  made through a pull request`;
- a conforming landing PR ([#57](https://github.com/musatarar/Agentic-Outreach-Planner/pull/57))
  merged through the same gates.

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
