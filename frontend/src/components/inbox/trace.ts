import type { RuleTrace, TraceCondition, TraceSignal } from '../../api/types';

/**
 * Flattening the rule trace into mono lines.
 *
 * `display` is rendered **on the server** and printed verbatim.
 * Nothing here reads `operator`, `threshold` or `value` to build text — three
 * frontend surfaces each re-deriving that string would drift on the first null
 * value or the first `usd` unit. `passed`, `unit` and `id` are for styling and
 * keys only.
 *
 * Groups nest exactly one level, so `depth` is 0 or 1 and never needs a
 * recursive walk.
 */

export interface TraceLine {
  key: string;
  display: string;
  passed: boolean;
  depth: 0 | 1;
}

function conditionLines(conditions: TraceCondition[], prefix: string): TraceLine[] {
  return conditions.map((condition, index) => ({
    key: `${prefix}-${index}-${condition.id}`,
    display: condition.display,
    passed: condition.passed,
    depth: 1 as const,
  }));
}

/** Priority signals: conditions at depth 0, groups followed by their children. */
export function flattenSignals(signals: TraceSignal[], prefix = 'signal'): TraceLine[] {
  const lines: TraceLine[] = [];
  signals.forEach((signal, index) => {
    const key = `${prefix}-${index}-${signal.id}`;
    lines.push({ key, display: signal.display, passed: signal.passed, depth: 0 });
    if (signal.kind === 'group') {
      lines.push(...conditionLines(signal.conditions, key));
    }
  });
  return lines;
}

/** The matched rule's conditions — all at depth 0, they have no parent line. */
export function flattenConditions(
  conditions: TraceCondition[],
  prefix = 'condition',
): TraceLine[] {
  return conditions.map((condition, index) => ({
    key: `${prefix}-${index}-${condition.id}`,
    display: condition.display,
    passed: condition.passed,
    depth: 0 as const,
  }));
}

/**
 * Is this trace older than the day being triaged?
 *
 * the trace is a snapshot written once at plan time and never
 * recomputed, so `days_since_last_contact > 21d → 28d` stays 28d forever. When
 * the snapshot date differs from the queue's date the UI says so, and a stale
 * recommendation announces itself instead of quietly misleading. Both values
 * are server-supplied ISO dates and are compared as strings — no `new Date()`.
 */
export function isStaleTrace(trace: RuleTrace, queueDate: string): boolean {
  return Boolean(queueDate) && trace.today !== queueDate;
}
