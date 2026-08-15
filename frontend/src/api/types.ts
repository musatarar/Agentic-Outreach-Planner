/**
 * Mirrors the DRF serializers in project/app/serializers.py. The backend is a
 * frozen contract for this app
 */

export type Priority = 1 | 2 | 3;

export interface Lead {
  id: number;
  agency_name: string;
  contact_name: string;
  contact_email: string;
}

export interface OutreachAction {
  id: number;
  lead: Lead;
  priority: Priority;
  action_type: string;
  reason: string;
  suggested_copy: string | null;
  needs_human: boolean;
  further_action: string | null;
  created_at: string;
}

/** One of the pre-defined action types a reviewer can pick from. */
export interface ActionOption {
  value: string;
  label: string;
}

export interface ReviewQueue {
  items: OutreachAction[];
  action_options: ActionOption[];
}

/** One selectable model within a provider, from GET /api/llm/catalog/. */
export interface LLMModel {
  id: string;
  label: string;
  context_window: number;
  default_max_tokens: number;
  input_price_per_mtok_usd: number;
  output_price_per_mtok_usd: number;
  tier: string;
  notes: string;
}

/**
 * One provider entry from GET /api/llm/catalog/. sort_order is optional in
 * the type (not shown in the single-item contract example) — display code
 * falls back to array order when it's absent.
 */
export interface LLMProvider {
  key: string;
  label: string;
  api_key_url: string;
  api_key_label: string;
  api_key_prefix: string;
  sort_order?: number;
  models: LLMModel[];
}

/** GET /api/llm/catalog/ — the full list of providers/models to choose from. */
export interface LLMCatalog {
  providers: LLMProvider[];
}

/** Where the active API key came from — never the key itself. */
export type LLMKeySource = 'database' | 'environment' | 'none';

/** GET /api/llm/config/ — the active provider/model config. Never returns the key. */
export interface LLMConfig {
  provider: string;
  model: string;
  max_tokens: number;
  has_key: boolean;
  key_last_four: string | null;
  key_source: LLMKeySource;
  updated_at: string;
}

/**
 * PUT body for /api/llm/config/. api_key is write-only: omit to keep the
 * stored key, null to clear it, a string to set/replace it.
 */
export interface LLMConfigInput {
  provider: string;
  model: string;
  max_tokens: number;
  api_key?: string | null;
}

/** Result of POST /api/llm/config/test/ — either shape may come back. */
export type LLMTestResult =
  | { ok: true; latency_ms: number; model_echo: string }
  | {
      ok: false;
      error_kind: 'auth' | 'rate_limit' | 'unknown_model' | 'network';
      message: string;
    };

export type DecisionKind = 'select_existing' | 'propose_new';
export type DecisionStatus = 'resolved' | 'pending_engineering';

export interface ReviewDecision {
  id: number;
  outreach_action: number;
  kind: DecisionKind;
  selected_action_type: string | null;
  proposed_name: string | null;
  proposed_what: string | null;
  proposed_when: string | null;
  reviewer: string;
  status: DecisionStatus;
  created_at: string;
}

/** POST body for /api/review-decisions/ — id/status/created_at are server-set. */
export type ReviewDecisionInput =
  | {
      outreach_action: number;
      kind: 'select_existing';
      selected_action_type: string;
      reviewer: string;
    }
  | {
      outreach_action: number;
      kind: 'propose_new';
      proposed_name: string;
      proposed_what: string;
      proposed_when: string;
      reviewer: string;
    };

// ===== MUS-37: magic-link auth =====================================

export interface AuthMe {
  authenticated: boolean;
  email: string | null;
}

export interface AuthRequestLinkInput {
  email: string;
}

export interface AuthRequestLinkResult {
  status: 'sent';
  expires_in: number;          // seconds, e.g. 900
  resend_after: number;        // seconds, e.g. 30
  dev_link: string | null;     // non-null ONLY in DEBUG + console delivery
}

export interface AuthConsumeInput {
  token: string;
}

