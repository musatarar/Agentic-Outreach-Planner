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
