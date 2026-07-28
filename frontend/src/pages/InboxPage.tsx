import { useCallback, useEffect, useState } from 'react';
import { errorMessage } from '../api/client';
import { approveQueueItem, editQueueCopy } from '../api/endpoints';
import type { QueueItem } from '../api/types';
import { useQueue } from '../hooks/useQueue';
import { ActionBar } from '../components/inbox/ActionBar';
import { DraftEditor } from '../components/inbox/DraftEditor';
import { InboxHeader } from '../components/inbox/InboxHeader';
import { LeadCard } from '../components/inbox/LeadCard';
import { QueueRail } from '../components/inbox/QueueRail';
import { useLiveVerify } from '../components/inbox/useLiveVerify';
import '../components/inbox/inbox.css';

/** Local, uncommitted edits, keyed by queue item id. */
type DraftMap = Record<number, string>;

/**
 * The triage inbox.
 *
 * The loop this screen exists for is not "generate emails". It is a human
 * calibrating trust in a machine's judgement, one lead at a time: show the
 * reasoning, make agreement one keystroke, make disagreement cheap to express.
 *
 * Everything the screen needs arrives in the single prefetch behind `useQueue`,
 * so moving between leads is a state change and never a request. There is no
 * spinner between leads because there is nothing to wait for.
 */
export function InboxPage() {
  const queue = useQueue();
  const { current, items, index, counts, date, replace, settle } = queue;

  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  // Drafts outlive the editor on purpose: navigating away mid-edit must not
  // throw the work away. Only Esc discards, and only the current lead's draft.
  const [drafts, setDrafts] = useState<DraftMap>({});
  const [editingId, setEditingId] = useState<number | null>(null);

  useEffect(() => {
    document.title = 'Triage inbox';
  }, []);

  const pending = current ? drafts[current.id] : undefined;
  const hasPendingEdit =
    current !== null && pending !== undefined && pending !== current.effective_copy;
  // Resume an interrupted edit on return, but do not steal focus doing it.
  const editing = current !== null && (editingId === current.id || hasPendingEdit);
  const draft = pending ?? current?.effective_copy ?? '';

  const live = useLiveVerify(
    current?.id ?? null,
    current?.verification ?? null,
    draft,
    editing,
  );
  const report = live.report;

  const setDraft = useCallback((id: number, value: string) => {
    setDrafts((current) => ({ ...current, [id]: value }));
  }, []);

  const clearDraft = useCallback((id: number) => {
    setDrafts((current) => {
      if (!(id in current)) return current;
      const next = { ...current };
      delete next[id];
      return next;
    });
  }, []);

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

  /** Cmd/Ctrl+Enter — persist the draft and re-verify server-side. */
  const commitEdit = useCallback(
    (item: QueueItem, copy: string) =>
      run(async () => {
        const updated = await editQueueCopy(item.id, { copy });
        replace(updated);
        clearDraft(item.id);
        setEditingId(null);
      }),
    [clearDraft, replace, run],
  );

  /** Esc — the draft goes, the committed copy stays. */
  const cancelEdit = useCallback(
    (item: QueueItem) => {
      clearDraft(item.id);
      setEditingId(null);
    },
    [clearDraft],
  );

  /**
   * `suggested_copy` is immutable server-side, so an edit is always recoverable.
   * The same endpoint with a null copy is the documented way back (§5.2).
   */
  const revert = useCallback(
    (item: QueueItem) =>
      run(async () => {
        const updated = await editQueueCopy(item.id, { copy: null });
        replace(updated);
        clearDraft(item.id);
        setEditingId(null);
      }),
    [clearDraft, replace, run],
  );

  const approve = useCallback(
    (item: QueueItem) =>
      run(async () => {
        // Approve acts on what is *persisted* — it takes an empty body and uses
        // the stored copy. An uncommitted edit therefore has to land first, or
        // the user would approve text they can see they changed.
        const localCopy = drafts[item.id];
        if (localCopy !== undefined && localCopy !== item.effective_copy) {
          await editQueueCopy(item.id, { copy: localCopy });
        }
        const approved = await approveQueueItem(item.id);
        clearDraft(item.id);
        setEditingId(null);
        settle(approved, 'approved');
      }),
    [clearDraft, drafts, run, settle],
  );

  return (
    <div className="inbox">
      <InboxHeader counts={counts} date={date} />

      <div className="inbox__body">
        <QueueRail items={items} index={index} onSelect={queue.select} />

        <main className="inbox__main">
          {queue.error && (
            <div className="inbox__center">
              <p className="inbox-error" role="alert">
                Could not load the queue: {queue.error}
              </p>
            </div>
          )}

          {/* The only spinner in the product, and it is only ever seen once. */}
          {queue.loading && (
            <p className="inbox-status" role="status">
              Loading today's queue…
            </p>
          )}

          {current && report && (
            <>
              {(actionError || live.error) && (
                <div className="inbox__center">
                  <p className="inbox-error" role="alert">
                    {actionError ?? `Could not re-check the copy: ${live.error}`}
                  </p>
                </div>
              )}
              <LeadCard
                key={current.id}
                item={current}
                report={report}
                queueDate={date}
                position={index + 1}
                total={items.length}
                draft={
                  editing ? (
                    <DraftEditor
                      value={draft}
                      onChange={(value) => setDraft(current.id, value)}
                      onCommit={() => void commitEdit(current, draft)}
                      onCancel={() => cancelEdit(current)}
                      report={report}
                      verifying={live.verifying}
                      autoFocus={editingId === current.id}
                    />
                  ) : undefined
                }
                onDraftClick={() => setEditingId(current.id)}
                actions={
                  <ActionBar
                    report={report}
                    // While an edit is live the dry-run report is the honest
                    // gate; otherwise the item's own server verdict is (§9.3).
                    canApprove={live.isLive ? report.can_approve : current.can_approve}
                    approving={busy}
                    onApprove={() => void approve(current)}
                    onEdit={() => setEditingId(current.id)}
                    onRevert={() => void revert(current)}
                    editing={editing}
                    isEdited={current.is_edited}
                    hasPendingEdit={hasPendingEdit}
                  />
                }
              />
            </>
          )}

          {queue.cleared && (
            <p className="inbox-status" role="status">
              Queue clear.
            </p>
          )}
        </main>
      </div>
    </div>
  );
}
