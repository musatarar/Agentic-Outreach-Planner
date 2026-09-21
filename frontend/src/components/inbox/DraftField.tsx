import { useEffect, useRef } from 'react';
import type { KeyboardEvent } from 'react';
import type { DraftSegment } from './spans';
import { DraftRun } from './VerifiedDraft';

export interface DraftFieldProps {
  label: string;
  value: string;
  onChange: (value: string) => void;
  /** Cmd/Ctrl+Enter — persists via POST /edit/. */
  onCommit: () => void;
  /** Esc — throws the edit away and returns to the committed copy. */
  onCancel: () => void;
  /** The live report's runs for this field; null while they are stale. */
  segments: DraftSegment[] | null;
  /** True for the subject: one line, so Enter is not a newline. */
  singleLine?: boolean;
  autoFocus?: boolean;
}

/**
 * One editable field of the draft. Three layers share a grid cell: a hidden
 * sizer for height, a mirror that draws the text with its underlines, and a
 * textarea with transparent glyphs so only its caret shows. The mirror
 * underlines only when `segments` describe the on-screen text exactly;
 * mid-keystroke it falls back to plain text so stale spans never mark the
 * wrong words.
 */
export function DraftField({
  label,
  value,
  onChange,
  onCommit,
  onCancel,
  segments,
  singleLine = false,
  autoFocus = false,
}: DraftFieldProps) {
  const input = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    if (!autoFocus) return;
    const node = input.current;
    if (!node) return;
    node.focus();
    // Caret at the end, not select-all, so the first keystroke keeps the draft.
    node.setSelectionRange(node.value.length, node.value.length);
  }, [autoFocus]);

  // The pair `useHotkeys` allow-lists through its text-field guard.
  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      onCommit();
      return;
    }
    if (event.key === 'Enter' && singleLine) {
      // A subject is one line; a newline in it would move the boundary the
      // draft is split on.
      event.preventDefault();
      return;
    }
    if (event.key === 'Escape') {
      event.preventDefault();
      onCancel();
    }
  }

  // The zero-width space gives the sizer the empty last line the textarea
  // reserves for the caret; a trailing newline has no height of its own.
  const sizerText = value.endsWith('\n') ? `${value}​` : value;
  const draftClass = `draft${singleLine ? ' draft--subject' : ''}`;

  return (
    <div className="draft-field">
      <div className="draft-field__label">{label}</div>
      <div className="draft-edit__stack">
        <div className="draft-edit__layer draft-edit__sizer" aria-hidden="true">
          <div className={draftClass}>{sizerText}</div>
        </div>

        <div className="draft-edit__layer draft-edit__mirror" aria-hidden="true">
          <div className={draftClass}>
            {segments ? <DraftRun segments={segments} /> : value}
          </div>
        </div>

        <textarea
          ref={input}
          className={`${draftClass} draft-edit__layer draft-edit__input`}
          value={value}
          rows={1}
          // Off: the browser's red wavy misspelling underline would collide
          // with the mark reserved for an unverified claim.
          spellCheck={false}
          aria-label={`Draft ${label.toLowerCase()}`}
          onChange={(event) =>
            onChange(singleLine ? event.target.value.replace(/\n/g, ' ') : event.target.value)
          }
          onKeyDown={handleKeyDown}
        />
      </div>
    </div>
  );
}
