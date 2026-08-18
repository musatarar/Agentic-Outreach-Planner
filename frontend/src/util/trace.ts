/**
 * Rendering rules for the "How this draft was reached" section: one
 * `TraceStepView` per persisted `AgentStep`, by payload kind. Kept free of
 * React/JSX so node:test can run it directly.
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
      // The payload schema is server-owned and may grow, so unknown kinds
      // render as a bare labelled step rather than crashing.
      return { seq: step.seq, title: step.kind, text: '', pre: false };
  }
}

export function traceStepViews(steps: AgentTraceStep[]): TraceStepView[] {
  return steps.map(traceStepView);
}

/**
 * A 404 from the trace endpoint means "single-shot action, no agent run" — the
 * toggle disappears rather than erroring. Checked structurally to keep this
 * module import-free.
 */
export function hidesTraceToggle(error: unknown): boolean {
  return (
    error instanceof Error &&
    'status' in error &&
    (error as { status: unknown }).status === 404
  );
}
