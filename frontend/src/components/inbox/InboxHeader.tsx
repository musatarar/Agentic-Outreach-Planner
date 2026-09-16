import { Link } from 'react-router-dom';
import { Button, ThemeToggle } from '../ui';

/** `03` — padded to the width of the total so the digits never re-flow. */
function padCount(value: number, total: number): string {
  return String(value).padStart(String(Math.max(total, 1)).length, '0');
}

export interface InboxHeaderProps {
  /** Items on this page still awaiting a decision. */
  pending: number;
  /** Items on this page. */
  loaded: number;
  /** Everything the server holds, which may exceed this page. */
  total: number;
  onReload: () => void;
}

/** `03 / 14 to review`, the count in mono, plus a thin progress bar. */
export function InboxHeader({ pending, loaded, total, onReload }: InboxHeaderProps) {
  const decided = loaded - pending;
  const percent = loaded > 0 ? Math.round((decided / loaded) * 100) : 0;

  return (
    <header className="inbox-header">
      <span className="inbox-header__brand">Review</span>

      <div className="inbox-header__progress">
        <div className="inbox-header__count">
          <span className="inbox-header__done">{padCount(pending, loaded)}</span>
          <span className="inbox-header__total">/ {loaded}</span>
          <span className="inbox-header__unit">to review</span>
        </div>
        <div
          className="inbox-header__bar"
          role="progressbar"
          aria-valuenow={decided}
          aria-valuemin={0}
          aria-valuemax={loaded}
          aria-label={`${decided} of ${loaded} decided`}
        >
          <div className="inbox-header__fill" style={{ width: `${percent}%` }} />
        </div>
      </div>

      <span className="inbox-header__spacer" />

      {total > loaded && (
        <span className="inbox-header__hint">
          showing {loaded} of {total}
        </span>
      )}

      <Button variant="ghost" size="sm" onClick={onReload}>
        Refresh
      </Button>

      <Link className="inbox-header__link" to="/leads/">
        Leads
      </Link>

      <ThemeToggle />
    </header>
  );
}
