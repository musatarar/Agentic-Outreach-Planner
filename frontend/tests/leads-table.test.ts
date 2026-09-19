/**
 * Ordering, review-flagging and the proposal join for the leads table.
 *
 * These are pure functions on purpose. The table's three jobs before you have
 * opened anything — put the leads worth chasing at the top, mark the ones
 * already awaiting review, and show each lead what the engine chose for it —
 * are all decisions, and a decision buried in JSX is a decision nobody can
 * test.
 *
 * The failure modes here are quiet rather than loud. A book-size column sorted
 * as text puts $900k above $2M and still looks like a sorted column. A sort
 * that treats "never contacted" as the freshest possible date buries exactly
 * the leads the page exists to surface. Neither throws.
 *
 * Run with `npm test`.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  DEFAULT_SORT,
  openLeadIds,
  proposalsByLead,
  sortLeads,
} from '../src/components/leads/leadTable.ts';
import type { LeadRecord, ProposedAction } from '../src/api/types.ts';

/** A lead with every field defaulted, so each test states only what it varies. */
function lead(overrides: Partial<LeadRecord> = {}): LeadRecord {
  return {
    id: 'lead_001',
    agency_name: 'Acme Insurance',
    contact_name: 'Dana Reed',
    contact_email: 'dana@acme.example',
    contact_phone: '555-0100',
    state: 'TX',
    num_producers: 4,
    years_in_business: 9,
    estimated_book_size_usd: 1_000_000,
    stage: 'active_trial',
    signed_up_date: '2026-01-05',
    last_login_date: '2026-08-01',
    quotes_created: 3,
    quotes_submitted: 1,
    deals_closed: 0,
    last_contacted_date: '2026-08-01',
    hubspot_notes: '',
    ...overrides,
  };
}

const names = (leads: LeadRecord[]) => leads.map((entry) => entry.id);

test('sorting leaves the caller’s array untouched', () => {
  const input = [lead({ id: 'lead_002' }), lead({ id: 'lead_001' })];

  const sorted = sortLeads(input, 'agency_name', 'asc');

  assert.notEqual(sorted, input, 'should return a new array, not sort in place');
  assert.deepEqual(names(input), ['lead_002', 'lead_001'], 'input order must survive');
});

test('text columns sort case-insensitively', () => {
  // A plain `<` puts every capitalised name above every lowercase one, so
  // "acme" would sort after "Zenith" and the column would look shuffled.
  const leads = [
    lead({ id: 'lead_z', agency_name: 'Zenith Group' }),
    lead({ id: 'lead_a', agency_name: 'acme insurance' }),
  ];

  assert.deepEqual(names(sortLeads(leads, 'agency_name', 'asc')), ['lead_a', 'lead_z']);
});

test('book size sorts numerically, not as text', () => {
  // Sorted as strings, "2000000" < "900000" and the biggest book on the page
  // renders below the smallest.
  const leads = [
    lead({ id: 'lead_small', estimated_book_size_usd: 900_000 }),
    lead({ id: 'lead_big', estimated_book_size_usd: 2_000_000 }),
  ];

  assert.deepEqual(names(sortLeads(leads, 'estimated_book_size_usd', 'desc')), [
    'lead_big',
    'lead_small',
  ]);
});

test('never-contacted leads come first when sorting by last contact', () => {
  // The whole point of the default sort: nobody has ever reached out to these,
  // so they are the stalest rows on the page, not the newest.
  const leads = [
    lead({ id: 'lead_recent', last_contacted_date: '2026-08-10' }),
    lead({ id: 'lead_never', last_contacted_date: null }),
    lead({ id: 'lead_old', last_contacted_date: '2026-02-01' }),
  ];

  assert.deepEqual(names(sortLeads(leads, 'last_contacted_date', 'asc')), [
    'lead_never',
    'lead_old',
    'lead_recent',
  ]);
});

