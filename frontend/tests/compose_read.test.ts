/**
 * Run composer, the advisory read: the pure view logic behind the suggestion
 * cards (MUS-47, component 11 — `fe_read`).
 *
 * `components/compose/suggestionView.ts` is where a read stops being API data
 * and becomes something a reviewer can act on. It is a plain module rather than
 * a hook or a component because `node --test` strips types but cannot transform
 * JSX, and this repo has no jsdom and no Testing Library — `docs/contracts/
 * run-composer.md` says so outright and calls component rendering
 * review-verified, not unit-tested. Same seam `util/trace.ts` gives the MUS-29
 * reports page.
 *
 * Two of those decisions are the reason the module exists at all:
 *
 * 1. **A lead the model had nothing to say about gets no card.** Twenty-six
 *    silent rows are the ordinary outcome of a read and its most useful signal;
 *    they belong in one summary line, not in twenty-six empty cards. So
 *    `suggestionViews` deliberately returns fewer views than it was given rows,
 *    and `readSummary` is what accounts for the difference.
 * 2. **The record and the model do not share a typeface.** A quote is bytes
 *    lifted out of the lead's own notes; a rationale is model prose written
 *    about them. The view keeps the two in separate fields carrying separate
 *    faces, so a component cannot render one in the other's voice. That is the
 *    visible half of the argument the read's fail-closed posture makes
 *    structurally — flattening rationale and quotes into one blob would erase
 *    it without breaking anything a type checker can see.
 *
 * RED SKELETON. Every test is `{ todo: true }`, and `suggestionView.ts` does not
 * exist on this branch — so the import happens INSIDE each test body. A
 * top-level `await import()` of a missing module is a load error, which takes
 * the whole frontend suite down instead of failing the todo tests that own it.
 * The wire types below come from `api/types.ts`, which does exist and is frozen:
 * `import type` is erased outright, so it costs the loader nothing.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import type {
  RuleTrace,
  RunLeadRow,
  Suggestion,
  SuggestionEvidence,
  SuggestionKind,
  SuggestionState,
} from '../src/api/types.ts';

/**
 * Annotated `string`, not left as a literal, so tsc treats the dynamic specifier
 * as `any` instead of trying to resolve a file the skeleton has not written. The
 * resolution that matters happens at runtime, relative to this file, exactly as
 * a literal specifier would.
 */
const SUGGESTION_VIEW: string = '../src/components/compose/suggestionView.ts';

/** Typed locally: `SuggestionView`/`ReadSummary` land in the module with it. */
interface EvidenceView extends SuggestionEvidence {
  face: string;
}

interface SuggestionView {
  leadId: string;
  state: SuggestionState;
  delta: string;
  rationale: string;
  rationaleFace: string;
  evidence: EvidenceView[];
  collapsed: string | null;
  muted: boolean;
}

interface ReadSummary {
  quiet: string;
  discarded: string | null;
}

interface SuggestionViewModule {
  suggestionViews(rows: RunLeadRow[]): SuggestionView[];
  readSummary(rows: RunLeadRow[], discarded: number): ReadSummary;
}

const loadModule = async (): Promise<SuggestionViewModule> =>
  (await import(SUGGESTION_VIEW)) as SuggestionViewModule;

// --- fixtures ---------------------------------------------------------------

// Action types come from `project/app/services/actions.py`, never invented: the
// rules wrote `nudge_usage` at classify, and `reengage_dormant` is a different
// member of SELECTABLE_ACTION_TYPES for an `action_change` to propose.
const RULES_ACTION = 'nudge_usage';
const PROPOSED_ACTION = 'reengage_dormant';
const RULES_REASON = 'Active but underusing the portal.';

// Model prose. Distinctive on purpose: a view that flattened the rationale into
// the evidence list would smuggle this exact sentence in among the quotes, and
// the last test looks for it there.
const RATIONALE = 'Renewal is three weeks out and nobody has logged in since April.';

// Quotes, verbatim out of the sanitized bytes this lead's model was shown, and
// stored as the model ORIGINALLY wrote them — the casefolded, whitespace-
// collapsed form is a comparison detail of the backend validator, not a storage
// format, and the card renders the quote beside the note it came from.
const NOTE_QUOTE = 'renewal is in three weeks';
const EVENT_QUOTE = '145 days with no recorded activity';

