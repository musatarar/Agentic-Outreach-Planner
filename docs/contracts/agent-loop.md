# Contract: agent-loop (MUS-29)

Frozen coordination artifact for the `feat/agent-loop` integration branch. Component
PRs implement exactly these interfaces; changing a signature here requires touching
every consumer in the same PR — which the file map below is designed to prevent.
The former repo-wide contract mandate is retired; this feature keeps one anyway as
its coordination artifact.

Test artifacts: `project/app/tests_agent_loop_<component>.py`, one per component,
planted red (`@unittest.expectedFailure`) by the skeleton PR. A component PR takes
its own module to zero markers and leaves every sibling module's marker count
untouched.

## Interfaces

### llm_tools — `services/llm/chat_types.py`, `base.py`, `claude.py`, `openai_compatible.py`, `stub.py`

```python
# chat_types.py (pure stdlib, no Django)
@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: Mapping[str, Any]          # JSON schema

@dataclass(frozen=True, slots=True)
class ToolCallRequest:
    id: str
    name: str
    arguments: Mapping[str, Any]

@dataclass(frozen=True, slots=True)
class Message:
    role: str                              # "user" | "assistant" | "tool_result"
    content: str = ""
    tool_calls: tuple[ToolCallRequest, ...] = ()
    tool_call_id: str = ""                 # set only when role == "tool_result"

# base.py
class LLMResult:                           # widened: new LAST field, defaulted
    tool_calls: tuple[ToolCallRequest, ...] = ()

class LLMClient:
    async def agenerate_chat(self, messages: Sequence[Message], *,
                             tools: Sequence[ToolSpec] = (),
                             max_tokens: int | None = None,
                             timeout: float | None = None) -> LLMResult: ...
    # default raises NotImplementedError, mirroring agenerate
```

`agenerate_chat` is **async-only** by design: the sync Claude client keeps 2
SDK-internal retries and would double-retry under the loop's own retry policy.

### agent_models — `models.py`, migration `0007_agent_loop`

Everything named in the plan's "Data model & migrations" section, exactly:
`AgentLeadRun` (with `STATUS_*` constants, `NON_TERMINAL_STATUSES`, `claim_epoch`
CAS fields, `alr_one_run_per_lead_per_trace`, `alr_resume_scan`), append-only
`AgentStep` (`astep_one_seq_per_run`, `ordering = ["seq"]`), `AEAvailabilitySlot`,
`OutboundSend` (OneToOne on `outreach_action`, PROTECT), `ReviewDecision`
alterations (OneToOne → FK `related_name="review_decisions"`, `KIND_APPROVE_SEND`,
`KIND_REJECT_SEND`, `RESOLUTION_KINDS`, `SEND_KINDS`, `approved_copy`,
`approved_body_sha256`, `voided_at`, `rd_one_resolution_per_action`,
`rd_one_live_send_per_action`), `OutreachAction.STATUS_SENT`
(`approved → {pending, sent}`, `sent` terminal).

### agent_tools — `services/agent/tools.py`, `product_catalog.py`, `ingest_data`

```python
MAX_TOOL_RESULT_CHARS = 2000
TOOL_SPECS: tuple[ToolSpec, ...]   # get_lead_history, get_similar_won_deals,
                                   # get_product_details, check_ae_calendar
class UnknownTool(ValueError): ...

@dataclass(frozen=True, slots=True)
class ToolContext:
    lead_id: str
    history: tuple[Mapping[str, Any], ...]
    similar_won_deals: tuple[Mapping[str, Any], ...]
    product_details: Mapping[str, Any]
    ae_slots: tuple[Mapping[str, Any], ...]

def similar_won_deals_for(lead: Any, all_leads: Sequence[Any]) -> tuple[dict[str, Any], ...]
def build_tool_context(lead: Any, prior_actions: Sequence[Mapping[str, Any]],
                       similar: tuple[Mapping[str, Any], ...],
                       ae_slots: Sequence[Mapping[str, Any]],
                       today: datetime.date | None) -> ToolContext
def execute_tool(name: str, arguments: Mapping[str, Any], context: ToolContext) -> str
```

