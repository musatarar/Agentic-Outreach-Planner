/**
 * The composer's stage machine (MUS-47): which of the four stages a run is
 * waiting on, and which stages the rail is allowed to let you act on.
 *
 * `components/compose/stages.ts` ships with `fe_scope` rather than as a
 * component of its own, and the contract now says so outright: it is the
 * shell's gate, it lands with the route, and every later stage reads it. It is
 * a pure module for the same reason `util/trace.ts` is — the harness is
 * `node --test` with no jsdom and no Testing Library, so the stage rail's React
 * shell is review-verified while the rules it renders are pinned here.
 *
 * The vocabulary is `StageName` from `api/types.ts`: four stages, and
 * `classify` is one of them. `POST /api/runs/{id}/classify/` is a real step a
 * draft run sits waiting on, and a three-stage rail would have to hide it
 * inside `scope` — where nothing could say whether it had run.
 *
 * Two of these tests are product promises rather than bookkeeping. The read
 * stage is OPTIONAL: a classified run may go straight to generate, and a spec
 * that only walked draft → classified → read → generate would let that quietly
 * harden into a mandatory paid step. And a scope that matched zero leads has to
 * say so at stage 01 — `lead_count === 0` puts the reviewer back on the filters
 * with both paid stages dark, rather than selling them a read over an empty set.
 *
 * RED SKELETON. Every test is `{ todo: true }`, and the module under test does
 * not exist yet — so each import of it happens INSIDE its test body. A
 * top-level `await import()` of a missing module is a load error, which takes
 * down the whole file instead of failing the todo tests that own it. The type
 * import below is exempt on both counts: `api/types.ts` exists and is frozen,
 * and `import type` is erased before anything tries to resolve it.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import type { RunDetail, RunStatus, StageName } from '../src/api/types.ts';

/**
 * Annotated `string`, not left as a literal, so tsc treats the dynamic specifier
 * as `any` instead of trying to resolve a file the skeleton has not written.
 */
const STAGES_MODULE: string = '../src/components/compose/stages.ts';

interface StagesModule {
  stageFor(run: RunDetail | null): StageName;
  stageEnabled(stage: StageName, run: RunDetail | null): boolean;
}

const loadStages = async (): Promise<StagesModule> =>
  (await import(STAGES_MODULE)) as StagesModule;

/** Rail order, so a loop over "every stage" cannot quietly drop one. */
const ALL_STAGES: StageName[] = ['scope', 'classify', 'read', 'generate'];

/**
 * A full `RunDetail` at `status`, holding `leadCount` rows. The machine reads
 * exactly two fields plus the null-ness of the run itself, but `RunDetail` is a
 * frozen wire shape, so the fixture is the real record rather than a convenient
 * subset — a machine that starts reading a third field must still compile
 * against what the serializer actually sends. Money arrives as a JSON string or
 * null (DRF's `COERCE_DECIMAL_TO_STRING` default); nothing here parses it.
 */
const runAt = (status: RunStatus, leadCount: number): RunDetail => {
  const terminal = status === 'completed' || status === 'discarded';
  return {
    id: 7,
    status,
    scope: { stage: 'active_trial' },
    lead_count: leadCount,
    spread: status === 'draft' ? null : { '1': leadCount },
    classify_ms: status === 'draft' ? null : 38,
    discarded_suggestions: 0,
    read_provider: '',
    read_model: '',
    generate_provider: '',
    generate_model: '',
    read_cost_estimate_usd: null,
    read_cost_actual_usd: null,
    generate_cost_estimate_usd: null,
    generate_cost_actual_usd: null,
    created_at: '2026-06-22T09:00:00Z',
    created_by: 'reviewer@example.com',
    finished_at: terminal ? '2026-06-22T09:40:00Z' : null,
  };
};

test('the composer opens on scope when there is no run at all', { todo: true }, async () => {
  const { stageFor } = await loadStages();
  assert.equal(stageFor(null), 'scope');
});

test('every run status reports the stage it is waiting on', { todo: true }, async () => {
  const { stageFor } = await loadStages();
  // A draft has a scope and no RunLead rows: the step it is waiting on is the
  // free one, classify — not scope, which is already done.
  assert.equal(stageFor(runAt('draft', 0)), 'classify');
  assert.equal(stageFor(runAt('classified', 12)), 'read'); // leads in hand
  assert.equal(stageFor(runAt('read', 12)), 'generate');
  // 'generated' stays on generate: re-running it to retry a failed lead is a
  // legal generated → generated transition, so there is no fifth stage to
  // move to.
  assert.equal(stageFor(runAt('generated', 12)), 'generate');
  // A terminal run waits on nothing, and the rail still has to point somewhere:
  // stage 01, where the next run starts.
  assert.equal(stageFor(runAt('completed', 12)), 'scope');
  assert.equal(stageFor(runAt('discarded', 12)), 'scope');
});

