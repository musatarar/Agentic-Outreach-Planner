/**
 * Composer stage 01 (MUS-47): the scope editor's chip <-> scope round trip.
 *
 * The composer's first screen is a filter bar, and a filter bar is two pure
 * functions with a React shell around them: a scope object comes back from the
 * server (or off a SavedScope) and has to render as chips, and the chips a
 * reviewer edits have to serialize back to the exact JSON `POST /api/runs/`
 * will accept. `components/compose/scopeChips.ts` is that pair, extracted from
 * the page so it can be tested at all -- node:test strips types but cannot
 * transform JSX, the same seam `util/trace.ts` exists for.
 *
 * `Scope`, `ScopeField` and `Chip` are imported from `api/types.ts` rather than
 * re-declared here, so a change to the wire shape lands as a type error in this
 * file instead of as a fixture that quietly describes a catalog the server no
 * longer serves.
 *
 * Three design commitments are under test, not just formatting:
 *
 * 1. The field catalog (`GET /api/scopes/fields/`, a projection of the scope
 *    engine's FILTERABLE map) is the only thing that can put a filter on
 *    screen. A key the catalog does not carry is dropped, never rendered as a
 *    raw key -- the backend answers an unknown key with 400 `unknown_filter`,
 *    so a chip the FE invents is a request that cannot succeed.
 * 2. `key` is unique in that catalog and `label` deliberately is not. A chip is
 *    composed from label + bound, which is what lets `book_min` and `book_max`
 *    both print the noun "book" and still read as opposite filters.
 * 3. A chip carries its own `value`; `chipsToScope` reads that field and never
 *    re-parses `display`. Display is a one-way rendering of a mono string for
 *    humans, and a round trip that went back through a parser would make every
 *    formatting decision below load-bearing for correctness.
 *
 * Planted red by the skeleton PR: `scopeChips.ts` does not exist yet, so every
 * import of it is *inside* a test body. A top-level `await import()` of a
 * missing module throws at load and takes the whole file's suite down with it,
 * including the sibling tests -- `{ todo: true }` only tolerates a failing
 * test, not a failing module.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import type { Chip, Scope, ScopeField } from '../src/api/types.ts';

interface ScopeChipsModule {
  scopeToChips(scope: Scope, fields: ScopeField[]): Chip[];
  chipsToScope(chips: Chip[]): Scope;
}

// Typed as `string`, not a literal, so tsc cannot try to resolve a module the
// scope component has not written yet. The resolution that matters happens at
// runtime, relative to this file, exactly as a literal specifier would.
const SCOPE_CHIPS: string = '../src/components/compose/scopeChips.ts';

const loadScopeChips = async (): Promise<ScopeChipsModule> =>
  (await import(SCOPE_CHIPS)) as ScopeChipsModule;

/**
 * The whole catalog, in the order `scope_field_catalog()` serves it -- all
 * seventeen contract keys, including every `_max` twin, because the twins are
 * the case the label rule exists for. `label` is the mono field token the chip
 * prints verbatim: the FE never rewrites the server's noun, it only supplies
 * the operator and formats the value. "has notes" carries a space on purpose,
 * so nothing here can assume a label is a single token.
 */
const FIELDS: ScopeField[] = [
  { key: 'stage', label: 'stage', bound: 'exact', kind: 'select', choices: ['active_trial', 'demo_completed'] },
  { key: 'state', label: 'state', bound: 'exact', kind: 'select', choices: ['CA', 'NY', 'TX'] },
  { key: 'book_min', label: 'book', bound: 'gte', kind: 'int', choices: [] },
  { key: 'book_max', label: 'book', bound: 'lte', kind: 'int', choices: [] },
  { key: 'producers_min', label: 'producers', bound: 'gte', kind: 'int', choices: [] },
  { key: 'producers_max', label: 'producers', bound: 'lte', kind: 'int', choices: [] },
  { key: 'years_min', label: 'years', bound: 'gte', kind: 'int', choices: [] },
  { key: 'quotes_created_min', label: 'quotes_created', bound: 'gte', kind: 'int', choices: [] },
  { key: 'quotes_created_max', label: 'quotes_created', bound: 'lte', kind: 'int', choices: [] },
  { key: 'quotes_submitted_min', label: 'quotes_submitted', bound: 'gte', kind: 'int', choices: [] },
  { key: 'quotes_submitted_max', label: 'quotes_submitted', bound: 'lte', kind: 'int', choices: [] },
  { key: 'deals_min', label: 'deals', bound: 'gte', kind: 'int', choices: [] },
  { key: 'deals_max', label: 'deals', bound: 'lte', kind: 'int', choices: [] },
  { key: 'last_contacted_gt_days', label: 'last_contacted', bound: 'days', kind: 'days', choices: [] },
  { key: 'signed_up_within_days', label: 'signed_up', bound: 'days', kind: 'days', choices: [] },
  { key: 'dormant_days', label: 'dormant', bound: 'days', kind: 'days', choices: [] },
  { key: 'has_notes', label: 'has notes', bound: 'bool', kind: 'bool', choices: [] },
];

