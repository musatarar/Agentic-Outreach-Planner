import { useEffect, useMemo, useState } from 'react';
import type { QueueItem } from '../../api/types';
import { Button } from '../ui';
import { formatCountdown } from './format';

/**
 * Milliseconds left on the server's undo window, re-read once a second.
 * Display only — never used to decide whether a request is allowed.
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
 * Undo (or un-snooze — same endpoint) returns the item to `pending`. The
 * control disappears when the window closes rather than failing on click; if
 * a skewed clock lets it linger, the click is sent and the caller handles the
 * 409 — never a preflight that suppresses a request the server would honour.
 */
export function UndoControl({ item, onUndo, sending, expired }: UndoControlProps) {
  const remaining = useUndoCountdown(item.undo.expires_at);
  const lapsed = remaining !== null && remaining <= 0;
  // Keyed off status, not `snooze.until`: approve/dismiss leave the snooze
  // fields in place, so a formerly-snoozed approved row still carries them.
  const label = item.status === 'snoozed' ? 'Un-snooze' : 'Undo';

  if (expired || (item.undo.available && lapsed)) {
    return <span className="done-undo__closed">undo window closed</span>;
  }

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
          // Urgency is weight, not colour: red/amber belong to the priority ramp.
          title={`Undo closes at ${item.undo.expires_at}`}
        >
          {formatCountdown(remaining)}
        </span>
      )}
    </span>
  );
}
