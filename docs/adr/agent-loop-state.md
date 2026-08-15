# ADR: Checkpointed agent-loop state — LangGraph vs hand-rolled (MUS-29)

**Status:** accepted · **Date:** 2026-08-12 · **Owner:** agent-loop feature

**Context.** MUS-29 needs per-lead resumable state for a bounded tool loop: crash
mid-run, resume without re-billing finished provider calls or duplicating
`OutreachAction` rows, and expose the full step trace to the reports page.
Candidates: LangGraph with a durable checkpointer, or a hand-rolled state machine
over the existing database.

**Options considered.** (1) LangGraph + `langgraph-checkpoint-postgres`/`-sqlite`.
(2) Hand-rolled: two Django models, event-sourced.

**Decision: hand-rolled**, for four repo-specific reasons:

1. **Database duality.** The repo defaults to SQLite with `DATABASE_URL` switching
   to Postgres, and CI runs both legs (`ci.yml` matrix). LangGraph's durable
   checkpointing means two backend-specific checkpointer packages — a second
   persistence layer outside Django migrations, invisible to
   `makemigrations --check` and the fixtures-based test discipline. A Django model
   is one schema, both backends, one migration file.
2. **Dependency posture.** `requirements.txt` is exact-pinned with per-dependency
   rationale and already rejected the gRPC OTLP exporter purely over grpcio's
   transitive wheel risk on the py3.13 CI leg. LangGraph drags `langchain-core`,
   `ormsgpack`, etc. — the same test fails it.
3. **Provider agnosticism.** LangGraph's model integration is LangChain chat-model
   adapters, which would sit beside and fight the repo's own `LLMClient`
   abstraction, the `complete()`/`agenerate` mock seams, and the registry-enforced
   `StubClient`. The loop must run on whichever of claude/chatgpt/deepseek/groq/stub
   the DB config selects.
4. **The loop is small.** Four read-only tools, ≤ 6 steps, one agent. The hard
   requirements — per-lead resume keyed to `trace_run_id`, no double-billing, trace
   persisted for the reports page, continuity with the existing OTel GenAI spans —
   are all keyed to this repo's tables and telemetry; LangGraph provides none of
   them for free.

**Design.** `AgentLeadRun` (the resume unit; claim via epoch-CAS conditional
UPDATE, the `LoginToken` single-use pattern from `services/login_links.py:195-197`)
+ append-only `AgentStep` (`unique (lead_run, seq)`). State is event-sourced: the
message list is `fold(steps ordered by seq)` — the inspectable trace and the crash
checkpoint are the same artifact, so they cannot drift.

**Consequences, stated honestly.**

- We forgo LangGraph's replay tooling and graph visualization; the step log and
  the reports-page trace substitute.
- **Re-billing bound is at-most-one, not zero:** each provider response and its
  step records are persisted in one transaction *after* the response arrives; a
  kill inside the window between provider response and commit re-bills at most one
  call per in-flight lead on resume. Exactly-once against an external biller is
  impossible; the write-ahead step log makes the loss ≤ 1 call per lead.
- Tools are read-only and their results are persisted as steps, so resume replays
  them from the log without re-execution.
- The documented two-overlapping-runs race (`outreach.py:2172-2180`, different
  `trace_run_id`s) is **narrowed, not closed**: idempotent finalization guards one
  run's resume via the existing `oa_one_row_per_lead_per_run` partial unique
  constraint (`models.py:193-197`), but two runs with different run ids can still
  both plan a lead. Out of scope here, as it was for MUS-26.
