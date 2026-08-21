# Adversarial schema review

Reviewed at `b0e3430`. DDL below is the real Postgres output of Django's schema
editor for the current model state, not a paraphrase.

---

## Questions I had to answer myself

The template arrived with the context blocks unfilled. I derived what I could and
assumed the rest. Every assumption below is load-bearing; correct me and findings move.

| | Assumed | Derived from |
|---|---|---|
| **A1** | Postgres 16, `READ COMMITTED`, container defaults (`work_mem` 4MB, `shared_buffers` 128MB) | `docker-compose.yml` → `postgres:16-alpine`; no `SET` anywhere in the codebase |
| **A2** | Django 4.2.30 ORM, `ATOMIC_REQUESTS` off, no `.iterator()` anywhere | `requirements.txt`; grep |
| **A3** | Single tenant | no tenant column exists |
| **A4** | 10× = 10,000 leads, planner runs daily; 100× = 100,000 leads; ~5 events/lead/month | demo seed is 12 leads / 61 events; the planner writes one row per lead per run |
| **A5** | No DBA. Migrations run unattended via `manage.py migrate` in the container entrypoint. Low daytime downtime tolerance | `docker/entrypoint.sh:6` |
| **A6** | No compliance program stated; I assume GDPR/CCPA erasure applies because lead PII (name, email, phone) is stored and copied into free text | `models/lead.py`, `models/llm.py` |

### F0 — Access patterns were not supplied. That is the finding.

I reconstructed thirteen query shapes from `project/app/views/` and the management
commands. Nobody has written them down, which means nobody owns the latency budget,
and two of the three worst findings below are queries that were never designed —
they are `for` loops that happen to hit a database. The list I reconstructed is in
Pass 2 under *Scale & physics*; if it is wrong, that is itself the answer.

---

## Pass 1 — Steelman

**`OutreachAction` as an append-only decision log rather than mutable current state.**
Every run writes a new row; nothing is updated in place except lifecycle columns. This
gives you a free audit trail of what the planner recommended and when, survives a bad
model deploy, and makes the eval corpus (`OutreachEdit`) meaningful because
`suggested_copy` is immutable. Most teams get this backwards and mutate.

**`rule_trace` and `verification` as versioned JSONB snapshots.** Both carry an explicit
`"version"` key and a `"today"`, and the comment is emphatic that they are never
recomputed. This is correct and unusual: the trace is only true as of the date it was
computed, and freezing it is the only way `explain()` stays honest after the rules
change. A relational decomposition of a rule trace would be a schema you rewrite every
time you add a condition.

**Snapshot strings on `ProviderTrace` instead of FKs to the catalog.** `provider` and
`model_id` are `varchar` copies, not references to `LLMProvider`/`LLMModel`. The
docstring's justification is right: the catalog is mutable seeded data, and a reseed must
not rewrite what actually ran. Resisting a foreign key here is the correct instinct.

**Partial unique indexes doing real work.** Three of them, each expressing a rule that a
plain unique constraint cannot:

```sql
CREATE UNIQUE INDEX "oa_one_row_per_lead_per_run" ON "app_outreachaction"
  ("trace_run_id", "lead_id") WHERE NOT ("trace_run_id" = '');
CREATE UNIQUE INDEX "rd_one_resolution_per_action" ON "app_reviewdecision"
  ("outreach_action_id") WHERE "kind" IN ('select_existing', 'propose_new');
CREATE UNIQUE INDEX "rd_one_live_send_per_action" ON "app_reviewdecision"
  ("outreach_action_id") WHERE ("kind" IN ('approve_send','reject_send') AND "voided_at" IS NULL);
```

The third is the good one: "one *live* send decision, and undo voids rather than deletes"
is a business rule that survives in the database, not in a service.

**Epoch-CAS on `AgentLeadRun` instead of `SELECT ... FOR UPDATE`.** `claim_epoch integer`
with a conditional `UPDATE` returning rowcount. The stated reason — `FOR UPDATE` is a
whole-database lock on SQLite — is real, and the CAS is also strictly better on Postgres:
no lock held across the network call to the provider. Dead-worker takeover falls out for
free.

**`timestamp with time zone` on every datetime, `date` only for genuinely date-typed
lead fields.** No naive timestamps anywhere. `numeric(10,4)` for per-token prices rather
than float.

**The `LLMConfiguration` singleton via `CHECK ("id" = 1)`.** Enforced twice — in `save()`
and in the database. The database half is the half that matters.

---

## Pass 2 — Break it

### Correctness & invariants

**FK actions do not exist in this database.** Every foreign key is emitted as:

```sql
ALTER TABLE "app_event" ADD CONSTRAINT "app_event_lead_id_3b49d5b4_fk_app_lead_id"
  FOREIGN KEY ("lead_id") REFERENCES "app_lead" ("id") DEFERRABLE INITIALLY DEFERRED;
```

No `ON DELETE`. Django's `on_delete=CASCADE` on `Event`, `OutreachAction`,
`AgentStep`, `DismissedOutreachKey`, and `on_delete=SET_NULL` on
`DismissedOutreachKey.source_action` are **Python**. The database's behavior is
`NO ACTION`, deferred to commit. Three consequences, in order of how much they will
hurt:

1. `DELETE FROM app_lead WHERE id = 'lead_042';` in psql at 2am does not cascade. It
   succeeds, then throws at `COMMIT` — after you have typed `COMMIT` and believe you are
   done. Anyone who does this via a script wrapped in autocommit gets the error; anyone
   who does it in a transaction with other work loses the whole transaction.
2. Deleting a lead *through the ORM* pulls `Event`, `OutreachAction`, `ReviewDecision`,
   `OutreachEdit`, `AgentLeadRun`, `AgentStep` and `DismissedOutreachKey` into one
   Python-driven transaction — then hits `OutboundSend`, which is `PROTECT` on both FKs,
   and raises. So an erasure request for any lead whose copy was ever sent fails
   *partway through* a cascade the database never agreed to.
3. `DEFERRABLE INITIALLY DEFERRED` means FK violations surface at `COMMIT`, not at the
   statement. The savepoint-based `IntegrityError` handler in
   `ReviewDecisionListCreateView` catches unique violations (checked immediately) but
   would not catch an FK violation at the point it expects to.

*Wrong*, not preference: the schema states an intent the database does not implement.

