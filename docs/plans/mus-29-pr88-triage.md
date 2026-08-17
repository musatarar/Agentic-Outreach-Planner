# PR 88 review — verified triage

Adversarial verification pass over the 49 raw candidates in
`mus-29-pr88-review-findings.md`, run against a read-only checkout of PR 88's head
(554dccf, diff base 34d3183). Each candidate was handed to a verifier told to **refute
first** and to cite the guard, constraint, transaction boundary, or caller contract that
makes the claimed failure impossible; a CONFIRMED verdict required a concrete reachable
trigger under the code as written.

Severity rubric used: **merge-blocker** = correctness, data-integrity, double-billing or
security defect on a reachable path, or a violation of MUS-29 acceptance (inspectable
per-lead trace; kill-safe resume without lost or duplicated work; nothing sends without a
recorded human approval). **followup** = real but tolerable to land. **trivial** =
cosmetic.

Result: **3 merge-blockers, 4 refuted, the rest follow-ups.** Nothing found requires
editing a pinned test.

## Merge-blockers (fix before landing PR 88)

### MB1 — `dispatch()` checks the approval outside the transaction that consumes it

`project/app/services/dispatch.py:30-61`. The live-decision lookup and the sha256
comparison run **before** `transaction.atomic()`; the CAS inside it conditions only on
`status=approved`, and the `OutboundSend` row is then written against the pre-fetched
decision and pre-computed digest. Nothing re-reads either inside the transaction, and
there is no `select_for_update` in the file.

The enabling interleave is one the PR explicitly supports: undo voids the live
`approve_send` decision and returns the action to pending (`views_queue.py:600-614`), and
re-approve mints a fresh live row — `QueueApproveView`'s own comment describes exactly
this cycle. So a dispatcher that passed its checks against decision D1 can win the CAS
after a reviewer has voided D1, edited the copy, and re-approved as D2, recording an
`OutboundSend` bound to a voided decision carrying a stale digest. `OutboundSend.decision`
is a plain PROTECT FK with no liveness constraint.

That is the gate's own stated contract — liveness and hash equality *at send time* —
broken in the mechanism this PR ships as its deliverable, and it is the MUS-29 acceptance
clause "nothing sends without a recorded human approval".

Mitigating context: `dispatch()` has **no production caller** in this PR (only tests
import it; no URL routes a send endpoint), and the window is a few statements wide.

Fix: inside the atomic block, after the CAS wins, re-select the live resolved
`approve_send` decision `select_for_update` and recompute the hash from a fresh
`effective_copy` read; raise `DispatchBlocked` on mismatch so the rollback undoes the CAS.
Blast radius is `dispatch.py` only; the four pinned approval-gate tests are single-threaded
and pass unchanged.

### MB2 — a run checkpointed `failed`/`exhausted` before a crash can never be resumed

`project/app/services/agent/state.py:254-263`, `loop.py:138-143`, `outreach.py:2475`.
`failed` and `exhausted` are outside `NON_TERMINAL_STATUSES`, so `_claim_sync`'s UPDATE
matches zero rows and `claim()` returns False. `run_agent_lead` maps that to
`AgentClaimLost`, and phase 5 drops `AgentClaimLost` leads from the rows to write. The
`KIND_FINAL` replay short-circuit doesn't apply because a failed run has no final step.
Nothing anywhere resets a terminal `AgentLeadRun` — `create_lead_runs` is `get_or_create`
only.

So: lead A hits an `LLMError`, its run is durably checkpointed `failed`, the process dies
before phase 5 writes rows. Every subsequent resume claims nothing for A, drops it
silently, and returns 200 with N−1 rows. In the non-crash flow that same lead would have
produced a `needs_human` row. The contract doc defines `AgentClaimLost` as "another worker
won … whose own finalize writes this lead's row" — which is false here; no worker ever
writes it.

The crash window is reachable without a kill signal: phase 3 deliberately re-raises a
sibling lead's raw exception after the gather, skipping phase 5 — the pinned assembly test
asserts exactly that zero-rows state.

