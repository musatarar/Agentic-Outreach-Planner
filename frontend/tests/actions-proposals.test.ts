/**
 * The proposals list's decisions.
 *
 * The failure modes are quiet. A Generate button offered on a proposal that
 * already has a draft still looks like a button; it just spends a click on a
 * 409. A list updated in place still holds the right data; the row simply never
 * repaints, so the same proposal invites the same wasted call again.
 *
 * Run with `npm test`.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  canGenerate,
  urgencyTone,
  withDraft,
} from '../src/components/actions/proposals.ts';
import type { ProposedAction } from '../src/api/types.ts';

/** A proposal with every field defaulted, so each test states only what it varies. */
function proposal(overrides: Partial<ProposedAction> = {}): ProposedAction {
  return {
    id: 42,
    lead: {
      id: 'lead_118',
      agency_name: 'Harbor Insurance',
      contact_name: 'Dana Ruiz',
      contact_email: 'dana@harbor.example',
    },
    action: { key: 'reward_power_user', label: 'Reward power user', urgency: 'high' },
    reasons: ['Closed 20+ deals'],
    weight: 4,
    decided_at: '2026-09-19T06:15:00Z',
    draft_id: null,
    ...overrides,
  };
}

test('a proposal with no draft yet can be generated', () => {
  assert.equal(canGenerate(proposal()), true);
});

test('a proposal that already has a draft cannot', () => {
  // The server answers 409 for this one, so the click buys nothing.
  assert.equal(canGenerate(proposal({ draft_id: 7 })), false);
});

test('recording a draft returns a new array and a new row', () => {
  const before = [proposal({ id: 1 }), proposal({ id: 2 })];

  const after = withDraft(before, 2, 99);

  assert.notEqual(after, before, 'should return a new array, not mutate in place');
  assert.notEqual(after[1], before[1], 'the changed row must be a new object');
  assert.equal(after[1]?.draft_id, 99);
  assert.equal(before[1]?.draft_id, null, 'the caller’s list must survive untouched');
});

test('recording a draft leaves every other proposal alone', () => {
  const before = [proposal({ id: 1 }), proposal({ id: 2 })];

  const after = withDraft(before, 2, 99);

  assert.equal(after[0], before[0], 'untouched rows should keep their identity');
});

test('an id no longer in the list passes through', () => {
  const before = [proposal({ id: 1 })];

  assert.deepEqual(withDraft(before, 404, 99), before);
});

test('urgency reads on the same ramp as inbox priority', () => {
  assert.equal(urgencyTone('high'), 'p1');
  assert.equal(urgencyTone('medium'), 'p2');
  assert.equal(urgencyTone('low'), 'p3');
});
