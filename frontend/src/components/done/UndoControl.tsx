import { useEffect, useMemo, useState } from 'react';
import type { QueueItem } from '../../api/types';
import { Button } from '../ui';
import { formatCountdown } from './format';

/**
 * Milliseconds left on the server's undo window, re-read once a second.
 *
 * This is the only clock this page reads, and it reads it to *display* an
 * absolute server timestamp — never to decide whether a request is allowed
 * (CONTRACT §9.6).
 */
function useUndoCountdown(expiresAt: string | null): number | null {
  const target = useMemo(
    () => (expiresAt ? Date.parse(expiresAt) : Number.NaN),
    [expiresAt],
  );
  const [remaining, setRemaining] = useState<number | null>(() =>
    Number.isNaN(target) ? null : target - Date.now(),
  );

  useEffect(() => {
    if (Number.isNaN(target)) {
      setRemaining(null);
      return;
    }
    setRemaining(target - Date.now());
    const timer = window.setInterval(() => setRemaining(target - Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [target]);

  return remaining;
}

export interface UndoControlProps {
  item: QueueItem;
  onUndo: (item: QueueItem) => void;
  /** An undo for this row is in flight. */
  sending: boolean;
  /** The server answered 409 `undo_window_expired` for this row. */
  expired: boolean;
}

/**
 * Undo — and for a snoozed row, un-snooze, which is the same transition and the
 * same endpoint. Both return the item to `pending`, which puts it back at its
 * position in `/inbox`, not here.
 *
 * The control disappears when the window closes rather than failing on click.
 * Clock skew is bounded on both sides by design:
 *   - browser ahead of the server: the control vanishes a few seconds early,
 *     and the user loses a little of a five-minute window;
 *   - browser behind: the control lingers, the click is sent anyway, and the
 *     409 is handled by the caller.
 * What never happens is a preflight check that suppresses a request the server
 * would have honoured.
 */
export function UndoControl({ item, onUndo, sending, expired }: UndoControlProps) {
  const remaining = useUndoCountdown(item.undo.expires_at);
  const lapsed = remaining !== null && remaining <= 0;
  // Keyed off status, never off `snooze.until` being non-null: approve and
  // dismiss leave the snooze fields in place (CONTRACT §5.2 clears them for
  // undo only), so a formerly-snoozed approved row still carries them.
  const label = item.status === 'snoozed' ? 'Un-snooze' : 'Undo';

  if (expired || (item.undo.available && lapsed)) {
    return <span className="done-undo__closed">undo window closed</span>;
  }

  // No window left to render: either the server never offered one, or it has
  // already lapsed on a row that was loaded after the fact.
  if (!item.undo.available) return null;

  const urgent = remaining !== null && remaining <= 60_000;

  return (
    <span className="done-undo">
      <Button variant="secondary" size="sm" loading={sending} onClick={() => onUndo(item)}>
        {label}
      </Button>
      {remaining !== null && (
        <span
          className={`done-undo__countdown${urgent ? ' done-undo__countdown--urgent' : ''}`}
          // Urgency is weight, not colour: red and amber belong to the
          // priority ramp and nothing else may borrow them (CONTRACT §7.1).
          title={`Undo closes at ${item.undo.expires_at}`}
        >
          {formatCountdown(remaining)}
        </span>
      )}
    </span>
  );
}