**`dedupe_key` — the planner's idempotency invariant — is indexed, not unique.**

```sql
"dedupe_key" varchar(128) NOT NULL,
CREATE INDEX "app_outreachaction_dedupe_key_cb5bf63e" ON "app_outreachaction" ("dedupe_key");
```

Contrast the suppression ledger, which got it right:

```sql
"dedupe_key" varchar(128) NOT NULL UNIQUE   -- app_dismissedoutreachkey
```

The code documents the gap and leaves it open. Full treatment in Pass 3.

**Nullable columns encode state, and nothing ties them to `status`.** `OutreachAction`
carries `snooze_until`, `snooze_trigger`, `snooze_activity_after`, `status_changed_at`,
`dismiss_reason` — all nullable or `''`-defaulted, all meaningful only for particular
values of `status`. The model comment asserts *"Non-NULL whenever status == 'snoozed'"*.
No constraint says so.

The concrete failure is a row that becomes permanently invisible. One statement:

```sql
UPDATE app_outreachaction SET status = 'snoozed' WHERE id = 12345;
```

`snooze_until` stays NULL, so `unsnooze_due`'s time branch (`snooze_until <= now()`) never
matches and its activity branch (`snooze_activity_after IS NOT NULL`) never matches.
`/api/queue/` filters `status='pending'` and skips it. `/api/queue/done/` filters
`status_changed_at` within today and skips it. The row exists, holds its `dedupe_key`
against re-planning, and is reachable by nothing. Nobody finds it, ever.

**Illegal states are representable.** `"status" varchar(16)`, `"priority" integer`,
`"action_type" varchar(64)`, `"kind" varchar(32)`, `"dismiss_reason" varchar(64)` —
none has a CHECK, an enum, or an FK. `ALLOWED_TRANSITIONS` is a Python dict. The state
machine is reconstructable from `status_changed_at` only as a single most-recent
timestamp: there is no transition log, so "how long did this sit in pending" is
unanswerable and "who moved it and when" survives only where a `ReviewDecision` happens
to have been written.

**Money, time, identity.** Prices are `numeric(10,4)` — clean. `estimated_book_size_usd`
is `bigint` whole dollars — clean, though `QueueDoneView` sums it in Python across every
row it fetched rather than in the database. Timestamps are `timestamptz` throughout —
clean. But the planner's date arithmetic runs off `datetime.date.today()` — the *server's*
local date — while the triage views use `ZoneInfo(settings.TRIAGE_TIMEZONE)`. Two
different "todays" in one system, and `rule_trace["today"]` records the wrong one.
Currently masked by `TIME_ZONE = "UTC"` and `TRIAGE_TIMEZONE = "UTC"`; it breaks the day
someone sets `TRIAGE_TIMEZONE=America/Chicago`, which is the first thing a US BD team
will do.

**Sequential `bigint` identity, exposed.** `/api/queue/<int:pk>/`,
`/api/outreach/<int:pk>/trace/`. `GENERATED BY DEFAULT AS IDENTITY` means row 40,000
tells any authenticated user how many recommendations you have ever produced, and the
ids are walkable. Single-tenant today, so this is *preference*, not *wrong* — it
converts to wrong the day a second tenant exists.

**`Lead.id varchar(32)` as a natural key from the CRM.** Steelmanned above as legible
and join-free. The cost: it is the FK in five tables (`app_event`, `app_outreachaction`,
`app_dismissedoutreachkey`, `app_agentleadrun`, and via those into `app_agentstep`), and
`ingest_data` takes `row["id"]` from a JSON file with no validation. The assumption is
that a HubSpot record id is stable forever. When a lead is merged or re-keyed upstream —
routine in HubSpot — you get a new `Lead` row and the old one's entire history orphans
under an id that no longer exists upstream. There is no `merged_into` column and no
place to put one.

### Concurrency & isolation

**Phantom → write skew: the planner's open-item skip rule.** Two overlapping
`plan_outreach()` runs each read the set of open `dedupe_key`s, neither sees the other's
uncommitted inserts, both plan and both insert. Correct only under `SERIALIZABLE`. Runs
at `READ COMMITTED`. Pass 3, risk 3.

**Lost update: every triage transition except `dispatch`.** `views/queue.py` reads the
row, evaluates `can_transition_to()` in Python, writes. Approve-racing-approve and
approve-racing-dismiss both violate `rd_one_live_send_per_action` — an uncaught
`IntegrityError`, a 500 where `ReviewDecisionListCreateView` returns a clean 409 for the
identical collision. Snooze and undo have no constraint behind them and simply lose the
write.

**Upsert semantics: `update_or_create` is not an upsert.**

```python
DismissedOutreachKey.objects.update_or_create(dedupe_key=key, defaults={...})
```

Django compiles this to `SELECT` → `UPDATE`-or-`INSERT`, not `INSERT ... ON CONFLICT`.
The constraint it *should* target is exactly right — `dedupe_key UNIQUE` is the business
rule — but two reviewers dismissing the same recommendation race between the SELECT and
the INSERT, and the loser gets an `IntegrityError` inside the enclosing
`transaction.atomic()` in `QueueDismissView`, which rolls back the dismiss and returns
500. Same pattern in `create_lead_runs`, where it is at least wrapped in a `try`.

**`SELECT ... FOR UPDATE`.** Used once, in `dispatch._live_approval(lock=True)`, inside
the consuming transaction, with the CAS ahead of it. That is correct, and it is the only
place in the codebase that locks anything. No deadlock ordering problem exists because no
second lock ordering exists. **Clean** — and the fact that it is clean here and absent
everywhere else is the finding, not this.

**Locks held across network calls.** None. Phase 3 of the planner is explicitly
ORM-free and the `Checkpoint` writes are short transactions between provider calls, not
around them. **Clean.** This was clearly thought about.

**`unsnooze_due` vs online traffic.** Both branches issue `UPDATE ... WHERE
status='snoozed' AND ...`. Under `READ COMMITTED` Postgres re-evaluates the qualification
after acquiring the row lock, so a reviewer who approves a row a microsecond earlier wins
and the update skips it. **Clean** — the comment's claim that concurrent runs are safe is
correct, for a subtler reason than the comment gives.

