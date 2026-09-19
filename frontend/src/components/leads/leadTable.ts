/**
 * Ordering, review-flagging and the proposal join for the leads table.
 *
 * Kept apart from the JSX because both are decisions rather than presentation,
 * and a decision inside a render function is one nothing can test. See
 * `tests/leads-table.test.ts` for the behaviour each rule buys.
 */
import type { LeadRecord, ProposedAction } from '../../api/types';

export type SortKey =
  | 'agency_name'
  | 'contact_name'
  | 'stage'
  | 'estimated_book_size_usd'
  | 'last_contacted_date';

export type SortDirection = 'asc' | 'desc';

export interface SortState {
  key: SortKey;
  direction: SortDirection;
}

/**
 * Stalest contact first. This page exists to answer "who has gone quiet?", and
 * that is the row order which answers it without touching a control.
 */
export const DEFAULT_SORT: SortState = {
  key: 'last_contacted_date',
  direction: 'asc',
};

/**
 * A missing date sorts below every real one, so ascending puts never-contacted
 * leads at the top — they are the stalest rows on the page, not the freshest.
 * Because it is an ordering rule rather than a pinned position, reversing the
 * column moves them to the bottom like anything else.
 */
function compareValues(left: string | number | null, right: string | number | null): number {
  if (left === null || left === '') return right === null || right === '' ? 0 : -1;
  if (right === null || right === '') return 1;
  if (typeof left === 'number' && typeof right === 'number') return left - right;
  // Case-insensitive: a plain `<` files every capitalised agency above every
  // lowercase one, which reads as an unsorted column.
  return String(left).localeCompare(String(right), undefined, { sensitivity: 'base' });
}

/** Sorted copy — the caller's array is never reordered in place. */
export function sortLeads(
  leads: readonly LeadRecord[],
  key: SortKey,
  direction: SortDirection,
): LeadRecord[] {
  return [...leads].sort((a, b) => {
    const primary = compareValues(a[key], b[key]);
    if (primary !== 0) return direction === 'desc' ? -primary : primary;
    // Ties always break ascending by id, in both directions: it is what keeps
    // rows from swapping under a checked box when the table re-renders.
    return a.id.localeCompare(b.id);
  });
}

/**
 * Lead ids whose latest recommendation is still awaiting a decision. Composing
 * for these answers 409, so the table flags them rather than spending a call to
 * find out. A decided item does not flag its lead: the planner will happily
 * recommend again once the last one is approved.
 *
 * Typed structurally rather than against `ReviewItem` because the status and
 * the id are the only fields involved, and a narrower dependency is a cheaper
 * one to satisfy.
 */
export function openLeadIds(
  items: readonly { status: string; lead: { id: string } }[],
): Set<string> {
  return new Set(
    items.filter((item) => item.status === 'pending').map((item) => item.lead.id),
  );
}

/**
 * Each lead's proposal, keyed by lead id, so a row finds its decision without
 * scanning the list once per render.
 *
 * A lead the engine has not chosen an action for is simply absent: the list
 * only carries jobs that reached a decision, and "no action", "still queued"
 * and "never judged" are all the same blank from here.
 */
export function proposalsByLead(
  proposals: readonly ProposedAction[],
): Map<string, ProposedAction> {
  // Newest decision first, so the first proposal for a lead is the one to keep.
  const byLead = new Map<string, ProposedAction>();
  for (const proposal of proposals) {
    if (!byLead.has(proposal.lead.id)) byLead.set(proposal.lead.id, proposal);
  }
  return byLead;
}

/** Anything in a row that answers a click itself: the email link, the toggle. */
interface ClickTarget {
  closest(selector: string): unknown;
}

/**
 * Whether a click on a row was meant for the row.
 *
 * The whole row opens the lead, but it carries controls of its own — the
 * contact's mailto link, and the disclosure button that already toggles. A
 * click landing on one of those belongs to it: without this the email link
 * would open a mail client *and* expand the row, and the button would toggle
 * twice and appear dead.
 */
export function isRowBackgroundClick(target: ClickTarget | null): boolean {
  return target !== null && target.closest('a, button') === null;
}
