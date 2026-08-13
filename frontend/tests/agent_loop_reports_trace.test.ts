/**
 * Reports-page agent trace (MUS-29): the pure rendering rules behind the
 * "How this draft was reached" section. The React page is exercised through
 * `util/trace.ts` because node:test strips types but cannot transform JSX —
 * the same seam the auth-destination tests use for `hooks/authDestination.ts`.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

const { hidesTraceToggle, traceStepViews } = await import('../src/util/trace.ts');
const { ApiError } = await import('../src/api/client.ts');

test('renders llm_call, tool_result and final steps as a numbered list', () => {
  const views = traceStepViews([
    {
      seq: 1,
      kind: 'llm_call',
      payload: {
        text: '',
        tool_calls: [{ id: 'c1', name: 'get_lead_history', arguments: {} }],
      },
      created_at: '2026-06-22T09:00:00Z',
    },
    {
      seq: 2,
      kind: 'tool_result',
      payload: { tool_call_id: 'c1', name: 'get_lead_history', result: '{"events": []}' },
      created_at: '2026-06-22T09:00:01Z',
    },
    {
      seq: 3,
      kind: 'final',
      payload: { text: 'Hi Dana — checking in on your trial.' },
      created_at: '2026-06-22T09:00:02Z',
    },
  ]);

  // The <ol> numbering is the persisted seq order, one item per step.
  assert.deepEqual(
    views.map((view) => view.seq),
    [1, 2, 3],
  );
  assert.equal(views[0].title, 'Model call');
  assert.equal(views[1].title, 'Tool result — get_lead_history');
  assert.equal(views[1].text, '{"events": []}');
  assert.equal(views[1].pre, true); // capped result string renders in a <pre>
  assert.equal(views[2].title, 'Final draft');
  assert.equal(views[2].text, 'Hi Dana — checking in on your trial.');
  assert.equal(views[2].pre, true); // the draft renders preformatted too
});

test('shows "requested: {tool names}" for a text-less llm_call step', () => {
  const [askedForTools] = traceStepViews([
    {
      seq: 1,
      kind: 'llm_call',
      payload: {
        text: '',
        tool_calls: [
          { id: 'c1', name: 'get_lead_history', arguments: {} },
          { id: 'c2', name: 'check_ae_calendar', arguments: {} },
        ],
      },
      created_at: '2026-06-22T09:00:00Z',
    },
  ]);
  assert.equal(askedForTools.text, 'requested: get_lead_history, check_ae_calendar');
  assert.equal(askedForTools.pre, false);

  // A call that DID produce text shows the text, not the tool request.
  const [spoke] = traceStepViews([
    {
      seq: 2,
      kind: 'llm_call',
      payload: {
        text: 'Enough context — drafting now.',
        tool_calls: [{ id: 'c3', name: 'get_product_details', arguments: {} }],
      },
      created_at: '2026-06-22T09:00:03Z',
    },
  ]);
  assert.equal(spoke.text, 'Enough context — drafting now.');
});

test('hides the trace toggle when the endpoint 404s (single-shot action)', () => {
  // The contract's single-shot answer: 404 {"error": "no_agent_trace"}.
  assert.equal(hidesTraceToggle(new ApiError('error: no_agent_trace', 404)), true);
  // Anything else is a real failure and must surface, not vanish.
  assert.equal(hidesTraceToggle(new ApiError('HTTP 500', 500)), false);
  assert.equal(hidesTraceToggle(new Error('network down')), false);
  assert.equal(hidesTraceToggle(undefined), false);
});