**Batch vs hot path.** `migrate` runs in the container entrypoint with no `lock_timeout`
and no `statement_timeout`. Any `ALTER TABLE` needing `ACCESS EXCLUSIVE` queues behind the
longest running read — and every query that arrives behind *it* queues too, because
Postgres lock requests are FIFO. One slow `/api/reports/` scan (see below) plus one
deploy equals a full outage of an otherwise healthy database. With `conn_max_age=600`
the connections stay pinned while it happens.

### Scale & physics

The thirteen access patterns I reconstructed, with the index each needs:

| # | Query | Index needed | Exists? |
|---|---|---|---|
| 1 | `/api/queue/` — pending, ordered by `(priority, lead_id)` | `(status, priority, lead_id)` | ✅ `oa_queue_order` |
| 2 | `/api/queue/done/` — `status IN (…) AND status_changed_at` in a day range | `(status, status_changed_at)` | ✅ `oa_done_order` |
| 3 | `day_counts()` — 4 filtered aggregates | same as 2 | ✅ |
| 4 | `/api/outreach/`, `/api/review-queue/` — latest row per lead | `(lead_id, created_at DESC, id DESC)` | ❌ |
| 5 | `/api/reports/` — full history, `ORDER BY created_at DESC, id DESC` | `(created_at DESC, id DESC)` | ❌ |
| 6 | planner: open keys, `status IN (…) AND dedupe_key <> ''` | partial on `dedupe_key` | ⚠️ full index |
| 7 | planner: `DismissedOutreachKey WHERE revoked_at IS NULL` | partial on `revoked_at` | ❌ (seq scan) |
| 8 | planner: `Lead.objects.prefetch_related("events")` | `(lead_id)` on events | ✅ (sorted in Python) |
| 9 | `queue_queryset()` — events per lead ordered `-timestamp, -id` | `(lead_id, timestamp DESC)` | ❌ |
| 10 | `unsnooze_due` time branch | partial on `snooze_until` | ⚠️ full index |
| 11 | `unsnooze_due` activity branch — `EXISTS` on events | `(lead_id, timestamp)` | ❌ |
| 12 | auth: `token_hash` equality | unique on `token_hash` | ✅ |
| 13 | `dump_edit_corpus` — `DATE(created_at) >= x` | `(created_at)` | ⚠️ non-sargable |

**Thirteen indexes on the write-hot table, five of them dead.** Django silently adds a
`varchar_pattern_ops` twin to every indexed `varchar`:

```sql
CREATE INDEX "app_outreachaction_status_049634a0_like"     ON "app_outreachaction" ("status" varchar_pattern_ops);
CREATE INDEX "app_outreachaction_dedupe_key_cb5bf63e_like" ON "app_outreachaction" ("dedupe_key" varchar_pattern_ops);
CREATE INDEX "app_outreachaction_trace_run_id_171e27ae_like" ON "app_outreachaction" ("trace_run_id" varchar_pattern_ops);
CREATE INDEX "app_outreachaction_lead_id_631d6a42_like"    ON "app_outreachaction" ("lead_id" varchar_pattern_ops);
```

Those four serve `LIKE 'prefix%'` queries. This application issues none. A fifth,
`app_outreachaction_status_049634a0`, is a standalone index on a column with five
distinct values whose every use is already covered by the `oa_queue_order` prefix.
`dedupe_key` is a 64-character SHA-256 hex string, so its two indexes cost roughly
`2 × 3.65M × ~80 bytes ≈ 580 MB` at twelve months of 10× — to support equality lookups
one index would serve. Every `bulk_create` of a 10,000-lead run writes 130,000 index
tuples instead of 80,000.

**Where writes serialize.** `LLMConfiguration` is a single row by CHECK constraint,
written only by an operator — no contention. The planner's `bulk_create` is one big
insert, not a counter. There is no counter column anywhere and no hot row on the write
path. **Clean.** The serialization point in this system is not a row; it is the single
`asyncio.Lock` + shared connection in `Checkpoint`, which is application, not schema.

**Which table hits the wall first.** `app_outreachaction`, by a wide margin, and not on
row count — on row *width*. Logical width per row:

| column | ~bytes |
|---|---|
| `reason` | 250 |
| `suggested_copy` | 900 |
| `edited_copy` | 0–900 |
| `further_action` | 0–400 |
| `rule_trace` jsonb | ~5,000 |
| `verification` jsonb | ~2,000 (includes a **third full copy** of the draft, in `verification->>'copy'`) |

≈ 8.5 KB logical per row. At 10×/daily that is 3.65M rows and roughly 10 GB of heap plus
TOAST after compression, inside twelve months. `app_agentstep` is second and grows faster
per-lead once the agent path is enabled, since each step payload holds a conversation
turn.

**Partitioning seam.** `created_at` is the natural key — the table is a time-series of
decisions and every access pattern except #4 is either "today" or "this run". It is
present, it is `NOT NULL`, and it is immutable. What breaks: `oa_one_row_per_lead_per_run`
and any unique index would have to include `created_at` to be enforceable across
partitions, and `ReviewDecision`/`OutreachEdit`/`OutboundSend` FKs to a partitioned
parent need the partition key too. That is a real cost, and it is a cost that only goes
up. Pattern #4 — latest-per-lead — is the one query that fights partitioning, and it is
also the one that has no index today; fixing it properly (a current-row pointer) fixes
both.

**Vacuum and bloat.** The planner's phase 5 does `DELETE` of superseded failed rows
followed by `bulk_create`, inside one transaction, once per run. That is a small,
predictable churn — fine. The real interaction is the other direction: pattern #5
(`/api/reports/`, unbounded) holds a long snapshot while it scans, which holds back
`xmin` and blocks vacuum from reclaiming anything newer across the whole database for the
duration. A slow report is not just slow; it stops autovacuum from doing its job.

### ORM & application-layer traps

**N+1.** `queue_queryset()` prefetches correctly and `plan_outreach` prefetches events
explicitly with a comment explaining the 1+4N it avoids. The serializers are the risk —
`QueueItemSerializer` walks `lead.events.all()` from a `Prefetch`, which is correct only
as long as nobody adds a `.filter()` inside the serializer, at which point the prefetch is
silently discarded and every row re-queries. There is a `test_a_twelve_lead_run_costs_a_
fixed_number_of_queries` guarding the planner and nothing guarding the serializers.

**Cascades the ORM enforces that the database does not.** Covered above — this is the
single biggest gap between "what the model file says" and "what the database will do".