Tools never touch the ORM; every free-text field is `sanitize_untrusted()`-cleaned
at context build; results are capped at `MAX_TOOL_RESULT_CHARS` at execution;
`lead_id` is never a model-supplied argument (unknown argument keys are dropped
against each spec's schema, and the executor is bound server-side to the current
lead's context).

### loop — `services/agent/state.py`, `loop.py`, `runtime.py`, telemetry

```python
# state.py
KIND_LLM_CALL, KIND_TOOL_RESULT, KIND_FINAL = "llm_call", "tool_result", "final"
@dataclass(frozen=True, slots=True)
class StepRecord:
    seq: int
    kind: str
    payload: Mapping[str, Any]
class AgentClaimLost(RuntimeError): ...
def fold_messages(prompt: str, steps: Sequence[StepRecord], *,
                  force_final: bool = False) -> list[Message]
def create_lead_runs(trace_run_id: str, lead_ids: Sequence[str]) -> dict[str, int]  # sync/phase 2
def load_prior_steps(lead_run_pk: int) -> tuple[StepRecord, ...]                    # sync/phase 2
class Checkpoint:
    def __init__(self) -> None: ...        # owns worker token + asyncio.Lock
    async def claim(self, lead_run_pk: int) -> bool
    async def append(self, lead_run_pk: int, records: Sequence[StepRecord], *,
                     status: str, steps_used: int, tool_calls_used: int) -> None

# loop.py
@dataclass(frozen=True, slots=True)
class AgentOutcome:
    draft_text: str = ""
    error: Exception | None = None
    steps_used: int = 0
    tool_calls_used: int = 0
async def run_agent_lead(*, prompt: str, lead_run_pk: int,
                         prior_steps: Sequence[StepRecord], context: ToolContext,
                         client: LLMClient, runtime: PlannerRuntime,
                         checkpoint: Checkpoint) -> AgentOutcome

# runtime.py — PlannerRuntime gains (defaults):
agent_enabled: bool = False              # OUTREACH_AGENT_ENABLED
agent_max_steps: int = 6                 # OUTREACH_AGENT_MAX_STEPS
agent_max_tool_calls: int = 8            # OUTREACH_AGENT_MAX_TOOL_CALLS
agent_per_lead_s: float = 300.0          # OUTREACH_AGENT_PER_LEAD_TIMEOUT_S, validated ≥ request_s

# semconv.py / genai.py
OPERATION_EXECUTE_TOOL = "execute_tool"; GEN_AI_TOOL_NAME = "gen_ai.tool.name"
@contextmanager
def tool_span(name: str, *, args_sha256: str | None = None
              ) -> Iterator[Callable[[str | None], None]]   # yields set_result_sha256
def run_span(*, verify_level, max_in_flight, run_id: str | None = None)  # reuse id on resume
```

`AgentOutcome` deliberately has **no** `action_type`/`priority`/`needs_human`
field — the rules engine keeps sole authority over classification, and
`services/agent/` never imports the send-gate module (both facts are pinned by
`tests_agent_loop_assembly.py`).

Steps persist the *sanitized, unwrapped* tool result; `wrap_untrusted()` delimiters
are applied exactly once, at fold time.

### approval_gate — `services/dispatch.py`, `views_queue.py`, `serializers.py`

```python
class DispatchBlocked(RuntimeError): ...
def dispatch(action: OutreachAction) -> OutboundSend
```

`dispatch()` hard-raises without a live (`voided_at IS NULL`), resolved
`approve_send` decision whose `approved_body_sha256` equals
`sha256(action.effective_copy)`; the `approved → sent` flip is a conditional-UPDATE
CAS; the `OutboundSend` OneToOne is the DB backstop. `QueueApproveView` records the
approve decision atomically with the status flip; `QueueDismissView` records a
`reject_send` decision; `QueueUndoView` voids live send decisions on
approved→pending and dismissed→pending. `ReviewDecisionSerializer` rejects both
send kinds (they are recorded only via the queue endpoints).

### reports_trace — `views_trace.py`, frontend

`GET /api/outreach/<int:pk>/trace/` → 200

```json
{"action_id": 1, "lead_id": "lead_001", "trace_run_id": "uuid", "status": "done",
 "steps_used": 2, "tool_calls_used": 1,
 "steps": [{"seq": 1, "kind": "llm_call", "payload": {}, "created_at": "iso8601"}]}
```

404 `{"error": "no_agent_trace"}` when the action has no agent run (single-shot
rows). Frontend: `fetchOutreachTrace(id: number): Promise<AgentTrace>` and a
collapsible "How this draft was reached" per reports entry.

### assembly — `services/outreach.py`, `views.py`

```python
def plan_outreach(resume_run_id: str | None = None)
# Both refusals are raised by plan_outreach itself, before any read or span, so
# a refused resume writes no row and makes no provider call. The view maps them:
#   UnknownRun    → 400 {"error": "unknown_run"}     no AgentLeadRun rows match
#   AgentDisabled → 400 {"error": "agent_disabled"}  OUTREACH_AGENT_ENABLED off
# Unknown is checked first, so a typo is reported as a typo whatever the flag.
# POST /api/outreach/run/ accepts {"resume_run_id": "<uuid>"}
async def _agenerate_for(item, client, runtime, client_error=None,
                         agent_plan=None, checkpoint=None)

@dataclass(frozen=True, slots=True)
class AgentLeadPlan:
    lead_run_pk: int
    prior_steps: tuple[StepRecord, ...]
    context: ToolContext
```

`ReviewQueueView.decided_ids` narrows to `RESOLUTION_KINDS` so a send decision
never hides an unresolved `needs_human` row. Phase 5 gains an idempotent finalize
(skip leads that already have a row for this `trace_run_id`) immediately before the
`bulk_create`.

## Data shapes

`AgentStep.payload` by `kind`:

- `llm_call`: `{"text", "tool_calls": [{"id", "name", "arguments"}], "provider",
  "model", "input_tokens", "output_tokens", "raw_finish_reason", "latency_s"}`
- `tool_result`: `{"tool_call_id", "name", "result"}` — result already sanitized
  and capped, **not** wrapped
- `final`: `{"text"}`

`request_sha256`/`result_sha256` on `AgentStep` carry the same `genai.sha256_of`
hashes the spans carry, so span and step cross-reference without leaking content.

## Error contract

- Skeleton stubs raise `NotImplementedError` (each message names the owning
  component).
- `DispatchBlocked` — any missing link in the approval chain; nothing was sent.
- `AgentClaimLost` — another worker won the epoch-CAS claim; the loser writes
  nothing and produces no `OutreachAction` row.
- `UnknownTool` — a tool name outside `TOOL_SPECS` reached `execute_tool`.
- `LLMMalformedResponseError` — only when a response has neither text nor tool
  calls (or tool-call arguments are not valid JSON on the OpenAI-compatible path).
- `LLMError` subclasses and `TimeoutError` inside the loop map to
  `AgentOutcome(error=...)` with run status `failed` (or `exhausted` when budgets
  ran out before a final) — mirroring `_agenerate_for`'s mapping.

## File map

| Component | Owns (creates or modifies) |
| --- | --- |
| skeleton (shared) | `project/app/urls.py`, `project/settings.py`, `frontend/src/api/types.ts`, `project/app/services/agent/__init__.py`, `docs/contracts/agent-loop.md`, `docs/adr/agent-loop-state.md`, all seven `tests_agent_loop_*.py` artifacts, `frontend/tests/agent_loop_reports_trace.test.ts`, stub bodies of every module below |
| llm_tools | `services/llm/chat_types.py`, `services/llm/base.py`, `services/llm/claude.py`, `services/llm/openai_compatible.py`, `services/llm/stub.py` |
| agent_models | `project/app/models.py`, `project/app/migrations/0007_agent_loop.py` |
| agent_tools | `services/agent/tools.py`, `services/agent/product_catalog.py`, `project/app/management/commands/ingest_data.py` |
| loop | `services/agent/state.py`, `services/agent/loop.py`, `services/llm/runtime.py`, `services/telemetry/semconv.py`, `services/telemetry/genai.py`, `project/settings.py` (wiring only) |
| approval_gate | `services/dispatch.py`, `project/app/views_queue.py`, `project/app/serializers.py` |
| reports_trace | `project/app/views_trace.py`, `frontend/src/api/endpoints.ts`, `frontend/src/pages/ReportsPage.tsx`, `project/app/static/frontend/**` (rebuilt bundle) |
| assembly | `project/app/services/outreach.py`, `project/app/views.py` |

A component PR's diff stays inside its row (plus stripping the markers in its own
test artifact). Self-check before push:
`git diff origin/feat/agent-loop --stat`, module green with zero markers, sibling
marker counts untouched.