This is the "kill-safe resume without losing work" acceptance criterion. Scope caveat: a
later *fresh* run (new `trace_run_id`) does recover the lead, so the loss is confined to
the resume mechanism — which is the mechanism under acceptance.

Fix: in phase 2b (ORM-land, before `load_prior_steps`), reset to `STATUS_PENDING` any
`AgentLeadRun` for this `trace_run_id` that is `failed`/`exhausted` and has no
`OutreachAction` row — a conditional UPDATE, ideally via a new `state.py` helper so phase 3
stays ORM-free. Failed runs then genuinely retry; exhausted runs re-claim, find the budget
already spent, and fall to the exhausted append with zero provider calls. **Do not** change
`_claim_sync` semantics — two pinned loop tests hold terminal runs to refusing claims. No
pinned test covers resume-after-failed-checkpoint, so this lands without test edits.

### MB3 — resuming with the agent flag off silently re-bills every lead single-shot

`project/app/views.py:47-49` gates resume purely on `AgentLeadRun` rows existing, never on
the flag. Those rows survive a crash (each committed in its own short transaction under
autocommit). In `plan_outreach`, `agent_plans` is set to `None` before the
`if runtime.agent_enabled:` block, so all resume machinery — run lookup, `load_prior_steps`,
claims — sits inside the flag, while the telemetry layer reuses the supplied run id and
stamps it on every new row. Phase 3 then falls through to the single-shot `agenerate_copy`
call for **every** lead, including ones whose `AgentLeadRun.status='done'` a flag-on resume
replays with zero provider calls. The already-finalized filter is skipped too, and the trace
endpoint joins on `(trace_run_id, lead_id)` — so the crashed agent attempt's step log is
served as "How this draft was reached" for a draft actually written single-shot.

Trigger: crash with the flag on, then an operator flips the flag off and restarts (a natural
incident response) or a heterogeneous worker fleet disagrees. The resume returns 200 while
re-billing every non-finalized lead and mislabeling traces — violating both "kill-safe resume
without duplicated work" and "inspectable per-lead trace".

The verifier **narrowed** the original claim: the `IntegrityError` leg is not the common
case, because finalized rows are `pending` with non-empty dedupe keys and the open-item rule
skips those leads. It needs an extra condition (a finalized row whose status left
`{pending,snoozed}`, e.g. queue-approved after a partial finalize, or a concurrent flag-on
resume). Both reachable, neither typical.

Fix: reject `resume_run_id` when `not runtime.agent_enabled` with a 4xx. Best done inside
`plan_outreach` together with FU-A below, so validation lives with the mechanism it protects.
No pinned test covers flag-off + resume.

## Refuted (no action)

- **ALT6 — Checkpoint's `connections._connections` borrow is fragile.** Accurate as
  description, unreachable as failure. Django is pinned at 4.2.30 where the private
  attribute exists; asgiref's `single_thread_executor` is a 1-worker pool with no outer
  `async_to_sync`/`ThreadSensitiveContext`, so every thread-sensitive callable in the
  process serializes on that one thread and no other code can observe the borrowed slot —
  even across two concurrent planner runs. `state.py:227` is the only `sync_to_async` call
  site in production code. File the hardening as a ticket if wanted; not a blocker.
- **WC5 — Claude adapter rewrites unknown roles to `user`.** The asymmetry is real in code
  but unreachable: `Message.role` is contractually one of user/assistant/tool_result, the
  only production caller is `fold_messages`, which constructs exactly those three, and no
  `role="system"` Message is constructed anywhere in the repo.
- **ALT3 — phase-3 crash semantics fork on the flag.** Not a latent divergence but the
  test-pinned implementation of kill-safe resume. Flag-off exception absorption is
  defensive-only (the single-shot body already catches `Exception` itself); on the agent
  path every recoverable failure arrives as a value, so an escaping exception is by
  construction the crash case, and absorbing it would write rows for an unfinished run. The
  assembly test literally simulates the kill as a raw `RuntimeError` and asserts zero rows.
  "Fixing" it would fail a pinned test.
