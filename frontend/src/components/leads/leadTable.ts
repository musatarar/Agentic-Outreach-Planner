/**
 * Ordering and queue-flagging for the leads table.
 *
 * Kept apart from the JSX because both are decisions rather than presentation,
 * and a decision inside a render function is one nothing can test. See
 * `tests/leads-table.test.ts` for the behaviour each rule buys.
 */
import type { LeadRecord } from '../../api/types';

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
 * Lead ids that already have an open item in the triage queue. Composing for
 * these answers 409, so the table flags them rather than spending a call to
 * find out.
 *
 * Typed structurally rather than against `QueueItem` because the id is the only
 * field involved, and a narrower dependency is a cheaper one to satisfy.
 */
export function queuedLeadIds(items: readonly { lead: { id: string } }[]): Set<string> {
  return new Set(items.map((item) => item.lead.id));
}