export interface AuthConsumeResult {
  authenticated: true;
  email: string;
  session_expires_at: string;  // ISO 8601
}

/** Every non-2xx body in this API. `detail` is always present. */
export interface ApiErrorBody {
  code: ApiErrorCode;
  detail: string;
  retry_after?: number;
}

export type ApiErrorCode =
  | 'invalid_email'
  | 'invalid_token'
  | 'expired_token'
  | 'empty_copy'
  | 'invalid_snooze'
  | 'invalid_reason'
  | 'validation_error'
  | 'not_authenticated'
  | 'csrf_failed'
  | 'not_found'
  | 'method_not_allowed'
  | 'invalid_transition'
  | 'unverified_claims'
  | 'undo_window_expired'
  | 'rate_limited';

// ===== MUS-39 / MUS-42: triage queue ===============================

export type QueueStatus = 'pending' | 'approved' | 'snoozed' | 'dismissed';

export type SnoozeTrigger =
  | 'tomorrow'
  | 'in_3_days'
  | 'next_week'
  | 'custom'
  | 'on_activity';

export type DismissReason =
  | 'not_a_fit'
  | 'bad_timing'
  | 'wrong_contact'
  | 'already_handled'
  | 'copy_unusable'
  | 'other'
  | '';

// ---- rule trace (schema v1, MUS-42) ----

export type TraceOperator =
  | '>=' | '<=' | '>' | '<' | '==' | '!='
  | 'in' | 'contains' | 'exists' | 'absent';

export type TraceUnit =
  | 'days' | 'usd' | 'count' | 'date' | 'text' | 'bool' | 'none';

export type TraceSource = 'lead' | 'events' | 'notes' | 'derived';

export interface TraceCondition {
  kind: 'condition';
  id: string;
  field: string;
  label: string;
  operator: TraceOperator;
  threshold: unknown;
  value: unknown;
  unit: TraceUnit;
  passed: boolean;
  weight: number;
  source: TraceSource;
  /** Server-rendered mono line, e.g. "trial_ends_in <= 6d → 4d". Render VERBATIM. */
  display: string;
}

export interface TraceGroup {
  kind: 'group';
  id: string;
  label: string;
  operator: 'all_of' | 'any_of';
  passed: boolean;
  weight: number;
  display: string;
  conditions: TraceCondition[];   // exactly one level of nesting
}

export type TraceSignal = TraceCondition | TraceGroup;

export interface TracePriorityBand {
  priority: 1 | 2 | 3;
  min_score: number;
}

export interface RuleTrace {
  version: 1;
  today: string;                  // ISO date the trace was evaluated at
  generated_at: string;
  priority: {
    value: Priority;
    score: number;
    bands: TracePriorityBand[];
    signals: TraceSignal[];
  };
  action: {
    value: string;
    rule_id: string;
    rule_label: string;
    matched_rule_index: number;
    conditions: TraceCondition[];
    rejected_rules: {
      rule_id: string;
      rule_label: string;
      matched: false;
      conditions: TraceCondition[];
    }[];
  };
}

// ---- verification spans (schema v1, MUS-42) ----

export type ClaimKind =
  | 'amount'
  | 'deals_count'
  | 'quotes_count'
  | 'producers_count'
  | 'years_count'
  | 'iso_date'
  | 'contact_name'
  | 'goal_reference'
  | 'future_date'
  | 'unauthorized_offer'
  | 'omission'
  | 'unsupported_year';

export interface VerificationClaim {
  id: string;
  kind: ClaimKind;
  /** Unicode CODE POINT offsets into `VerificationReport.copy`. Null for omissions. */
  start: number | null;
  end: number | null;
  text: string;
  /** true = green underline, false = red underline, null = no underline. */
  verified: boolean | null;
  field: string;
  expected: unknown;
  claimed: unknown;
  message: string;
  counts_toward_summary: boolean;
}