/** The five stems that ship both a `_min` and a `_max`, sharing one label. */
const TWIN_STEMS = ['book', 'producers', 'quotes_created', 'quotes_submitted', 'deals'];

const labelOf = (key: string): string => {
  const field = FIELDS.find((entry) => entry.key === key);
  if (!field) throw new Error(`fixture has no catalog entry for ${key}`);
  return field.label;
};

test('every scope key renders one mono chip, operator and unit from the field', { todo: true }, async () => {
  const { scopeToChips } = await loadScopeChips();

  const chips = scopeToChips(
    {
      stage: 'active_trial',
      state: 'TX',
      book_min: 50000,
      producers_min: 3,
      deals_max: 1200,
      last_contacted_gt_days: 14,
      signed_up_within_days: 30,
      dormant_days: 60,
      has_notes: true,
    },
    FIELDS,
  );

  // One chip per key -- no key silently merged into a neighbour's chip.
  assert.equal(chips.length, 9);

  // The operator is the field's `bound`, never a guess from the value or a
  // string match on the key: `gte` -> ">=", `lte` -> "<=", `exact` -> "=", and
  // `bool` prints no operator at all. `days` is the one bound that does not name
  // its own comparison -- all three day filters share it and the backend gives
  // them different edges (`last_contacted_gt_days` and `dormant_days` are
  // strictly greater, `signed_up_within_days` is a closed window), so a days
  // chip takes its edge from the key and prints the unit "d". Reads like the
  // MUS-42 rule-trace `display` lines on purpose: same font, same grammar.
  assert.deepEqual(
    chips.map((chip) => chip.display),
    [
      'stage = active_trial', // select prints the stored enum token, not a prettified label
      'state = TX',
      'book >= $50,000', // grouped with a HARD-CODED en-US separator; a bare
      'producers >= 3', // toLocaleString() renders "50.000" under a CI locale
      'deals <= 1,200', // grouping is for every int, the "$" only for the money pair
      'last_contacted > 14d',
      'signed_up <= 30d',
      'dormant > 60d', // strictly greater, matching apply_scope's dormancy cutoff
      'has notes', // a true bool is the label alone -- no operator, no "= true"
    ],
  );

  // The negative bool prefixes rather than inventing an antonym: the FE has no
  // authority to turn the catalog's "has notes" into "no notes".
  const [notes] = scopeToChips({ has_notes: false }, FIELDS);
  assert.equal(notes.display, 'not has notes');
});

test('min/max twins share one label and are told apart by the bound', { todo: true }, async () => {
  const { scopeToChips, chipsToScope } = await loadScopeChips();

  // The contract's worked example, and the reason catalog uniqueness is pinned
  // on `key` and never on `label`: a renderer that identified a field by its
  // label would collide these two, and a catalog forbidden from repeating a
  // label would have to ship "book min", printing the bound twice and once badly.
  assert.equal(labelOf('book_min'), 'book');
  assert.equal(labelOf('book_max'), 'book');

  const [low, high] = scopeToChips({ book_min: 50000, book_max: 250000 }, FIELDS);
  assert.equal(low.display, 'book >= $50,000');
  assert.equal(high.display, 'book <= $250,000');
  assert.notEqual(low.display, high.display); // one label, two readable filters

  // Both survive as their own keys, so the band the reviewer drew is the band
  // POST /api/runs/ receives -- not one half of it.
  assert.deepEqual(chipsToScope([low, high]), { book_min: 50000, book_max: 250000 });

  // The same rule across the rest of the catalog, asserted structurally so it
  // covers stems this file does not spell a display string for.
  for (const stem of TWIN_STEMS) {
    const label = labelOf(`${stem}_min`);
    assert.equal(labelOf(`${stem}_max`), label);

    const chips = scopeToChips({ [`${stem}_min`]: 2, [`${stem}_max`]: 9 }, FIELDS);
    assert.deepEqual(
      chips.map((chip) => chip.key),
      [`${stem}_min`, `${stem}_max`],
    );
    assert.ok(chips[0].display.startsWith(`${label} >= `), `${stem}_min renders ">=" after the label`);
    assert.ok(chips[1].display.startsWith(`${label} <= `), `${stem}_max renders "<=" after the label`);
  }
});

