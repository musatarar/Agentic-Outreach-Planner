/**
 * Display formatters for the leads table.
 *
 * `formatDateOnly` exists because the obvious implementation is wrong in a way
 * that never throws. `new Date('2026-08-01')` is parsed as UTC midnight, so
 * `toLocaleDateString()` renders it as July 31 anywhere west of Greenwich —
 * every "last contacted" date on the page silently a day early. These are
 * date-only strings from a DRF DateField, so they are formatted as text and
 * never handed to `Date` at all.
 *
 * Run with `npm test`.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { formatDateOnly, formatStage, formatUsdCompact } from '../src/util/labels.ts';

test('a date-only string is not shifted a day by timezone parsing', () => {
  // The bug this pins: 'Jul 31, 2026' anywhere with a negative UTC offset.
  assert.equal(formatDateOnly('2026-08-01'), 'Aug 1, 2026');
});

test('a never-contacted lead reads as Never, not as a blank cell', () => {
  // Blank would be indistinguishable from a rendering failure, and this is the
  // single most decision-relevant value in the column.
  assert.equal(formatDateOnly(null), 'Never');
});

test('book sizes in the millions are abbreviated', () => {
  assert.equal(formatUsdCompact(2_000_000), '$2M');
  assert.equal(formatUsdCompact(1_250_000), '$1.3M');
});

test('book sizes under a million are abbreviated in thousands', () => {
  assert.equal(formatUsdCompact(900_000), '$900K');
});

test('a zero book size renders as a number, not as an empty cell', () => {
  assert.equal(formatUsdCompact(0), '$0');
});

test('pipeline stages read as prose', () => {
  assert.equal(formatStage('active_trial'), 'Active trial');
  assert.equal(formatStage('demo_completed'), 'Demo completed');
});

test('an unrecognised stage is shown as-is rather than blanked', () => {
  // The backend is free to add stages; a new one must degrade to something
  // readable instead of rendering an empty cell nobody can interpret.
  assert.equal(formatStage('renewal_pending'), 'Renewal pending');
});