// `outreach.explain()`'s schema-v1 envelope — the real keys, not a lookalike.
// The card reads none of it, but a row is the wire shape the page was handed,
// and `rule_trace` has exactly one schema across MUS-42, MUS-47 and MUS-40.
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
    value: RULES_ACTION,
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
    rules_action: RULES_ACTION,
    rules_reason: RULES_REASON,
    effective_priority: 2,
    effective_action: RULES_ACTION,
    effective_reason: RULES_REASON,
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

/** A lead nothing was ever proposed for: `suggestion` is `{}`, not five blanks. */
const noneRow = (leadId: string): RunLeadRow => row(leadId);

/**
 * The persisted suggestion shape: ALL FIVE keys, always, with explicit
 * placeholders rather than absent keys, so no reader ever branches on `in`
 * (docs/contracts/run-composer.md, "read"). `proposed_priority` is `null` for a
 * kind that carries no priority and `proposed_action` is `""` — a sentinel `0`
 * would read back as a real priority, which is exactly why the placeholder is
 * null and not a number.
 */
function suggestion(
  kind: SuggestionKind,
  { priority = null, action = '' }: { priority?: number | null; action?: string } = {},
): Suggestion {
  return {
    suggestion: kind,
    proposed_priority: priority,
    proposed_action: action,
    rationale: RATIONALE,
    evidence: [
      { source: 'note', quote: NOTE_QUOTE },
      { source: 'event', quote: EVENT_QUOTE },
    ],
  };
}

/** A `raise` from P2 to P1, in whichever decision state the caller wants. */
function raiseRow(
  leadId: string,
  state: Exclude<SuggestionState, 'none'> = 'proposed',
): RunLeadRow {
  return row(leadId, {
    // Accept moves the effective columns; reject restores them to the rules
    // values. Both are supplied here as the backend would have written them.
    effective_priority: state === 'accepted' ? 1 : 2,
    suggestion: suggestion('raise', { priority: 1 }),
    suggestion_state: state,
  });
}

/** An `action_change`: the kind carries no priority, so that key is `null`. */
const actionChangeRow = (leadId: string): RunLeadRow =>
  row(leadId, {
    suggestion: suggestion('action_change', { action: PROPOSED_ACTION }),
    suggestion_state: 'proposed',
  });

// --- tests ------------------------------------------------------------------

test('a proposed suggestion becomes one card carrying delta, rationale and quotes', { todo: true }, async () => {
  const { suggestionViews } = await loadModule();

  const [card] = suggestionViews([raiseRow('lead_a')]);

  assert.equal(card.leadId, 'lead_a');
  assert.equal(card.state, 'proposed');
  // The delta is the whole recommendation in one mono line: what the rules said
  // and what the model wants instead. Never the effective columns — nothing has
  // been accepted yet, and a card that read "P1 -> P1" would be claiming it had.
  assert.equal(card.delta, 'rules P2 -> proposes P1');
  assert.equal(card.rationale, RATIONALE);
  assert.deepEqual(card.evidence, [
    { source: 'note', quote: NOTE_QUOTE, face: 'mono' },
    { source: 'event', quote: EVENT_QUOTE, face: 'mono' },
  ]);
  // Nothing to collapse and nothing to mute until a human has answered.
  assert.equal(card.collapsed, null);
  assert.equal(card.muted, false);

  // An action_change reads off proposed_action under the same grammar, and its
  // proposed_priority is the null placeholder. A view that mistook that null for
  // a priority would print "rules P2 -> proposes P0" over a row about actions.
  // The placeholder is asserted, not assumed: `null`, never absent (the type
  // makes all five keys required) and never a sentinel `0`, which would read
  // back through `number | null` as a genuine priority the model never proposed.
  const proposal = suggestion('action_change', { action: PROPOSED_ACTION });
  assert.equal(proposal.proposed_priority, null);
  assert.equal(proposal.proposed_action, PROPOSED_ACTION);

  const [changed] = suggestionViews([actionChangeRow('lead_b')]);
  assert.equal(changed.delta, 'rules nudge_usage -> proposes reengage_dormant');
});

