import { useCallback, useState } from 'react';
import { errorMessage } from '../../api/client';
import {
  approveAction,
  dismissAction,
  editCopy,
  reopenAction,
} from '../../api/endpoints';
import type { CopyPair, DismissReason, ReviewItem } from '../../api/types';
import { ActionBar } from './ActionBar';
import { DraftEditor } from './DraftEditor';
import { LeadCard } from './LeadCard';
import { writeToClipboard } from './clipboard';
import { samePair, useLiveVerify } from './useLiveVerify';

export interface ReviewCardProps {
  item: ReviewItem;
  /** Hand the server's fresh copy of the item back to the list. */
  onReplace: (item: ReviewItem) => void;
  /** Transient confirmation, shown once by the page. */
  onToast: (message: string) => void;
}

/**
 * One reviewable recommendation: its own draft state, its own live grounding
 * check, its own decision. Nothing here is shared with the next card, so an
 * edit in one cannot render against another's report.
 */
export function ReviewCard({ item, onReplace, onToast }: ReviewCardProps) {
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  // The draft outlives the editor: closing it must not lose the work.
  const [draft, setDraft] = useState<CopyPair | null>(null);
  const [editing, setEditing] = useState(false);

  const committed: CopyPair = {
    subject: item.effective_subject,
    body: item.effective_body,
  };
  const hasPendingEdit = draft !== null && !samePair(draft, committed);
  const pair = draft ?? committed;
  const open = editing || hasPendingEdit;

  const live = useLiveVerify(item.id, item.verification, committed, pair, open);
  // The dry-run report while editing, the stored one otherwise; the server
  // always sends one, so there is never nothing to render.
  const report = live.report ?? item.verification;

  /** Wraps a mutation so one failure path handles every action. */
  const run = useCallback(async (work: () => Promise<void>) => {
    setBusy(true);
    setActionError(null);
    try {
      await work();
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setBusy(false);
    }
  }, []);

  const commitEdit = () =>
    run(async () => {
      onReplace(await editCopy(item.id, pair));
      setDraft(null);
      setEditing(false);
    });

  /** A null copy reverts to the immutable server-side `suggested_copy`. */
  const revert = () =>
    run(async () => {
      onReplace(await editCopy(item.id, { copy: null }));
      setDraft(null);
      setEditing(false);
    });

  /**
   * Copy to clipboard, approve. The clipboard write starts first, unawaited:
   * it must run in the click's user-gesture task or Safari revokes permission.
   */
  const approve = () => {
    // The composed draft is what leaves via the clipboard; the server owns the
    // composition, so this is the last report's copy, not a rebuild of it.
    const clipboardWrite = writeToClipboard(live.aligned ? report.copy : item.effective_copy);
    return run(async () => {
      // Approve uses the *stored* copy, so an uncommitted edit must land first.
      if (hasPendingEdit) await editCopy(item.id, pair);
      const approved = await approveAction(item.id);
      setDraft(null);
      setEditing(false);
      onReplace(approved);
      onToast(
        (await clipboardWrite)
          ? 'Approved · copied to clipboard'
          : 'Approved · clipboard blocked',
      );
    });
  };

  const dismiss = (reason: DismissReason) =>
    run(async () => {
      onReplace(await dismissAction(item.id, { reason }));
      setDraft(null);
      setEditing(false);
      onToast('Dismissed');
    });

  const reopen = () =>
    run(async () => {
      onReplace(await reopenAction(item.id));
      onToast('Reopened');
    });

  return (
    <>
      {(actionError || live.error) && (
        <div className="inbox__center">
          <p className="inbox-error" role="alert">
            {actionError ?? `Could not re-check the copy: ${live.error}`}
          </p>
        </div>
      )}
      <LeadCard
        item={item}
        report={report}
        draft={
          open ? (
            <DraftEditor
              value={pair}
              onChange={setDraft}
              onCommit={() => void commitEdit()}
              onCancel={() => {
                setDraft(null);
                setEditing(false);
              }}
              report={report}
              aligned={live.aligned}
              verifying={live.verifying}
              autoFocus={editing}
            />
          ) : undefined
        }
        onDraftClick={item.status === 'pending' ? () => setEditing(true) : undefined}
        actions={
          <ActionBar
            itemId={item.id}
            report={report}
            status={item.status}
            // Live edits gate on the dry-run report, not the stale server verdict.
            canApprove={live.isLive ? report.can_approve : item.can_approve}
            busy={busy}
            onApprove={() => void approve()}
            onEdit={() => setEditing(true)}
            onRevert={() => void revert()}
            onDismiss={(reason) => void dismiss(reason)}
            onReopen={() => void reopen()}
            copyText={live.aligned ? report.copy : item.effective_copy}
            editing={open}
            isEdited={item.is_edited}
            hasPendingEdit={hasPendingEdit}
          />
        }
      />
    </>
  );
}
