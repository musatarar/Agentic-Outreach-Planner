import { useEffect, useRef } from 'react';
import { KeyHint } from '../ui';

const SHORTCUTS: { keys: string[]; description: string }[] = [
  { keys: ['J'], description: 'Next lead' },
  { keys: ['K'], description: 'Previous lead' },
  { keys: ['↓'], description: 'Next lead' },
  { keys: ['↑'], description: 'Previous lead' },
  { keys: ['A'], description: 'Approve, copy to clipboard, advance' },
  { keys: ['E'], description: 'Edit the draft in place' },
  { keys: ['S'], description: 'Snooze' },
  { keys: ['X'], description: 'Dismiss' },
  { keys: ['⌘', '⏎'], description: 'Save an edit' },
  { keys: ['esc'], description: 'Discard an edit, or close this' },
  { keys: ['?'], description: 'This list' },
];

export interface ShortcutOverlayProps {
  onClose: () => void;
}

/**
 * The `?` overlay.
 *
 * A backstop, not the primary teaching surface — the chips in the header are
 * that, because a shortcut list you have to go looking for is a list nobody
 * learns from.
 */
export function ShortcutOverlay({ onClose }: ShortcutOverlayProps) {
  const closeButton = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    closeButton.current?.focus();
  }, []);

  return (
    <div className="overlay" role="presentation" onClick={onClose}>
      <div
        className="overlay__panel"
        role="dialog"
        aria-modal="true"
        aria-label="Keyboard shortcuts"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="overlay__head">
          <h2 className="overlay__title">Keyboard</h2>
          <button
            ref={closeButton}
            type="button"
            className="overlay__close"
            onClick={onClose}
            aria-label="Close"
          >
            ✕
          </button>
        </div>

        <dl className="overlay__list">
          {SHORTCUTS.map((shortcut) => (
            <div className="overlay__row" key={shortcut.keys.join('+') + shortcut.description}>
              <dt className="overlay__keys">
                <KeyHint keys={shortcut.keys} />
              </dt>
              <dd className="overlay__desc">{shortcut.description}</dd>
            </div>
          ))}
        </dl>

        <p className="overlay__foot">
          Shortcuts are ignored while you are typing, except <kbd>esc</kbd> and{' '}
          <kbd>⌘⏎</kbd>.
        </p>
      </div>
    </div>
  );
}