- **ALT4 — flag-gated idempotent finalize.** The structural description is right, but the
  claimed silent duplication is prevented by `oa_one_row_per_lead_per_run` (flag-independent,
  covers single-shot rows, raises loudly) and by resume being unreachable without
  `AgentLeadRun` rows, which only the flag-on path creates. The unconditional pre-read was
  tried and reverted because it failed three pinned planner query-count tests.

## Confirmed follow-ups — correctness

Grouped as they should become tickets. All fail closed or are contained; none breaks an
acceptance criterion.

**FU-A — resume validation belongs inside `plan_outreach`** (ALT7). The unknown-id guard
lives only in the view; `plan_outreach` adopts any string as a run id and, flag-on, mints
fresh `AgentLeadRun` rows under it — a fully-billed run under a typo'd id, no error. The
view's own docstring names the hazard. Natural home for the MB3 fix too; ship as one ticket.

**FU-B — queue-view concurrency and audit** (LP3, LB2, LB6, ALT8, plus raw B2/B3).
`get_action` is an unlocked read, `can_transition_to` checks a stale in-memory status, and
the writes are plain `save()`s rather than CAS. Consequences: concurrent double-approve
gives an unhandled 500 where the contract owes a 409 (`IntegrityError` is caught in the
review-decision endpoint but not here); a concurrent undo/dispatch race can resurrect a sent
action to pending and void a consumed approval; dismiss records a `reject_send` decision with
a blank reviewer while approve 401s on the same session. Also worth folding in: the
transition↔decision invariant is inline discipline across three view bodies with no service
seam, and `OutreachActionAdmin` exposes an editable `status` that bypasses all of it. Fix as
one ticket: CAS the status flip in approve/dismiss/undo, 409 on zero rows, hoist the reviewer
guard, extract `record_approval`/`record_rejection`/`void_send_decisions` service functions,
and add `readonly_fields` on the admin.

**FU-C — adapter tool-call parsing** (WC1, WC2, WC4, WC7). An empty-string `arguments` (what
several OpenAI-compatible servers emit for zero-argument tools — and all four agent tools are
zero-argument) raises `JSONDecodeError` → non-retryable `LLMMalformedResponseError` → run
failed, lead needs_human. Entries missing id/name are silently dropped, and since the loop
never reads `finish_reason`, a tool-call turn can degrade into a "final" made of the model's
narration. Claude's fold emits one user message per tool_result rather than one message with
all blocks, which Anthropic's docs warn degrades parallel tool use. All three are structurally
untested: every `ToolCallRequest` any test constructs is a single call with `arguments={}`.
Fix: treat blank arguments as `{}`, raise (or honor `finish_reason`) on dropped entries, merge
consecutive tool_results, and add the missing adapter tests.

