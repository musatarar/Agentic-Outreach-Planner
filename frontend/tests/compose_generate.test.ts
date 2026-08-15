/**
 * Stage 04's two pure modules (MUS-47): who a run generates for
 * (`components/compose/selection.ts`) and what that will cost, shown before the
 * button that spends it (`components/compose/estimateLine.ts`).
 *
 * Both are plain functions for the same reason `util/trace.ts` is — the harness
 * is `node --test` with no jsdom and no Testing Library, so `SelectionTable`'s
 * React shell is review-verified while the rules it renders are pinned here.
 *
 * Four of these are product promises rather than bookkeeping:
 *
 * - **The bulk selects follow `effective_priority`.** A row a reviewer raised
 *   from P3 to P1 at stage 03 has to appear under "All P1", and a row they
 *   lowered has to stay out of it. Selecting on `rules_priority` would discard
 *   every decision the read stage was paid for — and it would fail invisibly,
 *   because the two columns agree on almost every row.
 * - **`already_queued` is unselectable, full stop.** No bulk action reaches such
 *   a row and a direct click on one does nothing. That rule is the only thing
 *   between an operator and two identical drafts of the same recommendation
 *   sitting in the same triage queue.
 * - **`formatEstimate` is stage-aware.** `Estimate.lead_count` is "the rows this
 *   stage will actually bill for", and that is a different population in each
 *   stage: a read prices every `RunLead` in the run (there is no selection to
 *   respect yet), a generate prices only `selected=True, already_queued=False`.
 *   So a read renders "31 leads × Haiku 4.5 ≈ $0.02" and a generate renders
 *   "12 selected × Opus 5 ≈ $0.14". Printing "selected" over a read estimate
 *   would tell the operator they had picked 31 leads they never touched.
 * - **A price is visible before the paid button.** `formatEstimate` never prints
 *   a bare number, and its zero state is a prompt to select rather than a
 *   confident `$0.00` for a run nobody has scoped yet.
 *
 * MONEY IS A STRING. `usd_est` and `actual_usd` are DRF `DecimalField`s, and this
 * project does not set `COERCE_DECIMAL_TO_STRING`, so DRF's default applies and
 * both arrive as JSON strings — `"0.1372"`, never `0.1372` (the contract's
 * "Money on the wire is a string"). The fixtures below are typed against
 * `api/types.ts` and seeded with strings for exactly that reason: `formatEstimate`
 * does the `Number()` parse itself, at the edge, and formats from the number. A
 * formatter written against a `number` type-checks perfectly and then throws or
 * prints `$NaN` against the real API, which is the bug this file is shaped to
 * catch before anyone ships it.
 *
 * RED SKELETON. Every test is `{ todo: true }`, and neither module exists yet —
 * so each import happens INSIDE its test body. A top-level `await import()` of a
 * missing module is a load error, which takes down the whole file instead of
 * failing the todo tests that own it. The type import below is exempt on both
 * counts: `api/types.ts` exists and is frozen, and `import type` is erased before
 * anything tries to resolve it.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import type {
  Estimate,
  GenerateResult,
  RuleTrace,
  RunLeadRow,
  Suggestion,
  SuggestionKind,
} from '../src/api/types.ts';

/**
 * Annotated `string`, not left as literals, so tsc treats the dynamic specifiers
 * as `any` instead of trying to resolve files the skeleton has not written. The
 * resolution that matters happens at runtime, relative to this file, exactly as
 * a literal specifier would.
 */
const SELECTION_MODULE: string = '../src/components/compose/selection.ts';
const ESTIMATE_MODULE: string = '../src/components/compose/estimateLine.ts';

/**
 * Typed locally: `SelectionState`/`SelectionAction` land in the module with it.
 * The rows they carry are the frozen `RunLeadRow`, not a convenient subset, so a
 * serializer change breaks this file at compile time rather than in the browser
 * — and `rules_priority` rides along on every row precisely so a reducer that
 * reads the wrong column has something to get wrong.
 */
interface StateLike {
  /** Display order: effective priority ascending, then lead id. */
  rows: RunLeadRow[];
  /** The POST /api/runs/{id}/select/ body, in `rows` order — never click order. */
  selectedIds: string[];
}

type ActionLike =
  | { type: 'load'; rows: RunLeadRow[] }
  | { type: 'toggle'; leadId: string }
  | { type: 'select_all' }
  | { type: 'select_none' }
  | { type: 'select_up_to'; priority: number };