**Implicit transactions.** `ATOMIC_REQUESTS` is off, so each `save()` is its own
autocommit transaction. `QueueApproveView` wraps its four writes in `atomic()`;
`QueueSnoozeView` wraps none — its single `save()` is fine alone, but the read-check-write
straddling it is not in any transaction at all, which is why the guard is decorative.

**Schema decisions that exist to please the ORM.** `app_llmconfiguration` carries a
surrogate `bigint` identity PK *and* a `CHECK (id = 1)` — the PK exists because Django
requires one, and the check exists to neutralize it. Harmless. `app_outreachedit` and
`app_agentstep` likewise. Nothing here fights the ORM: there are no composite primary
keys, which is the right call on Django 4.2. **Clean.**

### Evolvability

Ranked by pain, most painful first:

1. **Adding `tenant_id`.** Every unique constraint becomes wrong simultaneously:
   `app_dismissedoutreachkey.dedupe_key UNIQUE`, `app_logintoken.token_hash UNIQUE`,
   `oa_one_row_per_lead_per_run`, `alr_one_run_per_lead_per_trace`,
   `app_llmmodel (provider_id, model_id)`, and the `CHECK (id = 1)` singleton, which
   becomes flatly incompatible with per-tenant LLM configuration. Every one requires a
   rebuild under load. Free today.
2. **Changing `Lead.id` off the CRM's natural key.** Five FK columns, all `varchar(32)`,
   all requiring a rewrite plus index rebuilds, plus `dedupe_key` — which is
   `sha256("v1|{lead_id}|{action_type}")` and therefore has the old id baked into every
   suppression row and every `OutreachAction`. Re-keying leads means recomputing the
   entire suppression ledger.
3. **Splitting the JSONB columns out.** `rule_trace` and `verification` are legitimately
   schema-less — they are versioned frozen snapshots of a computation, which is the
   textbook correct use. `AgentStep.payload` is not: its shape varies by `kind` and is
   documented in `docs/contracts/agent-loop.md`, which is a schema someone declined to
   write down. `OutreachEdit.diff_ops` is a bare `jsonb` list with the version in a
   docstring rather than in the data — the one versioning miss.
4. **Adding CHECK constraints for status/priority.** Cheap at any size if done as
   `NOT VALID` then `VALIDATE CONSTRAINT`, which takes only `SHARE UPDATE EXCLUSIVE`.
   Genuinely deferrable.

**Retrofitted columns.** `0005_triage_queue` added nine columns and both queue indexes to
an existing `app_outreachaction`; `0006` added `trace_run_id` plus its partial unique.
On a small table this was free. The pattern to watch: `status` was retrofitted with
`default='pending'`, and because Django 4.2 on PG11+ uses a non-rewriting default, that
was fast — but nothing in the repo pins that property, and the next `ALTER TABLE ... ADD
COLUMN ... DEFAULT` of a *volatile* default will rewrite 3.65M rows under `ACCESS
EXCLUSIVE`, at boot, unattended (A5).

**Soft deletes.** None — `revoked_at` and `voided_at` are tombstones on ledger rows, not
soft deletes on entities, and both are correctly reflected in the partial unique indexes
that ignore them. **Clean.**

### Data quality & observability

Four invariants no constraint can express. These should run on a schedule and page:

```sql
-- 1. Snoozed rows that can never wake (the invisible-row bug).
SELECT id, lead_id, status_changed_at FROM app_outreachaction
WHERE status = 'snoozed'
  AND snooze_until IS NULL
  AND (snooze_trigger <> 'on_activity' OR snooze_activity_after IS NULL);

-- 2. Duplicate live recommendations for the same lead+action (the dedupe race).
SELECT dedupe_key, count(*), array_agg(id), array_agg(trace_run_id)
FROM app_outreachaction
WHERE dedupe_key <> '' AND status IN ('pending','snoozed')
  AND NOT (needs_human AND suggested_copy = '' AND action_type <> 'unknown')
GROUP BY dedupe_key HAVING count(*) > 1;

-- 3. Stored approvals whose verification snapshot no longer describes the copy in play.
--    QueueApproveView only recomputes when report->>'copy' differs, so a verifier
--    bugfix leaves already-stored can_approve=true rows approvable forever.
SELECT id, lead_id, verification->>'version' AS v
FROM app_outreachaction
WHERE status IN ('approved','sent')
  AND (verification->>'copy') IS DISTINCT FROM COALESCE(NULLIF(edited_copy,''), suggested_copy);

-- 4. Provider-trace content with no reachable owner (PII with no data subject).
SELECT c.id, c.trace_id, length(c.request) AS bytes
FROM app_providertracecontent c
JOIN app_providertrace t ON t.id = c.trace_id
LEFT JOIN app_agentstep s ON s.provider_trace_id = t.id
WHERE s.id IS NULL;
```

**Point-in-time reconstruction: not possible for `Lead`.** `ingest_data` does
`update_or_create` in place. There is no history table, no `updated_at`, no
`valid_from`/`valid_to`. "What did this lead look like on 1 March" is answerable only for
leads that happened to be planned that day, and only for the handful of fields
`rule_trace` captured.

**And the event log is destroyed nightly.** `ingest_data` does
`Event.objects.filter(lead_id=...).delete()` then recreates. There is no natural key and
no unique constraint on `app_event` — the surrogate `bigint` ids churn on every ingest, so
nothing can safely hold a foreign key to an `Event`, and `snooze_activity_after` compares
a stored watermark against timestamps on rows that have been deleted and reinserted since
the watermark was taken. Funnel timing, time-to-first-quote, cohort retention, "when did
this agency go quiet" — the four questions a BD tool is asked in year one — all require an
append-only activity log, and this is a table that is truncated and rebuilt.

### Security & multi-tenancy

**No tenant column anywhere.** Isolation today is "there is only one customer." There is
no RLS, no `tenant_id`, and no policy to attach one to. This is fine as a decision and
catastrophic as an accident; see the closing question.

**Encryption at rest.** `encrypted_api_key bytea` is Fernet-encrypted — correct, and
correctly not indexed. `key_last_four varchar(4)` in cleartext is the right tradeoff.
Everything else is plaintext, including `app_providertracecontent.request`, which holds
lead names, email addresses, phone numbers and third-party CRM notes as `text`. Encrypting
it would break nothing, because nothing indexes or searches it — the only access path is
by `trace_id`. That is an unusually cheap encryption win and worth taking.

