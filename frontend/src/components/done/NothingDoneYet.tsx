import { useNavigate } from 'react-router-dom';
import { Button, Card, KeyHint } from '../ui';
import { formatDayLabel } from './format';

export interface NothingDoneYetProps {
  /** `DoneResponse.date` — the server's day, never the browser's. */
  date: string;
  timeZone: string;
}

/**
 * `summary.total === 0`. Not a failure state and not a celebration: the day
 * simply has not started. So it stays quiet, and its whole job is to point at
 * the one place where something can happen.
 *
 * The shortcut chips are here rather than only on the inbox because nobody
 * learns a keyboard interface from a screen they are about to leave.
 */
export function NothingDoneYet({ date, timeZone }: NothingDoneYetProps) {
  const navigate = useNavigate();

  return (
    <Card padding="lg">
      <div className="done-blank">
        <p className="done-blank__eyebrow">
          <span>{formatDayLabel(date)}</span>
          <span className="done-blank__zone">{timeZone}</span>
        </p>
        <h2 className="done-blank__headline">Nothing done yet today.</h2>
        <p className="done-blank__body">
          Everything you approve, snooze or dismiss lands here, and stays undoable
          for a few minutes after. Until then, this page is empty on purpose.
        </p>
        <div className="done-blank__actions">
          {/* The one accent fill on this page. /done spends no accent
              anywhere else (CONTRACT §7.1). */}
          <Button variant="primary" onClick={() => navigate('/inbox')}>
            Open the inbox
          </Button>
          <span className="done-blank__hints">
            <KeyHint keys={['J', 'K']} />
            <span className="done-blank__hint-label">move</span>
            <KeyHint keys={['A']} />
            <span className="done-blank__hint-label">approve</span>
            <KeyHint keys={['S']} />
            <span className="done-blank__hint-label">snooze</span>
          </span>
        </div>
      </div>
    </Card>
  );
}