interface SelectionModule {
  selectionReducer(state: StateLike, action: ActionLike): StateLike;
}

interface EstimateModule {
  formatEstimate(e: Estimate): string;
}

const loadSelection = async (): Promise<SelectionModule> =>
  (await import(SELECTION_MODULE)) as SelectionModule;

const loadEstimateLine = async (): Promise<EstimateModule> =>
  (await import(ESTIMATE_MODULE)) as EstimateModule;

// --- fixtures ---------------------------------------------------------------

/**
 * `outreach.explain()`'s schema-v1 envelope — the real keys, not a lookalike.
 * The reducer reads none of it; a row is the wire shape the page was handed, and
 * `rule_trace` has exactly one schema across MUS-42, MUS-47 and MUS-40.
 */
const RULE_TRACE: RuleTrace = {
  version: 1,
  today: '2026-02-15',
  generated_at: '2026-02-15T09:00:00Z',
  priority: {
    value: 2,
    score: 4,
    bands: [
      { priority: 1, min_score: 6 },
      { priority: 2, min_score: 3 },
    ],
    signals: [],
  },
  action: {
    // From `project/app/services/actions.py`, never invented.
    value: 'nudge_usage',
    rule_id: 'underusing_portal',
    rule_label: 'Active but underusing the portal',
    matched_rule_index: 0,
    conditions: [],
    rejected_rules: [],
  },
};

/** A serialized `RunLead` row, as `GET /api/runs/{id}/` hands it to the page. */
function row(leadId: string, over: Partial<RunLeadRow> = {}): RunLeadRow {
  return {
    lead_id: leadId,
    agency_name: `Agency ${leadId}`,
    contact_name: 'Dana Reyes',
    rules_priority: 2,
    rules_action: 'nudge_usage',
    rules_reason: 'Active but underusing the portal.',
    effective_priority: 2,
    effective_action: 'nudge_usage',
    effective_reason: 'Active but underusing the portal.',
    rule_trace: RULE_TRACE,
    already_queued: false,
    selected: false,
    suggestion: {},
    suggestion_state: 'none',
    suggestion_decided_at: null,
    suggestion_decided_by: '',
    generated_action_id: null,
    generation_error: '',
    ...over,
  };
}

/**
 * The persisted five-key suggestion shape (contract, "read"): explicit
 * placeholders, never absent keys. Only the two rows whose effective priority
 * disagrees with their rules priority carry one, because an accepted suggestion
 * is the only thing that can move those columns apart.
 */
const decided = (kind: SuggestionKind, priority: number): Suggestion => ({
  suggestion: kind,
  proposed_priority: priority,
  proposed_action: '', // a priority move proposes no action; "" is the placeholder
  rationale: 'Renewal is three weeks out and nobody has logged in since April.',
  evidence: [{ source: 'note', quote: 'renewal is in three weeks' }],
});

/** What `useReducer` is initialised with, before any run has been classified. */
const EMPTY: StateLike = { rows: [], selectedIds: [] };

/**
 * One classified run's rows, deliberately NOT in display order, and deliberately
 * disagreeing with themselves: `lead_004` was raised 3 → 1 by an accepted
 * suggestion, `lead_019` was lowered 1 → 3, and `lead_002` is a P1 whose
 * recommendation is already sitting open in somebody's queue.
 */
const ROWS: RunLeadRow[] = [
  row('lead_030'),
  row('lead_004', {
    rules_priority: 3,
    effective_priority: 1,
    suggestion: decided('raise', 1),
    suggestion_state: 'accepted',
    suggestion_decided_by: 'reviewer@example.com',
    suggestion_decided_at: '2026-02-15T09:20:00Z',
  }),
  row('lead_019', {
    rules_priority: 1,
    effective_priority: 3,
    suggestion: decided('lower', 3),
    suggestion_state: 'accepted',
    suggestion_decided_by: 'reviewer@example.com',
    suggestion_decided_at: '2026-02-15T09:21:00Z',
  }),
  row('lead_002', { rules_priority: 1, effective_priority: 1, already_queued: true }),
  row('lead_011'),
];

/** `ROWS` after `load`, which every selection test starts from. */
const classified = async (): Promise<StateLike> => {
  const { selectionReducer } = await loadSelection();
  return selectionReducer(EMPTY, { type: 'load', rows: ROWS });
};

