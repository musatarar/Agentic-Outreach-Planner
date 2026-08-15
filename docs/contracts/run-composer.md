# Contract: run-composer (MUS-47)

Frozen coordination artifact for the `feat/run-composer` integration branch. Component
PRs implement exactly these interfaces; changing a signature here requires touching
every consumer in the same PR — which the file map at the bottom is designed to prevent.
Same shape as `docs/contracts/agent-loop.md`, and for the same reason: a feature built
by several agents in sequence needs one place that says what the seams are.

Test artifacts: `project/app/tests_compose_<component>.py` and
`frontend/tests/compose_<component>.test.ts`, one per component, planted red
(`@unittest.expectedFailure` on the BE side, `{ todo: true }` on the FE side) by the
skeleton PR. A component PR takes its own artifact to zero markers and leaves every
sibling artifact's marker count untouched.

## Organizing principles

1. **The rules keep sole authority over classification.** `rules_priority` and
   `rules_action` are written once, at classify, and never written again. Everything the
   model contributes lands on `effective_*`, and only after a human accepts it.
2. **Nothing spends without a price shown first.** Every paid stage has an estimate
   endpoint reachable before it, and records its actual cost after.
3. **`plan_outreach()` does not change behavior.** The composer is additive. Component 3
   extracts shared functions out of it; every existing test — including
   `tests_planner_perf`'s query-count pins and the MUS-29 resume tests — must pass
   **unedited**.

## Named non-goals

- **Composer generation is single-shot.** The MUS-29 agent loop stays on
  `plan_outreach()`'s path. `OUTREACH_AGENT_ENABLED` has no effect on the composer.
- **The grounding verifier is not extended.** See "The fail-closed gate" below.
- **No budget enforcement.** Estimates only (MUS-51 owns enforcement).
- **One active run.** Concurrency across runs is out of scope; the DB enforces one.

## The fail-closed gate

`services/verify.py` runs unchanged, and this feature adds **no new fact source upstream
of it**. Three commitments, each with a test:

- `suggestion["rationale"]` is model prose derived from attacker-reachable notes. It is
  sanitized on write, rendered in the UI, and **never enters a copy-generation prompt**.
  `tests_compose_generate.py` asserts a built prompt cannot contain it.
- `effective_priority` is not in the copy prompt today and is not added to it.
- An accepted `action_change` swaps `action_type` only — a first-party enum
  `verify._check_offer` already covers — and the reason string is rebuilt from
  `ACCEPTED_ACTION_REASON`, a first-party template, never from model text.

## Interfaces

### models — `models.py`, migration `0008_run_composer`

