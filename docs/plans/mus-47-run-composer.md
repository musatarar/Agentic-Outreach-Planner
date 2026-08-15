# MUS-47 — Run Composer: implementation plan

Feature branch: `feat/run-composer`. Interfaces are frozen in
`docs/contracts/run-composer.md`; this file is the delivery order, the ground truth it
was checked against, and the risks that survived.

Held to `.claude/skills/planning-discipline`. The three rules that actually bit are
called out inline: **the pinned-test rule** (component 3), **the fail-closed-gate rule**
(component 9), and **the untrusted-channel rule** (component 7).

## Ground truth, verified against `origin/master` @ `280ffe7`

| Claim | Verified |
| --- | --- |
| Backend suite | `python manage.py test project.app`, 21 `project/app/tests*.py` modules |
| Frontend suite | `node --test "tests/**/*.test.ts"` — 3 files, **no jsdom, no Testing Library** |
| Structured rule trace | already ships — `outreach.explain()`, schema v1, stored on `OutreachAction.rule_trace` |
| Rule authority | `determine_priority` / `determine_action` are pure and Django-free |
| Actor identity | Django `User` via magic link; audit columns elsewhere are **email strings** |
| Tool-calling | `agenerate_chat(messages, tools=...)` exists in every adapter (MUS-29); **no `tool_choice`** |
| Prices | `LLMModel.input_price_per_mtok_usd` / `output_price_per_mtok_usd`, `Decimal` |
| Usage actuals | `LLMResult.input_tokens` / `output_tokens` (MUS-45) |
| Dedupe identity | `dedupe.dedupe_key(lead_id, action_type)`, sha256, `v1` |
| Untrusted pipeline | `sanitize_untrusted` → `wrap_untrusted` → `UNTRUSTED_*` markers |
| Query pins | `tests_planner_perf.planner_queries(n)` — must come out **identical** |

Two ticket corrections that follow from the above, both applied in the contract:

1. MUS-48's "refactor the rule functions to emit a structured trace" is **already done**
   (MUS-42). `RunLead.rule_trace` is `default=dict` holding `explain()`'s envelope, not
   `default=list`.
2. MUS-48's `created_by = ForeignKey(User, PROTECT)` becomes `EmailField`, matching
   `OutreachEdit.editor`, `DismissedOutreachKey.dismissed_by` and `ReviewDecision.reviewer`
   — best-effort attribution, explicitly never authorization.

## Delivery order

Each row is one PR, red-first (the first commit is the failing test), ≤400 changed lines,
merged into `feat/run-composer`. `master` is only touched by MUS-66.