/**
 * A `GET /api/runs/{id}/estimate/` envelope, typed as the frozen wire shape.
 * `usd_est` is a STRING here because it is a string on the wire — DRF quantizes
 * the Decimal to 4dp and serializes it as `"0.1372"`. Each case pushes in only
 * the fields the line renders.
 */
const estimate = (over: Partial<Estimate>): Estimate => ({
  stage: 'generate',
  provider: 'claude',
  model: 'Opus 5',
  lead_count: 12,
  tokens_in_est: 18_400,
  tokens_out_est: 3_120,
  usd_est: '0.1372',
  is_estimate: true,
  ...over,
});

// --- selection --------------------------------------------------------------

test('rows sort by effective priority, then by lead id', { todo: true }, async () => {
  const state = await classified();

  // Sorting on rules_priority would put lead_019 second and lead_004 last; the
  // index `rl_selection_order` is (run, effective_priority, lead) for this reason.
  assert.deepEqual(
    state.rows.map((r) => r.lead_id),
    ['lead_002', 'lead_004', 'lead_011', 'lead_030', 'lead_019'],
  );

  // Lead id is the tiebreak, so two P2 rows never swap places between renders.
  assert.deepEqual(
    state.rows.map((r) => r.effective_priority),
    [1, 1, 2, 2, 3],
  );

  // RunLead.selected defaults to False: a freshly classified run selects nothing,
  // and the reviewer spends nothing until they say so.
  assert.deepEqual(state.selectedIds, []);

  // The fetched array is sorted into a copy. An in-place .sort() would reorder
  // the response object React is still holding, under a component mid-render.
  assert.equal(ROWS[0].lead_id, 'lead_030');
});

test('toggling a row selects it, and toggling it again lets it go', { todo: true }, async () => {
  const { selectionReducer } = await loadSelection();
  const state = await classified();

  const one = selectionReducer(state, { type: 'toggle', leadId: 'lead_030' });
  assert.deepEqual(one.selectedIds, ['lead_030']);

  // Clicked second, but listed first: selectedIds follows row order, so the
  // rendered ticks and the POSTed lead_ids can never disagree about ordering.
  const two = selectionReducer(one, { type: 'toggle', leadId: 'lead_004' });
  assert.deepEqual(two.selectedIds, ['lead_004', 'lead_030']);

  const back = selectionReducer(two, { type: 'toggle', leadId: 'lead_004' });
  assert.deepEqual(back.selectedIds, ['lead_030']);

  // Toggling never disturbs the table underneath it.
  assert.deepEqual(
    back.rows.map((r) => r.lead_id),
    ['lead_002', 'lead_004', 'lead_011', 'lead_030', 'lead_019'],
  );
});

test('"select all" takes every row the run can still generate for', { todo: true }, async () => {
  const { selectionReducer } = await loadSelection();
  const all = selectionReducer(await classified(), { type: 'select_all' });

  // Four of five rows: lead_002 is already_queued and "all" does not mean it.
  assert.deepEqual(all.selectedIds, ['lead_004', 'lead_011', 'lead_030', 'lead_019']);
});

test('"select none" clears the selection and leaves the rows sorted', { todo: true }, async () => {
  const { selectionReducer } = await loadSelection();
  const all = selectionReducer(await classified(), { type: 'select_all' });
  const none = selectionReducer(all, { type: 'select_none' });

  assert.deepEqual(none.selectedIds, []);
  // Clearing a selection is not reloading a run — the table must survive it.
  assert.deepEqual(
    none.rows.map((r) => r.lead_id),
    ['lead_002', 'lead_004', 'lead_011', 'lead_030', 'lead_019'],
  );
});

