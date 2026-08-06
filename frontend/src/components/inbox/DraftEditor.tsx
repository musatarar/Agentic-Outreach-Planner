import { useEffect, useRef } from 'react';
import type { KeyboardEvent } from 'react';
import type { VerificationReport } from '../../api/types';
import { VerifiedDraft } from './VerifiedDraft';

export interface DraftEditorProps {
  value: string;
  onChange: (value: string) => void;
  /** Cmd/Ctrl+Enter — persists via POST /edit/. */
  onCommit: () => void;
  /** Esc — throws the edit away and returns to the committed copy. */
  onCancel: () => void;
  /** The live report, for the underlines under the caret. */
  report: VerificationReport;
  verifying: boolean;
  /** Focus on mount: true when the user opened the editor, false on resume. */
  autoFocus: boolean;
}

/**
 * In-place editing. Not a modal — same position, same typography, no shift.
 *
 * Three layers share one grid cell so the text never moves when the mode
 * changes:
 *
 *   1. a hidden sizer that gives the cell the height of the current text,
 *   2. the mirror, which draws the text *and its underlines*,
 *   3. the textarea, whose own glyphs are transparent so only its caret and
 *      selection show through.
 *
 * The point of the overlay is that verification stays visible while typing:
 * change `6 deals` to `9 deals` and the underline goes red under the caret,
 * about a quarter-second later. A plain textarea would hide the one signal the
 * user most needs while they are in the act of introducing the error.
 *
 * The mirror only draws underlines when the report describes exactly the text
 * on screen. Mid-keystroke the two differ, so it falls back to plain text —
 * spans from an older string laid over a newer one would mark the wrong words,
 * which is worse than no marks at all.
 */
export function DraftEditor({
  value,
  onChange,
  onCommit,
  onCancel,
  report,
  verifying,
  autoFocus,
}: DraftEditorProps) {
  const input = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    if (!autoFocus) return;
    const node = input.current;
    if (!node) return;
    node.focus();
    // Land the caret at the end rather than selecting everything, so the first
    // keystroke does not wipe the draft.
    node.setSelectionRange(node.value.length, node.value.length);
  }, [autoFocus]);

  // These two are the pair that `useHotkeys` allow-lists through the global
  // hotkey guard precisely so this editor can hear them from inside a textarea.
  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      onCommit();
      return;
    }
    if (event.key === 'Escape') {
      event.preventDefault();
      onCancel();
    }
  }

  const aligned = report.copy === value;
  // A trailing newline has no height of its own; the zero-width space gives the
  // sizer the empty last line the textarea reserves for the caret.
  const sizerText = value.endsWith('\n') ? `${value}\u200b` : value;

  return (
    <div className={`draft-edit${verifying ? ' draft-edit--verifying' : ''}`}>
      <div className="draft-edit__stack">
        <div className="draft-edit__layer draft-edit__sizer" aria-hidden="true">
          <div className="draft">{sizerText}</div>
        </div>

        <div className="draft-edit__layer draft-edit__mirror" aria-hidden="true">
          {aligned ? (
            <VerifiedDraft report={report} />
          ) : (
            <div className="draft">{value}</div>
          )}
        </div>

        <textarea
          ref={input}
          className="draft draft-edit__layer draft-edit__input"
          value={value}
          // Off deliberately: the browser's misspelling indicator is a red wavy
          // underline, which is exactly the mark this design reserves for an
          // unverified claim. Two different meanings in one glyph is worse than
          // no spellcheck.
          spellCheck={false}
          aria-label="Draft copy"
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={handleKeyDown}
        />
      </div>

      <p className="draft-edit__hint">
        <kbd className="draft-edit__key">⌘⏎</kbd> save
        <span className="draft-edit__sep">·</span>
        <kbd className="draft-edit__key">esc</kbd> discard
        <span className="draft-edit__sep">·</span>
        <span aria-live="polite">
          {verifying ? 'checking claims…' : aligned ? 'claims checked' : 'unchecked'}
        </span>
      </p>
    </div>
  );
}
