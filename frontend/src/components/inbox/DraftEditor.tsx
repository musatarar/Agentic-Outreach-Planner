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
 * In-place editing. Three layers share one grid cell: a hidden sizer for
 * height, a mirror that draws the text with its underlines, and a textarea
 * with transparent glyphs so only its caret shows. The mirror underlines only
 * when the report matches the on-screen text exactly; mid-keystroke it falls
 * back to plain text so stale spans never mark the wrong words.
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
    if (event.key === 'Escape') {
      event.preventDefault();
      onCancel();
    }
  }

  const aligned = report.copy === value;
  // The zero-width space gives the sizer the empty last line the textarea
  // reserves for the caret; a trailing newline has no height of its own.
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
          // Off: the browser's red wavy misspelling underline would collide
          // with the mark reserved for an unverified claim.
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