test(
  'the P1 and P1+P2 bulk selects follow effective priority, not rules priority',
  { todo: true },
  async () => {
    const { selectionReducer } = await loadSelection();
    const state = await classified();

    // "All P1". lead_004 is rules P3 and effective P1 — an accepted "raise" — and
    // it MUST be here. lead_019 is rules P1 and effective P3 — an accepted
    // "lower" — and it MUST NOT be. Both directions in one assertion, because a
    // reducer reading the wrong column gets both of them wrong at once.
    const p1 = selectionReducer(state, { type: 'select_up_to', priority: 1 });
    assert.deepEqual(p1.selectedIds, ['lead_004']);

    // "All P1+P2" widens by effective priority too, and lead_019 stays out of it.
    const p1p2 = selectionReducer(state, { type: 'select_up_to', priority: 2 });
    assert.deepEqual(p1p2.selectedIds, ['lead_004', 'lead_011', 'lead_030']);

    // Each bulk select replaces the selection rather than adding to it, so
    // clicking "All P1" after "All P1+P2" narrows down instead of doing nothing.
    const narrowed = selectionReducer(p1p2, { type: 'select_up_to', priority: 1 });
    assert.deepEqual(narrowed.selectedIds, ['lead_004']);
  },
);

test('an already-queued row cannot be selected, by bulk or by hand', { todo: true }, async () => {
  const { selectionReducer } = await loadSelection();
  const state = await classified();

  // lead_002 is effective P1 and would qualify for both bulk selects on priority
  // alone. already_queued outranks priority in every one of them.
  for (const action of [
    { type: 'select_all' } as const,
    { type: 'select_up_to', priority: 1 } as const,
    { type: 'select_up_to', priority: 2 } as const,
  ]) {
    const next = selectionReducer(state, action);
    assert.equal(
      next.selectedIds.includes('lead_002'),
      false,
      `${action.type} must not reach an already_queued row`,
    );
  }

  // And the row's own checkbox is inert: clicking it is a no-op, not a toggle.
  // An open OutreachAction already carries this dedupe_key, so a second draft
  // would land in the same triage queue as a duplicate of itself.
  const clicked = selectionReducer(state, { type: 'toggle', leadId: 'lead_002' });
  assert.deepEqual(clicked.selectedIds, []);

  // Still inert when the rest of the table is already ticked.
  const all = selectionReducer(state, { type: 'select_all' });
  const clickedAgain = selectionReducer(all, { type: 'toggle', leadId: 'lead_002' });
  assert.deepEqual(clickedAgain.selectedIds, ['lead_004', 'lead_011', 'lead_030', 'lead_019']);
});

// --- estimate line ----------------------------------------------------------

test(
  'a generate estimate is priced per selected lead, to two decimals',
  { todo: true },
  async () => {
    const { formatEstimate } = await loadEstimateLine();

    // The separators are the contract's literal characters: U+00D7 MULTIPLICATION
    // SIGN and U+2248 ALMOST EQUAL TO, not an ASCII "x" and "~".
    // usd_est arrives as a 4dp STRING and is parsed with Number() inside
    // formatEstimate, then ROUNDED to 2dp for display, not truncated — "0.1372"
    // is $0.14. A formatter typed against a number would reach for .toFixed() on
    // a string and throw here.
    assert.equal(
      formatEstimate(estimate({ lead_count: 12, model: 'Opus 5', usd_est: '0.1372' })),
      '12 selected × Opus 5 ≈ $0.14',
    );
    assert.equal(
      formatEstimate(estimate({ lead_count: 12, model: 'Opus 5', usd_est: '0.1349' })),
      '12 selected × Opus 5 ≈ $0.13',
    );

    // Two decimals always, so the line's width never twitches as the selection
    // grows. $12.5 is not a price anyone writes down.
    assert.equal(
      formatEstimate(estimate({ lead_count: 240, model: 'Opus 5', usd_est: '12.5000' })),
      '240 selected × Opus 5 ≈ $12.50',
    );

    // "selected" is the noun, so there is no singular/plural branch to get wrong
    // on the stage where a reviewer can legitimately price a single row.
    assert.equal(
      formatEstimate(estimate({ lead_count: 1, model: 'Opus 5', usd_est: '0.0121' })),
      '1 selected × Opus 5 ≈ $0.01',
    );

    // The model string is rendered verbatim. There is no label lookup in here —
    // whatever the estimate endpoint says it will bill against is what is shown,
    // and provider/token counts are not on this line at all.
    assert.equal(
      formatEstimate(estimate({ lead_count: 3, model: 'claude-haiku-4-5', usd_est: '0.0912' })),
      '3 selected × claude-haiku-4-5 ≈ $0.09',
    );
  },
);

