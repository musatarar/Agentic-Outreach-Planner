import { useState } from 'react';
import { Badge, Button } from '../ui';
import { CopyButton } from '../CopyButton';
import type { DismissReason, ReviewStatus, VerificationReport } from '../../api/types';
import { blockerCause, findBlockingClaim } from './spans';

const BLOCKER_HEADLINE = {
  unauthorized_offer: 'This draft promises something you cannot offer',
  unverified_claim: 'A claim in this draft does not match the record',
  unknown: 'This draft cannot be approved yet',
} as const;

const BLOCKER_BUTTON = {
  unauthorized_offer: 'Blocked — unauthorized offer',
  unverified_claim: 'Blocked — unverified claim',
  unknown: 'Blocked',
} as const;

/** Mirrors `OutreachGeneratedCopy.DISMISS_REASONS`; "" is the allowed no-reason. */
const DISMISS_REASONS: { value: DismissReason; label: string }[] = [
  { value: '', label: 'No reason given' },
  { value: 'not_a_fit', label: 'Not a fit' },
  { value: 'bad_timing', label: 'Bad timing' },
  { value: 'wrong_contact', label: 'Wrong contact' },
  { value: 'already_handled', label: 'Already handled' },
  { value: 'copy_unusable', label: 'Copy unusable' },
  { value: 'other', label: 'Other' },
];

const DECIDED_LABEL = {
  approved: 'Approved — copy it from here whenever you are ready.',
  dismissed: 'Dismissed — this recommendation will not come back on a re-run.',
} as const;

export interface ActionBarProps {
  /** Only used to keep the dismiss-reason label bound to its own select. */
  itemId: number;
  report: VerificationReport;
  status: ReviewStatus;
  /**
   * The server's verdict, taken whole — never recomputed from the summary
   * ("4 of 4 verified" can still be blocked by an unauthorized offer).
   */
  canApprove: boolean;
  busy: boolean;
  onApprove: () => void;
  onEdit: () => void;
  onDismiss: (reason: DismissReason) => void;
  onReopen: () => void;
  /** The text a plain copy (no approval) should put on the clipboard. */
  copyText: string;
  /** Back to `suggested_copy` via POST /edit/ with {"copy": null}. */
  onRevert: () => void;
  editing: boolean;
  /** Server-computed; the frontend never compares the copy strings. */
  isEdited: boolean;
  /** Local edits not yet sent to /edit/. */
  hasPendingEdit: boolean;
}

/**
 * The verification summary and the decision. When approval is blocked the
 * button is replaced, naming the specific claim, not merely disabled. A decided
 * item keeps only the two moves that still make sense: copy, and reopen.
 */
export function ActionBar({
  itemId,
  report,
  status,
  canApprove,
  busy,
  onApprove,
  onEdit,
  onRevert,
  onDismiss,
  onReopen,
  copyText,
  editing,
  isEdited,
  hasPendingEdit,
}: ActionBarProps) {
  const [reason, setReason] = useState<DismissReason>('');
  const blocker = canApprove ? null : findBlockingClaim(report);
  const cause = blockerCause(blocker);

  const summary = (
    <div className="action-bar__summary">
      <Badge tone={report.unverified_count === 0 ? 'verified' : 'unverified'}>
        {report.unverified_count === 0 ? 'grounded' : 'check'}
      </Badge>
      {/* Server-rendered, printed verbatim. */}
      <span className="action-bar__summary-text">{report.summary}</span>
      {hasPendingEdit && <span className="action-bar__pending">unsaved</span>}
    </div>
  );

  if (status !== 'pending') {
    return (
      <div className="action-bar">
        {summary}
        <div className="action-bar__actions">
          <span className="action-bar__decided">{DECIDED_LABEL[status]}</span>
          <CopyButton text={copyText} />
          <Button variant="secondary" loading={busy} onClick={onReopen}>
            Reopen
          </Button>
        </div>
      </div>
    );
  }

  // Same secondary row in both approvable states.
  const secondary = (
    <>
      {/* Copy without approving. */}
      <CopyButton text={copyText} />
      {/* `suggested_copy` is immutable, so an edit is always undoable. */}
      {isEdited && (
        <Button variant="ghost" onClick={onRevert}>
          Revert to original
        </Button>
      )}
      <span className="action-bar__dismiss">
        <label className="action-bar__dismiss-label" htmlFor={`dismiss-reason-${itemId}`}>
          Dismiss because
        </label>
        <select
          id={`dismiss-reason-${itemId}`}
          className="action-bar__select"
          value={reason}
          onChange={(event) => setReason(event.target.value as DismissReason)}
        >
          {DISMISS_REASONS.map((option) => (
            <option key={option.value || 'none'} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
        <Button variant="ghost" onClick={() => onDismiss(reason)}>
          Dismiss
        </Button>
      </span>
    </>
  );

  return (
    <div className="action-bar">
      {summary}

      {canApprove ? (
        <div className="action-bar__actions">
          <Button variant="primary" loading={busy} onClick={onApprove}>
            Approve &amp; copy
          </Button>
          {!editing && (
            <Button variant="ghost" onClick={onEdit}>
              Edit
            </Button>
          )}
          {secondary}
        </div>
      ) : (
        <div className="action-bar__blocked">
          <p className="action-bar__blocked-headline" role="alert">
            {BLOCKER_HEADLINE[cause]}
          </p>
          {blocker && (
            <p className="action-bar__blocked-detail">
              {blocker.text && <q className="action-bar__blocked-quote">{blocker.text}</q>}
              {blocker.message}
            </p>
          )}
          <div className="action-bar__actions">
            <Button variant="danger" disabled>
              {BLOCKER_BUTTON[cause]}
            </Button>
            {/* The way out of a block is to change the copy. */}
            {!editing && (
              <Button variant="secondary" onClick={onEdit}>
                Edit the draft
              </Button>
            )}
            {secondary}
          </div>
        </div>
      )}
    </div>
  );
}
