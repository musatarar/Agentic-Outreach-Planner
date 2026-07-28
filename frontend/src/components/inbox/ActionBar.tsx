import { Badge, Button, KeyHint } from '../ui';
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
   * The server's verdict, taken whole. CONTRACT §9.3 and §4.4: the frontend
   * never counts claims and never recomputes this from the summary. The two
   * answer different questions — a draft can read `4 of 4 claims verified` and
   * still be blocked, because an unauthorized offer does not count toward the
   * ratio but does block approval.
   */
  canApprove: boolean;
  approving: boolean;
  onApprove: () => void;
}

/**
 * The verification summary and the one primary action on the screen.
 *
 * When approval is blocked the primary button is replaced rather than merely
 * disabled, and the replacement names the specific claim in the user's own
 * words — quoting the text they can see underlined in red a few lines above.
 * A greyed-out button with no explanation is the failure mode this design
 * exists to avoid.
 */
export function ActionBar({ report, canApprove, approving, onApprove }: ActionBarProps) {
  const blocker = canApprove ? null : findBlockingClaim(report);
  const cause = blockerCause(blocker);

  return (
    <div className="action-bar">
      <div className="action-bar__summary">
        <Badge tone={report.unverified_count === 0 ? 'verified' : 'unverified'}>
          {report.unverified_count === 0 ? 'grounded' : 'check'}
        </Badge>
        {/* Server-rendered, printed verbatim (§9.3). */}
        <span className="action-bar__summary-text">{report.summary}</span>
      </div>

      {canApprove ? (
        <div className="action-bar__actions">
          <Button variant="primary" loading={approving} onClick={onApprove}>
            Approve &amp; copy
          </Button>
          <KeyHint keys={['A']} />
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
          </div>
        </div>
      )}
    </div>
  );
}