```python
class PlannerRun(models.Model):
    STATUS_DRAFT, STATUS_CLASSIFIED, STATUS_READ = "draft", "classified", "read"
    STATUS_GENERATED, STATUS_COMPLETED, STATUS_DISCARDED = "generated", "completed", "discarded"
    ACTIVE_STATUSES = (STATUS_DRAFT, STATUS_CLASSIFIED, STATUS_READ, STATUS_GENERATED)
    TERMINAL_STATUSES = (STATUS_COMPLETED, STATUS_DISCARDED)
    ALLOWED_TRANSITIONS = {
        STATUS_DRAFT:      (STATUS_CLASSIFIED, STATUS_DISCARDED),
        STATUS_CLASSIFIED: (STATUS_CLASSIFIED, STATUS_READ, STATUS_GENERATED, STATUS_DISCARDED),
        STATUS_READ:       (STATUS_READ, STATUS_GENERATED, STATUS_DISCARDED),
        STATUS_GENERATED:  (STATUS_GENERATED, STATUS_COMPLETED, STATUS_DISCARDED),
        STATUS_COMPLETED:  (),
        STATUS_DISCARDED:  (),
    }

    status = CharField(max_length=16, default=STATUS_DRAFT, db_index=True)
    scope = JSONField(default=dict)                  # validated through scope.validate_scope
    # True while the run is active, NULL once terminal. NULLs are distinct on both
    # backends, so the partial unique index below admits exactly one active run.
    active_sentinel = BooleanField(null=True, default=True)
    created_by = EmailField(blank=True, default="")  # audit only, never authorization
    created_at = DateTimeField(auto_now_add=True)
    finished_at = DateTimeField(null=True, blank=True, default=None)
    finished_by = EmailField(blank=True, default="")
    # Minted at generate, stamped on every OutreachAction the run writes, so a
    # composer run joins to its rows the same way a planner run does.
    trace_run_id = CharField(max_length=36, blank=True, default="", db_index=True)
    classify_ms = IntegerField(null=True, blank=True, default=None)
    read_provider = CharField(max_length=32, blank=True, default="")
    read_model = CharField(max_length=128, blank=True, default="")
    generate_provider = CharField(max_length=32, blank=True, default="")
    generate_model = CharField(max_length=128, blank=True, default="")
    read_cost_estimate_usd = DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    read_cost_actual_usd = DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    generate_cost_estimate_usd = DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    generate_cost_actual_usd = DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    discarded_suggestions = IntegerField(default=0)

    def can_transition_to(self, new_status) -> bool

    class Meta:
        constraints = [
            UniqueConstraint(fields=["active_sentinel"],
                             condition=Q(active_sentinel=True), name="pr_one_active_run"),
            CheckConstraint(
                check=(Q(active_sentinel=True, status__in=ACTIVE_STATUSES)
                       | Q(active_sentinel__isnull=True, status__in=TERMINAL_STATUSES)),
                name="pr_sentinel_matches_status"),
        ]
```

`ACTIVE_STATUSES`/`TERMINAL_STATUSES` are inlined as literal tuples inside `Meta`
(Meta cannot see the enclosing class namespace — same reason `ReviewDecision`'s
constraints spell their kind strings out).

```python
class RunLead(models.Model):
    SUGGESTION_NONE, SUGGESTION_PROPOSED = "none", "proposed"
    SUGGESTION_ACCEPTED, SUGGESTION_REJECTED = "accepted", "rejected"

    run = ForeignKey(PlannerRun, CASCADE, related_name="run_leads")
    lead = ForeignKey(Lead, CASCADE, related_name="+")

    # Written once by classify. NEVER written again — pinned by a test.
    rules_priority = IntegerField()
    rules_action = CharField(max_length=64)
    rules_reason = TextField()
    rule_trace = JSONField(default=dict)      # outreach.explain() envelope, schema v1
    dedupe_key = CharField(max_length=128, db_index=True)

    # Rules unless a human accepted a suggestion.
    effective_priority = IntegerField()
    effective_action = CharField(max_length=64)
    effective_reason = TextField()

    already_queued = BooleanField(default=False)   # an open OutreachAction shares dedupe_key
    selected = BooleanField(default=False)
    generated_action = OneToOneField(OutreachAction, null=True, blank=True,
                                     on_delete=SET_NULL, related_name="run_lead")
    generation_error = CharField(max_length=64, blank=True, default="")  # outreach.failure_kind

    suggestion = JSONField(default=dict, blank=True)   # validated shape, or {}
    suggestion_state = CharField(max_length=16, default=SUGGESTION_NONE)
    suggestion_decided_at = DateTimeField(null=True, blank=True, default=None)
    suggestion_decided_by = EmailField(blank=True, default="")

    class Meta:
        constraints = [UniqueConstraint(fields=["run", "lead"], name="rl_one_row_per_lead_per_run")]
        indexes = [Index(fields=["run", "effective_priority", "lead"], name="rl_selection_order")]


class SavedScope(models.Model):
    name = CharField(max_length=64, unique=True)
    filters = JSONField(default=dict)         # validated on write
    created_at = DateTimeField(auto_now_add=True)
    created_by = EmailField(blank=True, default="")
    class Meta:
        ordering = ["name"]
```

