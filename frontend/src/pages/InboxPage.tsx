import { useEffect } from 'react';
import { useQueue } from '../hooks/useQueue';
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

  useEffect(() => {
    document.title = 'Triage inbox';
  }, []);

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
            <LeadCard
              key={current.id}
              item={current}
              position={index + 1}
              total={items.length}
            />
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
