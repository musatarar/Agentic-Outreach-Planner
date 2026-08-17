import type { RuleTrace as RuleTraceEnvelope } from '../../api/types';
import { flattenConditions, flattenSignals, isStaleTrace, type TraceLine } from './trace';

function TraceLines({ lines }: { lines: TraceLine[] }) {
  return (
    <div className="trace">
      {lines.map((line) => (
        <div
          key={line.key}
          className={
            `trace__line trace__line--depth-${line.depth}` +
            (line.passed ? ' trace__line--passed' : '')
          }
        >
          {line.display}
        </div>
      ))}
    </div>
  );
}

export interface RuleTraceProps {
  trace: RuleTraceEnvelope;
  /** `QueueResponse.date` — the server's today, for the staleness note. */
  queueDate: string;
}

/**
 * The machine's arithmetic, one line per condition. The prose `reason` is
 * deliberately not rendered alongside it. Passed conditions sit at full text
 * colour, failed ones recede to `--color-text-subtle`.
 */
export function RuleTrace({ trace, queueDate }: RuleTraceProps) {
  const stale = isStaleTrace(trace, queueDate);

  return (
    <div className="inbox-section">
      <div className="inbox-section__label">
        Why this surfaced
        <span className="inbox-section__note">
          score {trace.priority.score} → P{trace.priority.value}
        </span>
        {stale && (
          <span className="inbox-section__note trace__stale" title="Evaluated on an earlier date">
            evaluated {trace.today}
          </span>
        )}
      </div>

      <TraceLines lines={flattenSignals(trace.priority.signals)} />

      <div className="inbox-section__label trace__rule">
        {trace.action.rule_label}
        <span className="inbox-section__note">{trace.action.rule_id}</span>
      </div>

      <TraceLines lines={flattenConditions(trace.action.conditions)} />

      {trace.action.rejected_rules.length > 0 && (
        <details className="trace__rejected">
          <summary className="trace__rejected-summary">
            {trace.action.rejected_rules.length} rule
            {trace.action.rejected_rules.length === 1 ? '' : 's'} considered first
          </summary>
          {trace.action.rejected_rules.map((rule) => (
            <div key={rule.rule_id} className="trace__rejected-rule">
              <div className="inbox-section__note">{rule.rule_label}</div>
              <TraceLines lines={flattenConditions(rule.conditions, rule.rule_id)} />
            </div>
          ))}
        </details>
      )}
    </div>
  );
}
