import { useCallback, useEffect, useMemo, useState } from 'react';
import { errorMessage } from '../api/client';
import {
  approveQueueItem,
  dismissQueueItem,
  editQueueCopy,
  snoozeQueueItem,
} from '../api/endpoints';
import type { DismissReason, QueueItem, SnoozeInput } from '../api/types';
import { useHotkeys, type HotkeyMap } from '../hooks/useHotkeys';
import { useQueue } from '../hooks/useQueue';
import { ActionBar } from '../components/inbox/ActionBar';
import { DismissPopover } from '../components/inbox/DismissPopover';
import { DraftEditor } from '../components/inbox/DraftEditor';
import { InboxEmptyState } from '../components/inbox/InboxEmptyState';
import { InboxHeader } from '../components/inbox/InboxHeader';
import { LeadCard } from '../components/inbox/LeadCard';
import { QueueRail } from '../components/inbox/QueueRail';
import { ShortcutOverlay } from '../components/inbox/ShortcutOverlay';
import { SnoozePopover } from '../components/inbox/SnoozePopover';
import { writeToClipboard } from '../components/inbox/clipboard';
import { useLiveVerify } from '../components/inbox/useLiveVerify';
import '../components/inbox/inbox.css';

/** Local, uncommitted edits, keyed by queue item id. */
type DraftMap = Record<number, string>;

/**
 * What owns the keyboard right now. Only `browse` gets the J/K/A/E/S/X map;
 * anything else would let `A` approve a lead from behind an open popover.
 */
