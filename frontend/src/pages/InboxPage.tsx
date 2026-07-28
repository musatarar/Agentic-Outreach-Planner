import { useCallback, useEffect, useState } from 'react';
import { errorMessage } from '../api/client';
import { approveQueueItem } from '../api/endpoints';
import { useQueue } from '../hooks/useQueue';
import { ActionBar } from '../components/inbox/ActionBar';
import { InboxHeader } from '../components/inbox/InboxHeader';
import { LeadCard } from '../components/inbox/LeadCard';
import { QueueRail } from '../components/inbox/QueueRail';
import '../components/inbox/inbox.css';

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
  const { current, items, index, counts, date } = queue;
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  useEffect(() => {
    document.title = 'Triage inbox';
  }, []);

  const { settle } = queue;

  const approve = useCallback(async () => {
    if (!current || busy) return;
    setBusy(true);
    setActionError(null);
    try {
      const updated = await approveQueueItem(current.id);
      settle(updated, 'approved');
    } catch (error) {
      // The server gates approval independently and answers 409
      // `unverified_claims` even when the button looked enabled (§4.6). Surface
      // it rather than pretending the item moved.
      setActionError(errorMessage(error));
    } finally {
      setBusy(false);
    }
  }, [busy, current, settle]);

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

          {current && (
            <>
              {actionError && (
                <div className="inbox__center">
                  <p className="inbox-error" role="alert">
                    {actionError}
                  </p>
                </div>
              )}
              <LeadCard
                key={current.id}
                item={current}
                report={current.verification}
                queueDate={date}
                position={index + 1}
                total={items.length}
                actions={
                  <ActionBar
                    report={current.verification}
                    canApprove={current.can_approve}
                    approving={busy}
                    onApprove={approve}
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
