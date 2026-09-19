/**
 * The expanded row's decisions, kept out of the JSX.
 *
 * Two of them matter. Whether a proposal can still be drafted decides what the
 * button does, and getting it wrong spends a provider call that can only answer
 * 409. Recording the draft a click produced decides whether the row updates
 * without a refetch — an array mutated in place looks identical and re-renders
 * nothing. See `tests/leads-proposals.test.ts`.
 */
import type { BadgeTone } from '../ui';
import type { ProposedAction, Urgency } from '../../api/types';

/**
 * Drafting is offered only while the proposal has no draft. A drafted one is a
 * link into the inbox instead: the server refuses a second draft for the same
 * lead and action, so offering the button would only buy a 409.
 */
export function canGenerate(proposal: ProposedAction): boolean {
  return proposal.draft_id === null;
}

/**
 * The list with one proposal's new draft recorded — a new array, so React sees
 * the change. Unknown ids pass through: a stale click is not worth a crash.
 */
export function withDraft(
  proposals: readonly ProposedAction[],
  proposalId: number,
  draftId: number,
): ProposedAction[] {
  return proposals.map((proposal) =>
    proposal.id === proposalId ? { ...proposal, draft_id: draftId } : proposal,
  );
}

/**
 * Urgency reads on the same three-step ramp as inbox priority, and on purpose:
 * the server writes the draft's priority from this same urgency, so a proposal
 * must not change colour the moment it becomes a draft.
 */
export function urgencyTone(urgency: Urgency): BadgeTone {
  if (urgency === 'high') return 'p1';
  return urgency === 'medium' ? 'p2' : 'p3';
}
