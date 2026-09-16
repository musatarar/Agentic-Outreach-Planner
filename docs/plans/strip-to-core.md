# Strip to Core — audit findings & execution plan

Date: 2026-09-15 · Base: `master` @ `b374260` · Status: **executed — Phases 1–7 landed on `claude/lucid-thompson-o5tckm`, 2026-09-15** (numbers at the end)

The owner's brief: strip the project down to four pillars — **LLM layer, OutreachAction,
Users, Leads** — and make it as approachable and plug-and-play as possible. This document
is (I) an adversarial audit of the whole repo including its environment/config surface,
and (II) the execution plan, sized so each phase is one reviewable PR with all gates green.

Evidence is cited as `file:line` throughout. (CLAUDE.md mandates citing "area codes" from
`docs/areas.toml` instead — that file does not exist; see finding F-1.)

Headline numbers:

| | today | after strip |
|---|---|---|
| Tracked lines (excl. built bundle) | 44,326 | ≈ 20,500 (−54%) |
| Backend code / backend tests | 10,573 / 15,378 | ≈ 6,300 / ≈ 6,900 |
| Frontend src | 9,077 | ≈ 5,400 |
| evals/ | 2,667 | 511 (rules eval only) |
| Django models / API routes / pages | 16 / 24 / 9 | 5 / ~13 / 4 |
| CI workflows | 4 (one broken, one spends money nightly, one leaks auth) | 1 |
| Env vars read by the app but undocumented | 7 | 0 (matrix closed) |
| Test suite | 956 tests, 10 s, green | ~500 tests, faster, green |

---

## Part I — Audit findings

### I.1 Security & environment (fix regardless of the strip)

- **S-1 · The demo tunnel leaks its own auth.** `demo-tunnel.yml` runs the app on a
  public `*.trycloudflare.com` origin with `LOGIN_LINK_DELIVERY: console`
  (`.github/workflows/demo-tunnel.yml:55`) and then live-tails the server log into the
  Actions job log (`:197`, and again at teardown `:218-225`). On a public repo, Actions
  logs are world-readable — **anyone who can read the run log can sign in**. README:91's
  claim that "the allowlist is the gate" is false in this configuration. The workflow's
  other hygiene (per-run minted keys, `DEBUG=False`, masking) is genuinely careful, which
  makes the one hole easy to miss.
- **S-2 · A working `DJANGO_SECRET_KEY` is committed.** `.env.example:17` ships a real
  key; `.env.example:11-12` says it's "fine to leave as-is", directly contradicting
  CLAUDE.md:16-17 ("REPLACE … never keep the example's values"). Every documented path
  (`README`, the SessionStart hook `.claude/hooks/session-start.sh:21`) copies it
  verbatim into `.env`, and since `.dockerignore` excludes `.env` but not `.env.example`,
  the key is baked into every Docker image.
- **S-3 · `DEBUG=True` reaches the "production" container.** `.env.example:21` ships
  `DJANGO_DEBUG=True` uncommented; docker-compose interpolates `.env`
  (`docker-compose.yml:23`), so `${DJANGO_DEBUG:-False}` resolves to True — falsifying
  `docker/entrypoint.sh:9-10` ("the image runs with DEBUG off"). The entrypoint is
  `runserver 0.0.0.0:8000 --insecure` as root, no gunicorn anywhere.
- **S-4 · Nightly unattended provider spend, gating against a void baseline.**
  `copy-eval.yml` crons real, paid LLM calls at 07:00 daily with no budget ceiling.
  Worse: commit `491cb2e` hand-relabeled `evals/baselines/copy.json` +
  `evals/results/copy-groq.json` from `llama-3.3-70b-versatile` to `openai/gpt-oss-20b`
  **without re-running the eval** (both still carry `generated_at: 2026-07-26` and the
  llama scores) — the nightly gate has compared apples to oranges since 2026-08-17.
- **S-5 · `redteam-eval.yml` is broken on both trigger paths.** It never runs
  `migrate`/`seed_llm_catalog` (compare `copy-eval.yml:59-61`) and
  `run_redteam_eval.py` reaches the ORM without `django.setup()`
  (`services/llm/__init__.py:71` → `config.py:54`) → `AppRegistryNotReady`, verified.
  It also documents a `config.toml` that doesn't exist anywhere (`:12`, `:21`,
  `run_redteam_eval.py:189`). It has been burning a nightly cron failing.