**Authorization.** There is no authorization data in the schema at all — no roles, no
grants, no shares. Every permission check is `IsAuthenticated`. So a permissions check is
zero queries, which is fast and also means there is nothing to get wrong yet. Judge this
when the roles arrive.

### Operations & lifecycle

**Erasure is not currently satisfiable.** A lead's PII fans out to, at minimum:
`app_lead` (name, email, phone), `app_outreachaction.suggested_copy` /
`edited_copy` / `reason` / `verification->>'copy'`, `app_outreachedit.before_text` /
`after_text`, `app_reviewdecision.approved_copy`, `app_agentstep.payload`, and
`app_providertracecontent.request` / `response`. The first eight are reachable by
`lead_id`. The last is not — `app_providertrace` has no `lead_id` column. See F4.

**Read-replica safety.** No replica exists yet. When one does, the read-after-write
dependencies are: `QueueApproveView` → `self.serialize(action)` re-reads through the
same instance (safe), and `dispatch()` → `action.refresh_from_db()` immediately after
commit (unsafe on a replica, and it is the send path). Two sites, both easy to pin to
primary — but nobody has marked them.

**Idempotency and retry safety.** `POST /api/outreach/run/` carries no idempotency key
and no request id; the run's own `trace_run_id` is minted *inside* the call, so a retry
mints a new one and defeats `oa_one_row_per_lead_per_run`. This is the mechanism by which
Pass-3 risk 3 fires. `POST /api/queue/{id}/approve/` is not idempotent either — a retry
after a lost response either 409s (fine) or double-writes (F6).

**Restore at 3am.** SQLite by default, Postgres in a Docker volume with no backup
configuration, no WAL archiving, no `pg_dump` cron, and no documented RPO/RTO. The
`postgres_data` volume is the only copy of everything. And the entrypoint runs
`populate_demo_data.py` — which deletes and recreates every event — on container start,
so restoring the volume and starting the container is a restore followed immediately by a
partial re-seed.

---

## Pass 3 — Load simulation at 24 months (10×: 10k leads, daily runs)

### Risk A — `/api/outreach/` and `/api/review-queue/` fetch the whole table

Query, as the ORM emits it:

```sql
SELECT app_outreachaction.*, app_lead.*
FROM app_outreachaction
INNER JOIN app_lead ON (app_outreachaction.lead_id = app_lead.id)
ORDER BY app_outreachaction.lead_id ASC,
         app_outreachaction.created_at DESC,
         app_outreachaction.id DESC;
```

No `LIMIT`. The dedupe to "latest per lead" happens in a Python `for` loop afterwards.

Expected plan at 3.65M rows:

```
Sort  (cost=… rows=3650000 width=8500)
  Sort Key: oa.lead_id, oa.created_at DESC, oa.id DESC
  Sort Method: external merge  Disk: ~31000000kB
  ->  Hash Join  (rows=3650000 width=8500)
        Hash Cond: (oa.lead_id = l.id)
        ->  Seq Scan on app_outreachaction oa  (rows=3650000 width=8400)
        ->  Hash  (rows=10000 width=100)
              ->  Seq Scan on app_lead l  (rows=10000)
```

No index can serve that sort — `app_outreachaction_lead_id` covers the first key only, and
the planner will not use it for a three-key sort over the whole table. With `work_mem` at
4 MB and ~31 GB of sort input, this is a multi-pass external merge sort. Two independent
kills, and the second arrives first:

- **Postgres:** tens of gigabytes to `pgsql_tmp`, minutes of wall clock, and an open
  snapshot the whole time that blocks vacuum database-wide.
- **The worker:** Django materializes every row as a model instance into a list before
  the loop runs. At ~9 KB per instance including detoasted text, a 1 GB worker dies at
  roughly **110,000 rows — 11 days of operation at 10×**.

The latency budget (call it 500 ms for a user-facing list) is blown far earlier, at
roughly 20,000 rows, or **two days**. `/api/reports/` is the same query without even the
dedupe loop.

The fix is one index plus `DISTINCT ON` plus a page size:

```sql
CREATE INDEX CONCURRENTLY ix_oa_lead_recent
  ON app_outreachaction (lead_id, created_at DESC, id DESC);
```

```sql
SELECT DISTINCT ON (lead_id) *
FROM app_outreachaction
ORDER BY lead_id, created_at DESC, id DESC
LIMIT 50 OFFSET :n;
```

`DISTINCT ON` with a matching index is an index skip-scan-shaped plan: one row read per
lead, 10,000 rows touched instead of 3.65M.

### Risk B — `unsnooze_due`'s activity branch has no supporting index

The correlated subquery, every minute:

```sql
UPDATE app_outreachaction SET status='pending', … WHERE id IN (
  SELECT oa.id FROM app_outreachaction oa
  WHERE oa.status = 'snoozed'
    AND oa.snooze_trigger = 'on_activity'
    AND oa.snooze_activity_after IS NOT NULL
    AND NOT (oa.snooze_until <= now())
    AND EXISTS (SELECT 1 FROM app_event e
                WHERE e.lead_id = oa.lead_id
                  AND e.timestamp > oa.snooze_activity_after));
```

`app_event` is indexed on `lead_id` alone. So the `EXISTS` cannot stop early on
`timestamp` — it must walk every event for that lead and filter in the heap:

```
Nested Loop Semi Join  (rows=…)
  ->  Bitmap Heap Scan on app_outreachaction oa  (rows=2000)
  ->  Index Scan using app_event_lead_id_3b49d5b4 on app_event e
        Index Cond: (lead_id = oa.lead_id)
        Filter: (timestamp > oa.snooze_activity_after)
        Rows Removed by Filter: ~119
```

| | snoozed rows | events/lead | heap fetches per run |
|---|---|---|---|
| 10× | 2,000 | 120 | 240,000 |
| 100× | 20,000 | 600 | 12,000,000 |

At 10× that is roughly 2–5 s warm, 20 s+ cold — survivable but it evicts a large share of
a 128 MB `shared_buffers` every minute, so the *user-facing* queries run cold afterwards.
At 100× the job cannot finish inside its own minute; runs overlap, and each holds an
`UPDATE` lock queue against the same rows the triage UI is writing.

One index removes it:

```sql
CREATE INDEX CONCURRENTLY ix_event_lead_ts ON app_event (lead_id, timestamp DESC);
```

