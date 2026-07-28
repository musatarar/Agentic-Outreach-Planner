/**
 * Mirrors the DRF serializers in project/app/serializers.py. The backend is a
 * frozen contract for this app — see CONTRACT.md.
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