export interface VerificationReport {
  version: 1;
  level: 'off' | 'standard' | 'strict';
  today: string;
  /** The EXACT string the offsets index into. Render THIS, never a local copy. */
  copy: string;
  copy_length: number;
  /** false => convert offsets with Array.from() before slicing. */
  is_astral_safe: boolean;
  verified_count: number;
  unverified_count: number;
  checked_count: number;
  /** e.g. "4 of 4 claims verified". Render VERBATIM. */
  summary: string;
  can_approve: boolean;
  claims: VerificationClaim[];
}

// ---- queue items ----

export interface QueueLeadEvent {
  type: string;
  timestamp: string;
  summary: string;
}

export interface QueueLead {
  id: string;
  agency_name: string;
  contact_name: string;
  contact_email: string;
  state: string;
  stage: string;
  num_producers: number;
  estimated_book_size_usd: number;
  quotes_created: number;
  quotes_submitted: number;
  deals_closed: number;
  signed_up_date: string | null;
  last_login_date: string | null;
  last_contacted_date: string | null;
  recent_events: QueueLeadEvent[];   // max 5, newest first
}

export interface QueueItem {
  id: number;
  status: QueueStatus;
  status_changed_at: string | null;
  priority: Priority;
  action_type: string;
  action_label: string;
  reason: string;
  needs_human: boolean;
  further_action: string;
  created_at: string;
  dedupe_key: string;
  lead: QueueLead;
  suggested_copy: string;   // IMMUTABLE
  edited_copy: string;      // "" when never edited
  effective_copy: string;   // edited_copy || suggested_copy -- use THIS
  is_edited: boolean;
  rule_trace: RuleTrace;
  verification: VerificationReport;
  can_approve: boolean;
  snooze: {
    until: string | null;
    trigger: SnoozeTrigger | '';
    activity_after: string | null;
  };
  dismiss_reason: DismissReason;
  undo: { available: boolean; expires_at: string | null };
}

export interface QueueCounts {
  total_today: number;
  done_today: number;
  remaining: number;
  approved_today: number;
  snoozed_today: number;
  dismissed_today: number;
}

export interface QueueResponse {
  date: string;         // server-computed "today". NEVER use new Date() instead.
  timezone: string;
  counts: QueueCounts;
  items: QueueItem[];
}

export interface DoneSummary {
  approved: number;
  snoozed: number;
  dismissed: number;
  total: number;
  /** true => MUS-41 celebration state; total === 0 => "nothing done yet" state. */
  queue_cleared: boolean;
  pipeline_value_usd: number;
  elapsed_seconds: number | null;
  first_action_at: string | null;
  last_action_at: string | null;
}

export interface DoneResponse {
  date: string;
  timezone: string;
  summary: DoneSummary;
  items: QueueItem[];
}

export interface EditCopyInput {
  copy: string | null;   // null = revert to suggested_copy
}

export interface VerifyCopyInput {
  copy: string;
}

export interface SnoozeInput {
  trigger: SnoozeTrigger;
  until: string | null;   // required (future ISO 8601) iff trigger === 'custom'
}

export interface DismissInput {
  reason: DismissReason;
}

// --- Agent trace (MUS-29) ----------------------------------------------------
// GET /api/outreach/{id}/trace/ — the persisted step log behind an agent-drafted
// action. 404 {"error": "no_agent_trace"} for single-shot actions.

export type AgentStepKind = 'llm_call' | 'tool_result' | 'final';

export interface AgentTraceStep {
  seq: number;
  kind: AgentStepKind;
  payload: Record<string, unknown>;
  created_at: string; // ISO 8601
}

export interface AgentTrace {
  action_id: number;
  lead_id: string;
  trace_run_id: string;
  status: string;
  steps_used: number;
  tool_calls_used: number;
  steps: AgentTraceStep[];
}

// --- Run Composer (MUS-47) ---------------------------------------------------
// The /compose funnel: scope (free) -> classify (free) -> read (paid, optional)
// -> generate (paid). Wire shapes are frozen in docs/contracts/run-composer.md.
//
// MONEY IS A STRING. DRF serializes DecimalField as a JSON string by default and
// this project does not override COERCE_DECIMAL_TO_STRING, so every USD field
// arrives as e.g. "0.1372". Parse with Number() at the edge; never assume a float.