test('chipsToScope(scopeToChips(s)) is s, for a scope using every kind', { todo: true }, async () => {
  const { scopeToChips, chipsToScope } = await loadScopeChips();

  // select, int, days, bool -- one of each, so no kind can round trip by
  // accident through another's branch.
  const scope: Scope = {
    stage: 'active_trial',
    book_min: 50000,
    last_contacted_gt_days: 14,
    has_notes: true,
  };
  assert.deepEqual(chipsToScope(scopeToChips(scope, FIELDS)), scope);

  // deepEqual is strict, so this also pins the types: a chip that stringified
  // its number would come back "50000" and POST a value the validator has to
  // coerce, and a bool that came back "true" is a different filter entirely.
  const [, book, days, notes] = scopeToChips(scope, FIELDS);
  assert.equal(book.value, 50000);
  assert.equal(days.value, 14);
  assert.equal(notes.value, true);

  // Falsy values are values. `has_notes: false` is "leads with no notes" and
  // `book_min: 0` is an explicit floor -- both vanish under an `if (!value)`.
  const falsy: Scope = { book_min: 0, has_notes: false };
  assert.deepEqual(chipsToScope(scopeToChips(falsy, FIELDS)), falsy);
});

test('an empty scope produces zero chips', { todo: true }, async () => {
  const { scopeToChips, chipsToScope } = await loadScopeChips();

  // The caller renders "all leads" off an empty array, so this must be a real
  // empty array -- not one placeholder chip, and not null.
  assert.deepEqual(scopeToChips({}, FIELDS), []);
  // And the way back: no chips is an unscoped run, which the API accepts.
  assert.deepEqual(chipsToScope([]), {});
});

test('a key the field catalog does not carry is dropped, not rendered raw', { todo: true }, async () => {
  const { scopeToChips, chipsToScope } = await loadScopeChips();

  const chips = scopeToChips(
    {
      stage: 'active_trial',
      book_mim: 50000, // typo: close enough to look right in a saved scope
      hasNotes: true, // camelCase: the API speaks snake_case only
      lead__id__in: '1', // ORM-shaped: the backend never **kwargs a scope into filter()
    },
    FIELDS,
  );

  assert.deepEqual(
    chips.map((chip) => chip.key),
    ['stage'],
  );
  // And the drop is permanent: an unknown key must not survive the round trip
  // back into a request body, or the next POST /api/runs/ 400s `unknown_filter`
  // on a chip the reviewer never typed.
  assert.deepEqual(chipsToScope(chips), { stage: 'active_trial' });
});

test('chip order follows the catalog, not the scope object key order', { todo: true }, async () => {
  const { scopeToChips } = await loadScopeChips();

  // Insertion order here is the exact reverse of FIELDS. A scope arriving from
  // the server, from a SavedScope row, or from JSON.parse has whatever key
  // order it was serialized with; the chip bar must not reshuffle itself when
  // the same filters come back from a different source.
  const chips = scopeToChips(
    {
      has_notes: true,
      dormant_days: 60,
      book_min: 50000,
      stage: 'active_trial',
    },
    FIELDS,
  );

  assert.deepEqual(
    chips.map((chip) => chip.key),
    ['stage', 'book_min', 'dormant_days', 'has_notes'],
  );

  // Same scope, opposite insertion order, identical output -- ordering is a
  // property of the catalog alone.
  const rebuilt = scopeToChips(
    {
      stage: 'active_trial',
      book_min: 50000,
      dormant_days: 60,
      has_notes: true,
    },
    FIELDS,
  );
  assert.deepEqual(
    rebuilt.map((chip) => chip.display),
    chips.map((chip) => chip.display),
  );
});
