import { useMemo } from 'react';
import type { CopyPair, VerificationReport } from '../../api/types';
import { DraftField } from './DraftField';
import { splitDraftSegments } from './spans';

export interface DraftEditorProps {
  value: CopyPair;
  onChange: (value: CopyPair) => void;
  /** Cmd/Ctrl+Enter — persists via POST /edit/. */
  onCommit: () => void;
  /** Esc — throws the edit away and returns to the committed copy. */
  onCancel: () => void;
  /** The live report, for the underlines under the caret. */
  report: VerificationReport;
  /** Whether `report` describes exactly the pair on screen. */
  aligned: boolean;
  verifying: boolean;
  /** Focus on mount: true when the user opened the editor, false on resume. */
  autoFocus: boolean;
}

/**
 * In-place editing of the two fields. The report still describes the composed
 * draft, and only the server composes -- so alignment is decided by comparing
 * pairs upstream, never by rebuilding the draft string here. One flag for both
 * fields: either both underline or neither does, so a half-verified view of one
 * report is impossible.
 */
export function DraftEditor({
  value,
  onChange,
  onCommit,
  onCancel,
  report,
  aligned,
  verifying,
  autoFocus,
}: DraftEditorProps) {
  const parts = useMemo(() => splitDraftSegments(report), [report]);

  return (
    <div className={`draft-edit${verifying ? ' draft-edit--verifying' : ''}`}>
      <DraftField
        label="Subject"
        value={value.subject}
        onChange={(subject) => onChange({ ...value, subject })}
        onCommit={onCommit}
        onCancel={onCancel}
        segments={aligned ? parts.subject : null}
        singleLine
        autoFocus={autoFocus}
      />
      <DraftField
        label="Body"
        value={value.body}
        onChange={(body) => onChange({ ...value, body })}
        onCommit={onCommit}
        onCancel={onCancel}
        segments={aligned ? parts.body : null}
      />

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