test(
  'a read estimate is priced per lead in the run, and never says "selected"',
  { todo: true },
  async () => {
    const { formatEstimate } = await loadEstimateLine();

    // The contract's worked example. `lead_count` on a read estimate is every
    // RunLead in the run — read runs before any selection exists, so there is
    // nothing selected for it to be counting.
    assert.equal(
      formatEstimate(estimate({ stage: 'read', model: 'Haiku 4.5', lead_count: 31, usd_est: '0.0184' })),
      '31 leads × Haiku 4.5 ≈ $0.02',
    );

    // The sharp version of the same rule: identical envelope, identical numbers,
    // and only `stage` differs. A formatter that ignored `stage` would return the
    // same string twice and quietly tell the operator they had picked 12 leads
    // they never touched.
    const read = formatEstimate(estimate({ stage: 'read', lead_count: 12, usd_est: '0.1372' }));
    const generate = formatEstimate(estimate({ stage: 'generate', lead_count: 12, usd_est: '0.1372' }));
    assert.equal(read, '12 leads × Opus 5 ≈ $0.14');
    assert.equal(generate, '12 selected × Opus 5 ≈ $0.14');
    assert.notEqual(read, generate);

    // Belt and braces: the word must not appear anywhere in a read line, however
    // the noun phrase is assembled.
    assert.doesNotMatch(read, /selected/);

    // The rounding rule is one rule, not one per stage.
    assert.equal(
      formatEstimate(estimate({ stage: 'read', model: 'Haiku 4.5', lead_count: 31, usd_est: '0.0149' })),
      '31 leads × Haiku 4.5 ≈ $0.01',
    );
  },
);

test('the zero state is distinct, and no estimate ever shows a bare number', { todo: true }, async () => {
  const { formatEstimate } = await loadEstimateLine();

  // Nothing ticked is not a $0.00 run, it is an unpriced one. Rendering a
  // confident zero next to a live Generate button is the wrong sentence. Only the
  // generate stage can reach this: `stages.ts` darkens read on a run whose
  // lead_count is 0, so an empty read estimate is never requested at all.
  const zero = formatEstimate(estimate({ stage: 'generate', lead_count: 0, usd_est: '0.0000' }));
  assert.equal(zero, 'Nothing selected — select leads to see a price');
  assert.doesNotMatch(zero, /\$/); // no dollar figure at all in the zero state

  // The product stance, mechanised: a price is visible before the paid button,
  // and it is a price — never a naked float someone has to guess the units of.
  for (const usd of ['0.1372', '12.5000', '0.0121', '7.0000']) {
    const line = formatEstimate(estimate({ lead_count: 5, model: 'Opus 5', usd_est: usd }));
    assert.match(line, /≈ \$\d+\.\d{2}$/, `${usd} should end in a two-decimal dollar amount`);
    assert.doesNotMatch(line, /≈ \d/, `${usd} lost its currency symbol`);
  }

  // lead_count comes from the server's estimate envelope, not from
  // selectedIds.length — the number priced and the number charged are one read.
  assert.equal(
    formatEstimate(estimate({ lead_count: 5, model: 'Opus 5', usd_est: '7.0000' })),
    '5 selected × Opus 5 ≈ $7.00',
  );
});

test('money crosses the wire as a string, on the estimate and on the result', { todo: true }, async () => {
  const { formatEstimate } = await loadEstimateLine();

  // Both fixtures are typed against api/types.ts. If the backend ever started
  // emitting floats, these two lines stop compiling — which is the whole point of
  // importing the wire types instead of hand-rolling them here.
  const priced: Estimate = estimate({ lead_count: 12, model: 'Opus 5', usd_est: '0.1372' });
  const spent: GenerateResult = { generated: 10, failed: 1, skipped: 1, actual_usd: '0.1401' };

  assert.equal(typeof priced.usd_est, 'string');
  assert.equal(typeof spent.actual_usd, 'string');

  // formatEstimate owns the parse: callers hand it the envelope exactly as it
  // came off the wire, strings and all, and get a formatted line back.
  assert.equal(formatEstimate(priced), '12 selected × Opus 5 ≈ $0.14');

  // POST /api/runs/{id}/generate/ answers with the same money-as-string rule, and
  // the page parses it the same way at the edge before showing what the run
  // actually cost against what it was quoted.
  assert.equal(Number(spent.actual_usd).toFixed(2), '0.14');
  assert.ok(Number(spent.actual_usd) > Number(priced.usd_est)); // the run ran a little over
});