Now the `EXISTS` is a descending index scan that stops at the first tuple past the
watermark: ~4 buffer reads per snoozed row, **~80,000 reads at 100×, well under 100 ms.**
Two orders of magnitude for one index and no application change.

### Risk C — the duplicate-run write skew, which is a spend incident, not a data incident

This one does not need scale. It needs a timeout.

A 10,000-lead run at 8 concurrent provider calls and ~4 s per call is roughly **90
minutes**. It is invoked by `POST /api/outreach/run/` (previous audit, finding 1). Any
scheduler, proxy, or load balancer in front of it has a timeout far below 90 minutes.
The scheduler times out and retries — the default behavior of every cron wrapper, every
Kubernetes CronJob with `backoffLimit`, and every human who refreshes.

Now two runs are live. Both executed, at t=0 and t=T:

```sql
SELECT dedupe_key FROM app_outreachaction
WHERE status IN ('pending','snoozed') AND dedupe_key <> ''
  AND NOT (needs_human AND suggested_copy = '' AND action_type <> 'unknown');
```

Run B's snapshot cannot contain run A's rows, because A does not insert until phase 5, at
t=90min. Both build identical work lists. Both make 10,000 provider calls. Both
`bulk_create`. `oa_one_row_per_lead_per_run` does not fire — the two runs have different
`trace_run_id`, which is precisely what that index keys on.

Outcome: 20,000 rows, every lead duplicated in the triage queue, and 10,000 duplicated
provider calls. At $0.002/call that is $20 per incident and a queue a human has to
hand-deduplicate; at Opus-tier pricing with the agent path enabled it is two orders of
magnitude more.

**Trigger: the first run that outlasts the caller's timeout.** At current per-lead latency
that is about 1,000 leads. This is not a 24-month problem — it is a next-quarter problem,
and it is a *runtime* threshold, not a data-volume one, which is why watching table sizes
will not warn you.

The database-side fix, with its tension stated honestly:

```sql
CREATE UNIQUE INDEX CONCURRENTLY ix_oa_one_open_per_key
  ON app_outreachaction (dedupe_key)
  WHERE dedupe_key <> '' AND status IN ('pending','snoozed');
```

This converts silent duplication into a loud failure — which is strictly better, but the
failure lands on `bulk_create`, aborting the entire run's 10,000-row insert after all the
money has been spent. `bulk_create(ignore_conflicts=True)` fixes that but stops returning
populated primary keys, which `tests_planner_perf` and the `app.E003` system check both
pin deliberately.

**The deciding question is whether a duplicated run should fail loudly or converge
silently.** If loudly: take the unique index and let phase 5 raise; the operator sees it
immediately. If silently: take the unique index *and* switch to
`ON CONFLICT (dedupe_key) WHERE … DO NOTHING` via `bulk_create(ignore_conflicts=True)`,
and re-`SELECT` the rows the API needs to return instead of relying on `bulk_create`'s pk
population. I would take the second — but it costs a pinned test and a system check, and
that is your call, not mine. Either way the index goes in; the argument is only about what
happens when it fires.

---

## Pass 4 — Verdict

Severity = P(bites) × cost-to-fix-after-data-lands.

### 1. `dedupe_key` has no unique constraint

**Decision.** `"dedupe_key" varchar(128) NOT NULL` + non-unique
`CREATE INDEX "app_outreachaction_dedupe_key_cb5bf63e"`.
**Failure.** Pass 3, risk C — a retried planner call duplicates the queue and the spend.
**Trigger.** The first run exceeding the caller's HTTP timeout; ~1,000 leads.
**Fix.** The partial unique index above, plus the loud-vs-silent decision.
**Cost now vs in 18 months.** Now: `CREATE INDEX CONCURRENTLY` on ~0 rows, instant. In 18
months: the index build is minutes, *and* it will fail outright because duplicates have
already accumulated — so it becomes a dedupe migration first, over rows a human must
adjudicate because both copies may carry independent reviewer decisions.

### 2. `app_providertrace` has no `lead_id`, and its content table holds PII

**Decision.** `CREATE TABLE "app_providertrace" ("id" …, "provider" …, "model_id" …,
"trace_run_id" …, "created_at" …)` — no lead reference; `app_providertracecontent`
holds `request`/`response` as plaintext `text`.
**Failure.** An erasure request for one lead. Their name, email, phone and CRM notes sit
in `request`, and there is no column to filter on. The only path is
`app_agentstep.provider_trace_id → app_agentleadrun.lead_id`, which exists only for
agent-path traces and is nullable — detection query #4 above finds the orphans.
Compounding it: `ProviderTrace` rows are created *only* in `services/agent/state.py`, so
in the shipped agent-off configuration the audit table records nothing at all. The
feature and the exposure are both dormant, which is why this has not surfaced.
**Trigger.** The first GDPR/CCPA erasure request after the agent flag is enabled. Or a
SOC2 auditor asking how you satisfy one.
**Fix.**
```sql
ALTER TABLE app_providertrace ADD COLUMN lead_id varchar(32)
  REFERENCES app_lead(id) ON DELETE CASCADE;
CREATE INDEX CONCURRENTLY ix_providertrace_lead ON app_providertrace (lead_id);
CREATE INDEX CONCURRENTLY ix_providertrace_created ON app_providertrace (created_at);
```
**Cost now vs in 18 months.** Now: **free — the table has zero rows.** In 18 months:
un-backfillable. For single-shot traces there is no join path to a lead and never will be;
the mapping is not recorded anywhere, so those rows are permanently unattributable PII.
This is the one finding on the list that gets *impossible*, not merely expensive.

### 3. FK actions are application-only

