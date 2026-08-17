import { Badge, Button, KeyHint } from '../ui';
import { CopyButton } from '../CopyButton';
import type { VerificationReport } from '../../api/types';
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

export interface ActionBarProps {
  report: VerificationReport;
  /**
   * The server's verdict, taken whole — never recomputed from the summary
   * ("4 of 4 verified" can still be blocked by an unauthorized offer).
   */
  canApprove: boolean;
  approving: boolean;
  onApprove: () => void;
  onEdit: () => void;
  onSnooze: () => void;
  onDismiss: () => void;
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
 * The verification summary and the primary action. When approval is blocked
 * the button is replaced, naming the specific claim, not merely disabled.
 */
export function ActionBar({
  report,
  canApprove,
  approving,
  onApprove,
  onEdit,
  onRevert,
  onSnooze,
  onDismiss,
  copyText,
  editing,
  isEdited,
  hasPendingEdit,
}: ActionBarProps) {
  const blocker = canApprove ? null : findBlockingClaim(report);
  const cause = blockerCause(blocker);

  // Same secondary row in both states.
  const secondary = (
    <>
      <Button variant="ghost" onClick={onSnooze}>
        Snooze
      </Button>
      <KeyHint keys={['S']} />
      <Button variant="ghost" onClick={onDismiss}>
        Dismiss
      </Button>
      <KeyHint keys={['X']} />
      {/* Copy without approving. */}
      <CopyButton text={copyText} />
      {/* `suggested_copy` is immutable, so an edit is always undoable. */}
      {isEdited && (
        <Button variant="ghost" onClick={onRevert}>
          Revert to original
        </Button>
      )}
    </>
  );

  return (
    <div className="action-bar">
      <div className="action-bar__summary">
        <Badge tone={report.unverified_count === 0 ? 'verified' : 'unverified'}>
          {report.unverified_count === 0 ? 'grounded' : 'check'}
        </Badge>
        {/* Server-rendered, printed verbatim. */}
        <span className="action-bar__summary-text">{report.summary}</span>
        {hasPendingEdit && <span className="action-bar__pending">unsaved</span>}
      </div>

      {canApprove ? (
        <div className="action-bar__actions">
          <Button variant="primary" loading={approving} onClick={onApprove}>
            Approve &amp; copy
          </Button>
          <KeyHint keys={['A']} />
          {!editing && (
            <Button variant="ghost" onClick={onEdit}>
              Edit
            </Button>
          )}
          <KeyHint keys={['E']} />
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
