import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { useQueue } from '../hooks/useQueue';
import { documentTitle } from '../util/brand';
import { InboxHeader } from '../components/inbox/InboxHeader';
import { ReviewCard } from '../components/inbox/ReviewCard';
import '../components/inbox/inbox.css';

/**
 * The review inbox: every lead's latest recommendation, newest decision and
 * all. Everything arrives in the single prefetch behind `useQueue`, and each
 * card owns its own draft and grounding check.
 */
export function InboxPage() {
  const { loading, error, items, total, pending, replace, reload } = useQueue();
  const [toast, setToast] = useState<string | null>(null);

  useEffect(() => {
    document.title = documentTitle('Review inbox');
  }, []);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 2400);
    return () => window.clearTimeout(timer);
  }, [toast]);

  return (
    <div className="inbox">
      <InboxHeader pending={pending} loaded={items.length} total={total} onReload={reload} />

      <div className="inbox__body">
        <main className="inbox__main">
          {error && (
            <div className="inbox__center">
              <p className="inbox-error" role="alert">
                Could not load the inbox: {error}
              </p>
            </div>
          )}

          {loading && (
            <p className="inbox-status" role="status">
              Loading the inbox…
            </p>
          )}

          {!loading && !error && items.length === 0 && (
            <div className="inbox__center">
              <p className="inbox-status" role="status">
                Nothing to review yet. Generate drafts from the{' '}
                <Link to="/leads/">book of leads</Link>.
              </p>
            </div>
          )}

          {items.map((item) => (
            <ReviewCard key={item.id} item={item} onReplace={replace} onToast={setToast} />
          ))}
        </main>
      </div>

      {toast && (
        <div className="toast" role="status">
          {toast}
        </div>
      )}
    </div>
  );
}
