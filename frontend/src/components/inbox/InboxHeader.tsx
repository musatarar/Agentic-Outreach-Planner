import { Link } from 'react-router-dom';
import { KeyHint, ThemeToggle } from '../ui';
import type { QueueCounts } from '../../api/types';

/** `03` — padded to the width of the total so the digits never re-flow. */
function padCount(value: number, total: number): string {
  return String(value).padStart(String(Math.max(total, 1)).length, '0');
}

export interface InboxHeaderProps {
  counts: QueueCounts;
  /** The server's date, rendered as-is. Never a locally computed day. */
  date: string;
}

/**
 * `03 / 14 today`, the count in mono, plus a thin progress bar.
 *
 * The queue being finite and visibly shrinking is what makes triage feel like
 * it ends, rather than like an inbox. The bar is the cheapest possible way to
 * say that, so it sits full-width along the bottom edge of the header where it
 * reads as a progress rail for the whole screen.
 */
export function InboxHeader({ counts, date }: InboxHeaderProps) {
  const { done_today: done, total_today: total } = counts;
  const percent = total > 0 ? Math.round((done / total) * 100) : 0;

  return (
    <header className="inbox-header">
      <span className="inbox-header__brand">Triage</span>

      <div className="inbox-header__progress">
        <div className="inbox-header__count">
          <span className="inbox-header__done">{padCount(done, total)}</span>
          <span className="inbox-header__total">/ {total}</span>
          <span className="inbox-header__unit">today</span>
        </div>
        <div
          className="inbox-header__bar"
          role="progressbar"
          aria-valuenow={done}
          aria-valuemin={0}
          aria-valuemax={total}
          aria-label={`${done} of ${total} triaged today`}
        >
          <div className="inbox-header__fill" style={{ width: `${percent}%` }} />
        </div>
      </div>

      <span className="inbox-header__spacer" />

      {/* Visible chips, not a manual. Nobody reads a shortcut list they have
          to go looking for. */}
      <div className="inbox-header__hints">
        <span className="inbox-header__hint">
          <KeyHint keys={['J', 'K']} /> move
        </span>
        <span className="inbox-header__hint">
          <KeyHint keys={['A']} /> approve
        </span>
        <span className="inbox-header__hint">
          <KeyHint keys={['E']} /> edit
        </span>
        <span className="inbox-header__hint">
          <KeyHint keys={['?']} /> all
        </span>
      </div>

      {/* The server decides which day this is; it is printed,
          never recomputed. */}
      <span className="inbox-header__hint" title="Queue date, server timezone">
        {date}
      </span>

      <Link className="inbox-header__link" to="/done">
        Done
      </Link>

      <ThemeToggle />
    </header>
  );
}
