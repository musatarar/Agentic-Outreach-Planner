import { useEffect, useRef } from 'react';
import type { QueueItem } from '../../api/types';

/** `power_user_reward` → `power user reward`. */
export function railActionLabel(actionType: string): string {
  return actionType.replace(/_/g, ' ');
}

export interface QueueRailProps {
  items: QueueItem[];
  index: number;
  onSelect: (index: number) => void;
}

/**
 * The left pane: where you are in the queue, and what is coming.
 *
 * Deliberately not a data table. Two lines per row — who, then the machine's
 * classification in mono — is enough to recognise a lead you just saw and to
 * know roughly what the next one is about. Anything more turns the rail into a
 * second thing to read, and the card is the thing to read.
 */
export function QueueRail({ items, index, onSelect }: QueueRailProps) {
  const activeRow = useRef<HTMLButtonElement | null>(null);

  // Keyboard navigation must not walk the caret off-screen. `nearest` keeps
  // the rail still until the active row actually reaches an edge.
  useEffect(() => {
    activeRow.current?.scrollIntoView({ block: 'nearest' });
  }, [index]);

  return (
    <nav className="rail" aria-label="Triage queue">
      {items.length === 0 ? (
        <p className="rail__empty">Queue clear</p>
      ) : (
        <ul className="rail__list">
          {items.map((item, position) => {
            const active = position === index;
            return (
              <li key={item.id}>
                <button
                  type="button"
                  ref={active ? activeRow : undefined}
                  className={`rail__row${active ? ' rail__row--active' : ''}`}
                  aria-current={active ? 'true' : undefined}
                  onClick={() => onSelect(position)}
                >
                  <span className="rail__name">{item.lead.contact_name}</span>
                  <span className="rail__meta">
                    P{item.priority} · {railActionLabel(item.action_type)}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </nav>
  );
}