**FU-D — loop budget and cancellation** (BUDGET, LP6). The tool-call budget is only consulted
when computing `force_final` before the next provider call, so every call in the current
response executes unconditionally and `tool_calls_used` can exceed the cap by a whole
response's fan-out. Note for whoever fixes it: a naive per-call `break` is **unsound** —
`fold_messages` folds the `llm_call`'s tool_calls verbatim, so an unexecuted call yields an
assistant `tool_use` with no `tool_result`, which the Anthropic API rejects; synthesize a stub
result or strip over-budget calls from the persisted payload instead. Separately, an
`asyncio.timeout` firing while the DONE append is in flight lets the append commit (the
executor thread can't be cancelled mid-work) and then the TimeoutError handler writes
`failed` over it, because the ownership UPDATE has no terminality guard — a run with a valid
persisted draft reported as provider trouble. One-line fix: add
`status__in=NON_TERMINAL_STATUSES` to the append's filter; the handler already swallows
`AgentClaimLost`.

**FU-E — verifier grounding vs. tool facts** (ALT1). `verify.py` is untouched by this PR and
still grounds amounts against this lead's own book size and event premiums with 10% tolerance,
while `get_similar_won_deals` serves *other* agencies' premiums and the agent addendum tells
the model to "reference it as facts when useful". A draft citing a gathered premium trips
`_check_amounts` and fails closed to needs_human. This is the planning-discipline rule about a
new fact source upstream of a fail-closed gate: the honest position now is (b) — scope it out
as a named non-goal with this follow-up specified, and make the measured needs-human rate an
input to the flag-flip decision. Fix when taken: thread the frozen ToolContext-derived facts
(similar-deal premiums, slot dates) into `verify_copy` as optional grounding, reading the
persisted sanitized snapshot, never the model's restatement.

**FU-F — exhaustion is signalled through the injection gate** (ALT2). An exhausted run returns
an empty draft with no error, so phase 4's shape gate rejects it and the reviewer sees
"possible prompt-injection / off-task generation" when the real event was "ran out of steps".
Deliberate and documented, and the true cause is one click away in the trace, but it is
mislabeled-failure noise. Fix: an `AgentBudgetExhausted` sentinel in `AgentOutcome.error` and a
dedicated `_describe_failure` branch naming the budget knobs.

**FU-G — demo AE slots are permanently in the past** (LB5). Slots are seeded to the fixed
anchor week 2026-06-22..26 while `build_tool_context` filters on wall-clock today, so as of
today `check_ae_calendar` always returns an empty list, defeating the tool the seed exists to
back. Output is truthful, so no correctness breach — but the demo is dead. Fix: seed relative
to `date.today()` (anchor = next Monday). No pinned test constrains this.

**FU-H — sticky trace error** (LB7). `TraceSection.load()` only fires from `phase === 'idle'`,
so one failed fetch makes the error permanent until remount; collapse/re-expand never retries.
One-line guard change; the pinned frontend tests only exercise the pure helpers.

**FU-I — force-final trace blemish** (LB8, trivial). A forced-final response's invented tool
calls are discarded for execution but persisted in the `llm_call` payload, so the log shows a
request with no matching result. Cosmetic on a rare path; the dangerous re-fold consequence is
unreachable today. Valuable mainly as a tripwire if the force-final path is ever relaxed.

## Confirmed follow-ups — quality

All 17 quality candidates were factually confirmed; the verifiers split them on worth.

**Worth doing** (one cleanup ticket, or fold into the tickets above): drop the denormalized
`steps_used`/`tool_calls_used` counters the loop already recomputes from the log (S1 — the
migration is new in this PR, so it can be amended rather than superseded); delete the
never-assigned `STATUS_GATHERING` (S2); extract the duplicated `agenerate`/`agenerate_chat`
HTTP bodies in both adapters (S3R — fold into FU-C, since that ticket edits the same methods);
promote the payload-sentinel step hashes to real `StepRecord` fields (S4); single-source the
`STATUS_*` constants (S7A — the verifier's preferred direction is a shared constants module,
**not** models-imports-state, which would pull vendor SDKs into `models.py`); share the agent
test builders (S8 — check whether this counts as editing pinned tests before starting); one
shared approval-digest helper (R3 — but **not** `genai.sha256_of`, which returns `None` for
empty text and would silently disable the gate's hash check).

**Efficiency, real but bounded**: bulk `create_lead_runs` (EF1), the `load_prior_steps` N+1
(EF2), the O(work×leads) `similar_won_deals_for` recomputation (EF3 — CPU only, the events are
prefetched), and the uncapped `prior_actions` prefetch (EF4). All flag-on and per-run rather
than per-request.

**Not worth changing**: EF5, EF6, S5 (the ignored `arguments` parameter is a deliberate seam
and deleting it removes the schema-driven filtering), S6R (the duck-typed 404 is documented and
arguably more refactor-proof), R4 (the N+1 half is refuted — leads come prefetched), R5 (the
product-catalog duplication is deliberate, precisely so verifier grounding stays consistent).