test('a lead the model said nothing about produces no card', { todo: true }, async () => {
  const { suggestionViews } = await loadModule();

  const views = suggestionViews([noneRow('lead_a'), raiseRow('lead_b'), noneRow('lead_c')]);

  // Silence is the read's signal, not a hole in the UI. Rendering an empty card
  // per quiet lead would bury the one row that has something to say inside
  // dozens that do not — the summary line carries them instead.
  assert.deepEqual(
    views.map((view) => view.leadId),
    ['lead_b'],
  );
});

test('readSummary counts the quiet rows against the whole row set', { todo: true }, async () => {
  const { readSummary } = await loadModule();

  const thirtyOne: RunLeadRow[] = [
    ...Array.from({ length: 5 }, (_unused, index) => raiseRow(`lead_p${index}`)),
    ...Array.from({ length: 26 }, (_unused, index) => noneRow(`lead_q${index}`)),
  ];
  // Both numbers come off the rows: 26 quiet, 31 read. Neither is a count the
  // server handed down, so a stale or filtered row set can never disagree with
  // the cards rendered beside it.
  assert.equal(readSummary(thirtyOne, 0).quiet, 'no changes proposed for 26 of 31');

  // "Quiet" means the model proposed nothing, not "no card is on screen right
  // now". A decided row had a proposal and stays out of the count in both
  // directions — otherwise rejecting a suggestion would silently inflate it.
  const decided: RunLeadRow[] = [
    raiseRow('lead_a', 'proposed'),
    raiseRow('lead_b', 'accepted'),
    raiseRow('lead_c', 'rejected'),
    noneRow('lead_d'),
    noneRow('lead_e'),
  ];
  assert.equal(readSummary(decided, 0).quiet, 'no changes proposed for 2 of 5');
});

test('a decided card collapses to one line, and a rejected one is muted', { todo: true }, async () => {
  const { suggestionViews } = await loadModule();

  const [accepted] = suggestionViews([raiseRow('lead_a', 'accepted')]);
  assert.equal(accepted.state, 'accepted');
  assert.equal(accepted.collapsed, 'P2 -> P1 . accepted');
  assert.equal(accepted.muted, false);

  const [rejected] = suggestionViews([raiseRow('lead_b', 'rejected')]);
  assert.equal(rejected.state, 'rejected');
  // Built from rules -> *proposed*, not rules -> effective: reject restores the
  // effective columns to the rules values, so a line derived from those would
  // read "P2 -> P2" and lose the record of what was turned down.
  assert.equal(rejected.collapsed, 'P2 -> P1 . rejected');
  assert.equal(rejected.muted, true);
});

test('the discarded-evidence line appears only when something was discarded', { todo: true }, async () => {
  const { readSummary } = await loadModule();

  const rows = [raiseRow('lead_a'), noneRow('lead_b')];

  // A discard is a suggestion whose quotes were not in the bytes the model was
  // shown. It is worth a line because it is the grounding check doing its job,
  // and a reviewer who never sees it cannot tell a quiet model from a caught one.
  assert.equal(
    readSummary(rows, 2).discarded,
    '2 suggestions discarded - fabricated evidence',
  );
  // Zero discards is the normal case and gets no line at all — a standing
  // "0 suggestions discarded" would train the reviewer to stop reading it.
  assert.equal(readSummary(rows, 0).discarded, null);
});

test('quotes and rationale keep separate fields and separate faces', { todo: true }, async () => {
  const { suggestionViews } = await loadModule();

  const [card] = suggestionViews([raiseRow('lead_a')]);

  // Record data renders mono, whatever its source: a note quote and an event
  // quote are both bytes off the lead's own record.
  assert.deepEqual(
    card.evidence.map((entry) => entry.face),
    ['mono', 'mono'],
  );
  assert.deepEqual(
    card.evidence.map((entry) => entry.source),
    ['note', 'event'],
  );
  // Model prose renders in the voice face. The two must not converge: the
  // typeface is how a reviewer tells "the record says" from "the model thinks".
  assert.equal(card.rationaleFace, 'serif');
  assert.notEqual(card.rationaleFace, card.evidence[0].face);
  // And the rationale must not have been folded into the quote list on the way
  // through — that is how a flattened view would pass every assertion above.
  assert.equal(
    card.evidence.some((entry) => entry.quote.includes('logged in since April')),
    false,
  );
});