- **S-6 · Demo data is PII-shaped without the fake-data conventions.**
  `raw_data/leads.json` uses routable NANP area codes (303, 614) instead of reserved
  `555-01xx`, and invented-but-plausible domains instead of `example.com` — while
  `evals/run_redteam_eval.py:60` shows the repo knows the convention. Synthetic, but
  indistinguishable at a glance from a real CRM export, and served on public tunnels.
- **S-7 · `.claude/settings.json` pre-approves what CLAUDE.md forbids.** 276 allow
  entries, no denies; blanket `Read`/`Edit`/`Write` (`:12-14`) that make the other ~270
  narrow entries moot; 49 entries hardcode the author's local
  `/Users/musatarar/...` path (can never match elsewhere, leak the machine layout);
  ~15 entries name deleted files; two MCP entries name a server `.mcp.json` doesn't
  define; and a pre-approved `Bash(rm project/app/migrations/0006_….py)` (`:231`) — a
  standing approval to delete a committed migration, the exact act CLAUDE.md:76-78 says
  a hook blocks.

### I.2 Fictional governance — docs describing systems that don't exist

This is the "AI slop debt" the owner is feeling: not sloppy code, but **confidently
documented fiction** that misdirects every human and agent who reads it.

- **F-1** CLAUDE.md:69-73 mandates the *area code* system: `docs/areas.toml` mapping
  governed seams, `# area:` comments marking binding sites, "reference areas — not line
  numbers, not tickets". Reality: the file does not exist and there are **zero** `# area:`
  comments in the codebase. The convention it forbids (MUS-nn ticket refs) is the one
  actually used — 183 references in Python alone.
