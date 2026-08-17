import type { RuleTrace, TraceCondition, TraceSignal } from '../../api/types';

/**
 * Flattens the rule trace into mono lines. `display` is server-rendered and
 * printed verbatim — nothing here rebuilds text from operator/threshold/value.
 * Groups nest exactly one level, so `depth` is 0 or 1.
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
 * Is this trace older than the day being triaged? Traces are plan-time
 * snapshots, never recomputed. Both are server ISO dates, compared as strings.
 */
export function isStaleTrace(trace: RuleTrace, queueDate: string): boolean {
  return Boolean(queueDate) && trace.today !== queueDate;
}