test('never-contacted leads move to the bottom when the sort is reversed', () => {
  // Nulls pinned to one end regardless of direction would mean clicking the
  // header never actually moves them, which reads as a broken control.
  const leads = [
    lead({ id: 'lead_recent', last_contacted_date: '2026-08-10' }),
    lead({ id: 'lead_never', last_contacted_date: null }),
    lead({ id: 'lead_old', last_contacted_date: '2026-02-01' }),
  ];

  assert.deepEqual(names(sortLeads(leads, 'last_contacted_date', 'desc')), [
    'lead_recent',
    'lead_old',
    'lead_never',
  ]);
});

test('ties break on lead id so row order is stable between renders', () => {
  // Every lead in this book shares a stage. Without a tie-break the order is
  // whatever the sort happens to do, and rows can swap under a checked box.
  const leads = [
    lead({ id: 'lead_003', stage: 'demo_completed' }),
    lead({ id: 'lead_001', stage: 'demo_completed' }),
    lead({ id: 'lead_002', stage: 'demo_completed' }),
  ];

  assert.deepEqual(names(sortLeads(leads, 'stage', 'asc')), [
    'lead_001',
    'lead_002',
    'lead_003',
  ]);
});

test('the default sort is stalest-contact-first', () => {
  assert.deepEqual(DEFAULT_SORT, { key: 'last_contacted_date', direction: 'asc' });
});

test('lead ids awaiting review are collected from the inbox items', () => {
  const open = openLeadIds([
    { status: 'pending', lead: { id: 'lead_002' } },
    { status: 'pending', lead: { id: 'lead_005' } },
  ]);

  assert.deepEqual([...open].sort(), ['lead_002', 'lead_005']);
});

// A decided lead is generable again, so flagging it would disable the one
// button that does anything for it.
test('a decided item does not flag its lead', () => {
  const open = openLeadIds([
    { status: 'approved', lead: { id: 'lead_002' } },
    { status: 'dismissed', lead: { id: 'lead_003' } },
    { status: 'pending', lead: { id: 'lead_004' } },
  ]);

  assert.deepEqual([...open], ['lead_004']);
});

test('an empty inbox flags nothing', () => {
  assert.equal(openLeadIds([]).size, 0);
});

/** A proposal with every field defaulted, so each test states only what it varies. */
function proposal(leadId: string, overrides: Partial<ProposedAction> = {}): ProposedAction {
  return {
    id: 7,
    lead: {
      id: leadId,
      agency_name: 'Acme Insurance',
      contact_name: 'Dana Reed',
      contact_email: 'dana@acme.example',
    },
    action: { key: 'reward_power_user', label: 'Reward power user', urgency: 'high' },
    reasons: ['Closed 20+ deals'],
    weight: 4,
    decided_at: '2026-09-19T06:15:00Z',
    draft_id: null,
    ...overrides,
  };
}

test('each lead finds its own proposal by id', () => {
  const byLead = proposalsByLead([proposal('lead_002'), proposal('lead_005', { id: 8 })]);

  assert.equal(byLead.get('lead_002')?.id, 7);
  assert.equal(byLead.get('lead_005')?.id, 8);
});

// The column reads "—" for these, and the row still expands to say so. A lookup
// that threw, or returned the wrong lead's decision, would be worse than blank.
test('a lead the engine chose nothing for has no proposal', () => {
  const byLead = proposalsByLead([proposal('lead_002')]);

  assert.equal(byLead.get('lead_999'), undefined);
  assert.equal(byLead.size, 1);
});

test('the newest decision wins when a lead has been judged more than once', () => {
  // The list arrives newest-decision-first, so the later job must not overwrite
  // the current one and show the row a decision the engine has moved past.
  const byLead = proposalsByLead([
    proposal('lead_002', { id: 9, decided_at: '2026-09-19T06:15:00Z' }),
    proposal('lead_002', { id: 3, decided_at: '2026-09-01T06:15:00Z' }),
  ]);

  assert.equal(byLead.get('lead_002')?.id, 9);
});

test('no proposals at all is an empty map, not a crash', () => {
  assert.equal(proposalsByLead([]).size, 0);
});