`rule_trace` is `default=dict`, not `default=list`: MUS-42 already ships the structured
trace as `outreach.explain()`'s schema-v1 **envelope**, and `OutreachAction.rule_trace`
stores exactly that. Reusing it is what lets MUS-40's `RuleTrace` component render a
composer row with no changes.

### scope — `services/compose/scope.py`, `views_compose.py`

```python
@dataclass(frozen=True, slots=True)
class FilterSpec:
    key: str                        # "book_min" — unique, the scope-JSON key
    label: str                      # "book" — a NOUN, deliberately SHARED by min/max twins
    bound: str                      # "gte" | "lte" | "exact" | "days" | "bool"
    kind: str                       # "select" | "int" | "days" | "bool"
    coerce: Callable[[Any], Any]
    choices: tuple[str, ...] = ()

class ScopeError(ValueError):
    key: str                        # the offending scope key, echoed in the 400

FILTERABLE: Mapping[str, FilterSpec]   # the ONLY path from JSON to a queryset

def validate_scope(scope: Mapping[str, Any]) -> dict[str, Any]
def apply_scope(queryset, scope: Mapping[str, Any], *, today: datetime.date) -> QuerySet
def scope_field_catalog() -> list[dict]      # feeds the FE add-filter list
```

Keys: `stage`, `state`, `book_min`, `book_max`, `producers_min`, `producers_max`,
`years_min`, `quotes_created_min`, `quotes_created_max`, `quotes_submitted_min`,
`quotes_submitted_max`, `deals_min`, `deals_max`, `last_contacted_gt_days`,
`signed_up_within_days`, `dormant_days`, `has_notes`.

- **`key` is unique across the catalog; `label` deliberately is not.** `book_min` and
  `book_max` share the label `book`, and the chip renderer composes label + bound into
  `book >= $50,000`. A uniqueness assertion on `label` would forbid the min/max pairs the
  product needs, so uniqueness is asserted on `key` only.
- Unknown key → `ScopeError` → 400 `{"code": "unknown_filter", "detail": "...", "key": "..."}`.
- Uncoercible value → `ScopeError` → 400 `{"code": "invalid_filter", "detail": "...", "key": "..."}`.
- **Never** `**kwargs` into `.filter()`. Every key maps through `FILTERABLE`.
- The four computed keys are annotated querysets, not Python loops:
  `dormant_days` annotates `Max("events__timestamp", filter=Q(events__type="login"))`;
  `last_contacted_gt_days` admits NULL (never contacted counts as overdue);
  `signed_up_within_days` and `has_notes` are plain predicates.
- `SavedScope.filters` goes through `validate_scope` on write, so a stored scope cannot
  smuggle a key past the validator later.

### phases — `services/outreach.py`

Pure extraction. Three functions become public and callable by `services/compose/`;
`plan_outreach()` calls the same three and behaves identically.

```python
def classify_lead(lead, *, today, suppressed=frozenset(), open_keys=frozenset()) -> WorkItem | None
def review_outcome(item, outcome, level, today) -> ReviewOutcome
def snapshot_for(item, review, *, level, today) -> tuple[dict, dict]   # (rule_trace, verification)
```

`classify_lead` is today's `_build_work_item` with keyword-only extras; `review_outcome`
is today's `_review`; `snapshot_for` is today's phase-5 snapshot comprehension body.
No behavior change, no test edits, and `tests_planner_perf`'s `planner_queries(n)`
expectations must come out identical.

### lifecycle — `services/compose/runs.py`, `views_compose.py`, `serializers_compose.py`

```python
class RunConflict(RuntimeError):
    active_run_id: int
class InvalidRunTransition(RuntimeError): ...
class RunNotFound(LookupError): ...

def create_run(*, scope: Mapping[str, Any], created_by: str) -> PlannerRun
def active_run() -> PlannerRun | None
def classify_run(run: PlannerRun) -> PlannerRun
def close_run(run: PlannerRun, *, actor: str) -> PlannerRun
def discard_run(run: PlannerRun, *, actor: str) -> PlannerRun
```

