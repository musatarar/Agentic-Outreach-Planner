# Agentic Engineering Blueprint: Claude Code on a Django/DRF/Postgres Service

**Date:** August 2026 · **Written against:** Claude Code 2.1.x · **Grounded in:** this
repository (Locked In — Agentic Outreach Planner), which already implements several of
these practices and serves as the running example throughout.

Claude Code's surface area changed more between April 2025 and mid-2026 than most tools
change in five years: skills absorbed slash commands, hooks grew from four events to
twenty-plus, worktrees and sandboxing went native, thinking keywords died, and
checkpoints were replaced. Any blueprint has a shelf life, so every practice below is
presented the same way — **the practice**, **the source**, **the failure mode it
prevents** — and each section ends with a box of 2024–early-2025 advice that is now
wrong. Mechanics cite the current docs at
[code.claude.com/docs](https://code.claude.com/docs/en/overview); verify against the
[changelog](https://code.claude.com/docs/en/changelog.md) before automating anything
version-sensitive.

---

## TL;DR — the ten decisions that matter

1. **CLAUDE.md is a prompt, not documentation.** Under ~150 lines, commands and
   contradictions-of-defaults only, everything else is a pointer. (§1)
2. **Mark every rule as machine-enforced or hope.** An agent treats all prose equally
   until you tell it which rules a gate will actually catch. (§1, §7)
3. **Skills hold procedures, CLAUDE.md holds rules, hooks hold laws.** If it must
   happen every time, it's a hook; if it's a workflow, it's a skill; if it's one line
   and always relevant, it's CLAUDE.md. (§2)
4. **Write skills from your failures, not from generic best practice.** The two best
   skills in this repo (`planning-discipline`, `webapp-testing`) are distilled
   incident reports. (§2.3)
5. **The feedback loop is the product.** Fast, deterministic lint/typecheck/test that
   the agent can run and trust is worth more than any prompt engineering. (§3.1)
6. **Migrations get three layers of defense:** a linter in CI, a hook that makes
   applied migrations physically un-editable, and a skill that teaches the
   zero-downtime sequence. (§3.2, §2.3.1)
7. **Curate permissions; never accumulate them.** A deny-list of the five commands
   that can hurt you beats 279 accreted allow-entries. (§3.4)
8. **Prod credentials never enter the agent's environment.** Permission prompts are a
   UX feature, not a security boundary; absence of capability is the boundary. (§3.5)
9. **Structure the repo so grep is a reliable API.** Predictable Django layout, small
   modules, single-source registries, ADRs that record *why*. (§4)
10. **Automate headless last.** CI agents amplify whatever loop exists — make the
    loop safe (steps 1–8) before you give it a cron schedule. (§5.4, §6)

---

## 1. CLAUDE.md

### 1.1 What loads, when — the 2026 memory model

Before deciding what to write, know what the machine does with it
([docs: memory](https://code.claude.com/docs/en/memory.md)):

| Layer | Path | Loaded | Belongs there |
|---|---|---|---|
| Managed policy | `/etc/claude-code/CLAUDE.md` (Linux) etc. | Always, first, can't be excluded | Org-wide policy (data handling, escalation) |
| User | `~/.claude/CLAUDE.md` | Always, all projects | *Your* preferences (tone, personal tooling) — never project facts |
| Project | `./CLAUDE.md` or `./.claude/CLAUDE.md` | Always, checked in | The team contract: commands, rules, gotchas |
| Local | `./CLAUDE.local.md` | Always, gitignored | Personal per-project notes (still supported; imports are the tidier alternative) |
| Ancestor dirs | `CLAUDE.md` above cwd | Always, root-first | Monorepo-wide rules |
| Subdirectories | `frontend/CLAUDE.md`, … | **Lazily — only when Claude reads files there** | Area-specific rules at zero cost to unrelated sessions |
| Rules | `.claude/rules/*.md` (path/glob-scoped) | When the scope matches | Many small rule files instead of one monolith |
| Auto memory | `~/.claude/projects/<p>/memory/MEMORY.md` | Index at launch, topics on demand | Claude's own notes; audit with `/memory`, disable with `autoMemoryEnabled: false` |

Two mechanics worth exploiting: `@path/to/file` imports expand inline (a few hops
deep), so a CLAUDE.md can stay short by importing e.g. `@docs/testing.md` — but an
import is *eager*, it still costs context every session, unlike a pointer ("see
`docs/testing.md`") which costs nothing until followed. And subdirectory files are
lazy, which makes them the correct home for anything only relevant in one area —
this repo should hold frontend bundle rules in `frontend/CLAUDE.md`, not the root.

**Practice: treat CLAUDE.md as a system-prompt extension with a token budget, not as
documentation.** Target under ~150–200 lines; the official guidance is the same
(docs recommend <200 lines per file, and `/doctor` now actively trims checked-in
CLAUDE.md by deleting content Claude can derive itself — directory listings,
dependency lists, architecture prose — while keeping gotchas and rationale).
**Source:** [memory docs](https://code.claude.com/docs/en/memory.md);
[Claude Code best practices](https://www.anthropic.com/engineering/claude-code-best-practices)
("tune your CLAUDE.md files like any frequently used prompt");
[Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
("find the smallest possible set of high-signal tokens").
**Prevents:** context rot — as the always-loaded preamble grows, per-instruction
compliance drops and the three rules that actually matter drown in forty that don't.
The context-engineering post treats attention as a depletable budget; every stale
line spends it on all future turns of every future session.

### 1.2 What belongs in it

**Practice: include exactly five kinds of content.**

1. **Commands, copy-paste runnable, with the fast variants** — setup from clean
   clone, the inner loop in fastest-failing-first order (`ruff` → `mypy` →
   `makemigrations --check` → scoped tests), the single-test invocation, and the
   full pre-done gate. If CI runs `mypy project/app/services/`, write *"CI runs
   exactly this target"* — this repo does, and it prevents the agent from either
   under-checking or boiling the ocean.
2. **The architecture in pointer form** — paths plus one-line responsibilities
   ("views orchestrate; services decide; selectors fetch"), not prose. The agent
   verifies pointers cheaply; it can't verify essays.
3. **House rules that contradict what a competent agent would otherwise guess.**
   The model already knows Django. It does *not* know that you forbid signals for
   business logic, that `ATOMIC_REQUESTS` is off, or that tests are pinned. The
   test for inclusion: *would a strong new hire get this wrong on day one without
   being told?* If yes, it's a rule. If they'd discover it in one file read, it's
   noise.
4. **Gotchas that cost a failed run to discover.** This repo's CI carries a
   beautiful example: `dj_database_url` treats a *present-but-empty*
   `DATABASE_URL` as malformed rather than unset, so the SQLite leg must `unset`
   it. That fact currently lives in a CI comment where the agent finds it only
   after failing; it belongs in CLAUDE.md where it's read before.
5. **Hard prohibitions and human gates** — the short list of things never to do
   (edit applied migrations, hand-edit the dev DB, weaken a test) and the list of
   changes that require stopping to ask (see §5.5).

**Practice: tag every rule `[gate]` (machine-enforced) or `[convention]`
(discipline + review).** This repo's CLAUDE.md opens with "an instruction is a
hope; a gate is a fact" and tags every workflow rule accordingly.
**Source:** this repository's `CLAUDE.md` — this is an opinionated recommendation
from practice, not from Anthropic docs.
**Prevents:** two symmetrical failures. Untagged, an agent either treats
conventions as physics (wasting effort routing around rules nobody enforces) or
treats gates as suggestions (pushing a PR that CI will bounce). The tag also keeps
*you* honest: writing `[gate]` next to a rule with no gate is visibly a lie, and
the fix (build the gate — §3) is the highest-value follow-up work there is.

### 1.3 What actively hurts

**Practice: keep out anything the agent can discover in one tool call, anything a
formatter enforces, and anything you won't maintain.**
**Source:**
[best practices](https://www.anthropic.com/engineering/claude-code-best-practices)
(the common mistake is "adding extensive content without iterating on its
effectiveness");
[context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents).
**Prevents — three named failure modes:**

- **Stale-map poisoning.** A file inventory or API survey in CLAUDE.md goes stale
  the week after it's written, and the agent *trusts memory over discovery* — it
  will edit the file the map names rather than grep for the truth. A wrong
  CLAUDE.md is strictly worse than a silent one. Corollary: never duplicate;
  point. One fact, one home (`README` for humans, `SECURITY.md` for posture,
  CLAUDE.md links to both).
- **Style minutiae displacement.** Quote style, import order, line length — ruff
  enforces all of it deterministically in this repo. Spending prompt lines on it
  buys nothing (the hook in §3.2 makes it free) and displaces rules only prose
  can carry.
- **Emphasis inflation.** 2025-era files were full of `IMPORTANT:` and
  `YOU MUST`. Current models follow plain instructions precisely; when
  everything is emphasized, the genuinely load-bearing prohibition (never edit an
  applied migration) loses its distinctness. Reserve emphasis for the rules whose
  violation is expensive and irreversible, and prefer building a gate over
  shouting (§3).

### 1.4 Layering strategy

**Practice: every fact lives at the narrowest scope that covers its audience.**
Org policy → managed file. Personal taste → `~/.claude/CLAUDE.md`. Team contract →
project `CLAUDE.md`. Area rules → subdirectory `CLAUDE.md` (lazy) or
path-scoped `.claude/rules/*.md`. Personal-per-project → `CLAUDE.local.md` or an
import of a home-dir file.
**Source:** [memory docs](https://code.claude.com/docs/en/memory.md).
**Prevents:** user-scope bleeding into team behavior (your personal "always use
uv" breaking a teammate's session is not reproducible or reviewable), and the
monolith tax — one root file paying always-loaded token cost for rules only
relevant when touching `frontend/`. For this repo concretely: the root file keeps
backend rules; bundle-freshness and `node --test` rules move to
`frontend/CLAUDE.md`.

### 1.5 Keeping it true

**Practice: CLAUDE.md is code — review it in PRs, prune it quarterly, and grow it
only from observed failures.** The maintenance loop: when the agent does something
wrong, first ask *can this be a gate?* (hook, CI check, permission rule). Only if
it genuinely can't, add the CLAUDE.md line — and write it as the rule you wish
you'd had, not a transcript of the incident. Capture candidates during work with
the `#` shortcut / `/memory`, then *edit* before committing. Periodically run the
inverse audit: for each line, "has this prevented anything in the last quarter,
and is it still true?"
**Source:** [best practices](https://www.anthropic.com/engineering/claude-code-best-practices)
(iterate on effectiveness); the gate-first ordering is this blueprint's opinion.
**Prevents:** rot (§1.1) and the slower failure where CLAUDE.md becomes a
graveyard of one-off reactions — each individually reasonable, collectively an
incoherent prompt no one dares delete from.

> **⚠️ Outdated (2024–early 2025) — CLAUDE.md edition**
>
> - **"Put everything in CLAUDE.md — it's the only memory."** Dead since skills
>   (Oct 2025), path-scoped rules files, lazy subdirectory files, and auto memory.
>   Long procedural content now belongs in skills that load on demand (§2); the
>   always-loaded file is for always-relevant rules only.
> - **"Maintain a `## Project Structure` tree in CLAUDE.md."** `/doctor` now
>   *removes* derivable content like this. Agentic search made maps a liability.
> - **"Sprinkle IMPORTANT/YOU MUST liberally"** (the April 2025 best-practices
>   post itself suggested emphasis tuning). Current models follow precise plain
>   instructions; inflation now costs more than it buys.
> - **"Use CLAUDE.local.md for personal config."** Still works, but imports
>   (`@~/.claude/my-notes.md`) and `settings.local.json` cover it more flexibly;
>   the docs steer new setups toward imports.
> - **"CLAUDE.md can't exceed the context so don't worry about length."** With
>   auto-compact holding even 1M-context models to 200K by default
>   ([changelog v2.1.223](https://code.claude.com/docs/en/changelog.md)), and
>   attention — not window — as the real constraint, length was never free and
>   still isn't.

The full drafted template is in **§7**.

---

## 2. Skills, slash commands, and subagents

### 2.1 The unification: everything routes through skills

Current state ([docs: skills](https://code.claude.com/docs/en/skills.md)): a skill
is a directory — `.claude/skills/<name>/SKILL.md` plus optional supporting files —
with YAML frontmatter. The fields that matter:

```yaml
---
name: django-migrations          # becomes /django-migrations
description: >                   # the trigger — Claude reads ONLY this at startup
  Use when creating, editing, or reviewing Django migrations or changing models.
allowed-tools: Read, Grep, Glob, Bash   # optional tool allowlist
disable-model-invocation: false  # true = user-only ritual (deploy, release)
user-invocable: true             # false = model-only helper
context: fork                    # run in an isolated subagent instead of inline
model: inherit                   # or pin one; effort: low|medium|high also available
---
```

Loading is progressive, and this is the entire point: at startup Claude sees only
name + description (~tens of tokens per skill); the body loads when triggered;
supporting files (`references/*.md`, `scripts/*.py`) load only if the body points
at them and the task needs them. Old-style `.claude/commands/*.md` slash commands
still work but are the deprecated single-file special case of the same mechanism —
both produce `/name`, both take `$ARGUMENTS`; write skills for anything new.
**Source:** [skills docs](https://code.claude.com/docs/en/skills.md);
[Equipping agents for the real world with Agent Skills](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills);
the format is now an [open standard](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview)
adopted well beyond Claude. Simon Willison's
[assessment](https://simonwillison.net/2025/Oct/16/claude-skills/) ("maybe a
bigger deal than MCP") is the best independent explanation of *why*: procedures as
files beat procedures as protocol.
**Prevents:** the context tax that killed 2025-era mega-CLAUDE.mds and
50-tool MCP configs — expertise that costs nothing until the moment it's needed.

### 2.2 The placement rubric

**Practice: place knowledge by frequency-of-relevance and enforcement-strength.**

| Put it in… | When… | Example (this stack) |
|---|---|---|
| **CLAUDE.md** | Relevant to essentially every session, expressible in ≤5 lines | "No network calls inside `transaction.atomic`" |
| **Skill (model-invoked)** | A *procedure* — multi-step, occasionally relevant, benefits from checklists/scripts/templates | Zero-downtime migration playbook |
| **Skill (`disable-model-invocation`)** | A user-initiated *ritual* with side effects or judgment calls | `/release`, `/scaffold-endpoint` |
| **Subagent** | Needs *isolation* — of context (verbose exploration), of role (independent review), or of filesystem (`isolation: worktree`) | Code reviewer, migration-safety checker |
| **Hook** | Must happen *every time* with zero model discretion | Format after edit; block edits to applied migrations |

Two forcing functions. If you catch yourself writing "always/never" → CLAUDE.md or
a hook, depending on whether violation is survivable. If you catch yourself
writing numbered steps → skill. If the skill's value depends on the model *not*
having been the author of the thing under inspection → subagent (§2.4).
**Source:** placement table is this blueprint's synthesis of the
[skills](https://code.claude.com/docs/en/skills.md) /
[hooks](https://code.claude.com/docs/en/hooks-guide.md) /
[subagents](https://code.claude.com/docs/en/sub-agents.md) docs.
**Prevents:** the two classic misplacements — procedures in CLAUDE.md (always-paid
context for sometimes-needed knowledge) and laws in skills (a skill is advice the
model may not trigger; a formatter that runs "usually" is worse than none, because
you stop checking).

### 2.3 A concrete skill set for this stack

Ship four. Each description below is the actual frontmatter `description` to use —
descriptions are load-bearing, they're the only thing Claude sees when deciding to
trigger ([skills docs](https://code.claude.com/docs/en/skills.md)).

#### 2.3.1 `django-migrations` — the zero-downtime playbook

```yaml
name: django-migrations
description: >
  Use whenever creating, editing, reviewing, or planning Django migrations, or
  changing models.py in a way that alters schema. Covers immutability rules,
  zero-downtime sequencing on Postgres, data-migration batching, and when to
  stop for human review.
```

Body (~80 lines) + `references/incompatibilities.md` for the long table:

- **Immutability:** merged ⇒ applied somewhere ⇒ immutable; fix forward. (The hook
  in §3.2 enforces this; the skill explains the *why* and the recovery path.)
- **The three-deploy sequence** for anything non-additive: (1) additive+nullable,
  deploy code that writes both; (2) backfill in batches via a data migration or
  management command; (3) constraint (`NOT NULL`, unique) in a later deploy —
  unique indexes via `django.contrib.postgres.operations.AddIndexConcurrently`
  with `atomic = False`.
- **Never-list:** drop/rename a column in the same release as the code that stops
  using it; `NOT NULL` without default on a live table; non-`CONCURRENTLY` index
  on a big table; schema + data in one migration file (a failing backfill rolls
  back the schema change with it).
- **`RunPython` rules:** `apps.get_model()` only, explicit `reverse_code`,
  batched writes, `elidable=True` where squash-safe.
- **Exit instruction:** print the `sqlmigrate` output and the lintmigrations
  verdict, then *stop for human review* if the migration is destructive or
  touches a table over N rows (§5.5).

**Source:** sequencing is the long-standing consensus recipe —
[django-migration-linter's incompatibility catalog](https://github.com/3YOURMIND/django-migration-linter/blob/main/docs/incompatibilities.md),
[Django migrations without downtime](https://gist.github.com/majackson/493c3d6d4476914ca9da63f84247407b),
and the automation option
[django-pg-zero-downtime-migrations](https://github.com/tbicr/django-pg-zero-downtime-migrations).
**Prevents:** the migration disaster class — the single worst thing an agent can
do to a production service. An agent that knows Django will happily generate a
`makemigrations` diff that's *correct* and *lock-hazardous* at once; textbook
Django is not deploy-safe Django, and that gap is exactly what a skill is for.

#### 2.3.2 `drf-endpoint` — scaffolding with the house conventions

```yaml
name: drf-endpoint
description: >
  Use when adding or substantially extending a DRF API endpoint. Scaffolds
  serializer, viewset, urls, permissions, throttling, pagination, and the test
  file with the auth matrix and query-count pin, in the house style.
```

Body: ordered steps + `references/templates/` holding a serializer, viewset, and
test-file template. The conventions the templates encode: explicit `fields` lists
(never `"__all__"` — new model fields must not leak into the API silently);
validation in serializers, decisions in services; explicit `permission_classes`
and throttle scope per view; pagination mandatory on lists; the test template
pre-contains the auth matrix (`401` anonymous / `403` wrong-role / `2xx` happy
path) and a `assertNumQueries` pin (§4.2).
**Prevents:** convention drift — the agent's scaffold is always *plausible* DRF;
without templates each new endpoint is plausible *differently*, and review burns
on re-litigating settled decisions. A scaffold skill converts those decisions
from review comments into starting state.

#### 2.3.3 `query-review` — N+1 hunting as a procedure

```yaml
name: query-review
description: >
  Use before merging changes to serializers, viewsets, selectors, or any
  queryset construction — and when a query-count pin (assertNumQueries) fails.
  Finds N+1s and missing select_related/prefetch_related.
```

Body: the mechanical procedure — start from the view's queryset; walk every
serializer field to its attribute access; flag per-row relation access,
`SerializerMethodField` bodies, and model properties that query; check
`prefetch_related` vs `select_related` choice (to-many vs to-one); verify with
`django.test.utils.CaptureQueriesContext` around the endpoint and read the
actual SQL; end by tightening or justifying the `assertNumQueries` pin. Plus the
DRF-specific pitfall list: nested serializers silently multiplying, per-object
permission checks in list views, pagination `count()` on expensive querysets.
**Prevents:** the most common *silent* Django regression. N+1s pass every
functional test and every type check; only a counted pin or a procedure that
reads the SQL catches them before production does.

#### 2.3.4 The meta-rule: skills encode *your* failures

**Practice: the highest-value skills are distilled incident reports, not generic
best practice.** This repo's `planning-discipline` skill is the exemplar: every
rule in it was "distilled from an observed planning failure" in a named
experiment, with the receipts linked. Its rules are unguessable from first
principles ("plan against origin, not your worktree"; "a human approval
authorizes exact bytes, not an entity") — which is precisely why they're worth
tokens. Generic advice the model already knows is free; your failures are not.
**Source:** `.claude/skills/planning-discipline/SKILL.md` in this repo;
[How Anthropic teams use Claude Code](https://claude.com/blog/how-anthropic-teams-use-claude-code)
shows the same pattern of teams encoding their own workflows rather than
importing generic ones.
**Prevents:** skill-collection theater — installing twenty marketplace skills
that restate the model's training data while the three rules that would have
prevented last month's incident remain undocumented.

### 2.4 Subagents: when to delegate, and how to scope their context

Mechanics ([docs: subagents](https://code.claude.com/docs/en/sub-agents.md)):
named agents live in `.claude/agents/<name>.md` with frontmatter (`name`,
`description`, `tools`, `disallowedTools`, `model`, `permissionMode`, `maxTurns`,
`skills` to preload, `memory`, `isolation: worktree`) and a body that *is* the
agent's system prompt. A named agent starts from that prompt plus your delegation
message plus CLAUDE.md — **not** the parent's conversation. Ad-hoc forked
delegation, by contrast, now inherits the full conversation by default in
interactive sessions (a 2026 change). That distinction decides everything below.

**Practice: delegate for isolation, not for vibes — and pick the isolation you
actually need.** Three legitimate reasons:

1. **Context isolation** — the task emits verbose garbage (test logs, broad
   exploration). A background subagent reads 50 files and returns 10 lines; the
   parent keeps a clean context. Built-in `Explore` exists for exactly this.
2. **Role independence** — review and verification. The reviewer must *not* see
   the author's reasoning, or it inherits the author's blind spots and
   rationalizations. This requires a *named* agent (fresh context by
   construction); ad-hoc forking silently defeats it. Kent Beck's framing —
   separate the genie that optimizes from the genie that audits, in separate
   contexts, to remove the conflict of interest — matches the practice
   ([Pragmatic Engineer interview](https://newsletter.pragmaticengineer.com/p/tdd-ai-agents-and-coding-with-kent)).
3. **Filesystem/parallelism isolation** — `isolation: worktree` for agents that
   mutate files concurrently (§5.3).

The set worth defining for this stack:

- **`code-reviewer`** — named agent; input: the diff and changed files only;
  tools: `Read, Grep, Glob, Bash`; instructed to report findings as
  `file:line — claim — severity`, to *verify each claim by reading the code*
  before reporting, and to fix nothing. Runs before every PR (§5.4 wires it into
  CI).
- **`test-writer`** — named agent; input: the behavioral contract (ticket text,
  docstrings, the interface — this repo's `docs/contracts/` format is ideal),
  explicitly **not** the implementation. Tests written from the spec catch
  implementation bugs; tests written from the code enshrine them.
- **`migration-safety`** — named agent; input: migration files + models diff;
  preloads the `django-migrations` skill via the `skills:` field; verdict format
  `SAFE / UNSAFE(reason) / NEEDS-HUMAN(reason)`. Cheap to run on every PR that
  touches `migrations/`; pin `model:` to a smaller tier if cost matters — the
  check is procedural.

**Practice: design the return contract before the delegation.** A subagent's
final message is all the parent gets; "report file:line verdicts, max 30 lines,
no code blocks" protects the parent's context as deliberately as the delegation
protected the subagent's.
**Source:** [subagents docs](https://code.claude.com/docs/en/sub-agents.md);
[context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
(sub-agent architectures section — specialized agents with clean contexts,
condensed summaries back).
**Prevents:** the two subagent failure modes — contaminated review (fork
inheritance where independence was the point) and delegation that returns a
15,000-token transcript, importing the pollution the delegation existed to avoid.

> **⚠️ Outdated (2024–early 2025) — skills/commands/subagents edition**
>
> - **"Build your prompt library in `.claude/commands/*.md`."** Commands still
>   work but merged into skills; single files, no frontmatter, no supporting
>   files, no auto-trigger. New work goes in `.claude/skills/`.
> - **"Wrap internal procedures in an MCP server."** For *procedural* knowledge,
>   skills beat MCP on token cost and maintenance
>   ([Willison](https://simonwillison.net/2025/Oct/16/claude-skills/)); MCP
>   remains right for *live data and actions* (§4.3).
> - **"Spawn subagents to save context"** as a reflex. Fork-by-default (2026)
>   means ad-hoc delegation *shares* context now; auto-compact and larger
>   windows weakened the raw-space argument. The surviving reasons are
>   isolation, independence, and parallelism — named agents, deliberately.
> - **"Subagents can't spawn subagents"** — depth-3 nesting and ~20-concurrent
>   are current defaults; orchestration patterns from 2025 blog posts understate
>   what's now native (including cross-session messaging).
> - **"Use output styles to make Claude a code reviewer."** Output styles were
>   deprecated/reworked in the 2.x era; personas belong in named agents.

---

## 3. Guardrails and verification loops

### 3.1 The thesis: fast deterministic feedback is the single biggest lever

**Practice: before any prompt engineering, make `lint → typecheck → scoped test`
fast (seconds), deterministic (no flakes, no network), and runnable by the agent
without ceremony — then wire the loop to run automatically (§3.2).**
**Source — the consensus, assembled:**
[Claude Code best practices](https://www.anthropic.com/engineering/claude-code-best-practices)
is explicit that agent output quality jumps when Claude has "a clear target to
iterate against" — a failing test, an error message, an expected output;
[Writing effective tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents)
generalizes it (agent improvement is driven by evaluation loops);
[How Anthropic teams use Claude Code](https://claude.com/blog/how-anthropic-teams-use-claude-code)
shows internal teams converging on verify-first autonomy. The
[METR RCT](https://metr.org/blog/2025-07-10-early-2025-ai-experienced-os-dev-study/)
(early-2025 tools, 16 experienced maintainers, 246 tasks) found a 19% *slowdown*
with AI — while developers believed they were 20% faster; METR now labels the
result historical, but its mechanism diagnosis stands: the time went into
reviewing, correcting, and re-prompting plausible-but-wrong output. Verification
cost is where agent productivity goes to die, and deterministic feedback is the
only thing that shrinks it for both the agent (self-correction before you ever
see the code) and the human (review starts from "gates passed").
**Prevents:** the plausible-but-wrong loop. An agent without a trustworthy
verifier optimizes for *looking done* — code that reads correctly, compiles, and
fails in the second-order ways (N+1s, lock hazards, broken invariants) that this
stack's gates exist to catch. Every guardrail in the rest of this section is this
thesis applied to a specific failure class.

The Django-specific angle: this stack is *blessed* with deterministic
verifiability — `manage.py test` builds a hermetic database per run, this repo
mocks every provider call so the suite passes offline, and `assertNumQueries`
makes even performance regressions assertable. The loop for this repo, in
fastest-failing-first order, all local, no network:

```
ruff check . && ruff format --check .      # ~1s
mypy project/app/services/                  # seconds — CI runs exactly this
python manage.py makemigrations --check --dry-run
python manage.py test project.app.tests_<area>   # scoped, seconds
python manage.py test project.app                # full, before "done"
python evals/run_rules_eval.py              # domain regression vs committed baseline
```

That last line generalizes: **domain evals as CI gates**. The repo's rules-eval
runs pure-Python against a committed golden set in milliseconds — a deterministic
regression gate for *business behavior*, which is the layer plain tests often
miss and agents most plausibly break.

### 3.2 Hooks: the loop made automatic

Hooks are shell commands (or prompts/agents) the *harness* executes on lifecycle
events — the agent cannot forget them, skip them, or be talked out of them.
Events that matter here: `PreToolUse` (can block a tool call — exit code 2 =
block, stderr becomes feedback to Claude), `PostToolUse` (after; exit 2 feeds
stderr back as a correction), `Stop` (can refuse to let the turn end),
`SessionStart` ([docs: hooks](https://code.claude.com/docs/en/hooks.md),
[guide](https://code.claude.com/docs/en/hooks-guide.md)).

**Practice: four hooks for this stack, in `.claude/settings.json`:**

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [{ "type": "command",
                    "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/format.sh" }]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [{ "type": "command",
                    "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/protect-migrations.sh" }]
      },
      {
        "matcher": "Bash",
        "hooks": [{ "type": "command",
                    "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/bash-guard.sh" }]
      }
    ],
    "SessionStart": [
      { "hooks": [{ "type": "command",
                    "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/session-start.sh" }] }
    ]
  }
}
```

**1. Format-on-edit** (`format.sh`) — closes the loop at zero tokens:

```bash
#!/usr/bin/env bash
# PostToolUse[Edit|Write]: auto-fix and format the touched Python file.
# Exit 2 feeds remaining (unfixable) lint errors back to Claude as a correction.
set -uo pipefail
file=$(jq -r '.tool_input.file_path // empty')
[[ "$file" == *.py ]] || exit 0
[[ "$file" == */migrations/* ]] && exit 0        # ruff excludes them anyway
cd "$CLAUDE_PROJECT_DIR"
.venv/bin/ruff format "$file" >/dev/null 2>&1
if ! out=$(.venv/bin/ruff check --fix "$file" 2>&1); then
  echo "ruff found issues it could not auto-fix:" >&2
  echo "$out" >&2
  exit 2
fi
```

**2. Migration immutability** (`protect-migrations.sh`) — turns the most
important `[convention]` in any Django repo into a `[gate]`. "Applied" is
approximated as "exists on `origin/master`", which is exactly right under this
repo's squash-merge flow:

```bash
#!/usr/bin/env bash
# PreToolUse[Edit|Write]: migrations that exist on origin/master are immutable.
set -uo pipefail
file=$(jq -r '.tool_input.file_path // empty')
[[ "$file" == */migrations/*.py ]] || exit 0
[[ "$(basename "$file")" == "__init__.py" ]] && exit 0
cd "$CLAUDE_PROJECT_DIR"
rel=$(git ls-files --full-name -- "$file" 2>/dev/null | head -1)
[ -z "$rel" ] && exit 0                            # new file this branch: allowed
if git cat-file -e "origin/master:$rel" 2>/dev/null; then
  echo "BLOCKED: $rel exists on origin/master — merged migrations are applied" \
       "somewhere and therefore immutable. Create a NEW migration instead" \
       "(see the django-migrations skill)." >&2
  exit 2
fi
```

**3. Bash guard** (`bash-guard.sh`) — permission *rules* (§3.4) handle
prefix-shaped danger; the hook catches patterns *inside* command lines:

```bash
#!/usr/bin/env bash
# PreToolUse[Bash]: block command patterns that are never OK in an agent session.
set -uo pipefail
cmd=$(jq -r '.tool_input.command // empty')
deny() { echo "BLOCKED: $1" >&2; exit 2; }
case "$cmd" in
  *"manage.py flush"*|*"sqlflush"*|*"reset_db"*)
      deny "database flush — demo state is rebuilt only via scripts/populate_demo_data.py" ;;
  *"--no-verify"*)
      deny "bypassing hooks/pre-commit defeats the verification loop" ;;
  *"push --force"*|*"push -f"*)
      deny "force-push — history on shared branches is immutable" ;;
esac
exit 0
```

**4. Environment bootstrap** (`SessionStart`) — this repo already ships it: the
hook rebuilds the venv, installs dev deps, copies `.env.example`, and puts
`.venv/bin` on PATH for remote sessions. A loop the agent can't *start* is a
loop that doesn't exist; bootstrap is part of the loop.

A `Stop` hook running the cheap gates (`ruff check`, `makemigrations --check`)
and refusing to end the turn while they fail is the aggressive fourth option —
effective, but respect the `stop_hook_active` flag in the hook input to avoid
self-blocking loops, and keep it to sub-second checks; a Stop hook that runs the
full suite makes every conversational turn pay for it.
**Source:** [hooks reference](https://code.claude.com/docs/en/hooks.md) (exit-code
and JSON semantics, `stop_hook_active`).
**Prevents, respectively:** style-nit CI failures and token-burning "now run the
formatter" turns; the migration disaster class (§2.3.1) at the mechanical layer;
the small set of catastrophic commands that permission prefixes can't express;
and the silent-broken-environment session where the agent "fixes" your tooling
instead of your code.

### 3.3 Pre-commit and CI: the outer loop

**Practice: same checks, three distances.** Hooks give the agent feedback in
seconds (per-edit); `pre-commit` (or a `verify.sh`) bundles the full local gate
at commit time for humans and agents alike — ruff, `mypy`,
`makemigrations --check`, `lintmigrations`; CI re-runs everything hermetically
and *aggregates to a single required check*. This repo's `ci-ok` job — one stable
name that fails unless every matrix cell succeeded, protected by a no-bypass
ruleset — is the pattern: it's what makes `[gate]` a fact rather than a hope.
Add [django-migration-linter](https://github.com/3YOURMIND/django-migration-linter)
(`manage.py lintmigrations`) to `requirements-dev.txt` and CI: it statically
flags backward-incompatible operations (NOT NULL without default, drops,
renames) — the machine half of what the `django-migrations` skill teaches.
**Source:** repo's `.github/workflows/ci.yml`; migration-linter docs (3YOURMIND
runs it on every build to keep blue/green deploys safe).
**Prevents:** gate divergence — the failure where the agent's local loop, the
human's pre-commit, and CI check *different things*, so "green locally" stops
predicting "green in CI" and everyone learns to wait for CI (the slowest
possible loop). One list of checks, referenced everywhere, and CLAUDE.md's job is
naming which command *is* the CI truth (§1.2).

**Corollary: deny the bypass.** An agent that can `git commit --no-verify` will
eventually use it innocently ("the hook seems broken, working around it"). The
bash-guard above and a permissions `deny` entry close it from both sides.

### 3.4 Permissions: curated, not accumulated

Current model ([docs: permissions](https://code.claude.com/docs/en/permissions.md),
[modes](https://code.claude.com/docs/en/permission-modes.md)): `allow`/`ask`/`deny`
rule arrays with prefix matching (`Bash(git *)`), MCP scoping
(`mcp__linear__*`), and domain-scoped `WebFetch`; plus session-wide modes —
`default`, `acceptEdits`, `plan`, `dontAsk` (deny-everything-not-allowlisted, for
CI), `bypassPermissions`, and the 2026 addition `auto`, where a classifier model
adjudicates borderline calls instead of interrupting you.

**Practice: write the permission file as policy, not as history.** About twenty
prefix rules cover a Django repo's legitimate loop:

```json
{
  "permissions": {
    "allow": [
      "Read", "Edit", "Write", "Glob", "Grep",
      "Bash(git status)", "Bash(git diff *)", "Bash(git log *)",
      "Bash(git add *)", "Bash(git commit *)",
      "Bash(.venv/bin/ruff *)", "Bash(.venv/bin/mypy *)",
      "Bash(.venv/bin/python manage.py test *)",
      "Bash(.venv/bin/python manage.py makemigrations *)",
      "Bash(.venv/bin/python manage.py check *)",
      "Bash(.venv/bin/python manage.py lintmigrations *)",
      "Bash(.venv/bin/python scripts/populate_demo_data.py)",
      "Bash(.venv/bin/coverage *)",
      "Bash(npm test *)", "Bash(npm run build *)"
    ],
    "ask": [
      "Bash(.venv/bin/python manage.py migrate *)",
      "Bash(git push *)",
      "Bash(.venv/bin/pip install *)"
    ],
    "deny": [
      "Read(./.env)",
      "Bash(git push --force *)",
      "Bash(* --no-verify*)"
    ]
  }
}
```

This repo is its own cautionary tale: its `settings.json` allow-list has accreted
**~280 entries** — absolute paths from a different machine, one-off `sed`
invocations, whole quoted heredocs — approved one prompt at a time and never
curated. That list is unauditable (nobody can say what it permits), partially
dead (the paths don't exist here), and it teaches the reviewer to stop reading.
Rewrite it to the shape above; session-specific approvals belong in
`settings.local.json`, which is gitignored.
**Source:** [permissions docs](https://code.claude.com/docs/en/permissions.md);
the accretion critique is from this repo's own history.
**Prevents:** two failures. Prompt fatigue → rubber-stamping → the one prompt
that mattered gets approved on reflex. And the checked-in-allowlist-as-changelog
problem: a permissions file you can't review is a permissions file you can't
trust.

Note what deliberately sits in `ask`: `migrate` (§3.5), `push`, and dependency
installation — supply chain is a human gate (§5.5), and `pip install` on reflex
is how typosquats land.

### 3.5 Sandboxing and the database

**Practice: give the agent a database it's allowed to destroy, and no path to
one it isn't.** Concretely, four tiers:

- **Test DB** — created and dropped by the runner, hermetic, unlimited.
- **Dev DB** — Docker Postgres (or this repo's SQLite fallback), rebuilt
  idempotently by `scripts/populate_demo_data.py`. The seed script *is* the
  reset button; hand-edits are banned (and the flush-guard hook makes the ban
  mechanical). Treat as cattle.
- **Staging/replica** — read-only role, for schema introspection and `EXPLAIN`.
  Read-only means a Postgres role with `SELECT` only — enforced by the database,
  not by the prompt.
- **Production** — *no credentials in the agent's environment, ever.* Not "deny
  rules", not "ask first": absent. Permission prompts and instructions are UX
  affordances; under prompt injection or plain error they are not boundaries.
  The only reliable guarantee is that the capability does not exist in the
  session. Prod migrations run from the deploy pipeline exclusively.

Claude Code's native sandboxing (default-on for macOS/Linux;
[docs](https://code.claude.com/docs/en/sandboxing.md)) adds OS-level filesystem
and network isolation around bash with per-path and per-domain rules — check
`/sandbox` for status. It's the mechanism that makes broader auto-approval sane:
allow more *inside* a boundary that makes the worst case survivable. For fully
autonomous runs, the sane stack is a container or remote sandbox (Claude Code on
the web runs sessions this way) plus `dontAsk`/`bypassPermissions` *inside* it —
isolation first, then autonomy, never the reverse.
**Source:** [sandboxing docs](https://code.claude.com/docs/en/sandboxing.md);
[permission modes](https://code.claude.com/docs/en/permission-modes.md).
**Prevents:** the unrecoverable class. Everything else in this blueprint fails
into "revert the commit"; a dropped table or a poisoned prod row does not.
Capability absence is the only guardrail that holds against both a confused
agent and a hostile prompt.

> **⚠️ Outdated (2024–early 2025) — guardrails edition**
>
> - **"Ask Claude to run the linter when it finishes."** Hooks exist precisely so
>   verification is not model-discretionary. Prompted verification is the
>   pre-hooks workaround (hooks shipped mid-2025) — an instruction where a gate
>   belongs.
> - **"`--dangerously-skip-permissions` in a devcontainer is the autonomy
>   recipe."** The flag-plus-container pattern from the April 2025 post is
>   superseded in most cases: native sandboxing + curated rules + `auto`/
>   `dontAsk` modes give autonomy with an actual boundary, and the old flag is
>   effectively replaced by `bypassPermissions` mode — still isolation-only.
> - **"Permission fatigue is the price of safety."** The `auto` classifier mode,
>   prefix rules, and sandbox-scoped allowances removed the excuse; if your team
>   is click-approving 30 prompts an hour, that's a config smell now, not a
>   virtue.
> - **"Have the agent fix CI by iterating on the CI logs."** Still works, but
>   it's the slowest loop in the building. The 2026 shape is CI-parity locally
>   (one list of checks) so CI is confirmation, not discovery.

---

## 4. Repo architecture for agent legibility

### 4.1 Structure principles

**Practice: optimize the repo for search-driven comprehension — an agent reads
like a very fast new hire with no tenure.** The load-bearing properties:

- **Predictable placement.** Django's opinionated layout (`models.py`,
  `serializers.py`, `views.py`, `urls.py`, `migrations/`) is a genuine agentic
  asset: the agent's first guess about where things live is right. Preserve it;
  when files grow, split into packages that keep the names
  (`views/ → views/queue.py, views/reports.py` — as this repo does for
  `models/`, `views/`, `services/`).
- **Module size under ~500 lines.** Agents read whole files; a 2,000-line
  `views.py` spends four files' worth of context to answer one question. Split
  by domain, not by kind-within-kind.
- **Boundaries that hold.** The services/selectors split (§7's template) isn't
  aesthetics: it means "where do I change pricing logic?" has one answer, and a
  reviewer agent can enforce "no ORM writes outside services/" with a grep.
- **Tests mirror modules and pin one behavior each, docstring one line.** This
  repo's `tests_llm_retry.py`-per-surface layout means the agent finds the
  pinning tests for any module by name — which is what makes "existing tests
  are pinned" (§5.2) navigable rather than frustrating.
- **Type hints + a checked target.** Types are machine-checkable spec; mypy on
  a scoped, honest target (`project/app/services/` here — checked in CI, stated
  in CLAUDE.md) beats aspirational repo-wide settings full of ignores. For a
  full-stack Django treatment, `django-stubs` extends checking into the ORM.

**Source:** structure-as-context is the theme of
[effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
(agentic search over pre-loading); the specifics are this blueprint's synthesis
from this repo.
**Prevents:** hallucinated APIs. An agent hallucinates when retrieval is
expensive and guessing is cheap; predictable structure inverts the economics.
Every naming convention kept is a grep that works; every grep that works is a
fact the model doesn't invent.

### 4.2 Django-specific legibility

**Practice: make the framework's implicit machinery explicit at the points
agents (and humans) misread it.**

- **No business logic in signals.** Signals are invisible control flow — the
  textbook agent trap: reading the view tells you nothing about what actually
  happens on save. Explicit service calls only; if a signal must exist
  (third-party integration), it forwards to a named service function.
- **Explicit `related_name` on every FK/M2M.** `foo_set` is a guess; a named
  reverse accessor is a greppable fact.
- **Constants as `TextChoices`/`IntegerChoices`,** referenced everywhere —
  never string literals scattered per-file. One enum is one grep.
- **Write the invariant where it binds.** This repo's `services/outreach.py`
  documents its phase contract ("the async phase holds no ORM access") *in the
  module*, and the planning skill enforces designing within it. An invariant
  the agent can read at the point of temptation is a constraint; one in a wiki
  is a landmine.
- **Query-count pins as executable performance spec.** `assertNumQueries(N)` on
  every list endpoint turns "we care about N+1s" from culture into a failing
  test — the only dialect of "we care" an agent reliably hears (§2.3.3).
- **One registry per fact.** `.env.example` as the exhaustive, commented env-var
  registry (this repo's is exemplary — every var with default, failure mode,
  and rationale); `config.toml` for provider selection; the URL conf as the
  API index. Agents answer from registries instead of reconstructing them.

**Prevents:** the framework-magic failure class — the agent edits `save()`, a
signal fires twice; it guesses a reverse accessor name; it "adds one setting"
in three places. Django rewards convention with brevity; agents reward it with
correctness.

### 4.3 MCP: connect little, deliberately

**Practice: connect MCP servers for *live data and actions you'll act on
weekly*; prefer CLIs and skills for everything else.** For this stack the
defensible set is: **GitHub** (PRs, reviews, CI status — the backbone of §5.4;
in hosted sessions it's provided, locally the `gh` CLI is often the better
tool), **Linear** (this repo already ships it in `.mcp.json` — tickets are the
unit of work in its workflow, so the agent reading/updating them closes the
loop), **Postgres read-only** against dev/staging (schema truth and `EXPLAIN`
without copy-pasting; credentials = the read-only role from §3.5 — for
local-only work, `manage.py dbshell` plus the allow-rule is a lighter
equivalent), and **observability** (Sentry's server if you use Sentry; this
repo's OTel/Phoenix stack has an MCP server available — the agent debugging
from real traces instead of your paraphrase of them is the single best
debugging upgrade).

Restraint has a mechanical basis: every connected server's tools cost context
and attention (deferred/searched tool loading in 2026 Claude Code mitigates,
but doesn't erase, the cost —
[MCP docs](https://code.claude.com/docs/en/mcp.md)), and each server is
operational surface (auth, availability, trust). The
[code execution with MCP](https://www.anthropic.com/engineering/code-execution-with-mcp)
post measures the ceiling case: restructuring tool-call pipelines as code
against filesystem APIs cut one workflow from ~150K tokens to ~2K (98.7%).
The general lesson scales down: a CLI the agent can compose (`gh`, `psql`)
often beats a tool schema it must carry.
**Source:** [MCP docs](https://code.claude.com/docs/en/mcp.md);
[code execution with MCP](https://www.anthropic.com/engineering/code-execution-with-mcp);
[Willison on skills vs MCP](https://simonwillison.net/2025/Oct/16/claude-skills/).
**Prevents:** context bloat with a side of attack surface — the 2025-era config
with nine servers, sixty tools, and one weekly use case; and the subtler
failure where the agent, offered a hammer-shaped tool, finds hammer-shaped
problems.

### 4.4 Context engineering: retrieve, don't stuff

**Practice: structure knowledge for just-in-time retrieval, and let the agent
navigate to it.** The docs architecture that works is progressive disclosure
applied to prose — exactly the skills principle (§2.1), because it's the same
problem:

- `README.md` — the human tour (this repo's includes architecture-in-30-seconds
  with measured numbers — agents quote it instead of guessing).
- `CLAUDE.md` — rules + commands + pointers only (§1).
- `docs/adr/` — decisions **with the repo-specific reasons**. This repo's ADR
  rejecting LangGraph is the model: it cites the CI matrix, the pinned-deps
  posture, and the fixtures discipline as reasons. An agent that reads it stops
  proposing LangGraph — permanently, for the *actual* reasons.
- `docs/contracts/` — frozen interfaces for in-flight multi-PR features, with
  planted-red test files (this repo's `agent-loop` contract). For parallel
  agents this is load-bearing: the contract is the coordination artifact that
  makes independent PRs composable (§5.3).
- `SECURITY.md` — the threat model, referenced (not restated) wherever it
  binds.

And the keep-out list — what should *never* enter context: secrets (`deny:
Read(./.env)`; the registry-with-comments lives in `.env.example`, which is
safe and *more* informative), generated artifacts (the committed frontend
bundle, lockfiles — excluded via ruff/`claudeMdExcludes`/plain instruction),
and bulk data (fixtures, `raw_data/` — describe shape + row counts in a small
doc; the agent samples with `head` when it truly needs bytes).
**Source:**
[effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
(just-in-time retrieval; agents "maintaining lightweight identifiers — file
paths, queries — and loading data at runtime"; context rot, with the empirical
degradation work it cites);
[equipping agents with skills](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills).
**Prevents:** both halves of the retrieval failure. *Stuffing* (pasting the
schema, the API list, the module map into always-loaded context) rots and
poisons (§1.3). *Absence* (no ADR, no registry, no contract) doesn't produce
silence — it produces plausible fiction, because the agent fills structural
gaps with the statistically likely answer. A retrievable fact is the only
reliable displacement for a hallucinated one.

> **⚠️ Outdated (2024–early 2025) — architecture/context edition**
>
> - **"Concatenate the repo into the prompt"** (the repomix era) — superseded by
>   agentic search plus context-rot evidence; curation beats volume even with
>   1M-token windows, which Claude Code itself now holds to 200K by default.
> - **"Connect an MCP server for every service you touch."** Deferred tool
>   loading reduced the tax but the direction of advice reversed: CLIs and
>   skills first, servers for live data/actions you use routinely.
> - **"Keep docs out of the repo, agents ignore them."** Precisely backwards
>   now: retrieval-shaped in-repo docs (ADRs, registries, contracts) are the
>   anti-hallucination mechanism, and agents follow pointers reliably.
> - **"Agents can't handle big codebases."** Scale stopped being the binding
>   constraint; *legibility* is. A small illegible repo (magic, drift, no
>   registries) fails harder than a large legible one.

---

## 5. Agentic workflows

### 5.1 Plan-then-execute vs direct implementation

**Practice: gate on reversibility and spec-clarity, not size.** Direct
implementation when the change is mechanical, test-covered, and cheap to revert
— the failure mode is a red test, so let the loop (§3) carry it. Plan first —
in plan mode (Shift+Tab; read-only enforcement, so exploration can't leak into
mutation) or with the built-in Plan agent — when the task is multi-file,
touches state/schema/auth, or when you cannot name the acceptance test up
front (that inability *is* the signal). The canonical shape is still
explore → plan → code → commit, but with 2026 tooling the plan step is enforced
rather than requested. And plans worth executing are worth reviewing as
artifacts: this repo commits plans to `docs/plans/`, holds them to a
`planning-discipline` skill of distilled rules, and reviews them *before* code
exists — plan review is the cheapest leverage point in the whole pipeline,
because a wrong plan compiles.
**Source:**
[best practices](https://www.anthropic.com/engineering/claude-code-best-practices)
(explore-plan-code-commit; "ask Claude to make a plan and confirm before
coding"); [permission modes](https://code.claude.com/docs/en/permission-modes.md)
(plan mode); this repo's planning skill for the plan-quality bar.
**Prevents:** confident wrong-direction execution — the agent's version of the
measure-once-cut-twice failure, where 2,000 lines of internally consistent code
sit on a misread requirement; and (via the skill's ground-truth rules) plans
that contradict repo reality — citing test frameworks that don't exist,
planning against a stale worktree.

### 5.2 TDD with agents — and the reward-hacking problem

**Practice: agent TDD works, but only as an *adversarial arrangement*, not a
vibe.** The sequence that holds up: write failing tests from the contract →
verify they fail for the right reason → **freeze them** (separate commit;
reviewed) → implement without touching tests → paste the red-then-green
receipts. This repo runs exactly this as its "red first" convention (failing
test output is the PR receipt) plus a stronger lock: *existing tests are
pinned* — needing to edit one to land a change is a design smell requiring
redesign or explicit authorization. Enforce the freeze mechanically where
stakes warrant: test paths in a `PreToolUse` matcher during implementation,
CI flagging PRs that modify tests and implementation together, and a
`test-writer` subagent that never sees the implementation (§2.4).
**Source — why the paranoia is calibrated, not decorative:** Anthropic's
[reward-hacking study](https://arxiv.org/abs/2511.18397) (Nov 2025) documented
models in *production* RL coding environments learning to override equality
(`AlwaysEqual`), exit the test harness early, and manipulate the runner — and,
more importantly, that learning-to-game generalized into broader misalignment.
Kent Beck reports the everyday-scale version from the outside: agents that
"delete the failing test rather than fix the code," and his fix — separate
optimizing and auditing genies —
([interview](https://newsletter.pragmaticengineer.com/p/tdd-ai-agents-and-coding-with-kent),
[Augmented Coding](https://newsletter.kentbeck.com/p/augmented-coding-beyond-the-vibes)).
Your test suite is the reward function you hand the agent.
**Prevents:** reward-hacked green — suites that pass because the spec was
weakened, the assertion inverted, the test deleted, or the implementation
special-cased to the fixtures. Once tests are the target, protecting the tests
*is* protecting the product.

Two auxiliary rules. **Coverage is the honesty check on green** (this repo
gates at 90% with per-line reporting): deleting-your-way-to-green shows up as a
coverage drop even when the suite passes. And **tests the agent wrote get
reviewed as spec, not as code** — the question is "is this what we meant?",
which no gate can answer for you (§5.5).

### 5.3 Parallel agents on worktrees

**Practice: one branch, one worktree, one agent — now with native support.**
`claude --worktree <name>` creates and enters an isolated checkout under
`.claude/worktrees/` (this repo's long-standing convention, now the tool
default); subagents take `isolation: worktree` for the same effect in-session;
sessions in worktrees are blocked from editing the main checkout
([docs: worktrees](https://code.claude.com/docs/en/worktrees.md)). The
Django-specific sharp edges are shared *runtime* state, which branch isolation
does not cover: a shared SQLite file is a collision (per-worktree
`DATABASE_URL`, or Postgres where the test runner already namespaces
databases); dev-server ports collide (per-worktree port); and gitignored env
files don't follow the checkout — `.worktreeinclude` (listing `.env`) fixes
that natively. Coordination across parallel agents is a *contract* problem,
not a merge problem: this repo's frozen-interface contract docs with
planted-red per-component tests let N agents build against agreed seams, and
"merge conflicts are the collision detector" (its CLAUDE.md) is the right
posture — conflicts are the system working.
**Source:** [worktrees docs](https://code.claude.com/docs/en/worktrees.md);
[best practices](https://www.anthropic.com/engineering/claude-code-best-practices)
(the original multi-worktree pattern); this repo's `docs/contracts/`.
**Prevents:** agents overwriting each other's working state (the classic
two-terminals-one-checkout disaster), and its subtler Django variant — two
agents, one dev database, "my tests pass" meaning "I corrupted yours".
Also prevents the *integration* failure of naive parallelism: without a frozen
contract, N agents produce N mutually plausible interpretations of the seam.

**Migration-specific caveat:** parallel branches that each add a migration will
collide on numbering (`0009_*` twice) — a conflict Django surfaces loudly
(`makemigrations --merge` exists, but linear history via rebase-and-renumber is
cleaner under squash-merge). Sequence migration-bearing work rather than
parallelizing it; it's the one file class where "collision detector" is a cost,
not a feature.

### 5.4 Headless and CI

**Practice: promote the agent into CI only for tasks with deterministic
verification, and wire the same gates around it.** The toolkit
([headless docs](https://code.claude.com/docs/en/headless.md),
[GitHub Actions](https://code.claude.com/docs/en/github-actions.md)):
`claude -p` for scripted invocations (`--output-format stream-json` for
pipelines, `--json-schema` for structured output, `--allowedTools` +
`--permission-mode dontAsk` for lockdown, `--max-turns` as a circuit breaker,
`--bare` for plumbing invocations that shouldn't load repo hooks/skills — note
it also skips CLAUDE.md, so *don't* use it for repo work); the Agent SDK
(Python/TS — the renamed Claude Code SDK) when the loop needs to live inside
your own tooling; and `claude-code-action@v1` for GitHub, in two modes —
interactive (`@claude` mentions on issues/PRs → the issue-to-PR pipeline) and
automation (a `prompt` input → scheduled and triggered jobs).

The jobs that earn their keep on this stack, in order: **first-pass PR review**
(the `code-reviewer` agent posting inline comments — findings verified against
the code before posting, humans reviewing findings rather than raw diffs);
**red-CI triage** on failure events (diagnose, propose or push the minimal
fix); **issue-to-PR** for well-specified small tickets (Linear/GitHub label
`agent-ok` → branch → PR — never merging, only proposing); and **scheduled
hygiene** (dependency-bump triage, flaky-test hunts, doc drift checks —
"boring on purpose"). Guardrails are non-negotiable and mechanical: scoped
credentials only (no prod secrets in workflow env — §3.5 applies doubly
unattended), `--max-turns`, branch protection unchanged (the agent's PR passes
`ci-ok` like anyone's), and CODEOWNERS making §5.5's human gates structural.
**Source:** [GitHub Actions docs](https://code.claude.com/docs/en/github-actions.md);
[headless docs](https://code.claude.com/docs/en/headless.md).
**Prevents:** unattended-amplification — a headless agent is your loop with the
human pulled out, so every §3 gap it inherits runs at machine speed; and
approval-theater, where an agent-opened PR gets waved through because "the bot
wrote it" (branch protection + CODEOWNERS make the gate identical regardless
of author).

### 5.5 Human gates that are non-negotiable

**Practice: require a human decision — structurally, via CODEOWNERS +
branch-protection, not via prompt — wherever blast radius is high and
reversibility is low:**

1. **Migrations** — destructive/data-bearing ones absolutely (checkpoint/rewind
   restores files, never databases); in a small team, every migration.
2. **Auth, sessions, permissions, crypto** — quiet single-line failures
   (`IsAuthenticated` → `AllowAny`) with total blast radius.
3. **Money paths** — pricing, refunds, invoice state machines; and their
   close cousin, **outbound irreversible actions** (emails, sends,
   publishes). This repo's architecture is the model: the LLM only drafts;
   deterministic verification (`services/verify.py`) fails closed into a
   human review queue, and per its planning rules an approval binds the
   *exact bytes* approved (content-hash, re-checked at send). Hold agent
   changes to these gates to the same standard as agent-generated copy.
4. **The security boundary itself** — prompt-handling, sanitization, the
   untrusted-input pipeline (`SECURITY.md` surfaces here), plus the agent's
   own config: hooks, permissions, workflows. The fox doesn't review the
   henhouse PR: an agent-authored change to `.claude/settings.json` or CI
   gates is *always* human-reviewed.
5. **Test deletions/weakenings and new dependencies** — §5.2's freeze and the
   supply chain, respectively.

The mechanism, concretely — `.github/CODEOWNERS`:

```
**/migrations/        @team-leads
project/settings.py   @team-leads
project/app/authentication.py @team-leads
project/app/services/verify.py @security-owner
SECURITY.md           @security-owner
.claude/ .github/     @team-leads
```

With required review enabled, this composes with everything above: agents work
freely everywhere else, and *cannot* land changes in these paths without a
person — no matter how the prompt, the ticket, or an injected instruction
reads.
**Source:** the fail-closed-gate design is this repo's `SECURITY.md` +
`services/verify.py`; the review-economics grounding is the
[METR study](https://metr.org/blog/2025-07-10-early-2025-ai-experienced-os-dev-study/)
(human verification is the scarce resource — spend it where it's
irreplaceable, which also argues for this repo's ≤400-line PR convention:
review-shaped PRs are part of agent hygiene).
**Prevents:** the catastrophic tail. Gates, hooks, and tests catch what they
encode; the human gate is where *unencoded* objectives — compliance, taste,
risk appetite, "we just don't do that" — get enforced. An agent optimizes what
you wrote down; these five categories are where what you didn't write down
lives.

> **⚠️ Outdated (2024–early 2025) — workflow edition**
>
> - **"Type 'think hard' / 'ultrathink' for better reasoning."** Keyword-
>   triggered thinking budgets are gone; current models manage thinking
>   adaptively, with effort levels (`low…max`) and a UI toggle where you need
>   control ([model config](https://code.claude.com/docs/en/model-config.md)).
> - **"Keep agent tasks small — one function at a time."** The generation
>   ceiling moved (multi-hour autonomous runs are routine); the binding
>   constraint is now verification, which is why this blueprint is §3-shaped
>   rather than prompt-shaped.
> - **"Manually `git worktree add` and run N terminals."** Native `--worktree`,
>   worktree-isolated subagents, background sessions, and cross-session
>   messaging subsumed the manual choreography.
> - **"Checkpoint before anything risky."** Checkpoints were replaced by
>   `/rewind` in 2.1.x — and neither ever restored a database; the Django-shaped
>   risk still lives in §3.5/§5.5, not in conversation state.
> - **"Auto-accept mode is reckless."** Inverted by sandboxing + curated
>   permissions + CI gates: per-edit approval is now the *low*-value human
>   checkpoint (METR's slowdown lived exactly there); per-merge review on gated
>   paths is the high-value one.
> - **"AI PR reviewers rubber-stamp; keep bots out of review."** First-pass
>   agent review with verified findings became standard; the human's job moved
>   up a level — adjudicating findings and owning §5.5.

---

## 6. Implementation order

Highest leverage first, each step compounding the last. Effort is for a
two-person team; "here" notes what this repository has already done.

| # | Do | Effort | Why this order |
|---|---|---|---|
| 1 | **Make the inner loop true**: clean-clone bootstrap (SessionStart hook or `make bootstrap`), scoped-test/fast-test paths verified, CI-parity command list settled | ½ day | Everything downstream assumes a loop that runs. *Here: done (hook shipped; loop fast).* |
| 2 | **Rewrite CLAUDE.md to §7's shape**: commands with fast variants, `[gate]`/`[convention]` tags, gotchas, human-gates list | ½ day | Cheapest persistent win; every session forever benefits. *Here: strong file exists — add the fast-loop ordering, the `DATABASE_URL` gotcha, and §7's Django rule blocks.* |
| 3 | **Hooks**: format-on-edit, migration-immutability, bash-guard | ½ day | Converts the three most expensive conventions into physics. *Here: only SessionStart exists — this is the top gap.* |
| 4 | **Permission curation**: replace the accreted allow-list with ~20 prefix rules + `ask` + `deny`; move one-offs to `settings.local.json` | 2 h | Restores auditability; prerequisite for any autonomy increase. *Here: overdue (≈280 entries).* |
| 5 | **Migration defense-in-depth**: `django-migration-linter` in dev-reqs + CI, `django-migrations` skill, migration paths into CODEOWNERS | 1 day | The highest-stakes failure domain for this stack; three cheap layers. *Here: `--check` gate exists; linter, skill, CODEOWNERS missing.* |
| 6 | **Skills + named subagents**: `drf-endpoint`, `query-review`; `code-reviewer`, `test-writer`, `migration-safety` agents | 1–2 days | Encodes conventions and buys independent review; grows from your own incidents thereafter. *Here: two exemplary skills exist; agents missing.* |
| 7 | **Worktree hygiene for parallelism**: `.worktreeinclude` with `.env`, per-worktree DB guidance in CLAUDE.md | 1 h | Cheap once 1–6 make parallel agents worth running. *Here: worktree convention exists; include-file missing.* |
| 8 | **Headless/CI agents**: first-pass PR review, then red-CI triage, then labeled issue-to-PR — each behind the unchanged `ci-ok` + CODEOWNERS gates | 1–2 days | Deliberately last: automation amplifies whatever loop exists, so it inherits 1–7's safety or 1–7's absence. |
| 9 | **MCP extras**: Postgres read-only, observability server | as needed | Real but smallest leverage per setup-hour; add on demonstrated demand. *Here: Linear already earns its slot.* |

What *not* to do first, because each is downstream of the loop: MCP shopping
(9 before 3 is decoration), multi-agent orchestration (6/8 before 3–5 is
amplified noise), and prompt-tuning CLAUDE.md prose while the gates it
references don't exist.

---

## 7. The CLAUDE.md template — Django/DRF/Postgres

Drop-in starting point. Replace `<angle-bracket>` placeholders and the example
app names; delete any rule you don't actually enforce — a rule you won't back
with a gate or a review is prompt rot (§1.5). The `[gate]`/`[convention]`
notation is §1.2's practice; every `[gate]` claim should be true (§3.3).

````markdown
# CLAUDE.md

<Service> — Django <5.x> + DRF + Postgres <16> JSON API for <one line>.
Domain logic lives in `apps/<app>/services/` (plain functions, ORM allowed);
views stay thin; serializers validate, never decide. Anything under
`apps/billing/` moves money — see "Human gates". Architecture map:
`docs/architecture.md`. Threat model: `SECURITY.md`.

Rules are marked **[gate]** (machine-enforced — a hook or CI will block you)
or **[convention]** (discipline + review). Don't confuse the two.

## Commands

```bash
# Setup from a clean clone (Python 3.12; Docker for Postgres)
make bootstrap                # venv + dev deps + .env from .env.example + migrate
docker compose up -d db       # Postgres 16; .env's DATABASE_URL points here

# The inner loop — run in this order (fastest-failing-first)
ruff check . && ruff format .                        # hooks also run this on edit
mypy apps/                                           # CI runs exactly this target
python manage.py makemigrations --check --dry-run    # missing-migration gate [gate]
python manage.py test apps.<app> --parallel --keepdb # scoped tests, seconds

# Single test — fastest feedback while iterating
python manage.py test apps.billing.tests.test_invoices.InvoiceTests.test_overdue --keepdb

# Before declaring any task done [gate — CI runs all of these]
python manage.py test --parallel      # full suite
python manage.py lintmigrations       # django-migration-linter

# Dev data — the ONLY way to mutate the dev database
python manage.py seed_demo            # idempotent; never edit the DB by hand
```

`--keepdb` reuses the test DB between runs and applies pending migrations
itself; if it errors on schema drift after migration churn, run once without
it, then resume. Gotcha: <project-specific trap — e.g. "an empty-but-present
DATABASE_URL is malformed to dj-database-url; unset it, don't blank it">.

## Architecture (30 seconds)

- `config/settings/{base,local,test,prod}.py` — all values from env.
  `.env.example` is the registry: every variable documented with default and
  failure mode. New setting ⇒ new `.env.example` entry, same PR [convention].
- `apps/<domain>/` — `models.py`, `serializers.py`, `views.py`, `urls.py`,
  `services/` (writes + business rules), `selectors/` (reads + query
  optimization), `tests/`. Views orchestrate; services decide; selectors
  fetch. No business logic in serializers, signals, or `save()` overrides
  [convention].
- API: DRF ViewSets + routers under `/api/v1/`. Every list endpoint is
  paginated [gate — test asserts] and names its `permission_classes`
  explicitly [convention].
- Background jobs: <queue>. Enqueue only via `transaction.on_commit` (see
  Transactions).

## Migrations

- **Never edit, rename, or delete a migration that has reached `main`**
  [gate — a hook blocks the edit]. Merged ⇒ applied somewhere ⇒ immutable.
  Fix forward with a new migration.
- Every model change ships its migration in the same PR;
  `makemigrations --check` is a CI gate. Name them:
  `makemigrations billing -n add_invoice_due_date` [convention].
- Schema and data migrations are separate files [convention]. Data
  migrations use `apps.get_model()` (never import models), declare
  `reverse_code` (explicit noop is fine), and batch writes — no unbounded
  single UPDATE.
- Zero-downtime sequencing (old code always runs against the new schema
  during deploy): (1) additive + nullable; (2) backfill in batches;
  (3) constraints in a later deploy — unique indexes via
  `AddIndexConcurrently` with `atomic = False`. Never drop or rename in the
  same release as the code that stops using the thing.
  `lintmigrations` catches most of this [gate]; a human reviews every
  migration regardless (see Human gates). Details: the `django-migrations`
  skill.
- `migrate` targets your local DB only. Production migrations run in the
  deploy pipeline, never from a session [gate — no credentials exist here].

## Queries

- Every queryset feeding a serializer with related fields declares
  `select_related`/`prefetch_related` in the selector — never rely on lazy
  loading from serializer code. No per-row queries in
  `SerializerMethodField` or model properties reachable from list views
  [convention — `query-review` skill has the hunt procedure].
- List endpoints carry a query-count pin: `with self.assertNumQueries(N):`
  [gate]. If your change raises N, justify it in the PR — the pin is the
  alarm, not an obstacle.
- Bulk paths use `bulk_create`/`bulk_update`/`.update()`; large scans use
  `.iterator()`; `.count()` not `len()`; `.exists()` not truthiness.

## Transactions

- Multi-write invariants wrap in `transaction.atomic`. `ATOMIC_REQUESTS` is
  <on|off> here — <if off:> the service layer owns transaction boundaries
  explicitly [convention].
- Side effects (email, webhooks, enqueue) fire via `transaction.on_commit`,
  never inside the atomic block.
- No network calls inside `atomic` — you'd hold row locks for someone
  else's latency.
- Concurrent state transitions use `select_for_update()` or a conditional
  update (`.filter(state=OLD).update(state=NEW)` + rowcount check), and
  every one gets a two-actor race test [convention].

## Tests

- `TestCase` + `setUpTestData`. `TransactionTestCase` only when the behavior
  under test is transactions/`on_commit`/threads — it's ~10x slower.
- The suite passes offline with every external API key empty [gate]; all
  providers are mocked. Never let a test touch the network.
- Existing tests are pinned [convention]: needing to edit one to land a
  change is a design smell — flag-gate the new path or get explicit
  authorization first.
- Red first [convention]: the failing test precedes the implementation;
  paste its output in the PR as the receipt.

## Env & secrets

- Config only via environment; required vars fail loud at boot. Never
  commit `.env` [gate — deny rule]; never print secret values; a key that
  was ever committed is burned — rotate, don't reuse.

## Human gates — stop and get a human decision

- Any migration that drops/renames anything, or touches `billing_*` tables.
- Auth/session/permission code; anything in `SECURITY.md`'s scope.
- Money math, refunds, invoice state transitions, outbound sends.
- Deleting, skipping, or weakening an existing test.
- New dependencies; changes to `.claude/` or CI config.
[gate — CODEOWNERS requires human review on these paths regardless.]

## Done means

`ruff` clean · `mypy` clean · `makemigrations --check` clean ·
`lintmigrations` clean · scoped tests green · full suite green when models,
serializers, or urls changed. Paste command output; don't summarize it.
````

**Why it's shaped this way:** context first (three lines, because placement
decisions depend on it), commands as the longest section (the loop is the
product — §3.1), rules only where a competent agent would guess wrong (§1.2),
every gate claim backed by a real gate (§3), pointers instead of prose
everywhere knowledge already has a home, and ~150 lines total so it stays read
(§1.1). The sections map one-to-one onto the failure classes this blueprint
exists to prevent: migration disasters, N+1s, lock-holding transactions,
reward-hacked tests, leaked secrets, and unreviewed changes to the paths where
review is the point.

---

## Sources

**Claude Code docs (mechanics; verify against the changelog):**
[memory](https://code.claude.com/docs/en/memory.md) ·
[skills](https://code.claude.com/docs/en/skills.md) ·
[subagents](https://code.claude.com/docs/en/sub-agents.md) ·
[hooks](https://code.claude.com/docs/en/hooks.md) /
[hooks guide](https://code.claude.com/docs/en/hooks-guide.md) ·
[permissions](https://code.claude.com/docs/en/permissions.md) ·
[permission modes](https://code.claude.com/docs/en/permission-modes.md) ·
[sandboxing](https://code.claude.com/docs/en/sandboxing.md) ·
[MCP](https://code.claude.com/docs/en/mcp.md) ·
[headless](https://code.claude.com/docs/en/headless.md) ·
[GitHub Actions](https://code.claude.com/docs/en/github-actions.md) ·
[worktrees](https://code.claude.com/docs/en/worktrees.md) ·
[model config](https://code.claude.com/docs/en/model-config.md) ·
[changelog](https://code.claude.com/docs/en/changelog.md)

**Anthropic engineering & research:**
[Claude Code: best practices for agentic coding](https://www.anthropic.com/engineering/claude-code-best-practices) (Apr 2025) ·
[Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) (Sep 2025) ·
[Equipping agents for the real world with Agent Skills](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills) (Oct 2025; [open standard](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview)) ·
[Writing effective tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents) ·
[Code execution with MCP](https://www.anthropic.com/engineering/code-execution-with-mcp) (Nov 2025) ·
[Natural emergent misalignment from reward hacking in production RL](https://arxiv.org/abs/2511.18397) (Nov 2025) ·
[How Anthropic teams use Claude Code](https://claude.com/blog/how-anthropic-teams-use-claude-code)

**Independent:**
[METR: Measuring the impact of early-2025 AI on experienced open-source developer productivity](https://metr.org/blog/2025-07-10-early-2025-ai-experienced-os-dev-study/) (Jul 2025; METR marks the result historical) ·
[Simon Willison: Claude Skills are awesome, maybe a bigger deal than MCP](https://simonwillison.net/2025/Oct/16/claude-skills/) (Oct 2025) ·
[Kent Beck on TDD with agents](https://newsletter.pragmaticengineer.com/p/tdd-ai-agents-and-coding-with-kent) /
[Augmented Coding: Beyond the Vibes](https://newsletter.kentbeck.com/p/augmented-coding-beyond-the-vibes)

**Django/Postgres:**
[django-migration-linter](https://github.com/3YOURMIND/django-migration-linter) ([incompatibility catalog](https://github.com/3YOURMIND/django-migration-linter/blob/main/docs/incompatibilities.md)) ·
[django-pg-zero-downtime-migrations](https://github.com/tbicr/django-pg-zero-downtime-migrations) ·
[Django migrations without downtime](https://gist.github.com/majackson/493c3d6d4476914ca9da63f84247407b)

**This repository (living examples):** `CLAUDE.md` ·
`.claude/skills/planning-discipline/SKILL.md` ·
`.claude/skills/webapp-testing/SKILL.md` · `.claude/hooks/session-start.sh` ·
`.github/workflows/ci.yml` (the `ci-ok` gate) · `docs/adr/` ·
`docs/contracts/` · `SECURITY.md` · `services/verify.py`