**Decision.** Every FK: `FOREIGN KEY (…) REFERENCES … DEFERRABLE INITIALLY DEFERRED` —
no `ON DELETE`.
**Failure.** Three, detailed in Pass 2: psql deletes fail at `COMMIT` rather than
cascading; ORM deletes cascade in Python across seven tables and then hit `PROTECT` on
`app_outboundsend` partway through; deferred violations surface where the code does not
expect them.
**Trigger.** The first lead deletion by any means other than a clean ORM call on a lead
that was never sent to.
**Fix.** Make the database state the intent — for each relation, `db_constraint=False` on
the model plus explicit DDL, or a `RunSQL` that redeclares:
```sql
ALTER TABLE app_event DROP CONSTRAINT app_event_lead_id_3b49d5b4_fk_app_lead_id;
ALTER TABLE app_event ADD CONSTRAINT app_event_lead_id_fk
  FOREIGN KEY (lead_id) REFERENCES app_lead(id) ON DELETE CASCADE;
```
Repeat for `app_outreachaction`, `app_dismissedoutreachkey` (`ON DELETE CASCADE`),
`app_agentleadrun` (`CASCADE`), `app_agentstep` (`CASCADE`),
`app_dismissedoutreachkey.source_action_id` (`ON DELETE SET NULL`). Leave
`app_outboundsend` at `NO ACTION`, which already matches `PROTECT`.
**Cost now vs in 18 months.** Now: each is a brief `ACCESS EXCLUSIVE` on an
empty-to-small table. In 18 months: adding `ON DELETE` requires re-validating the
constraint against 3.65M rows, holding `ACCESS EXCLUSIVE` for the duration — and per A5
that happens at container boot, unattended.

### 4. The latest-per-lead access pattern has no index and no limit

**Decision.** Indexes are `oa_queue_order (status, priority, lead_id)` and
`oa_done_order (status, status_changed_at DESC)`; nothing on `(lead_id, created_at)`.
**Failure.** Pass 3, risk A — external merge sort, then OOM.
**Trigger.** ~20,000 rows for the latency budget (2 days at 10×); ~110,000 rows for the
worker (11 days).
**Fix.** `CREATE INDEX CONCURRENTLY ix_oa_lead_recent ON app_outreachaction (lead_id,
created_at DESC, id DESC);` plus `DISTINCT ON` plus pagination.
**Cost now vs in 18 months.** Now: seconds. In 18 months: the index build itself is fine
under `CONCURRENTLY`, but the endpoints will have been down for seventeen of those months,
so this is not really a migration-cost question.

### 5. Status is a free-text column and its satellite columns are unconstrained

**Decision.** `"status" varchar(16) NOT NULL`, `"priority" integer NOT NULL`,
`"snooze_until" timestamptz NULL`, `"snooze_trigger" varchar(16) NOT NULL DEFAULT ''`.
**Failure.** The permanently-invisible snoozed row (Pass 2). One UPDATE creates it; no
query finds it; it holds its `dedupe_key` against re-planning forever.
**Trigger.** Any manual data fix, any second writer, any backfill.
**Fix.**
```sql
ALTER TABLE app_outreachaction ADD CONSTRAINT oa_status_valid
  CHECK (status IN ('pending','approved','snoozed','dismissed','sent')) NOT VALID;
ALTER TABLE app_outreachaction ADD CONSTRAINT oa_priority_range
  CHECK (priority BETWEEN 1 AND 3) NOT VALID;
ALTER TABLE app_outreachaction ADD CONSTRAINT oa_snooze_coherent CHECK (
  status <> 'snoozed'
  OR snooze_until IS NOT NULL
  OR (snooze_trigger = 'on_activity' AND snooze_activity_after IS NOT NULL)
) NOT VALID;
ALTER TABLE app_outreachaction ADD CONSTRAINT oa_status_stamped
  CHECK (status = 'pending' OR status_changed_at IS NOT NULL) NOT VALID;
-- then, off-peak:
ALTER TABLE app_outreachaction VALIDATE CONSTRAINT oa_status_valid;  -- etc.
```
**Cost now vs in 18 months.** Genuinely similar — `NOT VALID` takes only
`SHARE UPDATE EXCLUSIVE` and `VALIDATE` is a concurrent scan. The cost that grows is
cleaning up the rows that violate it by then.

### 6. Lost update on every triage transition

**Decision.** No `SELECT … FOR UPDATE` and no conditional `UPDATE` in `views/queue.py`;
the guard is `can_transition_to()` in Python.
**Failure.** Concurrent approve/approve and approve/dismiss violate
`rd_one_live_send_per_action` → uncaught `IntegrityError` → 500. Snooze/undo silently
lose writes.
**Trigger.** Two reviewers, or one reviewer double-clicking. Available today.
**Fix.** Application-side, but the shape is a schema question — make every transition a
CAS, exactly as `dispatch.py:68` already does:
```sql
UPDATE app_outreachaction
SET status = 'approved', status_changed_at = now()
WHERE id = :pk AND status = 'pending'
RETURNING id;
```
Zero rows back → 409. And catch `IntegrityError` → 409 on the `ReviewDecision` insert.
**Cost now vs in 18 months.** Constant — it is application code. Listed here because the
schema half (the partial unique index) is already correct and is the thing turning a
silent bug into a 500; the endpoints just have to stop being surprised by it.

### 7. `app_event` is truncated and rebuilt on every ingest

**Decision.** No unique constraint on `app_event`; surrogate `bigint` identity PK;
`ingest_data` does `Event.objects.filter(lead_id=…).delete()` then recreates.
**Failure.** No stable event identity, so nothing can FK to an event; no immutable
activity log, so funnel timing, time-to-first-quote and "when did this agency go quiet"
are unanswerable; `snooze_activity_after` compares a watermark against rows that have been
deleted and reinserted since it was taken.
**Trigger.** The first analytics question, or the first ingest where the upstream export
changes a historical timestamp.
**Fix.**
```sql
ALTER TABLE app_event ADD CONSTRAINT ev_natural_key
  UNIQUE (lead_id, type, timestamp);
```
and change the ingest to `INSERT … ON CONFLICT (lead_id, type, timestamp) DO UPDATE SET
meta = EXCLUDED.meta`, dropping the delete entirely.
**Cost now vs in 18 months.** Now: trivial, 61 rows. In 18 months: the unique constraint
build may fail on duplicates the delete-and-recreate has been hiding, and you will have
lost eighteen months of event history you never had in the first place — the data is
simply gone, overwritten nightly.

### 8. Thirteen indexes on the write-hot table, five dead