`create_run` inserts and lets `pr_one_active_run` decide: an `IntegrityError` becomes
`RunConflict(active_run_id=...)`, never a read-then-write. `classify_run` runs
`classify_lead` over the scoped queryset, computes `already_queued` from one bulk read of
open `OutreachAction.dedupe_key`s, bulk-creates `RunLead` rows, and makes **zero LLM
client constructions** — pinned by a test. Re-classifying a `classified` run replaces its
`RunLead` rows (scope may have changed); it is idempotent in effect, not additive.

### estimate — `services/compose/estimate.py`

```python
CHARS_PER_TOKEN = 4
READ_OUTPUT_TOKENS = 120
GENERATE_OUTPUT_TOKENS = 260
STAGE_READ, STAGE_GENERATE = "read", "generate"
STAGES = (STAGE_READ, STAGE_GENERATE)

@dataclass(frozen=True, slots=True)
class Estimate:
    stage: str            # "read" | "generate"
    provider: str
    model: str
    lead_count: int
    tokens_in_est: int
    tokens_out_est: int
    usd_est: Decimal      # quantized to 4dp
    is_estimate: bool = True

def estimate_stage(run, stage, *, provider, model) -> Estimate
def record_actuals(run, stage, *, results: Sequence[LLMResult], provider, model) -> Decimal
```

Prices come from the MUS-32 `LLMModel` catalog row for `(provider, model)` — never a
constant in this module. `Decimal` throughout; float money is a bug. An unknown
`(provider, model)` pair raises `LLMModel.DoesNotExist`, surfaced as a 400.
`record_actuals` sums `LLMResult.input_tokens`/`output_tokens` and writes both the
`*_actual_usd` column and the provider/model actually used. A result whose token counts are
`None` never observed usage and must not be silently priced as free: it is excluded from
the sum and counted in a warning log naming how many went unpriced.

**`lead_count` is the rows this stage will actually bill for**, which is not the same
number in both stages: `read` prices every `RunLead` in the run (there is no selection to
respect yet), `generate` prices only `selected=True, already_queued=False`. Because the two
mean different things, `formatEstimate` on the frontend is **stage-aware** — "31 leads ×
Haiku 4.5 ≈ $0.02" for a read, "12 selected × Opus 5 ≈ $0.14" for a generate. Rendering
"selected" for a read estimate would claim the operator picked 31 leads they never touched.

`estimate_stage` writes the estimate it returns onto the run
(`read_cost_estimate_usd` / `generate_cost_estimate_usd`) so the estimate-vs-actual gap is
recoverable afterwards. An estimate nobody stored cannot be compared to anything.

### structured — `services/llm/base.py`, `claude.py`, `openai_compatible.py`, `stub.py`

```python
class LLMClient:
    async def agenerate_chat(self, messages, *, tools=(), tool_choice: str | None = None,
                             max_tokens=None, timeout=None) -> LLMResult
```

`tool_choice=None` is today's behavior and must produce a **byte-identical** request body
to today's — the MUS-29 loop passes nothing and must not change. A non-`None` value is a
tool *name*, forcing that one tool: `{"type": "tool", "name": n}` on Claude,
`{"type": "function", "function": {"name": n}}` on the OpenAI-compatible path, a scripted
call on the stub. A name not present in `tools` raises `ValueError` before any request.

This is structured output, not agency: the model is handed exactly one callable thing.