- **F-2** CLAUDE.md:78 claims a hook blocks migration edits ("A hook blocks this; do not
  work around it"). The only hook configured is `SessionStart`
  (`.claude/settings.json:285-296`). **No such enforcement exists** — while S-7's
  pre-approved `rm` of a migration points the other way.
- **F-3** CLAUDE.md references skills `/safe-migration` (`:86`) and `release-check`
  (`:201`) that were never committed. `.claude/skills/` contains neither.
- **F-4** The Phoenix/OTel observability stack is documented in three files and
  implemented in zero: `.env.example:52-53` ("`docker compose up` … runs Arize Phoenix"),
  `requirements.txt:31-32`, `telemetry/setup.py:38-39` all describe a compose service
  that does not exist — `docker-compose.yml` has only `db` and `web` and not one `OTEL_*`
  line. Telemetry (1,333 LOC + 4 pinned packages) exports nothing in every documented
  run path.
- **F-5** DEMO.md describes a different application: "single vanilla-JS page" (it's a
  React 18/TS SPA), "27 tests" (956), `project/app/views.py` (deleted in `76f6e8a`),
  "Snooze … intentionally stubbed" (it shipped), Claude-only (default provider is groq).
- **F-6** README.md:30 claims "120 passing tests" (off by ~8×); §"Database cost" /
  §"Wall clock" (~120 lines) are marketing-register essay prose. The planning skill
  cites `docs/plans/mus-29-planning-experiment-report.md` (missing) and
  `services/llm/sanitize.py` (wrong path — it's `project/app/services/sanitize.py`);
  `.gitattributes:5` cites a missing `CONTRACT-MUS-35.md`; CLAUDE.md:97 warns about a
  "checked-in SQLite file" that is not checked in; `settings.py` carries four Django
  **5.2** doc links in a Django 4.2 project; `requirements-dev.txt:22-23` justifies
  `pyyaml` with a "workflow-gate rework" whose three files don't exist (and `import yaml`
  appears nowhere); the catalog seeds `claude-sonnet-5` while the adapter default is
  `claude-sonnet-4-6` (`seed_llm_catalog.py:37` vs `claude.py:37`).
- **F-7** CLAUDE.md's own env rule ("every new setting gets a .env.example entry and a
  docker-compose passthrough", `:170`) is violated in both directions: **7 vars the app
  reads are absent from `.env.example`** (`OUTREACH_AGENT_*` ×4,
  `OUTREACH_TRACE_CONTENT_ENABLED` — a PII-at-rest switch documented only in
  SECURITY.md, `DJANGO_LOG_LEVEL`, `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`), and 4
  documented vars have no compose passthrough (`COPY_VERIFY_LEVEL`, `TRIAGE_*` ×3).

### I.3 Dead and inert code

- **D-1 · `services/dispatch.py` + `OutboundSend` are a dead island** (verified
  independently). Zero non-test importers of `dispatch`; the approve path
  (`views/queue.py:313-372`) writes the approval hash under a comment reading
  `# Record of human dispatch` but **never dispatches anything**; the only send channel
  is the literal string `"console"`. `tests_agent_loop_approval_gate.py:13` carries the
  tell: `# OutboundSend: lazy until Task 3 lands` — Task 3 never landed. The sha256
  double-check machinery CLAUDE.md ring-fences guards a send that does not exist.
  The real outbound path today is the clipboard button.
- **D-2 · The agent loop is inert by default and self-contained.**
  `OUTREACH_AGENT_ENABLED` defaults off; `outreach.py:1980` says the merged code "is
  inert until an operator opts in". Cascade: `services/agent/` (923) + `models/agent.py`
  (3 models incl. `AEAvailabilitySlot`) + `views/trace.py` (with the flag off, the trace
  endpoint 404s for **every** action) + `ProviderTrace`/`ProviderTraceContent` (sole
  production writer is `agent/state.py:307,318` — verified) + `trace_run_id` field and
  its partial constraint (all remaining consumers are agent/telemetry/trace) + 8 agent
  test files (~2,000 LOC) + `docs/adr/` + `docs/contracts/`. ≈ **3,900 LOC total for a
  feature that ships off**.
- **D-3 · Telemetry is always-called, never-exporting.** Not flag-gated at call sites —
  `plan_outreach` always builds spans (`outreach.py:1901,2058,2071,2078-2082`), the SDK
  just exports nowhere in any shipped config (F-4). 1,333 LOC + ~1,800 test LOC + 4
  requirements pins. Removal means unpicking the `lead_spans` positional zip at the
  phase 3→4 boundary — the one mechanically delicate cut in this plan.
- **D-4 · Orphans:** `LeadComposeView` (`urls.py:55`) — a whole backend feature with
  zero frontend callers; `QueueDetailView` + its only (dead) caller `fetchQueueItem`
  (`endpoints.ts:75`); `unsnooze_due` command — a sweeper with no scheduler anywhere
  (no cron, CI, or entrypoint reference); `dump_edit_corpus` command — README one-liner
  only; `useHotkeys.ts:25 isAllowedInTextEntry`; `tests_planner_perf.py:46
  PLANNER_INSERT_FIELDS` (dead constant, silently rots).
- **D-5 · Ceremony tests:** `tests_gitignore.py` (23 LOC + subprocess to assert one
  gitignore line, and `check-ignore` can't even distinguish which rule matched);
  `tests_docker_entrypoint.py` (greps a shell script for `--insecure`, its stated
  premise falsified by S-3); `tests_frontend.py` (asserts literal `<title>` strings);
  `tests_stub_provider.py:322-363` (source-text ordering asserts on another file + a
  180 s benchmark subprocess inside the unit suite); `tests_trace.py:199` (re-runs the
  rules eval that CI already runs as its own job); `tests_verify_spans.py:306`
  `PRE_CHANGE_VIOLATIONS` (~100-line frozen snapshot of pre-refactor message prose —
  rewording an operator-facing string reddens CI); `tests_telemetry_genai.py:598-637`
  (asserts four chained private OpenTelemetry SDK attributes, then re-derives constants
  from themselves).

### I.4 Duplication & architectural bloat

- **A-1 · Three pages render the same model.** PlannerPage, DashboardPage and
  ReportsPage all present `OutreachAction` (Dashboard literally re-fetches the same
  `GET /api/outreach/` as Planner; `GET /api/reports/` is the same queryset unfiltered).
  Dashboard's one unique feature is a form filing "propose a new action type" rows at
  `status=pending_engineering` — a feature-request UI for engineering.
- **A-2 · Two generations of review flow coexist**: the `ReviewDecision`
  resolution flow (`review-queue/`, `review-decisions/`) and the 9-endpoint triage queue
  (`queue/*`). `ReviewDecision`'s send-kinds exist to authorize the dead dispatch (D-1).
- **A-3 · Copy-paste pairs:** `_as_date`, `_jsonable`, `_events` duplicated between
  `outreach.py` and `verify.py`; `normalize_copy` defined in both `verify.py:326` and
  `queue_copy.py:17`; two **divergent** `formatUsdCompact` implementations
  (`util/labels.ts:57` vs `done/format.ts:91`) — the same book size renders differently
  on `/leads` and `/done`.
- **A-4 · Repo's own rules broken by its own endpoints:** `GET /api/reports/` serializes
  the full unbounded table with no pagination or throttle scope
  (`views/outreach.py:61-67`), violating CLAUDE.md:118-119.

### I.5 What is *not* slop (kept, and worth saying)

The audit hypothesis "AI slop everywhere" is falsified in four places, which is exactly
why the strip should cut **by subsystem, not by line**:

- `services/llm/` is the best-factored code in the repo: three OpenAI-compatible
  providers are 26 LOC each over one 341-LOC base; `claude.py` correctly stands alone on
  the anthropic SDK; the retry/error taxonomy is real. Keep unchanged.
- The frontend has effectively **zero dead CSS** (the ~30 "unused" selectors are all
  template-literal false positives), zero unused dependencies, a real token system, and
  a committed bundle that is fresh and CI-guarded (`ci.yml:68-69`).
- Test *quality* is high: ~10 mock-assertions across 15k LOC; `tests_llm.py` uses real
  `httpx.Response`/`anthropic.types` objects; `tests_planner_perf.py` budgets really are
  computed from `connection.ops.bulk_batch_size` and hold on both backends. The bloat is
  volume aimed at subsystems being deleted (~49% of test LOC), not bad tests.
- SECURITY.md is the most accurate document in the repo (every spot-check passed,
  candid "Known gaps"), and `sanitize.py` + the magic-link implementation do what it says.

---

## Part II — The strip plan

### II.0 Target product

> **Leads in → rules pick who/why → LLM drafts copy (sanitized, grounded) → a human
> reviews in one inbox (live grounding check, edit, approve/dismiss) → approved copy
> leaves via the clipboard.** Nothing sends automatically; there is no send machinery to
> pretend otherwise (it was dead code — D-1).

| Pillar | Backend | Models | Routes | Frontend |
|---|---|---|---|---|
| **Leads** | `models/lead.py`, `views/leads.py`, `ingest_data`, `sanitize.py` | `Lead`, `Event` | `leads/`, `leads/<id>/compose/` | LeadsPage (+ per-lead **Generate** button, finally wiring the orphaned `LeadComposeView`) |
| **OutreachAction** | `services/outreach.py` (rules + planner), `verify.py`, `queue_copy.py`, `dedupe.py`, `actions.py`, slim review views | `OutreachAction` (slimmed), `DismissedOutreachKey` | `outreach/run/`, `outreach/` (paginated), `outreach/<pk>/edit|verify|approve|dismiss|reopen` | InboxPage (review core: LeadCard, DraftEditor, VerifiedDraft + live verify, ActionBar, CopyButton) |
| **Users** | `views/auth.py`, `login_links.py`, `authentication.py`, `throttling.py` — **unchanged** | `LoginToken` | `auth/*` ×4 | SignInPage, ConsumePage |
| **LLM layer** | `services/llm/` — **unchanged** (base, openai_compatible, claude, groq, chatgpt, deepseek, stub, errors, retry, runtime, config) | — (see D-A) | — (see D-A) | — (see D-A) |

**5 models** (from 16), **~13 routes** (from 24), **4 pages** (from 9).

Slimmed `OutreachAction`: keep `lead, created_at, priority, action_type, reason,
suggested_copy` (immutable), `edited_copy`, `needs_human, further_action, status,
status_changed_at, dedupe_key, verification`. Drop `snooze_*` ×3, `dismiss_reason` moves
to `DismissedOutreachKey` only, `trace_run_id` + `oa_one_row_per_lead_per_run`,
`rule_trace`. Status machine: `pending → approved|dismissed`, both reversible via
`reopen` (which also revokes the suppression row — replacing the entire undo-window
apparatus with one conditional UPDATE). `sent` is deleted with the send fiction.
The `dedupe_key` supersede-on-rerun logic stays untouched (`outreach.py:2139-2142` keys
on `dedupe_key`, not on anything being cut — verified).

### II.1 Decisions for the owner (recommendation first; approving the plan approves the recommendations)

- **D-A · LLM config: env-only (recommended) vs keep the DB catalog.** Cut
  `LLMProvider`/`LLMModel`/`LLMConfiguration`, `crypto.py` (Fernet),
  `LLM_KEY_ENCRYPTION_KEY`, the `app.E001` boot check, `seed_llm_catalog`,
  `/api/llm/*` ×3, and SettingsPage (566 LOC). Selection becomes `LLM_PROVIDER` +
  `LLM_MODEL` + `<PROVIDER>_API_KEY` env vars — the fallback path `llm/config.py`
  already implements. This is the single biggest plug-and-play win: one fewer secret,
  no seeding step, no catalog/adapter drift (F-6's sonnet-4-6 vs sonnet-5). Cost: no
  in-UI provider switcher. *Alternative:* keep the catalog — then SettingsPage, 3
  models, crypto and the seed command all stay (≈ +1,600 LOC) and the setup gains a
  mandatory seed step + optional second secret.
- **D-B · Keep `Event` + the rules engine (recommended) vs cut to "draft for every
  lead".** The 6 rules over lead/event history are the "agentic" in the name — cutting
  them leaves a mail-merge. Events also ground the verifier. Cost of keeping: ~950 LOC
  of pure, well-tested functions. *Alternative:* cut Event + rules (−~1,400 LOC more)
  and generate for every lead unconditionally.
- **D-C · Keep the live-verify draft editor (recommended) vs plain textarea.** The
  span-highlighted, fail-closed grounding check is the product's one distinguishing
  safety feature, and the backend gate must stay regardless (CLAUDE.md security
  invariant — this plan does not weaken it). Cost: `queue_copy.py`, the `verify`
  endpoint + throttle scope, `useLiveVerify`/`spans.ts`/`VerifiedDraft`, and
  `tests_verify_spans.py` minus its frozen-prose table (D-5). *Alternative:* textarea +
  verify-on-save only (−~1,900 LOC more).
- **D-D · Keep the rules eval (recommended) vs delete `evals/` entirely.**
  `run_rules_eval.py` + `golden/leads.jsonl` + `baselines/rules.json` (511 LOC) is
  free, deterministic (no DB/network/clock), runs in ms in CI, and imports only
  `services/{actions,outreach}` (verified — no dependency on anything being cut). It is
  the regression net for the one subsystem with real business logic. Everything else in
  `evals/` goes (S-4, S-5, bench, pricing, committed results).
- **D-E · Migration endgame: squash to a fresh `0001` (recommended, as the final,
  separately-executed step) vs keep the additive chain.** All strip migrations are
  written additively first (new `DeleteModel`/`RemoveField` files — never editing
  committed ones), so the repo is safe at every point. Since no production deployment
  exists (CLAUDE.md:199-201 says so), a final squash to one clean `0001` is safe and is
  the plug-and-play answer; demo DBs are recreated by `populate_demo_data`. This is a
  standing CLAUDE.md hard-rule exception, so it ships as its own PR that the owner
  merges knowingly. *Alternative:* live with ~13 chained migrations forever.

### II.2 Execution phases (each = one PR; all gates green before merge)

Gates for every phase: `ruff check . && ruff format --check .` · `mypy
project/app/services/` · `python manage.py makemigrations --check --dry-run` · `python
manage.py test project.app` · coverage ≥ 90 (source and tests for a subsystem are always
deleted **in the same phase** — deleting the well-tested-elsewhere code raises coverage;
deleting tests alone would drop it below the floor) · frontend phases additionally:
`npm run typecheck && npm test && npm run build` + commit the rebuilt bundle
(`git diff --exit-code -- project/app/static/frontend/` must pass).

**Phase 1 — Stop the bleeding (no product behavior change)**
1. Delete `demo-tunnel.yml` (S-1), `redteam-eval.yml` (S-5), `copy-eval.yml` (S-4).
2. `.env.example`: replace the committed key with `DJANGO_SECRET_KEY=` + generation
   instructions (S-2); ship `DJANGO_DEBUG=` blank (S-3); delete the Phoenix section
   (F-4). Add `scripts/setup_env.py` (~15 LOC): copies `.env.example` → `.env` with a
   freshly generated secret key, so plug-and-play setup stays one command **without** a
   shared committed secret. Update the SessionStart hook and README to call it.
3. Delete the dead island: `services/dispatch.py`, `OutboundSend`,
   `tests_agent_loop_approval_gate.py`'s dispatch halves; migration
   `0010_delete_outboundsend`. Remove `STATUS_SENT`.
4. CLAUDE.md minimal truth pass: delete the area-code section (F-1), the hook claim
   (F-2), the two phantom skills (F-3), the dispatch-gate references (D-1). Full
   rewrite comes in Phase 6; the fiction goes now so it stops misdirecting the
   sessions executing Phases 2–5.
5. Regenerate `raw_data/*.json` with `555-01xx` phones and `example.com`-family domains
   (S-6); update `tests_core.py` count pins only if counts change (keep 12 leads).

**Phase 2 — Remove the inert agent loop (D-2)**
`services/agent/`, `models/agent.py` (3 models), `views/trace.py` + route,
`ProviderTrace`/`ProviderTraceContent`, serializer trace fields, `OUTREACH_AGENT_*` +
`OUTREACH_TRACE_CONTENT_ENABLED` settings, `outreach.py` agent seams
(`:16-17, 1523-1545, 1607/1621 agent_plans threading, 1911-1918, 1983-2050, 2155-2161`),
`AgentDisabled`/`UnknownRun`/`resume_run_id` in `views/outreach.py`, 9 agent/trace test
files (~2,300 LOC), `docs/adr/` + `docs/contracts/`, `AEAvailabilitySlot` seeding in
`ingest_data`. Migration `0011_delete_agent_models`.

**Phase 3 — Remove telemetry (D-3, F-4) — the one delicate cut**
Delete `services/telemetry/`; unthread `outreach.py`: `run_span` wrapper
(`:1901,1952`), `provider_call_scope` (`:1205,1213`), the `lead_spans` list and its
positional zip through phases 3–4 (`:2058,2071,2078-2082`), `finish_lead`; drop
`apps.py` ready-hook; drop `trace_run_id` + `oa_one_row_per_lead_per_run` (migration
`0012`); delete 5 telemetry test files + `tests_telemetry_support.py` (~2,500 LOC);
remove the 4 OTel pins from `requirements.txt`. plan_outreach's phase structure,
timeouts, retries, and the perf-test query ledger must be byte-for-byte preserved —
`tests_planner_perf.py` and `tests_planner_async.py` are the regression net and are
**not** edited (the 11-query ledger loses no query: span writes were never ORM queries).

**Phase 4 — One review flow (A-1, A-2)**
- Backend: replace the 9 `queue/*` routes + `views/queue.py` (528) +
  `serializers/queue.py` with ~5 slim endpoints on `outreach/<pk>/`
  (`edit`, `verify` (D-C), `approve`, `dismiss`, `reopen`); delete snooze (fields,
  trigger vocabulary, `unsnooze_due`), the undo-window (`TRIAGE_UNDO_WINDOW_SECONDS`),
  the done feed, `TRIAGE_*` settings; delete `ReviewDecision` (both generations of A-2),
  `OutreachEdit` + `dump_edit_corpus` (its eval corpus is cut with D-D's paid evals);
  `DismissedOutreachKey` + `dedupe.py` **stay** (they are why a re-run doesn't resurrect
  dismissed items or duplicate pending ones); add pagination + throttle scope to the
  list endpoint (fixes A-4); delete `explain()`/`Condition` rule-trace primitives
  (`outreach.py:213-410`) and the `rule_trace` field — `reason` (plain text) remains
  the human-readable why; delete the "reviewer prose for failed generations" block only
  if the inbox drops it (it stays — it renders in the draft panel). Migration `0013`.
- Frontend: delete DashboardPage, ReportsPage, DonePage + `done/*` + `done.css`,
  `util/trace.ts`, `RuleTrace`, Snooze/Dismiss popover apparatus (dismiss reason becomes
  a plain select), ShortcutOverlay/useHotkeys, QueueRail; merge PlannerPage's
  run-button into LeadsPage and wire the per-lead **Generate** to the orphaned
  `LeadComposeView` (D-4); trim `types.ts` (~487→~310) and `endpoints.ts` (incl. dead
  `fetchQueueItem`); Nav 7→3 links; fix the `formatUsdCompact` divergence by deletion
  (A-3); rebuild bundle.
- Tests: delete `tests_queue.py` (1,585), trim `tests_api.py` review-decision classes;
  add `tests_review.py` (~300) covering the 5 endpoints end-to-end, both decision
  outcomes + reopen, and `suggested_copy` immutability. Delete
  `agent_loop_reports_trace.test.ts`; keep the other 4 frontend test files.

**Phase 5 — LLM config to env (D-A)**
Delete the 3 catalog models (migration `0014`), `views/llm.py`, `serializers/llm.py`,
`crypto.py`, `checks.py:E001`, `seed_llm_catalog`, SettingsPage + its CSS + Nav link;
implement `LLM_PROVIDER`/`LLM_MODEL` reads in `llm/config.py` (the env fallback already
exists — this promotes it to the only path); `.env.example` gains the two vars;
`populate_demo_data` drops the seed step. `cryptography` leaves `requirements.txt`.
Trim `tests_llm.py`'s Fernet/key-source classes; the three overlapping status-code
tables collapse to one parametrized table (its one sanctioned test edit).

**Phase 6 — Meta: evals, CI, tool-config, docs**
- `evals/`: keep `run_rules_eval.py`, `golden/`, `baselines/rules.json`, `__init__.py`
  (D-D); delete copy/redteam/bench/pricing/rubrics/results + `copy_checks.py` +
  `tests_copy_scorers.py`, `tests_redteam.py`, `seed_synthetic_leads`,
  `inspect-ai` + `pyyaml` from requirements-dev (F-6).
- CI: single `ci.yml` — lint, mypy, migrations-check, rules-eval, backend matrix
  (keep 3.12/3.13 × sqlite/postgres; it's free), frontend typecheck/test/build +
  bundle-staleness, `ci-ok`. Delete the coverage-badge job (a 5th full suite run +
  force-push for cosmetics).
- Ceremony tests: delete `tests_gitignore.py`, `tests_docker_entrypoint.py`,
  `tests_frontend.py`; `tests_stub_provider.py` keeps the stub-gate behavior tests,
  drops the whole-tree grep manifest, source-text asserts and benchmark subprocess;
  `tests_trace.py` dies in Phase 2; keep `tests_llm_runtime.py`'s Django-free-import
  guard (genuinely load-bearing).
- `.claude/settings.json` → ~25 lines (SessionStart hook + a handful of real allows);
  delete both skills (planning-discipline cites missing files and governs removed
  subsystems; webapp-testing duplicates the built-in), `.cursor/`, `.mcp.json` (Linear
  is personal tooling — configure it user-level, not in the repo).
- Docker: entrypoint drops the false DEBUG comment (S-3), README documents dev-server
  status honestly; compose env list shrinks to surviving vars.
- Docs: rewrite README (~150 lines: what it is, quickstart via `setup_env.py`, env
  table, commands, honest numbers); rewrite CLAUDE.md (~70 lines, everything in it
  true); trim SECURITY.md to the surviving surface (keep its register — it earned it);
  delete DEMO.md (F-5); fix `settings.py` 5.2 links, migrate the remaining bare
  `int()` env reads to `_env_int`; close the env matrix (F-7: documented == read ==
  compose-passthrough, verified by grep in the PR description).

**Phase 7 (optional, own PR, owner-merged) — squash migrations to a fresh `0001` (D-E).**

### II.3 Human sign-off register

CLAUDE.md's "always needs a human" list intersects this plan as follows — **approving
this plan is the sign-off** for each, scoped exactly to the phases above:

| Gate | Items | Where |
|---|---|---|
| Migrations | `0010`–`0014` additive delete-migrations; optional squash | P1–P5, P7 |
| `services/dispatch.py` + approval gate | deletion of the dead island (evidence D-1) | P1 |
| Verifier & sanitization | **no weakening** — gate and sanitizer kept; only the frozen-prose test table (D-5) and span-half scope per D-C | P4 |
| Audit-table retention | deleting `ProviderTrace*`, `AgentStep` (P2), `OutreachEdit` (P4); `LoginToken` untouched | P2, P4 |
| Auth/session/throttle | **untouched** (throttle scope for the verify endpoint carries over) | — |
| Provider spend | net reduction only: nightly paid evals deleted (S-4), retry/timeout knobs unchanged | P1, P6 |
| `.claude/` + CI workflows | P1 workflow deletions, P6 rewrite | P1, P6 |
| Existing-test deletions/edits | wholesale with their subsystems (listed per phase); the three named surgical edits: `tests_llm.py` tables (P5), `tests_stub_provider.py` trim (P6), `PRE_CHANGE_VIOLATIONS` removal (P4) | P2–P6 |
| Eval baselines | `baselines/copy.json` deleted as void (S-4); `baselines/rules.json` untouched | P6 |

### II.4 Execution guardrails

- Plan against origin: every phase starts by fetching `origin/master` and verifying the
  files it touches still match this document; drift → update the plan, not the code.
- Never edit a committed migration; additive only until P7.
- `suggested_copy` immutability, the fail-closed verifier, sanitize-before-prompt, and
  magic-link semantics survive every phase byte-for-byte; any test asserting them that
  fails is a stop-and-ask, never an edit.
- The committed bundle is rebuilt and committed in the same PR as any `frontend/`
  change; never hand-edited.
- Each phase's PR description carries: LOC delta, models/routes delta, the grep proving
  no dangling references to deleted symbols, and the green gate transcript.

### II.5 Definition of done

- `git clone` → `pip install -r requirements-dev.txt` → `python scripts/setup_env.py` →
  `migrate` → `populate_demo_data` → `runserver` → sign in via console link → generate
  → review → approve → copy. Two commands of config (`LLM_PROVIDER`, one API key) to go
  from stub-less demo to real drafts.
- Every environment variable the app reads appears in `.env.example` and docker-compose,
  and vice versa (closed matrix).
- Zero references anywhere in the repo to files, hooks, skills, services, or systems
  that do not exist; every number in README is generated from the tree or deleted.
- One CI workflow; suite green on the 4-cell matrix; coverage ≥ 90; rules eval green
  against untouched baselines.
- ≈ 20,500 tracked lines; 5 models; 4 pages; 1 secret (`DJANGO_SECRET_KEY`) + 1 API key.

### II.6 Out of scope (explicitly)

Real email/CRM sending (the strip removes the *fiction* of sending; adding real
dispatch later is a fresh, gated design), gunicorn/production serving (unchanged:
still a dev-server demo, now honestly labeled), auth redesign (D1: magic-link stays
as-is), re-adding telemetry behind a real backend, and any new features.

---

## Executed

Phases 1–7 landed. Measured as in the headline table (built bundle and lockfile
excluded), estimate → actual: tracked lines ≈20,500 → **22,933** (−48% from 44,326);
backend tests 6,900 → **8,139**; frontend src 5,400 → **4,584**; evals 511 → **667**;
models/routes/pages 5/~13/4 → **5 / 14 / 4**; suite ~500 → **542 green at 96% coverage**;
167 tracked files. The overshoot is tests kept rather than cut, plus one route that
stayed its own (`leads/<id>/compose/`). Phase 7 squashed the 14 migrations into a single
`0001_initial` — an owner-approved, one-time override of the never-edit-a-committed-
migration rule, safe because nothing is deployed; existing checkouts delete `db.sqlite3`
and re-seed.
