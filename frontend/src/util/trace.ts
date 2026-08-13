/**
 * Rendering rules for the "How this draft was reached" section (MUS-29):
 * one `TraceStepView` per persisted `AgentStep`, by payload kind —
 * llm_call shows the assistant text (or "requested: {tool names}" when the
 * model only asked for tools), tool_result shows the already-capped result
 * string in a <pre>, final shows the draft.
 *
 * Deliberately free of React and of any API import — like
 * `hooks/authDestination.ts`, this keeps the logic pure functions node:test
 * can run directly (the test runner strips types but cannot transform JSX).
 */

import type { AgentTraceStep } from '../api/types';

export interface TraceStepView {
  seq: number;
  title: string;
  text: string;
  /** Preformatted content (tool results, the draft) renders in a <pre> block. */
  pre: boolean;
}

function asString(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function toolNames(payload: Record<string, unknown>): string[] {
  const calls = payload.tool_calls;
  if (!Array.isArray(calls)) return [];
  return calls
    .map((call) =>
      call && typeof call === 'object'
        ? asString((call as Record<string, unknown>).name)
        : '',
    )
    .filter((name) => name !== '');
}

function traceStepView(step: AgentTraceStep): TraceStepView {
  const payload = step.payload;
  switch (step.kind) {
    case 'llm_call': {
      const text = asString(payload.text);
      const names = toolNames(payload);
      return {
        seq: step.seq,
        title: 'Model call',
        text: text !== '' ? text : names.length > 0 ? `requested: ${names.join(', ')}` : '',
        pre: false,
      };
    }
    case 'tool_result':
      return {
        seq: step.seq,
        title: `Tool result — ${asString(payload.name) || 'unknown tool'}`,
        text: asString(payload.result),
        pre: true,
      };
    case 'final':
      return {
        seq: step.seq,
        title: 'Final draft',
        text: asString(payload.text),
        pre: true,
      };
    default:
      // Unknown kinds render as a bare labelled step rather than crashing the
      // reports page: the payload schema is server-owned and may grow.
      return { seq: step.seq, title: step.kind, text: '', pre: false };
  }
}

export function traceStepViews(steps: AgentTraceStep[]): TraceStepView[] {
  return steps.map(traceStepView);
}

/**
 * A 404 from the trace endpoint is the contract's "single-shot action, no
 * agent run" answer — the toggle disappears rather than showing an error.
 * Structural (`status` on an Error) rather than `instanceof ApiError` to keep
 * this module import-free; ApiError is the only Error with a status here.
 */
export function hidesTraceToggle(error: unknown): boolean {
  return (
    error instanceof Error &&
    'status' in error &&
    (error as { status: unknown }).status === 404
  );
}