type Mode = 'browse' | 'snooze' | 'dismiss' | 'shortcuts';

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
  const [mode, setMode] = useState<Mode>('browse');
  const [toast, setToast] = useState<string | null>(null);
  // The single polite live region. Every status change writes here, so a
  // screen-reader user hears the same thing a sighted user sees.
  const [announcement, setAnnouncement] = useState('');
  // Drafts outlive the editor on purpose: navigating away mid-edit must not
  // throw the work away. Only Esc discards, and only the current lead's draft.
  const [drafts, setDrafts] = useState<DraftMap>({});
  const [editingId, setEditingId] = useState<number | null>(null);

  useEffect(() => {
    document.title = 'Triage inbox';
  }, []);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 2400);
    return () => window.clearTimeout(timer);
  }, [toast]);

  const pending = current ? drafts[current.id] : undefined;
  const hasPendingEdit =
    current !== null && pending !== undefined && pending !== current.effective_copy;
  // Resume an interrupted edit on return, but do not steal focus doing it.
  const editing = current !== null && (editingId === current.id || hasPendingEdit);
  const draft = pending ?? current?.effective_copy ?? '';

  const live = useLiveVerify(current?.id ?? null, current?.verification ?? null, draft, editing);
  const report = live.report;

  // Announce the lead the caret landed on, so advancing is not silent.
  useEffect(() => {
    if (!current) return;
    setAnnouncement(
      `Lead ${index + 1} of ${items.length}. ${current.lead.contact_name}, ` +
        `${current.lead.agency_name}. Priority ${current.priority}. ${current.action_label}.`,
    );
  }, [current, index, items.length]);

  const setDraft = useCallback((id: number, value: string) => {
    setDrafts((existing) => ({ ...existing, [id]: value }));
  }, []);

  const clearDraft = useCallback((id: number) => {
    setDrafts((existing) => {
      if (!(id in existing)) return existing;
      const next = { ...existing };
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
        setAnnouncement(`Edit saved. ${updated.verification.summary}.`);
      }),
    [clearDraft, replace, run],
  );

  /** Esc — the draft goes, the committed copy stays. */
  const cancelEdit = useCallback(
    (item: QueueItem) => {
      clearDraft(item.id);
      setEditingId(null);
      setAnnouncement('Edit discarded.');
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
        setAnnouncement('Reverted to the original draft.');
      }),
    [clearDraft, replace, run],
  );

  /**
   * One keystroke, three effects: the text lands on the clipboard, the item is
   * approved, and the queue advances.
   *
   * The clipboard write is started **first and without awaiting anything
   * before it**, because it must run inside the user-gesture task that the
   * keydown created. Await a fetch first and Safari revokes permission.
   */
  const approve = useCallback(
    (item: QueueItem) => {
      const localCopy = drafts[item.id];
      const dirty = localCopy !== undefined && localCopy !== item.effective_copy;
      const copyForClipboard = dirty ? localCopy : item.effective_copy;
      const clipboardWrite = writeToClipboard(copyForClipboard);

      return run(async () => {
        // Approve acts on what is *persisted* — it takes an empty body and uses
        // the stored copy. An uncommitted edit therefore has to land first, or
        // the user would approve text they can see they changed.
        if (dirty) await editQueueCopy(item.id, { copy: localCopy });
        const approved = await approveQueueItem(item.id);
        clearDraft(item.id);
        setEditingId(null);
        settle(approved, 'approved');

        const copied = await clipboardWrite;
        // A silent clipboard write feels like nothing happened, so it is always
        // confirmed — including when it failed, which the user needs to know
        // before they switch to Gmail and paste something stale.
        setToast(copied ? 'Approved · copied to clipboard' : 'Approved · clipboard blocked');
        setAnnouncement(
          copied
            ? `Approved ${item.lead.contact_name}. Draft copied to the clipboard.`
            : `Approved ${item.lead.contact_name}. The clipboard could not be written.`,
        );
      });
    },
    [clearDraft, drafts, run, settle],
  );

  const snooze = useCallback(
    (item: QueueItem, input: SnoozeInput) =>
      run(async () => {
        const updated = await snoozeQueueItem(item.id, input);
        setMode('browse');
        clearDraft(item.id);
        setEditingId(null);
        settle(updated, 'snoozed');
        setToast('Snoozed');
        setAnnouncement(`Snoozed ${item.lead.contact_name}.`);
      }),
    [clearDraft, run, settle],
  );

  const dismiss = useCallback(
    (item: QueueItem, reason: DismissReason) =>
      run(async () => {
        const updated = await dismissQueueItem(item.id, { reason });
        setMode('browse');
        clearDraft(item.id);
        setEditingId(null);
        settle(updated, 'dismissed');
        setToast('Dismissed');
        setAnnouncement(`Dismissed ${item.lead.contact_name}.`);
      }),
    [clearDraft, run, settle],
  );

  /**
   * The browse map. Rebuilt when the current lead changes, which is cheap, and
   * muted entirely while a popover or the overlay owns the keyboard.
   *
   * `useHotkeys` already refuses to fire inside a text field (§9.13), so
   * nothing here has to think about the inline editor.
   */
  const hotkeys = useMemo<HotkeyMap>(() => {
    const map: HotkeyMap = {
      j: queue.next,
      arrowdown: queue.next,
      k: queue.previous,
      arrowup: queue.previous,
      '?': () => setMode('shortcuts'),
    };
    if (current) {
      map.a = () => void approve(current);
      map.e = () => setEditingId(current.id);
      map.s = () => setMode('snooze');
      map.x = () => setMode('dismiss');
    }
    return map;
  }, [approve, current, queue.next, queue.previous]);

  useHotkeys(hotkeys, { enabled: mode === 'browse' && !busy });

  // Esc always gets out of whatever is open, from anywhere.
  useHotkeys(
    useMemo<HotkeyMap>(() => ({ escape: () => setMode('browse') }), []),
    { enabled: mode !== 'browse' },
  );

  return (
    <div className="inbox">
      <InboxHeader counts={counts} date={date} />

      <div className="inbox__body">
        <QueueRail items={items} index={index} onSelect={queue.select} />

        <main className="inbox__main">
          <div className="inbox-sr-only" role="status" aria-live="polite">
            {announcement}
          </div>

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
                  // The popovers are absolutely positioned siblings of the
                  // action bar, so this wrapper -- not .action-bar -- is their
                  // containing block. Without it they resolve against the
                  // viewport and `bottom: 100%` puts them off-screen.
                  <div className="action-bar-anchor">
                    <ActionBar
                      report={report}
                      // While an edit is live the dry-run report is the honest
                      // gate; otherwise the item's own server verdict is (§9.3).
                      canApprove={live.isLive ? report.can_approve : current.can_approve}
                      approving={busy}
                      onApprove={() => void approve(current)}
                      onEdit={() => setEditingId(current.id)}
                      onRevert={() => void revert(current)}
                      onSnooze={() => setMode('snooze')}
                      onDismiss={() => setMode('dismiss')}
                      copyText={draft}
                      editing={editing}
                      isEdited={current.is_edited}
                      hasPendingEdit={hasPendingEdit}
                    />

                    {mode === 'snooze' && (
                      <SnoozePopover
                        queueDate={date}
                        onSnooze={(input) => void snooze(current, input)}
                        onClose={() => setMode('browse')}
                      />
                    )}
                    {mode === 'dismiss' && (
                      <DismissPopover
                        onDismiss={(reason) => void dismiss(current, reason)}
                        onClose={() => setMode('browse')}
                      />
                    )}
                  </div>
                }
              />
            </>
          )}

          {queue.cleared && (
            <div className="inbox__center">
              <InboxEmptyState summary={queue.doneSummary} error={queue.doneSummaryError} />
            </div>
          )}
        </main>
      </div>

      {toast && (
        <div className="toast" role="status">
          {toast}
        </div>
      )}

      {mode === 'shortcuts' && <ShortcutOverlay onClose={() => setMode('browse')} />}
    </div>
  );
}
