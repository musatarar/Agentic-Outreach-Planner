/** Mirrors the DRF serializers in project/app/serializers/ (frozen contract). */

export type Priority = 1 | 2 | 3;

/** Mirrors `LeadSummarySerializer` — the lead nested inside an outreach action. */
export interface Lead {
  // `Lead.id` is a CharField primary key ("lead_001"), not an integer. It was
  // typed `number` here and never caught, because every use is a Map or React
  // key and both are happy either way.
  id: string;
  agency_name: string;
  contact_name: string;
  contact_email: string;
}

/**
 * Mirrors `LeadSerializer` (`fields = "__all__"`) — what `GET /api/leads/`
 * returns. Distinct from `Lead` above, which is the compact nested form.
 *
 * `hubspot_notes` is lead-controlled text. It is carried here for completeness
 * but must never be interpolated into a prompt on this side of the wire — see
 * SECURITY.md.
 */
export interface LeadRecord {
  id: string;
  agency_name: string;
  contact_name: string;
  contact_email: string;
  contact_phone: string;
  state: string;
  num_producers: number;
  years_in_business: number;
  estimated_book_size_usd: number;
  stage: string;
  /** DRF DateFields: "YYYY-MM-DD", or null when never recorded. */
  signed_up_date: string | null;
  last_login_date: string | null;
  quotes_created: number;
  quotes_submitted: number;
  deals_closed: number;
  last_contacted_date: string | null;
  hubspot_notes: string;
}

export type Urgency = 'low' | 'medium' | 'high';

/** Mirrors `ProposedActionTypeSerializer` — the catalog action a rule chose. */
export interface ProposedActionType {
  key: string;
  label: string;
  urgency: Urgency;
}

/**
 * Mirrors `ProposedActionSerializer` — one action the engine already chose,
 * before any copy exists for it.
 *
 * `reasons` is the names of the rules that fired, flattened by the server: the
 * raw decision payload (rule ids, inference verdicts) never crosses the wire.
 * `draft_id` is the draft this proposal has already produced, or null while it
 * has none — generating for a proposal that has one answers 409.
 */
export interface ProposedAction {
  id: number;
  lead: Lead;
  action: ProposedActionType;
  reasons: string[];
  weight: number | null;
  decided_at: string | null;
  draft_id: number | null;
}

/** DRF's `PageNumberPagination` envelope. */
export interface Paginated<T> {
  count: number;
  next: string | null;
  previous: string | null;
  results: T[];
}

// ===== magic-link auth =============================================

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
  | 'invalid_reason'
  | 'validation_error'
  | 'not_authenticated'
  | 'csrf_failed'
  | 'not_found'
  | 'method_not_allowed'
  | 'invalid_transition'
  | 'no_new_recommendation'
  | 'unverified_claims'
  | 'rate_limited';

// ===== the review flow =============================================

export type ReviewStatus = 'pending' | 'approved' | 'dismissed';

export type DismissReason =
  | 'not_a_fit'
  | 'bad_timing'
  | 'wrong_contact'
  | 'already_handled'
  | 'copy_unusable'
  | 'other'
  | '';

// ---- verification spans (schema v1) ----

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

// ---- review items ----

export interface ReviewLeadEvent {
  type: string;
  timestamp: string;
  summary: string;
}

export interface ReviewLead {
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
  recent_events: ReviewLeadEvent[];   // max 5, newest first
}

/** Mirrors `ReviewItemSerializer`: the list and every mutation return this. */
export interface ReviewItem {
  id: number;
  status: ReviewStatus;
  status_changed_at: string | null;
  priority: Priority;
  action_type: string;
  action_label: string;
  reason: string;
  needs_human: boolean;
  further_action: string;
  created_at: string;
  dedupe_key: string;
  lead: ReviewLead;
  suggested_copy: string;   // IMMUTABLE
  edited_copy: string;      // "" when never edited
  effective_copy: string;   // edited_copy || suggested_copy -- use THIS
  is_edited: boolean;
  verification: VerificationReport;
  can_approve: boolean;
}

export interface EditCopyInput {
  copy: string | null;   // null = revert to suggested_copy
}

export interface VerifyCopyInput {
  copy: string;
}

export interface DismissInput {
  reason: DismissReason;
}
