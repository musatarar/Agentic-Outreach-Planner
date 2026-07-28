import { Link } from 'react-router-dom';
import { KeyHint } from '../ui';
import type { DoneSummary } from '../../api/types';

const USD = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  maximumFractionDigits: 0,
});

/** `1147` → `19m 07s`. Server-supplied duration, not a clock reading. */
function formatElapsed(seconds: number | null): string | null {
  if (seconds === null || seconds < 0) return null;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  if (minutes === 0) return `${rest}s`;
  return `${minutes}m ${String(rest).padStart(2, '0')}s`;
}

function Figure({ value, label }: { value: string; label: string }) {
  return (
    <div className="cleared__figure">
      <div className="cleared__value">{value}</div>
      <div className="cleared__label">{label}</div>
    </div>
  );
}

export interface InboxEmptyStateProps {
  /** From GET /api/queue/done/. Null while it is still in flight. */
  summary: DoneSummary | null;
  error: string | null;
}

/**
 * The end of the queue.
 *
 * This is the payoff for the whole product and the screen someone screenshots,
 * so it gets real figures rather than a grey checkmark: how many drafts went
 * out, how much pipeline they touched, how long it took. The numbers are set
 * large in mono because they are machine output — the same voice as the rule
 * trace, which is what makes them read as *measured* rather than congratulatory.
 *
 * `queue_cleared` and `total` come from the server and select the state
 * directly (§5.2). Array lengths are never consulted: a reviewer who cleared
 * the queue in another tab must still see the celebration here.
 */
export function InboxEmptyState({ summary, error }: InboxEmptyStateProps) {
  if (error) {
    return (
      <div className="cleared">
        <h2 className="cleared__headline">Queue clear</h2>
        <p className="cleared__sub">Could not load today's figures: {error}</p>
      </div>
    );
  }

  if (!summary) {
    return (
      <div className="cleared">
        <h2 className="cleared__headline">Queue clear</h2>
        <p className="cleared__sub">Totalling up…</p>
      </div>
    );
  }

  if (summary.total === 0) {
    return (
      <div className="cleared">
        <h2 className="cleared__headline">Nothing to triage</h2>
        <p className="cleared__sub">
          No outreach was planned for today. Run the planner to fill the queue.
        </p>
        <Link className="cleared__link" to="/">
          Go to the planner
        </Link>
      </div>
    );
  }

  const elapsed = formatElapsed(summary.elapsed_seconds);

  return (
    <div className="cleared">
      <p className="cleared__eyebrow">Queue cleared</p>
      <h2 className="cleared__headline">
        {summary.approved} draft{summary.approved === 1 ? '' : 's'} approved and copied
      </h2>

      <div className="cleared__figures">
        <Figure value={USD.format(summary.pipeline_value_usd)} label="pipeline touched" />
        {elapsed && <Figure value={elapsed} label="elapsed" />}
        <Figure value={String(summary.total)} label="leads triaged" />
      </div>

      <p className="cleared__breakdown">
        {summary.approved} approved · {summary.snoozed} snoozed · {summary.dismissed} dismissed
      </p>

      <div className="cleared__actions">
        <Link className="cleared__link" to="/done">
          Review what you sent
        </Link>
        <span className="cleared__hint">
          <KeyHint keys={['?']} /> shortcuts
        </span>
      </div>
    </div>
  );
}