**MUS-66 landed** (`b80f3a5`, #96) and this branch is rebased onto it, so `tool_choice` is
built on top of the fixed parsing rather than beside it. That fix is load-bearing here, not
incidental: it made the adapters read `finish_reason`, treat blank tool arguments as `{}`,
raise on dropped entries, and merge consecutive tool results in Claude's fold. A forced
`emit_suggestion` call would have hit at least the blank-argument defect, which turns a
correct provider response into a failed run. Its tests live in
`tests_llm_tool_call_parsing.py`; this component must not duplicate them — it pins
`tool_choice` only.

### read — `services/compose/read.py`

```python
SUGGESTION_TOOL: ToolSpec            # name "emit_suggestion"
MAX_RATIONALE_CHARS = 200
MAX_EVIDENCE_ITEMS = 4
SUGGESTION_KINDS = ("raise", "lower", "action_change", "none")

@dataclass(frozen=True, slots=True)
class ReadSource:
    """Frozen plain-data snapshot, built in the synchronous phase.

    `notes_sanitized` and `timeline_sanitized` are *exactly the bytes the model
    sees*. The evidence validator checks quotes against these, never against raw
    `hubspot_notes` — a legitimate quote from sanitized text would otherwise fail
    wherever sanitization rewrote a character.
    """
    lead_id: str
    notes_sanitized: str
    timeline_sanitized: str
    rules_priority: int
    rules_action: str

def render_timeline(lead, *, today, limit: int = 12) -> str
def build_read_source(lead, *, today) -> ReadSource
def build_read_prompt(source: ReadSource) -> str
def validate_suggestion(raw: Mapping[str, Any], source: ReadSource) -> dict | None
async def aread_all(sources, *, client, runtime) -> tuple[dict[str, dict], int, list[LLMResult]]
def run_read(run, *, provider, model, actor) -> PlannerRun
```

`emit_suggestion`'s JSON schema carries `suggestion`, `proposed_priority`,
`proposed_action`, `rationale`, `evidence[]` — and **no `lead_id`**. The lead is bound
server-side from the loop. Unknown argument keys are dropped; a model-supplied
identifier is ignored, never honoured. A test proves a foreign id in the arguments
cannot change which `RunLead` is written.

`validate_suggestion` returns `None` (discard, counted in `discarded_suggestions`) when
any of these fail:

- `suggestion` outside `SUGGESTION_KINDS`;
- `raise`/`lower` without a `proposed_priority` in 1..3 moving in the named direction;
- `action_change` without a `proposed_action` in `SELECTABLE_ACTION_TYPES`;
- zero evidence entries on anything other than `none`;
- **any** `evidence[].source == "note"` whose `quote`, casefolded and
  whitespace-collapsed, is not a substring of `source.notes_sanitized` under the same
  normalization — likewise `source == "event"` against `timeline_sanitized`.

`rationale` is `sanitize_untrusted()`-cleaned and truncated to `MAX_RATIONALE_CHARS`.

**The persisted `suggestion` shape is fixed — all five keys, always present**, with
explicit placeholders rather than absent keys, so no reader ever branches on `in`:

```json
{"suggestion": "raise", "proposed_priority": 1, "proposed_action": "",
 "rationale": "…", "evidence": [{"source": "note", "quote": "…"}]}
```

`proposed_priority` is `null` when the kind does not carry one; `proposed_action` is `""`.
`evidence` entries keep the model's **original** quote text, not the normalized form the
validator compared — the frontend renders the quote verbatim beside the note, and
normalization is a comparison detail, not a storage format. `RunLead.suggestion` is `{}`
only when nothing was ever proposed.

Prompt assembly: notes and the rendered timeline go through `sanitize_untrusted()` →
`wrap_untrusted()` behind `outreach._UNTRUSTED_STANDING_INSTRUCTION`, applied **exactly
once**, never inside the instruction region. `tests_compose_read.py` carries a red-team
case asserting a planted payload reaches the final message list both redacted **and**
inside the `UNTRUSTED_*` markers.

One lead's failure never sinks the run: a provider error, a malformed response, or a
discarded suggestion all leave that lead at `suggestion_state = "none"` and the run
continues. Concurrency is bounded by `PlannerRuntime.max_in_flight`, reusing
`llm_runtime.get_planner_runtime()`.

### decisions — `services/compose/decisions.py`, `views_compose.py`

```python
ACCEPTED_ACTION_REASON = (
    "Reviewer accepted an agent suggestion to change the action from {old} to {new}."
)

def accept_suggestion(run, lead_id: str, *, actor: str) -> RunLead
def reject_suggestion(run, lead_id: str, *, actor: str) -> RunLead
```

Both write `suggestion_state`, `suggestion_decided_at` and `suggestion_decided_by` inside
one transaction, and both are re-runnable — accept → reject → accept is legal and the last
decision wins. Accept moves `effective_priority`/`effective_action`; reject restores both
to the `rules_*` values. `rules_priority` and `rules_action` are never written by either —
asserted directly. An accepted `action_change` also recomputes `dedupe_key` (the identity
of the recommendation changed) and rebuilds `effective_reason` from
`ACCEPTED_ACTION_REASON`, never from `suggestion["rationale"]`.

### generate — `services/compose/generate.py`

```python
def select_leads(run, lead_ids: Sequence[str], selected: bool) -> int
def generate_for_selection(run, *, provider, model, actor) -> dict
    # -> {"generated": int, "failed": int, "skipped": int, "actual_usd": Decimal}
```

Generates for `selected=True, already_queued=False` rows only. Reuses `_build_copy_prompt`
(via `classify_lead`'s `WorkItem`), `_agenerate_all`'s bounded pool, `review_outcome` and
`snapshot_for` — so a composer draft passes the same two output gates a planner draft
does. Creates `OutreachAction(status="pending", trace_run_id=run.trace_run_id, ...)`,
links `generated_action`, and records actuals. A failed lead gets `generation_error` set
and stays selectable for retry; the run does not fail.

### FE — `frontend/src/pages/ComposePage.tsx` and friends

Route `/compose`, added to `Nav`. Endpoint helpers in `api/endpoints.ts`, types in
`api/types.ts`. The pure modules below are the FE test surface — the harness is
`node --test` over `frontend/tests/*.test.ts`, with no jsdom and no Testing Library, so
**component rendering is review-verified, not unit-tested**. Adding a DOM test framework
is its own decision, not a side effect of this feature.

```ts
// components/compose/scopeChips.ts          — owned by fe_scope
export function scopeToChips(scope: Scope, fields: ScopeField[]): Chip[]
export function chipsToScope(chips: Chip[]): Scope
// components/compose/stages.ts              — owned by fe_scope (it gates every stage)
export function stageFor(run: RunDetail | null): StageName
export function stageEnabled(stage: StageName, run: RunDetail | null): boolean
// components/compose/suggestionView.ts      — owned by fe_read
export function suggestionViews(rows: RunLeadRow[]): SuggestionView[]
export function readSummary(rows: RunLeadRow[], discarded: number): ReadSummary
// components/compose/selection.ts           — owned by fe_generate
export function selectionReducer(state: SelectionState, action: SelectionAction): SelectionState
// components/compose/estimateLine.ts        — owned by fe_generate
export function formatEstimate(e: Estimate): string
//   stage "read"     -> "31 leads × Haiku 4.5 ≈ $0.02"
//   stage "generate" -> "12 selected × Opus 5 ≈ $0.14"
```

`stages.ts` belongs to `fe_scope` rather than a component of its own: it is the shell's
gate, it lands with the route, and every later stage reads it.

## Wire shapes

The frontend renders exactly these keys. Anything the frontend needs and the serializer
does not emit is a bug that only shows up in the browser, so both sides are pinned here.

```jsonc
// RunDetail — GET /api/runs/{id}/, /active/, and every mutating run endpoint's 200
{"id": 7, "status": "classified", "scope": {...},
 "lead_count": 31,                    // rows in the run. THE zero-lead gate for the FE
 "spread": {"1": 4, "2": 12, "3": 15},// null until classified
 "classify_ms": 38,
 "discarded_suggestions": 2,
 "read_provider": "", "read_model": "",
 "generate_provider": "", "generate_model": "",
 "read_cost_estimate_usd": null, "read_cost_actual_usd": null,      // strings when set
 "generate_cost_estimate_usd": null, "generate_cost_actual_usd": null,
 "created_at": "iso8601", "created_by": "a@b.c", "finished_at": null,
 "run_leads": [ /* RunLeadRow, only on GET /api/runs/{id}/ and /classify/ */ ]}

// RunLeadRow
{"lead_id": "lead_014", "agency_name": "…", "contact_name": "…",
 "rules_priority": 2, "rules_action": "nudge_usage", "rules_reason": "…",
 "effective_priority": 1, "effective_action": "nudge_usage", "effective_reason": "…",
 "rule_trace": { /* explain() envelope, schema v1 */ },
 "already_queued": false, "selected": false,
 "suggestion": {...} /* or {} */, "suggestion_state": "proposed",
 "suggestion_decided_at": null, "suggestion_decided_by": "",
 "generated_action_id": null, "generation_error": ""}

// Estimate — GET /api/runs/{id}/estimate/
{"stage": "generate", "provider": "claude", "model": "claude-opus-5",
 "lead_count": 12, "tokens_in_est": 9600, "tokens_out_est": 3120,
 "usd_est": "0.1372", "is_estimate": true}

// GenerateResult — POST /api/runs/{id}/generate/
{"generated": 10, "failed": 1, "skipped": 1, "actual_usd": "0.1401"}

// POST /api/runs/preview-count/
{"count": 31, "total": 200}
```

## Endpoints

```
POST   /api/runs/                          {scope}   201 RunDetail | 409 run_active
GET    /api/runs/active/                             200 RunDetail | 404 no_active_run
GET    /api/runs/{id}/                               200 RunDetail
POST   /api/runs/{id}/classify/                      200 RunDetail
POST   /api/runs/{id}/close/                         200 RunDetail
POST   /api/runs/{id}/discard/                       200 RunDetail
POST   /api/runs/preview-count/            {scope}   200 {"count": N, "total": M}
GET    /api/runs/{id}/estimate/?stage=&provider=&model=   200 Estimate
POST   /api/runs/{id}/read/                {provider?, model?}   200 RunDetail
POST   /api/runs/{id}/suggestions/{lead_id}/accept/  200 RunLead
POST   /api/runs/{id}/suggestions/{lead_id}/reject/  200 RunLead
POST   /api/runs/{id}/select/              {lead_ids, selected}   200 {"selected": N}
POST   /api/runs/{id}/generate/            {provider?, model?}    200 GenerateResult
GET    /api/scopes/ | POST /api/scopes/ | DELETE /api/scopes/{id}/
GET    /api/scopes/fields/                           200 {"fields": [...]}
```

### Error envelope

`{"code": "<slug>", "detail": "<sentence>"}`, produced by `views_queue.error()` — reused,
not reimplemented. **`code`, not `error`.** This is not a style preference:
`frontend/src/api/client.ts` reads `record.code` into `ApiError.code` and documents it as
"the machine slug callers branch on", so an envelope keyed `error` is one the frontend
cannot branch on at all. (`views_trace.py` and `views.py` do emit `{"error": ...}`; those
are the pre-MUS-37 endpoints `client.ts` calls out as having no `code`. Do not copy them.)

One endpoint adds a third key: the 409 from `POST /api/runs/` is
`{"code": "run_active", "detail": "...", "active_run_id": N}`. The extra key is the whole
point — it is what lets the frontend offer to resume rather than just refusing.

Slugs: `run_active`, `no_active_run`, `not_found`, `invalid_transition`, `unknown_filter`,
`invalid_filter`, `unknown_model`, `invalid_stage`, `no_suggestion`.

### Money on the wire is a string

`REST_FRAMEWORK` does not set `COERCE_DECIMAL_TO_STRING`, so DRF's default applies and
every `DecimalField` serializes as a **JSON string** (`"0.1372"`), never a float. That is
the correct behaviour — float money loses cents — and it is not to be "fixed" by flipping
the setting, which would change every existing endpoint. The frontend parses with
`Number(...)` at the edge and formats from the number.

This governs `usd_est`, `actual_usd`, and all four `*_cost_*_usd` columns.

### URL ordering

`/api/scopes/fields/` precedes `/api/scopes/{id}/` in `urls.py`, defensively, the same way
`queue/done/` precedes `queue/<int:pk>/`.

## File map

| Component | Owns (creates or modifies) |
| --- | --- |
| skeleton (shared) | `project/app/urls.py` (all compose routes), `project/app/views_compose.py` (stub view classes), `frontend/src/api/types.ts` (all compose types), `project/app/services/compose/__init__.py`, `docs/contracts/run-composer.md`, `docs/plans/mus-47-run-composer.md`, `docs/adr/run-composer-state.md`, all nine `tests_compose_*.py` artifacts, four `frontend/tests/compose_*.test.ts` artifacts, stub bodies of every module below |
| models | `project/app/models.py`, `project/app/migrations/0008_run_composer.py` |
| scope | `services/compose/scope.py`, `project/app/views_compose.py` (scope views), `serializers_compose.py` |
| phases | `project/app/services/outreach.py` |
| lifecycle | `services/compose/runs.py`, `views_compose.py` (run views), `serializers_compose.py` |
| estimate | `services/compose/estimate.py`, `views_compose.py` (estimate view) |
| structured | `services/llm/base.py`, `claude.py`, `openai_compatible.py`, `stub.py` |
| read | `services/compose/read.py`, `views_compose.py` (read view), `project/app/tests_redteam.py` |
| decisions | `services/compose/decisions.py`, `views_compose.py` (decision views) |
| generate | `services/compose/generate.py`, `views_compose.py` (select/generate views) |
| fe_scope | `frontend/src/pages/ComposePage.tsx`, `components/compose/**`, `api/endpoints.ts`, `main.tsx`, `components/Nav.tsx` |
| fe_read | `frontend/src/components/compose/SuggestionCard.tsx` and siblings |
| fe_generate | `frontend/src/components/compose/SelectionTable.tsx` and siblings |
| docs | `README.md`, `SECURITY.md`, `scripts/populate_demo_data.py`, `project/app/static/frontend/**` |

`views_compose.py` is touched by several components on purpose — they land sequentially,
each filling in its own stub view, so there is no parallel-edit collision to prevent. The
skeleton wires **every** route to a stub view raising `NotImplementedError`, so an endpoint
test fails on its assertion rather than on a Django 404. A 404 proves nothing about the
endpoint the test is specifying.

## Test conventions for this feature

Settled once here because thirteen artifacts were written independently and drifted:

- **Patch the provider client at `services.llm._build_client`.** It is the single
  `lru_cache`d constructor both `get_llm_client` and `build_client` funnel through, so the
  patch holds however the component under test spells its import. Patching
  `services.llm.get_llm_client` binds nothing if the component did
  `from … import get_llm_client`, and `assert_not_called()` then passes by construction —
  a test that cannot fail. Where a no-call assertion is the point, pair it with a positive
  control proving the patch is live.
- **`rule_trace` fixtures use a real `outreach.explain()` envelope** — or at minimum its
  real keys (`version`, `today`, `generated_at`, `priority`, `action`). Invented shapes
  like `{"rule": ..., "steps": []}` describe a schema that does not exist.
- **Base classes:** `SimpleTestCase` for anything that must not reach the database (it
  actually blocks queries; bare `unittest.TestCase` does not), Django `TestCase`
  otherwise, and the repo's `AuthenticatedAPITestCase` for endpoint tests.
- **Action types come from `services.actions`** — never invented (`check_in` is not one).
- **Artifacts stay near sibling size.** The existing `tests_agent_loop_*.py` run 76–594
  lines with roughly one line of docstring per test. An artifact at 990 lines is arguing,
  not specifying.
