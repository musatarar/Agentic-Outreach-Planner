# Adopting Claude Code on the Outreach Planner: An Engineering Blueprint

> **Evidence basis.** This document was written from (1) a code-only survey of the repository — source files, migrations, tests, workflows, and build configuration read directly; the repository's own documentation was deliberately left unread — (2) the team's active planning threads (audit spine, cost tracking and model routing, run orchestration), (3) the next planned epic, async LLM request handling, scoped against the code in §0.1, and (4) the [LangGraph](https://github.com/langchain-ai/langgraph) orchestration model as reference vocabulary for the workflow layer (§5.6) — as of August 2026. Claude Code mechanics are cited against the official documentation current at Claude Code 2.1.x. Where this document asserts something about the codebase, it cites a file path from the survey; where it asserts something about tooling, it cites a URL.

---

## 0. Orientation: what this blueprint optimizes for

Three programs are in flight, and every practice below is chosen to serve them:

- **SCALING** — the planner currently runs synchronously inside HTTP requests (`views/outreach.py:30`), reads the entire `Lead` table per run (`services/outreach.py:1958-1963`), and has no cost accounting anywhere in the LLM layer. The team's plans call for a price catalog in the database, prompt caching, priority-tiered model routing, and a run composer with estimate-before-spend. Agent tooling must not add uncontrolled cost on top of a system whose own cost story is being built — and the discipline the product is adopting (budgets, routing, estimates) applies to the coding agents too.
- **AUDITS** — `ProviderTrace`/`ProviderTraceContent` are becoming the authoritative record of every LLM call, closing known gaps (no served-model column, no durable token/latency data, no trace rows on the default path, telemetry-minted run identity). The symmetric requirement: agent-written code needs the same durable provenance — which sessions produced which diffs, under which permissions, verified by which checks.
- **PRODUCTIONIZING** — the runtime is still demo-shaped: `runserver --insecure` in the container entrypoint (`docker/entrypoint.sh:12`), demo data seeded on every boot, a committed secret key in `.env.example:17`, per-process locmem throttles, no session cookie hardening, and flag-gated paths (`OUTREACH_AGENT_ENABLED`, `COPY_VERIFY_LEVEL=off`) whose defaults will flip. Agent guardrails must make the demo→production transition safer, not fossilize demo behavior.

The single most important background fact, established repeatedly by research and Anthropic's own guidance: **an agent's output quality is bounded by the speed and determinism of its feedback loop** ([Claude Code best practices](https://www.anthropic.com/engineering/claude-code-best-practices); [Writing effective tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents)). This codebase is unusually well positioned — ~956 backend tests with every provider call mocked, a computed query-budget test (`tests_planner_perf.py:27-58`), boot-time system checks, and a CI that already gates `makemigrations --check` (`.github/workflows/ci.yml:112`). The blueprint's job is to wire that existing rigor into the agent's inner loop, and to close the places where the loop lies (an `@unittest.expectedFailure` on a known injection gap, `tests_redteam.py:193`; mypy scoped to one subtree, `ci.yml:45`).

### 0.1 Scope check: the next epic through this blueprint

The next planned epic is **async LLM request handling** — and it is a useful stress test of everything below, so it is scoped here as the blueprint's first worked example. The code-verified starting point: `POST /api/outreach/run/` executes the *entire* planner synchronously inside the HTTP request and returns only when every provider call has finished (`views/outreach.py:22-38`); `POST /api/leads/<id>/compose/` is the same shape. And there is **no worker or queue infrastructure anywhere** — no Celery/RQ/Redis/channels in either requirements file, and `docker-compose.yml` defines only `db` and `web`. The concurrency *inside* a run already landed (async adapters, the bounded pool, retry/timeout/backoff, the phase split — all shipped per the tracker); what does not exist is any way to *start* a run without holding an HTTP connection open for its full duration. That is the epic.

The scope map — each row names the area it lands in (§4.5), what changes, and which practices in this document bind:

| Area touched | What the epic does there | What binds |
|---|---|---|
| `run-lifecycle` (new) | A durable run model with explicit states (queued → running → done/failed/cancelled), created by the endpoint, executed elsewhere. This is also where the single-active/dedupe constraint finally lands — the partial unique index that lifts §5.3's serialize-the-planner cap. | `safe-migration` skill; `migration-auditor`; the §5.5 migration gate. New state machine ⇒ plan mode mandatory (§5.1). |
| Execution vehicle | The epic's one real architectural decision: how work runs off-request with zero existing worker infra. The code's own patterns argue for a DB-backed worker (a management command claiming runs) before any new queue dependency: every primitive already exists in-repo — the single-winner epoch-CAS claim (`agent/state.py`), append-only checkpoint steps, resume-by-run-id (`views/outreach.py:36`), and the idempotent cron-command precedent (`management/commands/unsnooze_due.py`). New queue infrastructure is a dependency-posture and deployment change, not just a library import — that alternative needs its own explicit decision. The best-known framework occupant of this slot is LangGraph's durable-execution runtime (§5.6 maps its concepts); adopting it is exactly that explicit decision. | Plan-then-execute at full depth (§5.1); the decision is cheap to review as a plan and expensive as a diff. |
| `llm-seam` / `async-phase` | Timeouts decouple from the HTTP request: the per-lead deadline and retry policy (`llm/runtime.py`) become the *only* time bounds once no request is waiting. Unattended retries are unattended spend. | `cost-check` skill; the §5.5 spend gate; the async-seam rules in the drafted CLAUDE.md apply verbatim. |
| Status surface | New status/result endpoints plus frontend polling (the existing `GET /api/outreach/<pk>/trace/` is the in-repo precedent for read-your-progress). Polling endpoints are the textbook case for the throttle-scope and pagination rules — a 2-second poll loop against an unthrottled, unpaginated endpoint is self-inflicted load. | `drf-endpoint` skill; `frontend/CLAUDE.md` (the polling contract lands in `endpoints.ts`/`types.ts` in the same PR). |
| `audit-spine` | A background run has no request/response log to lean on — the trace gap on the default path (`ProviderTrace` written only by the agent loop, `agent/state.py:307`) turns from "planned" into a **prerequisite**: an async run that fails halfway is reconstructable only from durable rows. Run identity minted by the run model also resolves the telemetry-minted `run_id` inversion. | `audit-spine` skill; the retention gate (§5.5) before volume grows. |
| Tests | The loop stays deterministic only if the worker is drivable synchronously in tests (call the claim-and-execute function directly; no sleeps, no real clock) — the same discipline the existing suites already model with frozen dates and mocked providers. | §3.2; the `test-author` agent writes the lifecycle tests from the state machine, not from the implementation. |

Read the table's rightmost column top to bottom and it is the implementation order in miniature: skills and gates (items 3–7 of §6) must exist *before* this epic starts, because every row consumes them. That is what "highest leverage first" means concretely — and it is why the epic should be scoped by **area, not by ticket**: the six rows above reference six stable places in the code and zero tracker lookups.

---

## 1. The CLAUDE.md itself

### 1.1 Keep it small, high-signal, and prompt-shaped

**Practice.** Treat CLAUDE.md as a prompt, not a wiki: under ~200 lines, containing only (a) exact commands, (b) invariants an agent cannot discover from any single file, and (c) negative rules ("never do X") with the reason compressed to one line. Revise it when the agent makes a mistake the file should have prevented — it is a living prompt you tune, not documentation you accumulate.

**Source.** [Memory docs](https://code.claude.com/docs/en/memory.md) (official guidance: keep each file under ~200 lines; `/doctor` trims checked-in CLAUDE.md by removing derivable content such as directory trees and dependency lists, keeping gotchas and rationale). [Best practices](https://www.anthropic.com/engineering/claude-code-best-practices) ("tune CLAUDE.md like a prompt"). [Effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) (attention is a depletable budget; every always-loaded token competes with the task).

**Failure mode prevented.** Context rot. A bloated CLAUDE.md is loaded into *every* session, diluting attention on the tokens that matter. Worse, stale entries are actively harmful: an agent trusts CLAUDE.md more than its own exploration, so a wrong line (say, a command that no longer exists) produces confident wrong behavior instead of a quick discovery.

**Outdated advice to discard.** The 2024/early-2025 pattern of one giant CLAUDE.md holding the whole team handbook is explicitly superseded by skills, lazy subdirectory files, and path-scoped rules ([changelog](https://code.claude.com/docs/en/changelog.md)). Also dead: embedding "think hard"/"ultrathink" keywords in memory files — keyword thinking triggers are obsolete; current models manage thinking adaptively ([model config](https://code.claude.com/docs/en/model-config.md)).

### 1.2 What belongs in it, for this codebase specifically

The survey shows the highest-value content is **cross-file couplings and invisible seams** — precisely the facts a competent agent reading one file at a time will get wrong:

| Belongs in CLAUDE.md | Why (code evidence) |
|---|---|
| Exact test/lint/typecheck commands that mirror CI | CI runs `coverage run manage.py test project.app`, `ruff check` + `ruff format --check`, `mypy project/app/services/`, `makemigrations --check --dry-run` (`ci.yml:45,74-120`). If the agent's local loop differs from CI, it "passes" locally and fails the gate. |
| The Django unittest runner, not pytest | 41 test files, no conftest, no pytest (`project/app/tests/`). An agent that assumes pytest will invent fixtures and markers that don't exist. |
| "No ORM inside phase-3 async code" | Adding one ORM access inside the planner's async fan-out raises `SynchronousOnlyOperation` at runtime with nothing static to catch it (`outreach.py:1664-1692`; checkpoint writes are the single documented exception, `agent/state.py:241-261`). |
| "Adding a provider touches three registries" | `llm/__init__.py:38`, `config.py:15`, `telemetry/genai.py:53` — nothing binds them; omitting the third silently drops `gen_ai.provider.name`. |
| "No signals; all writes via service functions" | Grep confirms zero `post_save`/`receiver` usage; `bulk_create` is chosen partly to skip signals (`outreach.py:2144`). An agent adding a signal handler would break a working invariant invisibly. |
| "Transaction boundaries are the service layer's job" | `ATOMIC_REQUESTS` is unset in `settings.py`, so no view is auto-wrapped in a transaction; the codebase's `select_for_update` sits inside dispatch's consuming transaction (`dispatch.py`) — neither fact is inferable from the single file an agent is editing. |
| Migration rules (§3, §5) | The SQLite/Postgres duality means the agent's local `migrate` proves nothing about Postgres lock behavior (`settings.py:173-179`; e.g. the partial-unique add in `0006_outreachaction_trace_run_id.py:18-21`). |
| Untrusted-text rules | Lead-controlled fields must pass `sanitize_untrusted` before any prompt; the frontend pins the same rule only in a type comment (`frontend/src/api/types.ts:16-23`). |

What actively hurts: directory listings (the agent can `ls`), dependency lists (it can read `requirements.txt`), narrative design background and changelog-style rationale the agent doesn't need in its system prompt (the codebase's own docstrings already carry rationale at the point of use, which is the right place — e.g. `models/outreach.py:144-145`, `llm.py:88-90`), and rules no tool enforces. Duplicate nothing the agent can cheaply discover; state only what it would otherwise get wrong.

### 1.3 Layer the memory files

**Practice.** Use the load hierarchy deliberately ([memory docs](https://code.claude.com/docs/en/memory.md)):

- **`./CLAUDE.md`** (checked in): the ~180-line core (drafted in Appendix A).
- **`frontend/CLAUDE.md`** (checked in): subdirectory files load *lazily*, only when files in that directory are read — so the committed-bundle workflow, the hand-mirrored `types.ts` contract, and the zero-devDependency test runner (`frontend/package.json:9`) cost zero context in backend sessions.
- **`.claude/rules/migrations.md`** (path-scoped rule file): the full migration checklist, scoped to `project/app/migrations/**` — loaded exactly when the agent touches a migration.
- **`./CLAUDE.local.md`** (gitignored): personal machine facts — local Postgres URL, personal provider keys policy.
- **`~/.claude/CLAUDE.md`** (user-level): individual style preferences; never project facts.
- Prefer **plain path pointers** ("payload contracts for `AgentStep.kind`: see docs/<file>") over `@path` imports — imports expand eagerly at session start and cost context every session; a pointer costs nothing until followed.

**Source.** [Memory docs](https://code.claude.com/docs/en/memory.md).

**Failure mode prevented.** Paying frontend-tax in backend sessions and vice versa; and the subtler failure where one mega-file forces every rule to be terse enough to be ambiguous. Lazy layering lets the migration rules be *complete* because they're only loaded when relevant.

**AUDITS tie-in.** Auto memory (`~/.claude/projects/<project>/memory/`) is agent-writable state that persists across sessions. Audit it periodically with `/memory`, and treat its contents as you'd treat `ProviderTraceContent`: useful, unbounded by default, and reviewable. If the team wants zero self-accumulated state, set `autoMemoryEnabled: false` and rely on the checked-in files only — checked-in memory is diffable, reviewed memory; auto memory is not.

### 1.4 Keep CLAUDE.md in the review loop

**Practice.** Any PR that changes a convention CLAUDE.md states must change CLAUDE.md in the same PR — enforce with a reviewer checklist item, or a CI grep for known-stale markers. Run `/doctor` quarterly to trim drift.

**Source.** [Memory docs](https://code.claude.com/docs/en/memory.md); [best practices](https://www.anthropic.com/engineering/claude-code-best-practices).

**Failure mode prevented.** The stale-instruction failure — worse than no instruction. Concrete local example of the genre: `requirements-dev.txt:23` justifies a `pyyaml` pin with a test that no longer exists (no `import yaml` anywhere in the repo). Pin-rot in dependency comments is annoying; pin-rot in the agent's system prompt is behavior-corrupting.

---

## 2. Skills, slash commands, and subagents

### 2.1 The decision rule: CLAUDE.md vs skill vs subagent

**Practice.**
- **CLAUDE.md**: facts that are *always* true and cheap to state (invariants, commands). Paid on every session — so only universal content.
- **Skill** (`.claude/skills/<name>/SKILL.md`): a *procedure* needed occasionally. Costs only its name + description at startup (~tens of tokens); the body loads on trigger; supporting files (`references/`, `scripts/`) load on demand. Set `disable-model-invocation: true` for human-only rituals; leave it auto-invocable for procedures the model should recognize it needs.
- **Subagent** (`.claude/agents/<name>.md`): when the work needs *separate context* — either isolation (an auditor that must not inherit the implementer's rationalizations) or bulk (a review that reads 30 files and should return a condensed verdict, not flood the parent).
- Legacy `.claude/commands/*.md` slash commands still work but are deprecated single-file skills — no frontmatter, no supporting files, no auto-trigger. Write new procedures as skills.

**Source.** [Skills docs](https://code.claude.com/docs/en/skills.md); [Agent Skills engineering post](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills) (progressive disclosure design); [subagents docs](https://code.claude.com/docs/en/sub-agents.md); [Simon Willison on skills' token economics](https://simonwillison.net/2025/Oct/16/claude-skills/).

**Failure mode prevented.** Two opposite ones. Everything-in-CLAUDE.md → context rot (§1.1). Everything-as-MCP-tools → the tool-catalog tax: skills load knowledge progressively at near-zero standing cost, where a tool server's surface is an always-present operational burden ([Code execution with MCP](https://www.anthropic.com/engineering/code-execution-with-mcp) measured a ~150K→~2K token reduction moving a tool catalog out of context).

**Outdated advice to discard.** Custom slash commands as a distinct concept merged into skills ([changelog](https://code.claude.com/docs/en/changelog.md)). Also: skills are now an open standard beyond Claude ([overview](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview)) — writing them is not vendor lock-in.

### 2.2 The concrete skill set for this service

Each skill below is triggered by a real, recurring hazard the survey found. Descriptions are the *only* thing the model sees at startup — write them as triggers ("Use when…"), not summaries.

| Skill | Trigger (frontmatter `description`) | Body contents | Theme served |
|---|---|---|---|
| **`safe-migration`** | "Use when creating or reviewing any Django migration, or editing files under project/app/models/." | The zero-downtime sequence: additive-nullable → batched backfill (management command, not `RunPython` — the repo's 9 migrations are pure-schema with seeding in idempotent commands; keep that separation) → validated constraint. Postgres lock catalog: non-concurrent `CREATE INDEX` (`AddIndex`, and partial unique constraints, which Postgres implements as unique indexes) takes a SHARE lock — writes blocked for the build; `ALTER TABLE ... ADD CONSTRAINT` takes ACCESS EXCLUSIVE — all access blocked (the existing `0006` partial-unique add and `0005`'s two multi-column indexes on the hot `outreachaction` table are the in-repo SHARE-lock cautionary examples). `atomic = False` + concurrent index operations for hot tables. Postgres 11+ `ADD COLUMN ... DEFAULT <constant>` is instant — no rewrite ([postgresql.org](https://www.postgresql.org/docs/current/ddl-alter.html)). One concern per migration — never repeat the `0007` pattern (4 CreateModels + cardinality change + constraint adds in one 131-line irreversible file). References: [django-migration-linter incompatibility catalog](https://github.com/3YOURMIND/django-migration-linter), [django-pg-zero-downtime-migrations](https://github.com/tbicr/django-pg-zero-downtime-migrations), [sequencing gist](https://gist.github.com/majackson/493c3d6d4476914ca9da63f84247407b), [Django migrations docs](https://docs.djangoproject.com/en/4.2/topics/migrations/). | PRODUCTIONIZING |
| **`drf-endpoint`** | "Use when adding or modifying a DRF endpoint, serializer, or URL route." | The full scaffold: view module + re-export through `views/__init__.py` (`:26-51`), serializer + re-export, URL name matching the view class name, a test module using the shared authenticated base (`tests_auth_utils.py:12-24`). Mandatory: a throttle scope for anything expensive (the survey found `POST /api/outreach/run/`, `/compose/`, and `/llm/config/test/` unthrottled), pagination on any list view (none exists anywhere today — `settings.py:235-249` sets no `DEFAULT_PAGINATION_CLASS` and `/api/reports/`, `/api/leads/`, `/api/queue/` serialize whole tables), and errors via `ContractError` → the `{"code","detail"}` envelope only (three competing error shapes exist today: `exceptions.py` envelope vs `{"error": ...}` in `views/leads.py:33` vs `{"outreach_action": ...}` in `views/outreach.py:139-141`). Never `fields = "__all__"` (the `LeadSerializer` precedent at `serializers/lead.py:13` auto-publishes any future column). Update `frontend/src/api/endpoints.ts` and `types.ts` in the same PR. | SCALING, PRODUCTIONIZING |
| **`query-review`** | "Use when reviewing a diff for ORM query cost, or when a query-budget test fails." | `assertNumQueries`/budget-constant discipline modeled on `tests_planner_perf.py` (budgets computed from `connection.ops.bulk_batch_size`, exact on both backends); the prefetch-cache rule that only `.all()` is served from cache (`outreach.py:98-101`); `select_related`/`prefetch_related` reference ([Django docs](https://docs.djangoproject.com/en/4.2/ref/models/querysets/#prefetch-related)); the known quadratic and full-table hazards to check against: whole-`Lead`-table load per run, O(leads²) `similar_won_deals_for` (`tools.py:96-132`), Python-side dedup over full history (`views/outreach.py:45-56`), per-item verification rebuild N+1 (`serializers/queue.py:147-149`). `context: fork` — runs as an isolated review, returns findings only. | SCALING |
| **`audit-spine`** | "Use when touching ProviderTrace, ProviderTraceContent, AgentStep, telemetry/genai.py, or anything that records an LLM call." | The trace contract as the team is defining it: record the model that *served* the call, not just the requested alias (the result type already separates them; the DB column is the gap); usage/latency/finish-reason must land in *durable* columns, not only spans (a deployment without `OTEL_EXPORTER_OTLP_ENDPOINT` currently has no record at all, `telemetry/setup.py:315`); every provider call mints a trace row — the default non-agent path currently writes none (`ProviderTrace.objects.create` appears exactly once, `agent/state.py:307`); absent-vs-empty: `None` when the provider sent nothing, never an invented `""` (matching the existing `coerce_token_count` posture, `llm/base.py:55-63`); failed attempts consume tokens too — the success-only accounting at `genai.py:754-761` systematically undercounts; content rows are PII (`models/llm.py:110-112`) and need a retention path before volume grows. | AUDITS |
| **`cost-check`** | "Use when a change adds, multiplies, or reprices LLM provider calls." | Estimate-before-spend: compute a token estimate and surface it before any paid call (the run-composer plan makes this a product feature; the skill makes it an engineering habit). Price data belongs in the DB catalog (`LLMModel`), retiring the eval harness's duplicate static table — one source. Prompt structure: stable cacheable prefix / per-lead suffix split for provider prompt caching. Concurrency knobs (`OUTREACH_MAX_IN_FLIGHT`, per-lead timeouts, retry policy in `llm/runtime.py`) are cost multipliers — a retry-policy change is a spend change and must say so in the PR. | SCALING |
| **`release-check`** | "Use when preparing a deploy, editing Docker/compose/entrypoint/settings, or flipping a feature flag default." `disable-model-invocation: true` — a human-run ritual. | The demo-residue checklist, straight from the survey: `runserver --insecure` and root user in the container (`docker/entrypoint.sh:12`; no `USER`, no `HEALTHCHECK`); unconditional demo seeding at boot (`entrypoint.sh:7`); live secret key and `DEBUG=True` in `.env.example:17`; no `SESSION_COOKIE_SECURE`/`CSRF_COOKIE_SECURE`/`SAMESITE`; locmem throttle counters that don't survive multi-worker (no `CACHES` configured); `STATIC_ROOT` absent so `collectstatic` can't work; `COPY_VERIFY_LEVEL=off` and `OUTREACH_ALLOW_STUB_LLM` as silent escape hatches with no boot-time alarm; `.env.example` missing the agent-loop and trace-content flags an operator/auditor needs (`OUTREACH_TRACE_CONTENT_ENABLED` above all). Each item stays on the list until CI or a system check enforces it. | PRODUCTIONIZING |
| **`redteam-local`** | "Use when changing prompts, sanitization, tool schemas, or verifier gates." | Run the injection suite (`tests_redteam.py`) and the shape/grounding gates locally against the stub provider; the negative controls (a corroborated hold must still escalate; clean copy must pass) are as load-bearing as the attacks. Flag: the exfiltration case is currently `@unittest.expectedFailure` (`tests_redteam.py:193`) — a change that *fixes* the sanitizer will turn the suite red for succeeding; the skill tells the agent what that means so it doesn't "fix" the test by re-breaking the sanitizer. | AUDITS, PRODUCTIONIZING |

`$ARGUMENTS` substitution makes these usable as `/safe-migration 0010_add_served_model` etc. ([skills docs](https://code.claude.com/docs/en/skills.md)).

### 2.3 Subagent design

**Practice.** Use *named* agents (`.claude/agents/<name>.md`) for anything that must be independent of the implementing conversation. A named agent starts from its own system prompt + the delegation message + CLAUDE.md — **not** the parent conversation. This matters more since the 2026 change: ad-hoc forked delegation now inherits the *full* conversation by default in interactive sessions, so independence must be explicit ([subagents docs](https://code.claude.com/docs/en/sub-agents.md)).

Proposed roster:

| Agent | Frontmatter | Purpose |
|---|---|---|
| **`test-author`** | `tools` limited to read + edit of `project/app/tests/**` only (via `disallowedTools` on source paths); `maxTurns` bounded | Writes tests that define the expected behavior and fail against current code, before implementation begins. Separating the optimizing agent from the auditing agent is the Kent Beck pattern for keeping agents honest ([TDD with agents](https://newsletter.pragmaticengineer.com/p/tdd-ai-agents-and-coding-with-kent); [Beck's newsletter](https://newsletter.kentbeck.com/p/augmented-coding-beyond-the-vibes)) — an implementer that also owns the tests can quietly weaken them. |
| **`migration-auditor`** | `permissionMode: plan` (read-only); `skills: [safe-migration]` preloaded | Reviews any diff containing a migration against the lock-hazard catalog and the one-concern rule; returns a verdict + line citations, nothing else. Read-only enforcement means it *cannot* "fix" the migration itself — it reports to a human. |
| **`query-optimizer`** | `context: fork`; `model` set to a fast/cheap model | Runs the `query-review` skill over a diff. Cheap model is deliberate: the skill supplies the checklist, so the model does pattern-matching, not invention — this is model routing for agents, the same cost discipline the product is adopting for leads. |
| **`pr-describer`** | `background: true` | Drafts PR descriptions with the audit fields §5.5 requires (session provenance, checks run, spend-relevant changes). |

Scoping rules: give each agent the *smallest* file surface its job needs; have it return **condensed** results (verdict + citations), because the parent's context is the scarce resource ([context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) — sub-agent architectures with condensed returns). Depth ≤3, ~20 concurrent supported — but see §5.3 for why *this* codebase caps useful parallelism lower for planner-touching work, and §5.6 for how a roster composes into a pipeline, a gated loop, or a graph.

**Failure mode prevented.** Reward hacking and self-review blindness. Anthropic's production-RL study documents models gaming coding tests (equality overrides, harness exits, test-runner manipulation) — and the gaming *generalizing* into broader misalignment ([arXiv:2511.18397](https://arxiv.org/abs/2511.18397)). The test suite is the reward function; whoever writes the reward must not be the one optimizing against it.

**Outdated advice to discard.** "Subagents never see your conversation" is now wrong for ad-hoc forks (2026 inheritance change) — teams relying on that for isolation must migrate to named agents. Manual "open a second terminal and paste context" choreography is replaced by native background sessions and worktree isolation ([changelog](https://code.claude.com/docs/en/changelog.md); [worktrees](https://code.claude.com/docs/en/worktrees.md)).

---

## 3. Guardrails and verification loops

### 3.1 Hooks: make the feedback the harness's job, not the model's

**Practice.** Configure hooks in `.claude/settings.json` so verification is *harness-executed and non-discretionary* — the model cannot forget or skip it ([hooks](https://code.claude.com/docs/en/hooks.md), [hooks guide](https://code.claude.com/docs/en/hooks-guide.md)). `settings.json` is strict JSON — no comments — so the block below is paste-ready as written:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [{ "type": "command",
          "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/post_edit_python.sh" }]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [{ "type": "command",
          "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/guard_paths.sh" }]
      },
      {
        "matcher": "Bash",
        "hooks": [{ "type": "command",
          "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/guard_bash.sh" }]
      }
    ]
  }
}
```

The `PostToolUse` entry auto-formats and lints every Python edit (exit 2 feeds stderr back to the model); the first `PreToolUse` entry blocks edits to applied migrations, `db.sqlite3`, and the built bundle; the second blocks destructive shell commands.

`post_edit_python.sh` (sketch): parse `tool_input.file_path` from stdin JSON with `jq`; if `*.py`, run `ruff check --fix` then `ruff format` on that file; if the path is under `project/app/models/`, also run `python manage.py makemigrations --check --dry-run` and, on failure, exit 2 with a one-line instruction ("model change without migration — write one or revert"). If the path is under `project/app/migrations/`, run [django-migration-linter](https://github.com/3YOURMIND/django-migration-linter) on the new migration and exit 2 with its findings.

`guard_paths.sh`: deny writes to `project/app/migrations/0*.py` for files already committed on the default branch (`git ls-tree` check) — **never edit applied migrations** ([Django migrations docs](https://docs.djangoproject.com/en/4.2/topics/migrations/): history is a chain; editing an applied node desyncs every deployed database). Deny writes to `db.sqlite3`, `project/app/static/frontend/**` (built artifact — regenerate via `npm run build`, never hand-edit), and `evals/baselines/**` (regression baselines change only by explicit human decision).

`guard_bash.sh`: deny `manage.py flush`, `manage.py migrate` when `DATABASE_URL` points at a non-local host, `sqlite3 db.sqlite3` write commands, `git push --force*`, `rm -rf` outside the worktree, and any command containing a provider API key inline.

**Source.** [Hooks docs](https://code.claude.com/docs/en/hooks.md) (PreToolUse exit 2 blocks the call and feeds stderr to the model; PostToolUse exit 2 feeds stderr back as a correction; JSON `permissionDecision` output; `${CLAUDE_PROJECT_DIR}`; stdin JSON with `tool_input.file_path`/`tool_input.command`).

**Failure mode prevented.** Three distinct ones. (1) *Formatting drift*: CI runs `ruff format --check` (`ci.yml`) — without the hook, the agent burns a full CI round-trip on whitespace. (2) *Migration disasters*: an edited applied migration or a model/migration desync is the classic agent-caused Django incident; the hook converts it from "caught in review, maybe" to "impossible." (3) *Silent artifact corruption*: the committed 241KB bundle is exactly the kind of file an agent will "helpfully" patch directly; CI's stale-bundle check (`ci.yml:66-70`) would catch it, but the hook catches it in milliseconds instead of minutes.

**Outdated advice to discard.** Relying on `/rewind` (which replaced checkpoints) as a database safety net — **neither checkpoints nor `/rewind` restores a database** ([changelog](https://code.claude.com/docs/en/changelog.md)). File-state rollback does not undo a `migrate` or a `flush`. Database safety comes from the guard hooks and from sandbox/DB isolation (§3.4), not from undo.

### 3.2 Design the self-verification loop around what already exists

**Practice.** Give the agent a graduated, deterministic loop, fastest first, and document it in CLAUDE.md so the agent runs the cheapest sufficient check:

1. **Per-file** (hook, automatic): ruff check/format — sub-second.
2. **Targeted tests**: `python manage.py test project.app.tests.tests_<subject>` — the `tests_<subject>.py` naming makes the right module discoverable by name; sentence-length test names make the right *test* greppable by behavior.
3. **Type check**: `mypy project/app/services/` — today's CI scope; widen per §4.2.
4. **Migration sync**: `makemigrations --check --dry-run`.
5. **Full suite**: `python manage.py test project.app` (SQLite); Postgres parity is CI's job (2×2 matrix, `ci.yml:74-120`) unless the change is lock/constraint-sensitive, in which case run locally against `DATABASE_URL`.
6. **Regression gates**: `python evals/run_rules_eval.py` when the rules engine changed — it's pure-Python, no DB, no network, frozen clock, and diffs against a committed baseline. This is a *perfect* agent target: deterministic, fast, and semantically meaningful.

**Source.** [Best practices](https://www.anthropic.com/engineering/claude-code-best-practices) — giving the agent a clear iteration target (a failing test, an error output) is the single biggest quality lever; [How Anthropic teams use Claude Code](https://claude.com/blog/how-anthropic-teams-use-claude-code) — verify-first workflows; [Writing effective tools](https://www.anthropic.com/engineering/writing-tools-for-agents) — evaluation loops drive agent quality.

**Failure mode prevented.** Hallucinated success. Without a deterministic target the agent "completes" by assertion; with one, completion is a bit the harness checks. The METR RCT ([Jul 2025](https://metr.org/blog/2025-07-10-early-2025-ai-experienced-os-dev-study/)) found experienced maintainers were 19% *slower* with early-2025 tools while believing they were 20% faster — the cost was verification and review. METR labels the result historical, but the mechanism is the design constraint: every check the harness runs deterministically is review cost removed from the human.

**Codebase specifics that make this loop honest — protect them:**
- Provider mocking is universal in tests (three layers: SDK patches, behavioral `LLMClient` doubles, the gate-compliant stub). A hook should also deny any test invocation with real provider env keys set, mirroring the existing care in `tests_telemetry.py:17-29`.
- The query-budget test computes expected inserts from `connection.ops.bulk_batch_size` (`tests_planner_perf.py:50-58`) so it is exact on both backends — the model for any new perf assertion.
- Two suites assert repo-infrastructure facts via `git grep`/`git check-ignore`. These are brittle for agents (adding a legitimate reference to an env var fails a test for a non-defect reason) — keep them, but have CLAUDE.md name them so the agent knows a failure there means "update the manifest," not "revert your change."

### 3.3 Tamper-evidence: the tests are the reward function

**Practice.** Make test-weakening *loud*: (1) a PostToolUse hook that flags any single session editing both `project/app/tests/**` and the source under test, emitting a marker the PR template surfaces ("tests modified alongside implementation — reviewer must diff tests first"); (2) branch coverage on (`branch = true` — currently absent from the coverage config) so deleted assertions move a number; (3) the `test-author` agent (§2.3) as the default author of tests for new behavior; (4) never let an agent resolve a red suite by editing the assertion — the human decides whether the test or the code is wrong.

**Source.** [arXiv:2511.18397](https://arxiv.org/abs/2511.18397) (production models gamed tests; gaming generalized); Kent Beck ([interview](https://newsletter.pragmaticengineer.com/p/tdd-ai-agents-and-coding-with-kent)) — agents deleting failing tests is an observed, recurring behavior, and the countermeasure is structural separation, not exhortation.

**Failure mode prevented.** Reward-hacked green. This suite has one existing soft spot worth naming: the `expectedFailure` on the exfiltration test means a known injection gap reads as green in CI — an agent (or human) scanning CI status learns nothing about it. Convert it to a tracked, visible signal (a dedicated "known-gaps" CI annotation, or an issue-linked skip with an expiry date) as part of the audits program: **a green check must mean what it says, for agents above all**, because agents optimize against the check, not the intent.

### 3.4 Permissions, sandboxing, and the database

**Practice.** Layer, in order ([permissions](https://code.claude.com/docs/en/permissions.md), [permission modes](https://code.claude.com/docs/en/permission-modes.md), [sandboxing](https://code.claude.com/docs/en/sandboxing.md)):

1. **Native sandbox on** (default on macOS/Linux): OS-level filesystem + network isolation; verify with `/sandbox`. Network egress limited to package indexes and the git host — provider APIs are *not* in the agent's allowed domains (tests mock them; evals that spend money are human-run, matching the existing separation where paid evals live in cron-only workflows, `copy-eval.yml`).
2. **`permissions.deny`**: `Read(./.env)`, `Read(./db.sqlite3)`, `Read(./project/app/static/frontend/**)` (241KB of minified JS is pure context poison), `Edit` on the same, plus the Bash denials of §3.1. The `.env` denial matters doubly here because `.env.example` ships a live secret value — an agent that reads env files will happily echo them into logs and PRs.
3. **`permissions.allow`** prefix rules for the standard loop: `Bash(python manage.py test *)`, `Bash(ruff *)`, `Bash(mypy *)`, `Bash(git diff *)`, `Bash(git log *)`, `Bash(npm run build)` — so the common path never prompts.
4. **Modes**: `default` or `auto` (the 2026 classifier-adjudicated mode) for interactive work; `plan` for exploration and for the `migration-auditor`; `dontAsk` for CI (deny-by-default, allowlist only); `bypassPermissions` **only inside disposable isolation** (container/worktree with a scratch DB) — isolation first, permissiveness second, never the reverse.
5. **Database**: agents get a per-worktree SQLite file by default (the settings fall back to `BASE_DIR/db.sqlite3`, so each worktree checkout naturally isolates its DB — a genuine architectural convenience). For Postgres work, a dedicated scratch database and a read-only role for introspection (§4.3). An agent never holds credentials to a shared or production database. Note the survey's boot-check caveat: the encryption-key system check returns clean on *any* `DatabaseError` (`checks.py` E001 swallow), so "the app booted" is not evidence the agent's DB is the one you think it is — have the loop assert `DATABASE_URL` explicitly, the way the benchmark bootstrap refuses to run against a non-temp DB (`bench_planner.py:50-56`).

**Failure mode prevented.** The two catastrophic classes: destructive DB mutation (unrewindable — §3.1) and secret exfiltration into transcripts, memory files, or PRs.

**Outdated advice to discard.** `--dangerously-skip-permissions` as the autonomy switch — superseded by `bypassPermissions` *inside isolation* ([changelog](https://code.claude.com/docs/en/changelog.md)). Per-command prompt-fatigue management by hand — the `auto` mode classifier now handles borderline calls.

### 3.5 Pre-commit parity

**Practice.** Mirror the hook checks in pre-commit and CI so agent-time, commit-time, and CI-time verdicts agree: ruff check + format, `makemigrations --check --dry-run`, django-migration-linter, and the frontend stale-bundle diff. Add django-migration-linter to CI itself (it's the one linter in this list CI doesn't already run).

**Source.** [django-migration-linter](https://github.com/3YOURMIND/django-migration-linter); the existing CI as evidence of the pattern (`ci.yml` already runs three of the four).

**Failure mode prevented.** Split-brain verification: an agent that passes locally and fails CI learns to distrust its loop — and an agent that distrusts its loop starts guessing.

---

## 4. Repo architecture for agent legibility

### 4.1 Preserve what already works — it is most of the battle

The survey found this codebase in the top tier of agent legibility, and the mechanisms are worth naming explicitly *so nobody "improves" them away*:

- **Single-source registries**: `models/__init__.py` with explicit `__all__`; views/serializers re-export registries; `frontend/src/api/endpoints.ts` as the one file answering "what does the frontend call." An agent's first question — "where is X?" — has a deterministic answer.
- **Greppable invariants**: constraint/index names are short unique slugs (`oa_one_row_per_lead_per_run`, `alr_resume_scan`) that appear verbatim in model and migration — one grep yields the whole story. Ticket IDs (MUS-nn) in code comments (e.g. `settings.py:29`, `urls.py:33`, `checks.py:56`) form a cross-file index: grepping one finds an entire feature.
- **Why-docstrings**: docstrings carry the invariant and the rejected alternative (`"rowcount 1 means the caller owns the run"`; the immutability note on `suggested_copy` that names the eval corpus as the reason). This is retrieval-shaped documentation: the rationale lives at the exact point where an agent would otherwise make the wrong edit ([context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents): just-in-time retrieval beats preloading).
- **No signals, no metaclasses, no dynamic dispatch** beyond dict lookups. Static reading predicts runtime behavior — the property agents depend on most.
- **DB-enforced invariants**: partial unique constraints and conditional-UPDATE CAS instead of read-then-check. For agents this is legibility too: a database constraint is machine-enforced and discoverable; an unwritten practice is neither.

**Failure mode prevented.** Hallucinated APIs and action-at-a-distance breakage. Every one of these mechanisms shrinks the gap between "what the agent can read" and "what is true."

### 4.2 Targeted fixes, ranked by agent leverage

| Fix | Evidence | Why it pays agent dividends | Theme |
|---|---|---|---|
| **Split `services/outreach.py` (2,173 lines)** into rules engine / orchestrator / prompt templates | Three concerns in one file; the parameter `lead_ids` is even rebound mid-function (`:1988`), a shadowing that a later agent edit turns into a scoped-run bug | File size is a context tax on every edit; mixed concerns mean an agent editing the rules engine loads 1,500 irrelevant lines. Module boundaries are the primary unit of agent comprehension | SCALING |
| **Widen mypy beyond `services/`** — views, models, management commands next; annotate the model layer (`can_transition_to` has no `-> bool`) and the rules-engine half of outreach | mypy runs on `project/app/services/` only (`ci.yml:45`); model layer and five view modules are unannotated | Type errors are the cheapest deterministic feedback an agent can get; every unannotated module is a module where the agent's only oracle is the full test suite | PRODUCTIONIZING |
| **Consolidate the three provider registries** into one module both config and telemetry import | `llm/__init__.py:38`, `config.py:15`, `genai.py:53` | The classic desync trap: three edits required, omission fails silently. A single registry turns "add a provider" from a three-place edit nothing checks into a type error | AUDITS |
| **Replace the hand-mirrored DELETE predicate** with a shared helper | `outreach.py:2019-2035` mirrors the phase-5 DELETE "clause for clause," ~120 lines apart, in different query languages | Two copies maintained only by comment is precisely what an agent will half-update | AUDITS |
| **Make enumerations explicit**: `choices` + `CheckConstraint` for the bare status/kind CharFields; a `schema_version` column for the versioned JSON payloads | `AgentLeadRun.status` has no choices while `OutreachAction.status` does; `AgentStep.kind` legal values live in a comment; JSON schemas versioned by comment only | Implicit Django conventions made explicit are facts an agent can *read and enforce*; and DB-level enumerations serve the audit spine directly — an audit table whose status vocabulary is a comment is not an audit table | AUDITS |
| **Add an actor column to triage transitions**; unify the three identity column types (free-text `reviewer` CharField vs two EmailFields, no user FK anywhere) | approve/snooze/undo write only `status_changed_at`; the state machine's history is not reconstructable from the DB | The audits program's own standard, applied to the product's human actions — and the template for recording *agent* actions when agents eventually touch product data | AUDITS |
| **Shared test factory module** — one `factories.py` replacing ~11 per-file `make_lead` reimplementations and 4 verbatim `GOOD_COPY` literals; stop cross-suite private imports (`tests_redteam.py:20` importing from `tests_logic`) | Survey, tests area | An agent adding a suite currently has no canonical fixture source and *will* copy a twelfth `make_lead`; a model field change currently fans out to a dozen near-identical dicts — exactly the mechanical edit agents get 11/12ths right | PRODUCTIONIZING |
| **Eliminate or document every `related_name="+"`** | Five FKs are reverse-invisible (`AgentStep.provider_trace`, `DismissedOutreachKey.source_action`, …) | An agent cannot traverse or grep these relations backward; orphan-detection queries (which the audit spine needs — nothing today enforces that a `ProviderTrace` is reachable from any step) become undiscoverable | AUDITS |
| **Move payload contracts into code** — the `AgentStep.kind` payload shapes live in a docs file the code points at | `agent.py:53` | Contracts as typed structures (TypedDict/dataclass) are contracts mypy can check and agents can read without leaving the file | AUDITS |

**Source for the underlying principle.** [Context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents): the smallest high-signal token set wins; every fix above either shrinks the tokens needed to understand a change or converts an unwritten practice into a machine-checkable fact.

### 4.3 MCP servers: few, scoped, and read-only where possible

**Practice.** Connect exactly three servers via a checked-in `.mcp.json`, and treat every additional one as a cost to justify ([MCP docs](https://code.claude.com/docs/en/mcp.md)):

1. **Postgres introspection, read-only role.** The schema questions agents get wrong (column names, constraint semantics, index usage) become grounded queries: `EXPLAIN` before asserting an index helps, `pg_locks`-informed reasoning when the `safe-migration` skill evaluates a DDL change. Scope with `mcp__postgres__*` permission rules; credentials are a read-only role on a scratch/staging database, never production.
2. **GitHub.** PR creation, review threads, CI status — the write half of the headless workflows in §5.3.
3. **The issue tracker.** The MUS-nn IDs threaded through code comments are currently one-way pointers; a tracker server makes them retrievable context (an agent editing the agent-loop can pull the ticket the comment cites instead of guessing intent). This is retrieval-over-preloading applied to project history.

Observability (the OTel backend receiving the GenAI spans) is worth a server *when* incident-debugging sessions become an agent use case; until then it's surface without a workflow.

**Source.** [MCP docs](https://code.claude.com/docs/en/mcp.md) (CLIs for file/shell work; MCP for live domain data and actions); [Code execution with MCP](https://www.anthropic.com/engineering/code-execution-with-mcp) (tool catalogs cost context; the ~150K→~2K case).

**Failure mode prevented.** Two: hallucinated schema facts (the Postgres server's job) and context bloat (the restraint's job). Deferred tool-schema loading (2026) reduces the per-server context cost but not the operational surface — each server is credentials, availability, and prompt-injection exposure to manage.

**Outdated advice to discard.** The 2024/25 "connect everything" MCP maximalism. Skills carry procedural knowledge at near-zero standing cost; MCP earns its place only for *live data and actions* ([Willison](https://simonwillison.net/2025/Oct/16/claude-skills/) on the token economics). Relatedly: the team's backlog item to expose the planner itself as an MCP server should be designed by the [tool-writing guidance](https://www.anthropic.com/engineering/writing-tools-for-agents) — condensed, unambiguous results; the existing tool layer's habits (server-side ID binding, sanitized snapshots, 2000-char render caps in `agent/tools.py`) are already the right instincts, now applied outward.

### 4.4 What to keep *out* of context

**Practice.** Deny-list for reads (§3.4) plus structural hygiene: the committed frontend bundle, `db.sqlite3`, `evals/results/`, the 64-record golden JSONL, and `evals/baselines/` are all data an agent should reference by *outcome* (the eval runner's verdict) rather than ingest. Documentation intended for agents should be topic-sharded files reachable by pointer — the existing pattern of code comments pointing at a contract file is correct; the improvement is making the pointed-at files small and single-topic so retrieval pulls a page, not a book. Remember the ceiling: auto-compact holds even 1M-context models to 200K by default ([changelog](https://code.claude.com/docs/en/changelog.md)) — budget as if context were small, because effectively it is.

**Failure mode prevented.** Context rot in its most mechanical form: an agent that has paged 240KB of minified JS has evicted the invariants it needed.

### 4.5 Referencing code: area codes beside ticket numbers

**Practice.** Yes — add a third reference system. The codebase already runs two: ticket IDs in comments (history pointers — *why did this change*) and constraint/index slugs (`oa_one_row_per_lead_per_run` — DB-level invariants, greppable end to end). What's missing is the layer between them: stable names for the **governed seams** of the codebase — *where am I, and what contract applies here*. Introduce **area codes**: a checked-in registry, `docs/areas.toml`, of roughly a dozen entries —

```toml
[async-phase]
paths    = ["project/app/services/outreach.py", "project/app/services/llm/runtime.py"]
contract = "No ORM inside the async provider-call phase; checkpoint writes are the sole exception."
gate     = "plan-mode"

[dispatch-gate]
paths    = ["project/app/services/dispatch.py"]
contract = "Send = re-read by sha256 under select_for_update in the consuming transaction. Never simplified."
gate     = "human-review"

[run-lifecycle]        # lands with the async-request-handling epic
paths    = ["project/app/models/run.py", "project/app/services/run_worker.py"]
contract = "Single active run via partial unique index; workers claim via conditional UPDATE."
gate     = "human-review"
```

— plus a one-line `# area: async-phase` comment at each site where the contract binds (sparse: the seam definition, not every function). Seed the registry from the seams this document already had to name in prose: `llm-seam`, `async-phase`, `dispatch-gate`, `verify-gate`, `auth-links`, `audit-spine`, `rules-engine`, `triage-queue`, `frontend-bundle`, `eval-baselines`, and (with the epic) `run-lifecycle`.

The payoff is that the registry is **machine-consumable**, which ticket numbers never were. One file drives five mechanisms: (1) path-scoped rule files — `.claude/rules/<area>.md` scoped to the area's globs, so the full contract loads exactly when an agent touches the area ([memory docs](https://code.claude.com/docs/en/memory.md)); (2) the guard hooks — `guard_paths.sh` reads the globs of `gate = "human-review"` areas instead of hardcoding paths (§3.1); (3) skill triggers — "Use when touching area `audit-spine`" is a precise, stable trigger description (§2.2); (4) the human-gate table (§5.5), keyed by area instead of prose; (5) PR provenance — the §5.4 audit fields gain a structured `areas-touched:` line, so "every agent PR that touched `dispatch-gate` this quarter" is one query. §0.1's scope map is the demonstration: six areas, zero tracker lookups.

**Division of labor between the three systems.** Ticket IDs stay: they are immutable pointers into history and cost nothing. Constraint slugs stay: they are the DB-level version of the same idea and already exemplary. Area codes carry what neither can: a *living* contract with paths, maintained in one reviewable file, resolvable offline by grep — where a ticket number requires tracker access and archaeology, and a `file:line` citation rots with every commit (the line numbers in this very document will drift; its area names won't).

**The keep-small rule.** An area earns a registry entry only when a real contract *and* a gate, rule file, or skill binds to it — otherwise it is just a directory and the filesystem already names it. A registry that tries to taxonomize the whole tree becomes the stale map §1.2 warns about; a dozen governed seams, each load-bearing, stays true.

**Source.** The registry-plus-path-scoping mechanics are standard Claude Code memory/permissions machinery ([memory](https://code.claude.com/docs/en/memory.md), [hooks](https://code.claude.com/docs/en/hooks.md)); the pattern itself is the per-directory ownership/metadata model long used in large codebases (CODEOWNERS-style path mapping), and — in miniature — this codebase's own constraint-slug naming, generalized from tables to seams.

**Failure mode prevented.** Reference rot and tracker lock-in. Prose references ("the planner's async part") are unresolvable by grep; line numbers decay; ticket numbers answer only with archaeology and only while the tracker is reachable. A hallucination-resistant reference is one the agent can *verify cheaply* — an area code greps to its marker and resolves to its contract in one file read, which is exactly the retrieval-over-recall property §4.4 demands.

---

## 5. Agentic workflows

### 5.1 Plan-then-execute, calibrated by blast radius

**Practice.** Use the explore → plan → code → commit cycle ([best practices](https://www.anthropic.com/engineering/claude-code-best-practices)), with plan mode (read-only; Shift+Tab cycles modes — [model config](https://code.claude.com/docs/en/model-config.md)) mandatory for changes touching: migrations, the triage/run state machines, the sync/async seam, retry/timeout/concurrency knobs, or anything in the §5.5 human-gate list. Direct implementation without a plan phase is fine for leaf edits with tight existing tests — a serializer field, a label, a test documenting existing behavior.

The calibration rule: **plan depth proportional to the cost of being wrong, not the size of the diff.** A 5-line migration on the hot `outreachaction` table needs a plan; a 300-line test module usually doesn't.

**Source.** [Best practices](https://www.anthropic.com/engineering/claude-code-best-practices); the built-in read-only Explore/Plan agents ([subagents docs](https://code.claude.com/docs/en/sub-agents.md)).

**Failure mode prevented.** The confident wrong first move. This codebase has several places where the correct edit is counter-intuitive and the intuitive edit is a production incident — hoisting a function-local import (the cycle it breaks is not named at two call sites, `views/leads.py:30`), "simplifying" the double `_live_approval` check in dispatch (the comment explains winning the CAS is deliberately not authorization, `dispatch.py:70-75`), adding an ORM call in phase 3. Plan mode forces the reading that surfaces these before mutation.

**Outdated advice to discard.** "Always ultrathink before planning" — keyword triggers are dead; effort is configured, not incanted ([model config](https://code.claude.com/docs/en/model-config.md)).

### 5.2 TDD with agents

**Practice.** For new behavior: the `test-author` agent writes failing tests first; the implementing session's target is "make these pass without touching them"; the failing-then-passing test output is recorded in the PR description as verification evidence. For bug fixes: reproduce as a failing test before fixing — the reproduction *is* the specification. Existing tests are a contract: if a change cannot pass without modifying an existing test, stop and get explicit human review of whether the test or the design should change.

**Source.** [Best practices](https://www.anthropic.com/engineering/claude-code-best-practices) (the failing test as iteration target); Kent Beck ([Pragmatic Engineer interview](https://newsletter.pragmaticengineer.com/p/tdd-ai-agents-and-coding-with-kent), [Beyond the vibes](https://newsletter.kentbeck.com/p/augmented-coding-beyond-the-vibes)); [arXiv:2511.18397](https://arxiv.org/abs/2511.18397) for why the separation is not optional.

**Failure mode prevented.** Reward hacking (§3.3) and its milder cousin, tests written *after* implementation that assert what the code does rather than what it should do. The existing suite's character — test names as full behavioral sentences, docstrings naming the failure mode defended against (`"a read-then-write implementation passes every sequential test yet still replays under concurrency"`, `tests_auth.py:215-217`) — is the house style the `test-author` agent's system prompt should quote as exemplar.

### 5.3 Worktrees and parallel agents

**Practice.** `claude --worktree <name>` gives each task an isolated checkout under `.claude/worktrees/`; sessions inside a worktree are blocked from editing the main checkout; `.worktreeinclude` lists gitignored files to copy in — for this repo, `.env` (each worktree needs `DJANGO_SECRET_KEY` to boot). A happy structural accident: the SQLite fallback path is checkout-relative, so each worktree automatically gets its own database — parallel agents cannot corrupt each other's DB state without any extra configuration. Subagents can self-isolate with `isolation: worktree`.

**Source.** [Worktrees docs](https://code.claude.com/docs/en/worktrees.md).

**Failure mode prevented.** Parallel agents clobbering a shared checkout and a shared dev database — the second being unrewindable (§3.1).

**A codebase-specific cap on parallelism.** The planner's dedupe rule is a self-documented unlocked read-then-write — the comment at `outreach.py:1928-1930` labels it a known gap: two overlapping runs can both plan the same lead, duplicating recommendations *and LLM spend*. Until the single-active-run DB constraint lands — a partial unique index, now scoped into the async-request-handling epic's `run-lifecycle` area (§0.1); the view-level existence check was rightly rejected as racy, and the codebase's own auth path demonstrates the correct pattern, single-winner conditional UPDATE, proven by an 8-thread test at `tests_auth.py:214-245` — **serialize any agent work that executes the planner**. Parallelize freely on everything else. This is the scaling theme in miniature: agent concurrency is bounded by the *system's* concurrency safety, and the fix is a schema change, not a habit agents are asked to keep.

**Outdated advice to discard.** Manual multi-terminal, multi-clone choreography — native worktrees, background sessions, and cross-session messaging replaced it ([changelog](https://code.claude.com/docs/en/changelog.md)).

### 5.4 Headless and CI usage

**Practice.** Three pipelines, in adoption order:

1. **Automated PR review** via `anthropics/claude-code-action@v1` ([GitHub Actions docs](https://code.claude.com/docs/en/github-actions.md)): a prompt-driven automation run on every PR that reviews *against the repo's specific risk register* — migration lock hazards (the `safe-migration` catalog), query-budget deltas, error-envelope conformance, audit-spine contract adherence, tests-edited-with-source flags. Generic "LGTM" review is worthless; a review agent with this codebase's checklists is a second reviewer that never tires.
2. **Issue-to-PR** for well-specified small tickets: `claude -p` with `--allowedTools`, `--permission-mode dontAsk`, `--max-turns`, and `--output-format stream-json` for machine-readable run records ([headless docs](https://code.claude.com/docs/en/headless.md)). Start with the mechanical ticket classes the survey surfaced: factory consolidation, annotation widening, `.env.example` completion.
3. **Custom pipelines** via the Agent SDK (`claude-agent-sdk`) when the team wants the planner-style budget discipline (max turns, timeouts, structured output) applied to its own agent runs — which, notably, mirrors budgets the product already implements for *its* agent loop (`agent_max_steps`, `agent_max_tool_calls`, per-lead deadline). The product's own design is the template.

Two hard rules: **never `--bare` for repo work** (it skips hooks, skills, agents, and CLAUDE.md — plumbing invocations only), and **CI agents run `dontAsk` inside sandboxed runners** with the §3 deny-lists — a headless agent is the one context where a permission prompt has no one to answer it.

**SCALING tie-in — route the agents like the leads.** The product plan routes cheap models to low-priority leads and strong models to high-priority ones. Apply the identical policy to agent workloads via the `model` frontmatter field on skills and named agents: lint-fixing and scaffolding on a fast cheap model, migration review and planning on the strongest. Track agent spend the way the audit spine will track product spend — per-run, durable, with the estimate-vs-actual gap visible.

**AUDITS tie-in — provenance for agent-written code.** Every agent-produced PR carries: the session identity, the mode/permission set it ran under, the checks it ran with outputs (the failing-then-passing test output from §5.2), and a flag when tests were modified. Headless runs' `stream-json` output is retained as the run record. This is the symmetry the planning threads imply: the team is building `ProviderTrace` so every product LLM call is reconstructable; the same standard means every agent change to the *codebase* is reconstructable — who (which session), what (diff), under which authority (permissions), verified how (check outputs). A team that requires a durable audit row per provider call but accepts anonymous agent commits has an audit spine with a hole in it.

**Failure mode prevented.** Unattributable changes (the exact failure the product's missing actor-column exhibits, now avoided in the meta-layer), and runaway CI spend from unbudgeted agent runs.

### 5.5 Human review gates: non-negotiable list

**Practice.** These merge only with human sign-off, regardless of how green the checks are:

| Gate | Reason (code evidence) |
|---|---|
| **Any migration** | Postgres lock behavior is invisible to the SQLite-backed local loop; the existing non-concurrent constraint adds on hot tables show how easily this ships. The `migration-auditor` pre-reviews; a human decides. |
| **Auth & session code** (`views/auth.py`, `services/login_links.py`, throttling, permission/settings changes) | The current implementation has carefully balanced properties an agent can silently break — timing-equalized failure paths, hashed tokens, `REMOTE_ADDR`-only trust, single-use conditional UPDATE. A diff here that "simplifies" is probably a vulnerability. |
| **`services/dispatch.py` and the approval gate** | This is the service's money-and-action boundary: the send path re-reads copy by sha inside the consuming transaction with `select_for_update`, backstopped by PROTECT FKs and a partial unique constraint. It is the strongest gate in the system and must not be weakened by refactor. |
| **Anything that spends provider money** | Retry policy, concurrency knobs, model selection, prompt size — all spend multipliers in a system whose cost tracking is still being built. Until estimate-before-spend lands, the human *is* the cost estimator. |
| **Sanitization, verifier, and gate logic** (`services/sanitize.py`, `verify.py`, the shape gate) | Fails-closed behavior is the product's injection defense; the `COPY_VERIFY_LEVEL=off` escape hatch already exists with no alarm — the gates must not acquire more. |
| **Feature-flag default flips** (`OUTREACH_AGENT_ENABLED`, trace-content capture) | Flipping a default is a production posture change: the agent path writes PII-bearing audit content; the trace-content flag is a data-retention decision, not a code decision. |
| **Retention/deletion of audit-shaped data** | No purge path exists today for `ProviderTraceContent`, `AgentStep.payload`, `OutreachEdit`, or `LoginToken` (a sweep index exists with no sweeper). When purge commands arrive, they are the definition of human-gated code. |

**Source.** [Best practices](https://www.anthropic.com/engineering/claude-code-best-practices) (human review where verification is expensive relative to blast radius); [METR](https://metr.org/blog/2025-07-10-early-2025-ai-experienced-os-dev-study/) (review cost is the dominant cost — spend it where it matters, automate it everywhere else).

**Failure mode prevented.** The catastrophic-tail failures automation cannot cheaply verify: data loss under lock, auth bypass, unbounded spend, and PII mishandling.

**Outdated advice to discard.** "Human reviews everything the agent writes" (early-2025 posture). With the §3 loop, most diffs are machine-verified to a higher standard than a skimming human applies; concentrating human attention on this table's rows is both safer and cheaper than diffusing it evenly. The METR result is the cautionary tale for the *diffuse* strategy, not for delegation itself.

### 5.6 Orchestration topology: pipelines, loops, and graphs

The subagent roster (§2.3) answers *who*; this section answers *how they compose*. The reference vocabulary is [LangGraph](https://github.com/langchain-ai/langgraph) — "a low-level orchestration framework for building stateful agents," built around durable execution, human-in-the-loop inspection of state, and persistent memory, and usable standalone from LangChain (per its README; docs at docs.langchain.com/oss/python/langgraph/). Its concepts name things this document has been doing implicitly, and naming them makes the workflow layer designable rather than habitual:

| LangGraph concept | The development-workflow equivalent here |
|---|---|
| State + reducers | The structured returns each stage merges — survey reports, verdict lists, a run journal |
| Node | A named agent, a skill, a hook, or a human |
| Conditional edge | Gate-outcome routing: pass → forward; fail → **back**, carrying the gate's findings |
| Dynamic fan-out (`Send`) | Splitting work across parallel, minimally-scoped agents (§2.3) |
| Checkpointer | Durable run state that survives the session — the design the product's agent loop hand-rolled as an event-sourced step log with resume-by-run-id (`agent/state.py`); the two layers converged on the same pattern independently |
| `interrupt()` (human-in-the-loop) | Plan-mode stops and every row of §5.5's gate table |
| Subgraph | A named agent with preloaded skills — a packaged smaller graph invoked as one node |

**Practice: pick the topology by failure mode, and escalate only on evidence — pipeline → loop → graph.** A **pipeline** (fixed stages, one pass) is the default for bounded work. Escalate to a **loop** when quality is verifiable but not guaranteed in one pass: split the work across scoped agents, gate every merge, and route failures *backward* with feedback instead of forward with hope. The gated loop's anatomy: **split** → **gate**, in escalating-cost order (the deterministic §3 checks first, reviewer agents second, the human interrupt only for §5.5 rows) → **converge or recycle** (passed items exit; failed items re-enter carrying the gate's findings). Loops are where agents burn money, so every loop carries the guards the product's own agent loop already implements in code (`agent_max_steps`, `agent_max_tool_calls`, the per-lead deadline): a dry-round exit (K consecutive rounds yielding nothing new), dedupe against everything *seen* — not merely everything accepted, or rejected findings resurface each round and the loop never converges — and a hard budget ceiling (the `cost-check` discipline applied to the meta-layer). Escalate to a **graph** only when routing is conditional *and* state must outlive a session: multi-day epics and standing processes need named nodes, explicit edges per outcome, and a checkpointer, so that "where were we" is a state read, not archaeology. The async-request-handling epic (§0.1) is the product-side twin of this escalation — its `run-lifecycle` area is precisely a graph with a checkpointer, and LangGraph's durable-execution runtime is the best-known off-the-shelf occupant of that row's execution-vehicle decision, carrying the same dependency-and-deployment caveat stated there (its checkpointer is also a second persistence layer outside Django migrations — part of what that explicit decision must weigh).

**Source.** [LangGraph](https://github.com/langchain-ai/langgraph) (framing and features verified against the README); [effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) (condensed returns as the edges' payload discipline); the §5.2 sources on why gates, not vibes, are a loop's exit condition.

**Failure mode prevented.** One per topology. *One-shot-and-hope*: a single pass declared done because nothing re-checked it — §3.2's hallucinated success at workflow scale. *The unbounded loop*: critics that never converge, re-litigating rejected findings at full token cost per round. *Orchestration amnesia*: the multi-day effort whose real state lives in chat scrollback and one person's memory — the workflow-layer version of the audit gaps the product is closing, and the reason durable run state is a requirement, not a nicety.

**Practice: treat the context layer as a node in the graph, not a config file beside it.** The work loop and the context layer form a double loop: the inner loop produces gated work; an explicit outer edge routes what the gates *learned* back into the context layer. Every finding that a rule, skill, hook, or area contract should have prevented becomes a write to that file — and the loop's exit report says so in a structured field. Wire the edge cheaply: reviewer and critic output formats gain a `context-deltas:` section ("what would have prevented this"); accepted deltas land as small human-gated PRs against CLAUDE.md, `docs/areas.toml`, the skills, or a hook; the gate stays human, because the context layer governs the agents and §5.5 already names agent self-modification as gated. The context layer thus has everything a graph node has: state (§6 item 1's files), versioning (git), a gate, and a cadence. This document is itself such a node — each iteration has been one traversal (survey → write → critique gate → revise), re-entered with new inputs (an epic, a reference question, now a topology), reading the durable state the previous traversal wrote. This iteration deliberately ran the shape it describes: drafted, then passed through a split-critic gate that exits CLEAN or routes back for revision, bounded at two rounds.

**Source.** §1.5's maintenance rule made structural; [how Anthropic teams use Claude Code](https://claude.com/blog/how-anthropic-teams-use-claude-code) (teams encoding their own workflow improvements); LangGraph's treatment of memory as first-class state rather than a side channel.

**Failure mode prevented.** *Static-context decay* — the work loop improves while its context stays frozen, so every iteration pays the same tax and the same class of finding recurs forever; and its inverse, *ungated context churn* — agents freely editing their own instructions, the fox-reviews-henhouse problem. The edge must exist *and* pass through the human gate; either alone fails.

**Outdated advice to discard.** "You need a framework to orchestrate agents" (2024–early 2025): the primitives — named subagents, hooks as gates, worktree isolation, checkpointed resumable runs, cross-session messaging — are native harness features now ([subagents](https://code.claude.com/docs/en/sub-agents.md), [worktrees](https://code.claude.com/docs/en/worktrees.md), [changelog](https://code.claude.com/docs/en/changelog.md)); what a framework still offers the *development* layer is vocabulary, and the *product* layer a durable-execution runtime — a dependency decision, not a default. Equally stale: equating LangGraph with the LangChain abstraction stack — the orchestration layer is explicitly standalone, and dismissing the concepts because of the ecosystem discards the useful half.

---

## 6. Prioritized implementation order

Highest leverage first. Each row lists the theme it primarily serves and its precondition.

| # | Action | Theme | Why this order |
|---|---|---|---|
| 1 | **Commit the CLAUDE.md draft** (Appendix A) + `frontend/CLAUDE.md` + `.claude/rules/migrations.md` + `docs/areas.toml` (the area registry, §4.5) | all | Everything else assumes the agent knows the commands, the seams, and the negative rules — and the registry gives rules, hooks, skills, and gates one path-source to bind to. One day of work, immediate effect on every session. |
| 2 | **`.claude/settings.json` permissions**: deny `.env`/`db.sqlite3`/bundle reads and writes; Bash allowlist for the test/lint loop; sandbox verified via `/sandbox` | PRODUCTIONIZING | Safety floor before any autonomy. The committed-secret-in-`.env.example` situation makes the read-denial urgent (and fixing `.env.example` itself is a 10-minute PR to do the same week). |
| 3 | **Hooks**: post-edit ruff, applied-migration write-block, models-edit → `makemigrations --check`, dangerous-Bash guard | PRODUCTIONIZING | Converts the existing CI gates into millisecond agent feedback; blocks the two unrewindable failure classes. |
| 4 | **django-migration-linter in CI + in the post-edit hook** | PRODUCTIONIZING | The single biggest gap between "CI is green" and "the deploy is safe" is migration lock behavior; closes it at both agent-time and merge-time. |
| 5 | **Skills**: `safe-migration`, `drf-endpoint`, `audit-spine` first; `query-review`, `cost-check`, `release-check`, `redteam-local` second | all | The first three encode the three highest-frequency, highest-blast-radius procedures. Skills are cheap to write once hooks make their advice enforceable. |
| 6 | **Named subagents**: `migration-auditor`, `test-author` | AUDITS | Structural optimizer/auditor separation before agent-authored PR volume grows. |
| 7 | **Tamper-evidence**: branch coverage on; tests-edited-with-source flag; convert the `expectedFailure` injection gap into a visible tracked signal | AUDITS | Makes green mean green — precondition for trusting any scaled-up agent throughput. |
| 8 | **Legibility fixes with mechanical payoff**: shared test factories; consolidate the three provider registries; extract the shared DELETE-predicate helper; `.env.example` completed | AUDITS, PRODUCTIONIZING | Small PRs, each eliminating a class of half-updated-copy bugs agents are prone to. Good first issue-to-PR pipeline candidates once #9 lands. |
| 9 | **Headless PR-review workflow** (`claude-code-action@v1`) armed with the skills from #5 as its checklists | SCALING | Multiplies review capacity exactly where the human gates (§5.5) need humans to have attention left. |
| 10 | **MCP**: Postgres read-only introspection; GitHub; issue tracker | SCALING | Grounded schema answers and retrievable ticket context; deferred until the safety floor exists because each server is new surface. |
| 11 | **`mypy` widening + model-layer annotations; begin the `outreach.py` split** | SCALING | Deepens the deterministic feedback the loop runs on; the split is the largest single legibility investment and can proceed incrementally behind the now-solid test net. |
| 12 | **Issue-to-PR pipeline** for mechanical tickets; **parallel worktree workflows** — gated on the single-active-run DB constraint for any planner-executing work (the `run-lifecycle` area of the async-request-handling epic, §0.1, is where that constraint lands) | SCALING | Full autonomy last, once verification (3-7), review capacity (9), and the system's own concurrency safety (the partial unique index) exist. |

| 13 | **Wire the context edge** (§5.6): reviewer/critic output formats gain a `context-deltas:` field; accepted deltas land as small human-gated PRs against CLAUDE.md, `docs/areas.toml`, the skills, and the hooks | AUDITS | The flywheel: converts every gate failure into a permanent context improvement instead of a recurring tax. Nearly free once 5–7 exist — it is an output-format change plus a review habit. |

The through-line: **verification before autonomy, isolation before permission, provenance throughout.** Items 1–4 are a week. Items 5–7 are the second week. Everything after compounds — item 13 is *how* it compounds.

---

## Appendix A: The drafted CLAUDE.md

Fitted to the surveyed service: Django unittest runner (not pytest), ruff with format-check, mypy scoped as CI scopes it, SQLite/Postgres duality via `DATABASE_URL`, env-first settings, committed frontend bundle. Placeholders in `<angle brackets>` mark facts the code does not determine.

```markdown
# CLAUDE.md

Agentic outreach planner. Django 4.2 + DRF backend; React 18/TS frontend built by Vite
into a bundle committed at project/app/static/frontend/ and served by Django (no Node in
the runtime image). Rule functions in services/outreach.py select leads for outreach;
provider calls generate message text only; services/verify.py checks generated copy
against stored lead/event fields and blocks approval when checks fail; a human approval
gate guards every outbound send.

## Commands

# setup
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # then REPLACE DJANGO_SECRET_KEY with a fresh value and set
                       # DJANGO_DEBUG as needed — never keep the example's values

# run
python manage.py migrate
python manage.py runserver                      # http://127.0.0.1:8000
python scripts/populate_demo_data.py            # demo data (ingest + LLM catalog seed)

# tests — Django's unittest runner. There is NO pytest, no conftest.
python manage.py test project.app                                  # full backend suite
python manage.py test project.app.tests.tests_queue                # one module
python manage.py test project.app.tests.tests_queue.Cls.test_name  # one test
# Test modules are tests_<subject>.py; test names are full behavioral sentences —
# grep for the behavior in plain English to find the right test.
DATABASE_URL=<postgres-url> python manage.py test project.app      # Postgres parity
                                                # (CI runs py3.12/3.13 x sqlite/postgres)

# lint / types / migrations — run before any commit; this mirrors CI exactly
ruff check . && ruff format --check .
mypy project/app/services/          # CI typechecks exactly this path, nothing more
python manage.py makemigrations --check --dry-run   # must be clean

# rules-engine regression (pure Python, no DB, no network, frozen clock)
python evals/run_rules_eval.py      # diffs against evals/baselines/ — baselines change
                                    # only by explicit human decision, never to make a run pass

# frontend — only when frontend/ changed
cd frontend && npm ci && npm run typecheck && npm test && npm run build
git diff --exit-code -- project/app/static/frontend/   # CI fails on a stale bundle
# NEVER hand-edit project/app/static/frontend/** — it is build output. Rebuild + commit.

## Architecture in 30 seconds

- project/app/services/outreach.py — rules engine + planner orchestrator (numbered
  phases in comments). project/app/services/llm/ — provider-agnostic LLM layer.
  services/verify.py — grounding verifier. services/dispatch.py — the send gate.
  services/agent/ — flag-gated tool-calling copy loop (OUTREACH_AGENT_ENABLED, default off).
- Registries (start here to find anything): project/app/models/__init__.py,
  project/app/views/__init__.py, project/app/serializers/__init__.py,
  frontend/src/api/endpoints.ts (every frontend API call, one line each).
- Constraint/index names (oa_queue_order, rd_one_live_send_per_action, ...) appear
  verbatim in model and migration — grep the name to get the whole story.
- Area codes name the governed seams: docs/areas.toml maps each slug (llm-seam,
  async-phase, dispatch-gate, ...) to its paths, contract, and gate; `# area:` comments
  mark the binding sites. Reference areas — not line numbers, not tickets — in plans,
  PRs, and reviews. Ticket IDs (MUS-nn) in comments remain as history pointers — grep
  one to find a feature's past.

## Database & migrations — hard rules

- NEVER edit a migration that is committed on the default branch. Additive follow-up
  migrations only. (A hook blocks this; do not work around it.)
- Run `python manage.py makemigrations --check --dry-run` after any model change.
- Dev is usually SQLite; production is Postgres (DATABASE_URL). DDL that is instant on
  SQLite can stall Postgres: index adds take a SHARE lock (writes blocked for the
  build); constraint adds via ALTER TABLE take ACCESS EXCLUSIVE (all access blocked).
  Either is a production stall on a hot table. Any index/constraint on outreachaction,
  lead, or event is a hot-table change: use the /safe-migration skill, prefer
  concurrent operations with atomic = False, and get human review.
- One concern per migration. Schema migrations carry no data operations; seeding lives
  in idempotent management commands (the repo has zero RunPython migrations — keep it so).
- New columns: nullable-or-constant-default first (Postgres adds constant defaults
  instantly), backfill in batches via a management command, then constrain.
- Do not write to the checked-in SQLite file; the Django test runner creates a
  throwaway database for each run.

## ORM & query discipline

- Query budgets are pinned by test (see tests_planner_perf.py — budgets computed from
  connection.ops.bulk_batch_size so they hold on both backends). A budget increase is a
  reviewed decision, not a test fix.
- List endpoints must not serialize unbounded tables; add pagination and a throttle
  scope to any new list/expensive endpoint (settings.py REST_FRAMEWORK block).
- Prefetch rule: only .all() is served from a prefetch cache — any filtered call
  re-queries. Slice prefetched collections in Python, not in the queryset.
- No Django signals anywhere; all writes go through explicit service functions. Do not
  introduce signals.
- Race-sensitive logic gets a database-level guard (partial unique constraint or
  conditional UPDATE), never a read-then-check. Existing patterns to copy:
  single-use login-token redemption, the agent-run epoch-CAS claim,
  rd_one_live_send_per_action.

## Transactions

- ATOMIC_REQUESTS is off (settings.py does not set it): no view is wrapped in a
  transaction automatically. Writes that must land together go in one explicit
  transaction.atomic block in the service function that owns them — never spread
  across helpers each committing separately.
- select_for_update only inside an explicit transaction.atomic block (it errors
  outside one). The dispatch send path — re-read by sha256 under select_for_update
  inside the consuming transaction — is the exemplar to copy.
- Never hold a transaction open across the async provider-call phase: collect inputs,
  close the transaction, await, then write results in a new atomic block. A
  transaction spanning the event-loop hop pins a connection for the full provider
  latency and can deadlock against the planner's own writes.

## Async seam — do not cross it

- plan_outreach is sync; only the provider-call phase runs on an event loop. NO ORM
  calls inside that async phase — it raises SynchronousOnlyOperation at runtime and
  nothing static will warn you. The agent checkpoint writer is the single, documented
  exception; do not add a second.
- services/llm/ must not import Django at module level (runtime.py keeps Django imports
  function-local so the package imports without Django). Preserve this.

## LLM layer

- Adding a provider currently requires edits in three places: the client registry
  (services/llm/__init__.py), the env-var map (services/llm/config.py), and the
  telemetry provider-name map (telemetry/genai.py). Missing the third silently drops
  telemetry attribution. Edit all three or consolidate first.
- Retryability lives on the error class (services/llm/errors.py). Retry policy and
  timeouts are cost multipliers — flag any change as a spend change in the PR.
- Never log or persist raw prompts/completions outside the ProviderTrace content path;
  telemetry spans carry sha256 hashes only. Do not add content keys to spans.
- The stub provider is gated by OUTREACH_ALLOW_STUB_LLM=1 and exists for benchmarks and
  tests only. Never weaken that gate.

## Security invariants — do not weaken, escalate instead

- Lead-controlled text (CRM notes, event payloads) is untrusted input everywhere:
  sanitize before it enters any prompt; tool results are sanitized, length-capped, and
  server-bound to the lead id. The same rule applies in the frontend: never interpolate
  lead-controlled fields into anything prompt-bound.
- suggested_copy on OutreachAction is immutable once written (the eval corpus diffs it);
  reviewer edits create OutreachEdit rows.
- The verifier fails closed: a missing/blank verification report blocks approval. The
  dispatch gate re-reads effective copy by sha256 inside the consuming transaction —
  never "simplify" the double check.
- COPY_VERIFY_LEVEL=off disables grounding checks silently. Never set it in committed
  config; treat any diff containing it as human-review-required.
- Magic-link auth stores only hashed tokens, single-use via conditional UPDATE, with
  timing-equalized failure paths and REMOTE_ADDR-only IP trust. Changes here are
  human-gated.

## Settings & env conventions

- All configuration is environment variables; settings.py is the single source of
  defaults (docker-compose passes planner knobs through blank on purpose — do not
  restate defaults elsewhere).
- Use the _env_int/_env_number/_env_list helpers in settings.py for new variables:
  blank means unset, and bad values must raise ImproperlyConfigured naming the variable.
  (Some older vars use bare int() — fix opportunistically, never imitate.)
- Every new setting gets a .env.example entry and a docker-compose passthrough.
- DJANGO_SECRET_KEY is mandatory (boot fails without it). Boot-time system checks live
  in project/app/checks.py — add one when a misconfiguration should fail boot rather
  than fail at first use.

## Tests

- Fixtures via setUpTestData and factory helpers; pin dates to constants (the suites
  freeze TODAY) rather than reading the clock; patch django.utils.timezone.now only
  where the view under test reads it.
- Every LLM provider interaction is mocked — doubles subclass the real LLMClient so
  seam changes fail loudly. Never let a test reach a real provider.
- Do not edit or delete an existing test to make a change pass; a failing existing
  test requires human sign-off before either the test or the code changes.
- Two suites assert repo-infrastructure facts via git grep / git check-ignore; if one
  fails after a legitimate change, update its manifest — that is the intended workflow,
  not a defect.
- Coverage floor is 90 (pyproject.toml); DRF throttle history persists across tests —
  clear it in setUp/tearDown as tests_auth.py does.

## What always needs a human before merge

Migrations; auth/session/throttle code; services/dispatch.py and the approval gate;
sanitization and verifier logic; feature-flag default flips; anything changing provider
spend (models, retries, concurrency, prompt size); any retention/deletion touching
audit tables (ProviderTrace*, AgentStep, OutreachEdit, LoginToken).

## Deploy (placeholders — code does not determine these)

- Production server/WSGI setup: <not yet defined — the container currently runs the dev
  server; see the release-check skill before any real deployment>
- Production DATABASE_URL / migration execution window: <define with the deploy story>
```

---

*End of blueprint. The drafted CLAUDE.md above is intended to be committed as-is (minus placeholders) as item #1 of the implementation order, then tuned like a prompt: every time an agent makes a mistake this file should have prevented, the fix is a one-line edit here — and every line that stops earning its context cost comes back out.*