export type RunStatus =
  | 'draft'
  | 'classified'
  | 'read'
  | 'generated'
  | 'completed'
  | 'discarded';

export type StageName = 'scope' | 'classify' | 'read' | 'generate';

export type EstimateStage = 'read' | 'generate';

export type SuggestionState = 'none' | 'proposed' | 'accepted' | 'rejected';

export type SuggestionKind = 'raise' | 'lower' | 'action_change' | 'none';

/** How a filter's value binds to its field. Drives chip rendering. */
export type FilterBound = 'gte' | 'lte' | 'exact' | 'days' | 'bool';

export type FilterKind = 'select' | 'int' | 'days' | 'bool';

/** One entry from GET /api/scopes/fields/. `key` is unique; `label` is NOT —
 *  book_min and book_max deliberately share the noun "book", and `bound` is what
 *  makes the chip read `book >= $50,000`. */
export interface ScopeField {
  key: string;
  label: string;
  bound: FilterBound;
  kind: FilterKind;
  choices: string[];
}

/** The filter set itself, as stored on a run and posted to preview-count. */
export type Scope = Record<string, string | number | boolean>;

export interface Chip {
  key: string;
  display: string; // mono; a machine predicate, e.g. "last_contacted > 14d"
  value: string | number | boolean;
}

export interface SuggestionEvidence {
  source: 'note' | 'event';
  quote: string; // the model's ORIGINAL text, not the normalized comparison form
}

/** All five keys are always present — placeholders, never absent keys, so no
 *  reader has to branch on `in`. */
export interface Suggestion {
  suggestion: SuggestionKind;
  proposed_priority: number | null;
  proposed_action: string;
  rationale: string;
  evidence: SuggestionEvidence[];
}

export interface RunLeadRow {
  lead_id: string;
  agency_name: string;
  contact_name: string;
  rules_priority: number;
  rules_action: string;
  rules_reason: string;
  effective_priority: number;
  effective_action: string;
  effective_reason: string;
  rule_trace: RuleTrace;
  already_queued: boolean;
  selected: boolean;
  suggestion: Suggestion | Record<string, never>; // {} when none was ever proposed
  suggestion_state: SuggestionState;
  suggestion_decided_at: string | null;
  suggestion_decided_by: string;
  generated_action_id: number | null;
  generation_error: string;
}

export interface RunDetail {
  id: number;
  status: RunStatus;
  scope: Scope;
  /** Rows in the run. THE zero-lead gate — status alone does not tell you. */
  lead_count: number;
  spread: Record<string, number> | null; // null until classified
  classify_ms: number | null;
  discarded_suggestions: number;
  read_provider: string;
  read_model: string;
  generate_provider: string;
  generate_model: string;
  read_cost_estimate_usd: string | null;
  read_cost_actual_usd: string | null;
  generate_cost_estimate_usd: string | null;
  generate_cost_actual_usd: string | null;
  created_at: string;
  created_by: string;
  finished_at: string | null;
  run_leads?: RunLeadRow[]; // only on GET /api/runs/{id}/ and /classify/
}

export interface Estimate {
  stage: EstimateStage;
  provider: string;
  model: string;
  /** Rows this stage will actually bill for: every row for a read, only the
   *  selected-and-not-already-queued rows for a generate. The two are different
   *  numbers, which is why formatEstimate is stage-aware. */
  lead_count: number;
  tokens_in_est: number;
  tokens_out_est: number;
  usd_est: string;
  is_estimate: boolean;
}

export interface GenerateResult {
  generated: number;
  failed: number;
  skipped: number;
  actual_usd: string;
}

export interface PreviewCount {
  count: number;
  total: number;
}

export interface SavedScope {
  id: number;
  name: string;
  filters: Scope;
  created_at: string;
  created_by: string;
}