| # | Branch `feat/run-composer--…` | Test artifact | Depends on |
| --- | --- | --- | --- |
| 0 | ~~MUS-66~~ — **landed on `master` as `b80f3a5` (#96)** | `tests_llm_tool_call_parsing.py` | — |
| S | *(the branch base — `f7af1a8`, not a PR)* | all artifacts, planted red | — |
| 1 | `models` | `tests_compose_models.py` | S |
| 2 | `scope` | `tests_compose_scope.py` | 1 |
| 3 | `phases` | `tests_compose_phases.py` | S |
| 4 | `lifecycle` | `tests_compose_lifecycle.py` | 2, 3 |
| 5 | `estimate` | `tests_compose_estimate.py` | 1 |
| 6 | `structured` | `tests_compose_structured.py` | **0** |
| 7 | `read` | `tests_compose_read.py` | 4, 6 |
| 8 | `decisions` | `tests_compose_decisions.py` | 7 |
| 9 | `generate` | `tests_compose_generate.py` | 4, 5 |
| 10 | `fe_scope` | `compose_scope.test.ts` + `compose_stages.test.ts` | 4 |
| 11 | `fe_read` | `compose_read.test.ts` | 8, 10 |
| 12 | `fe_generate` | `compose_generate.test.ts` | 9, 10 |
| 13 | `docs` | — | all |

`compose_stages.test.ts` rides with `fe_scope` because `stages.ts` is the shell's gate: it
lands with the route, and every later stage reads it.

## Running the suite locally

The developer `.env` in the main checkout carries live provider keys, a non-default
`OUTREACH_MAX_IN_FLIGHT`, and `OUTREACH_AGENT_ENABLED=True`. Copied into a worktree, that
combination makes seven test modules — `tests_planner_perf` among them — sit at 0% CPU
making real provider calls through real backoff, and the suite never terminates. Four other
tests fail because they assert the *defaults* apply.

`tests_planner_perf` is the module carrying the `planner_queries(n)` pins that component 3
is forbidden to move, so "cannot run it" would mean "cannot verify the one thing that PR
promises". This worktree's `.env` is therefore a copy of `.env.example` with only
`DJANGO_SECRET_KEY` filled in. Against that: **1062 tests, OK, 168 expected failures, 5.5s.**
Anyone picking this branch up should do the same rather than copying their working `.env`.

## The three rules that shaped this

### Component 3 — pinned tests

`plan_outreach()` carries query-count pins (`tests_planner_perf`), agent-loop resume
tests, and mock seams the API tests patch. The extraction is a **pure refactor**: three
functions become public, `plan_outreach` calls them, and the acceptance criterion is that
every existing test passes with **zero edits** and `planner_queries(n)` yields the same
numbers. Editing an existing test here is a design smell — if it becomes necessary, stop
and get authorization rather than writing it into the diff.

This is also why the composer needs no feature flag: it is additive. The one shared-path
change is this extraction, and its whole job is being invisible.

### Component 7 — a new untrusted-text channel

The advisory read feeds `hubspot_notes` and a rendered event timeline to a model. That is
a new attacker-reachable channel and inherits the **complete** existing treatment:
sanitization, length caps, **and** the delimiter fencing plus standing-instruction
wrapper, applied exactly once at the point the text enters the conversation. A red-team
test asserts a planted payload surfaces in the final message list redacted *and* inside
the `UNTRUSTED_*` markers — a sanitization test alone does not prove fencing.

The evidence validator checks quotes against the **sanitized rendering the model saw**,
not raw notes. Validating against raw text would reject legitimate quotes wherever
sanitization rewrote a character, which would look like the model hallucinating.

`emit_suggestion` carries no `lead_id`. The acting lead is bound server-side; a foreign
identifier in the tool arguments is dropped, and a test proves it cannot widen a read.

### Component 9 — the fail-closed gate

`services/verify.py` blocks approval while any claim is unverified. The read exists to
surface facts the gate's corpus lacks, so the discipline demands an explicit position.
**Position (b): the gate is not extended in this feature**, and the mitigation is
structural rather than hopeful —

- `suggestion["rationale"]` never enters a generation prompt (asserted);
- `effective_priority` is not a prompt input and is not made one;
- an accepted `action_change` swaps only `action_type`, already covered by
  `_check_offer`, with the reason rebuilt from a first-party template.

So no model-derived text reaches the generator, and the fail-closed rate should not move.
If it does, that is the signal — measure it before widening anything.

## Risks

- ~~**MUS-66 gates component 6.**~~ **Resolved** — landed on `master` as `b80f3a5` (#96),
  bringing `tests_llm_tool_call_parsing.py` with it. Component 6 is unblocked, and this
  branch is rebased onto that commit so `tool_choice` is built on the fixed parsing rather
  than beside it. The four defects it closed (`finish_reason` ignored, blank arguments
  raising, entries dropped silently, Claude's fold splitting tool results) all sat directly
  under the forced-tool mechanism, and `emit_suggestion` would have hit at least two.
- **`views_compose.py` is touched by five components.** They land sequentially, so this is
  sequencing, not conflict — but a parallel run would collide, and must not be attempted.
- **Frontend rendering is not unit-testable here.** Stage behavior is covered through pure
  modules (`scopeChips`, `estimateLine`, `selectionReducer`, `stages`); the components
  themselves are review- and `webapp-testing`-verified. Stated so nobody reads the FE test
  count as coverage.
- **Re-classify replaces `RunLead` rows.** A run whose scope changed after a read loses
  its suggestions. That is correct (the suggestions described a different lead set) but it
  is a surprise worth a UI warning, which component 10 owns.
- **One active run is global, not per-user.** Correct for a single-operator tool; it will
  need revisiting the moment a second operator exists.
