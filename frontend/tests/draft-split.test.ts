/**
 * Splitting one verification report across the two fields the inbox renders.
 *
 * The verifier's offsets index the composed draft (`Subject: …\n\n…`), so the
 * split is arithmetic on those offsets, not a second set of them. Getting the
 * boundary wrong does not throw — it underlines the wrong words — so the cases
 * that can move it are pinned here.
 *
 * Run with `npm test`.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import type { VerificationClaim, VerificationReport } from '../src/api/types.ts';
import { composeDraft, draftBounds, splitDraftSegments } from '../src/components/inbox/spans.ts';

function claim(overrides: Partial<VerificationClaim>): VerificationClaim {
  return {
    id: 'c1',
    kind: 'figure',
    field: 'deals_closed',
    text: '',
    start: null,
    end: null,
    verified: true,
    message: '',
    counts_toward_summary: true,
    ...overrides,
  } as VerificationClaim;
}

function report(copy: string, claims: VerificationClaim[] = []): VerificationReport {
  return {
    version: 1,
    copy,
    claims,
    checked_count: claims.length,
    unverified_count: 0,
    can_approve: true,
    is_astral_safe: copy.length === [...copy].length,
  } as VerificationReport;
}

const DRAFT = 'Subject: Six closed deals\n\nYou closed 6 deals this quarter.';

test('the subject and the body are split at the composer�s blank line', () => {
  const parts = splitDraftSegments(report(DRAFT));
  assert.equal(parts.subject.map((s) => s.text).join(''), 'Six closed deals');
  assert.equal(parts.body.map((s) => s.text).join(''), 'You closed 6 deals this quarter.');
});

test('a claim in the body keeps the offsets the verifier gave it', () => {
  const start = DRAFT.indexOf('6 deals');
  const parts = splitDraftSegments(
    report(DRAFT, [claim({ text: '6 deals', start, end: start + '6 deals'.length })]),
  );
  const marked = parts.body.filter((segment) => segment.claim !== null);
  assert.deepEqual(
    marked.map((segment) => segment.text),
    ['6 deals'],
  );
  // And it stays out of the subject, which has its own claim-free run.
  assert.equal(parts.subject.every((segment) => segment.claim === null), true);
});

test('a claim in the subject is underlined there, not in the body', () => {
  const start = DRAFT.indexOf('Six closed deals');
  const parts = splitDraftSegments(
    report(DRAFT, [claim({ text: 'Six closed deals', start, end: start + 16 })]),
  );
  assert.deepEqual(
    parts.subject.filter((s) => s.claim !== null).map((s) => s.text),
    ['Six closed deals'],
  );
  assert.equal(parts.body.every((segment) => segment.claim === null), true);
});

test('a draft with no Subject line is all body', () => {
  const parts = splitDraftSegments(report('Hand written, no subject line.'));
  assert.deepEqual(parts.subject, []);
  assert.equal(parts.body.map((s) => s.text).join(''), 'Hand written, no subject line.');
});

test('a subject line with nothing under it leaves the body empty', () => {
  const parts = splitDraftSegments(report('Subject: Alone'));
  assert.equal(parts.subject.map((s) => s.text).join(''), 'Alone');
  assert.equal(parts.body.map((s) => s.text).join(''), '');
});

test('an emoji in the subject does not move the boundary off by a unit', () => {
  // The bug this pins: `indexOf` counts UTF-16 units while the claims count
  // code points, so an astral subject would cut the body one character early.
  const copy = 'Subject: Nice work \u{1F680}\n\nYou closed 6 deals.';
  const units = [...copy];
  const bounds = draftBounds(copy, units);
  assert.equal(units.slice(bounds.subjectStart, bounds.subjectEnd).join(''), 'Nice work \u{1F680}');
  assert.equal(units.slice(bounds.bodyStart).join(''), 'You closed 6 deals.');

  const parts = splitDraftSegments(report(copy));
  assert.equal(parts.body.map((s) => s.text).join(''), 'You closed 6 deals.');
});

/*
 * `composeDraft` is clipboard-only, but it has to agree with the server's
 * `compose_email`. These literals are asserted on both sides -- see
 * tests_generated_copy_parts.py::ComposeEmailTests -- so a drift in either
 * composer fails a test rather than showing up in a reviewer's paste.
 */
test('a pair composes to the draft the server would store', () => {
  assert.equal(
    composeDraft({ subject: 'Six closed deals', body: 'You closed 6 deals this quarter.' }),
    DRAFT,
  );
});

test('composing flattens a multi-line subject, as the server does', () => {
  assert.equal(
    composeDraft({ subject: 'Two\n\nlines', body: 'The body.' }),
    'Subject: Two lines\n\nThe body.',
  );
});

test('a composed pair splits back into the pair it came from', () => {
  const pair = { subject: 'Six closed deals', body: 'You closed 6 deals this quarter.' };
  const parts = splitDraftSegments(report(composeDraft(pair)));
  assert.equal(parts.subject.map((s) => s.text).join(''), pair.subject);
  assert.equal(parts.body.map((s) => s.text).join(''), pair.body);
});