test('nothing but scope is live until a run exists', { todo: true }, async () => {
  const { stageEnabled } = await loadStages();
  // Scope is the one stage you can use before anything has been created.
  assert.equal(stageEnabled('scope', null), true);
  assert.equal(stageEnabled('classify', null), false);
  assert.equal(stageEnabled('read', null), false);
  assert.equal(stageEnabled('generate', null), false);
});

test('a draft run enables the two free stages and nothing paid', { todo: true }, async () => {
  const { stageEnabled } = await loadStages();
  const draft = runAt('draft', 0);
  assert.equal(stageEnabled('scope', draft), true);
  assert.equal(stageEnabled('classify', draft), true); // draft → classified
  // No RunLead rows exist yet, so there is nothing to read about or generate for.
  assert.equal(stageEnabled('read', draft), false);
  assert.equal(stageEnabled('generate', draft), false);
});

test('a classified run that matched zero leads darkens both paid stages', { todo: true }, async () => {
  const { stageFor, stageEnabled } = await loadStages();
  const empty = runAt('classified', 0);
  // Say it at stage 01: the rail sends the reviewer back to the filters instead
  // of parking them on a read they are not allowed to run.
  assert.equal(stageFor(empty), 'scope');
  // The free stages stay live — widen the scope, then classify again.
  assert.equal(stageEnabled('scope', empty), true);
  assert.equal(stageEnabled('classify', empty), true);
  // Both downstream stages are paid, and neither may be reachable over an empty
  // set. Status alone is not the gate, `lead_count` is: a run can be
  // `classified` and still bill for nothing.
  assert.equal(stageEnabled('read', empty), false);
  assert.equal(stageEnabled('generate', empty), false);
});

test('the read stage is optional: a classified run can generate unread', { todo: true }, async () => {
  const { stageEnabled } = await loadStages();
  const classified = runAt('classified', 12);
  assert.equal(stageEnabled('read', classified), true);
  // The product promise, pinned explicitly. Generate's only prerequisites are
  // classified leads and a legal transition — never a completed read, which is
  // a skippable paid stage. ALLOWED_TRANSITIONS[classified] carries `generated`.
  assert.equal(stageEnabled('generate', classified), true);
  // And classify stays live: classified → classified re-runs the rules over an
  // edited scope, replacing the run's RunLead rows.
  assert.equal(stageEnabled('classify', classified), true);
});

test('a run that has been read may re-read or generate, never re-classify', { todo: true }, async () => {
  const { stageEnabled } = await loadStages();
  const read = runAt('read', 12);
  assert.equal(stageEnabled('read', read), true); // read → read is a legal re-run
  assert.equal(stageEnabled('generate', read), true);
  // ALLOWED_TRANSITIONS[read] has no `classified`, so re-classifying earns a
  // 400 — and scope goes dark with it, because a filter edit that nothing can
  // consume is an offer the run cannot honour.
  assert.equal(stageEnabled('classify', read), false);
  assert.equal(stageEnabled('scope', read), false);
});

test('a generated run can generate again but cannot go back', { todo: true }, async () => {
  const { stageEnabled } = await loadStages();
  const generated = runAt('generated', 12);
  // ALLOWED_TRANSITIONS is generated → (generated, completed, discarded). There
  // is no path back to read or classified, so any enabled button upstream would
  // only earn a 400.
  assert.equal(stageEnabled('scope', generated), false);
  assert.equal(stageEnabled('classify', generated), false);
  assert.equal(stageEnabled('read', generated), false);
  assert.equal(stageEnabled('generate', generated), true); // retry a failed lead
});

test('a terminal run enables nothing', { todo: true }, async () => {
  const { stageEnabled } = await loadStages();
  // Completed and discarded are both dead ends: ALLOWED_TRANSITIONS leaves them
  // with no successors, so the next run starts from a null run, not from this one.
  const terminal: RunStatus[] = ['completed', 'discarded'];
  for (const status of terminal) {
    const done = runAt(status, 12);
    for (const stage of ALL_STAGES) {
      assert.equal(stageEnabled(stage, done), false, `${stage} should be dark on a ${status} run`);
    }
  }
});

test('the stage a run waits on is never one the rail has darkened', { todo: true }, async () => {
  const { stageFor, stageEnabled } = await loadStages();
  // The shell lands the reviewer on stageFor(). If that ever names a disabled
  // stage the composer is a dead end — which is precisely what a zero-lead run
  // becomes if `lead_count` gates stageEnabled but not stageFor.
  const active: RunDetail[] = [
    runAt('draft', 0),
    runAt('classified', 0),
    runAt('classified', 12),
    runAt('read', 12),
    runAt('generated', 12),
  ];
  for (const run of active) {
    const stage = stageFor(run);
    assert.equal(
      stageEnabled(stage, run),
      true,
      `${run.status} with ${run.lead_count} leads waits on a dark ${stage}`,
    );
  }
  assert.equal(stageEnabled(stageFor(null), null), true);
});
