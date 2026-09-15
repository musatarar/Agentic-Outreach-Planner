import { useCallback, useState } from 'react';
import { errorMessage } from '../../api/client';
import {
  approveAction,
  dismissAction,
  editCopy,
  reopenAction,
} from '../../api/endpoints';
import type { DismissReason, ReviewItem } from '../../api/types';
import { ActionBar } from './ActionBar';
import { DraftEditor } from './DraftEditor';
import { LeadCard } from './LeadCard';
import { writeToClipboard } from './clipboard';
import { useLiveVerify } from './useLiveVerify';

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
  const [draft, setDraft] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);

  const hasPendingEdit = draft !== null && draft !== item.effective_copy;
  const text = draft ?? item.effective_copy;
  const open = editing || hasPendingEdit;

  const live = useLiveVerify(item.id, item.verification, text, open);
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
      onReplace(await editCopy(item.id, { copy: text }));
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
    const clipboardWrite = writeToClipboard(text);
    return run(async () => {
      // Approve uses the *stored* copy, so an uncommitted edit must land first.
      if (hasPendingEdit) await editCopy(item.id, { copy: text });
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
              value={text}
              onChange={setDraft}
              onCommit={() => void commitEdit()}
              onCancel={() => {
                setDraft(null);
                setEditing(false);
              }}
              report={report}
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
            copyText={text}
            editing={open}
            isEdited={item.is_edited}
            hasPendingEdit={hasPendingEdit}
          />
        }
      />
    </>
  );
}