**Decision.** Four `varchar_pattern_ops` twins plus a standalone index on a five-value
`status` column, on top of the two composites that do the work.
**Failure.** 130,000 index tuples per 10,000-lead run instead of 80,000; ~580 MB of
`dedupe_key` index alone at twelve months; longer `bulk_create`, more WAL, more vacuum.
**Trigger.** Gradual — this is a tax, not a cliff.
**Fix.**
```sql
DROP INDEX CONCURRENTLY app_outreachaction_status_049634a0_like;
DROP INDEX CONCURRENTLY app_outreachaction_status_049634a0;
DROP INDEX CONCURRENTLY app_outreachaction_dedupe_key_cb5bf63e_like;
DROP INDEX CONCURRENTLY app_outreachaction_trace_run_id_171e27ae_like;
DROP INDEX CONCURRENTLY app_outreachaction_lead_id_631d6a42_like;
```
In the models, `db_index=False` on `status` and declare only what `Meta.indexes` needs.
**Cost now vs in 18 months.** Constant — `DROP INDEX CONCURRENTLY` is cheap at any size.
Cheap to defer.

### 9. `update_or_create` is not an upsert

**Decision.** `DismissedOutreachKey.objects.update_or_create(dedupe_key=key, …)` inside
`QueueDismissView`'s `transaction.atomic()`.
**Failure.** Two reviewers dismiss the same recommendation; the loser races between
`SELECT` and `INSERT`, hits `dedupe_key UNIQUE`, and the enclosing atomic block rolls the
dismiss back with a 500.
**Trigger.** Two reviewers. Today.
**Fix.** Real upsert:
```sql
INSERT INTO app_dismissedoutreachkey
  (dedupe_key, lead_id, action_type, reason, dismissed_at, dismissed_by, source_action_id, revoked_at)
VALUES (…)
ON CONFLICT (dedupe_key) DO UPDATE
  SET reason = EXCLUDED.reason, dismissed_by = EXCLUDED.dismissed_by,
      source_action_id = EXCLUDED.source_action_id, revoked_at = NULL;
```
**Cost now vs in 18 months.** Constant.

### 10. No `Lead` history, and prices are mutated in place

**Decision.** `ingest_data` `update_or_create`s leads; `seed_llm_catalog` `update_or_create`s
`app_llmmodel` — including `input_price_per_mtok_usd` — and runs on every container start.
**Failure.** "What did the book look like in Q1" is unanswerable. And even if you add
token counts to `ProviderTrace` (you should — it has none, so no run's cost is computable
at all), historical cost still will not be, because the price that applied at the time was
overwritten at the next boot.
**Trigger.** The first board question about pipeline or spend trend.
**Fix.** Add `input_price_per_mtok_usd`/`output_price_per_mtok_usd` and
`input_tokens`/`output_tokens` as snapshot columns on `app_providertrace`, alongside the
existing `provider`/`model_id` snapshot strings — the design instinct was already right,
it just stopped short. For `Lead`, either a `lead_history` table written by the ingest or
accept that `rule_trace` is your only historical record and say so.
**Cost now vs in 18 months.** Now: free (zero trace rows). In 18 months: the price
history is gone.

### 11–14, briefly

- **`dump_edit_corpus` filters `created_at__date >= x`** — `DATE(created_at)` is a
  function on the indexed column, so `app_outreachedit_created_at_a9a44fa9` is unusable.
  Fix: `created_at >= :date AT TIME ZONE 'UTC'` as a range predicate. *Wrong*, trivial.
- **`create_lead_runs` runs one transaction per lead** — 10,000 round trips before a run
  starts. Fix: `bulk_create(ignore_conflicts=True)` then one `SELECT`. *Wrong*, cheap.
- **`DismissedOutreachKey WHERE revoked_at IS NULL`** has no index and is read on every
  planner run. Fix: `CREATE INDEX CONCURRENTLY ix_dok_live ON app_dismissedoutreachkey
  (dedupe_key) WHERE revoked_at IS NULL;` *Preference* until the ledger is large.
- **`app_logintoken` grows forever** — email, IP and user-agent retained indefinitely,
  with a `logintoken_sweep (expires_at, consumed_at)` index built for a sweeper that was
  never written. Fix: the sweeper, or `DELETE FROM app_logintoken WHERE expires_at <
  now() - interval '30 days'` on a schedule. *Wrong* under any retention policy.

---

## Before the next 100,000 rows land

**1. `ALTER TABLE app_providertrace ADD COLUMN lead_id` (finding 2).** Free today —
zero rows. Genuinely impossible later. This is the only item on the list that stops
being fixable, and that is the entire argument for doing it first.

**2. The two missing indexes (findings 4 and Pass-3 risk B).**
```sql
CREATE INDEX CONCURRENTLY ix_oa_lead_recent ON app_outreachaction (lead_id, created_at DESC, id DESC);
CREATE INDEX CONCURRENTLY ix_event_lead_ts  ON app_event (lead_id, timestamp DESC);
```
Both are pure wins with no application change required to be *safe* — the pagination that
makes finding 4 fully correct can follow. The second one is a two-order-of-magnitude
improvement to a job that runs every minute.

**3. The `dedupe_key` partial unique index (finding 1)** — after you answer the
loud-vs-silent question in Pass 3, because that answer changes which `bulk_create` call
you ship it with.

## Cheap to defer

The `_like` index cleanup (finding 8) — `DROP INDEX CONCURRENTLY` costs the same at any
size. The CHECK constraints (finding 5) — `NOT VALID` plus a later `VALIDATE` avoids the
long lock at any table size; the growing cost is the violating rows, not the DDL. The
sequential-ID question — irrelevant until tenancy. The `Lead` history table — the data is
being lost now, but the *schema change* is no harder later.

---

## The one question

**Is this ever going to serve more than one tenant?**

Not "will you sell it" — will more than one BD organization's data, or more than one CRM
account's leads, ever live in this database.

If yes, six things are wrong simultaneously and all six are cheap today and expensive the
moment real data lands: `app_dismissedoutreachkey.dedupe_key UNIQUE`,
`app_logintoken.token_hash UNIQUE`, `oa_one_row_per_lead_per_run`,
`alr_one_run_per_lead_per_trace`, `app_llmmodel (provider_id, model_id)`, and
`CHECK ("id" = 1)` on `app_llmconfiguration` — which is not merely missing a tenant
column but is *structurally* single-tenant, since per-tenant provider keys cannot exist in
a one-row table. `Lead.id` as a natural key from one HubSpot account has the same problem.
Add `tenant_id` to every one of those constraints now and turn on RLS, and the answer costs
an afternoon.

If no — if this is and remains one company's internal tool — then finding 2 is the only
thing on this list that is urgent rather than merely important, and the rest of the review
is a normal backlog.

I cannot tell which from the schema, and the schema is currently written as though the
answer were "no" while the product framing reads as though it might be "yes."
