import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { errorMessage } from '../api/client';
import { fetchDone, undoQueueItem } from '../api/endpoints';
import type { DoneResponse, QueueItem } from '../api/types';
import { isUndoWindowExpired } from '../components/done/apiError';
import { DoneList } from '../components/done/DoneList';
import type { RowUndo } from '../components/done/DoneRow';
import { formatDayLabel } from '../components/done/format';
import { NothingDoneYet } from '../components/done/NothingDoneYet';
import { QueueCleared } from '../components/done/QueueCleared';
import { PageHeader } from '../components/PageHeader';
import '../components/done/done.css';

function SummaryStrip({ data }: { data: DoneResponse }) {
  const { summary } = data;
  return (
    <div className="done-summary">
      {/* The day comes from the server (TRIAGE_TIMEZONE); nothing on this
          page asks the browser what day it is. */}
      <span className="done-summary__date">{formatDayLabel(data.date)}</span>
      <span className="done-summary__zone">{data.timezone}</span>
      <div className="done-summary__counts">
        <span className="done-count">
          <span className="done-count__n">{summary.approved}</span>
          <span className="done-count__label">approved</span>
        </span>
        <span className="done-count">
          <span className="done-count__n">{summary.snoozed}</span>
          <span className="done-count__label">snoozed</span>
        </span>
        <span className="done-count">
          <span className="done-count__n">{summary.dismissed}</span>
          <span className="done-count__label">dismissed</span>
        </span>
      </div>
    </div>
  );
}

interface UndoneNotice {
  verb: string;
  contact: string;
}

/**
 * `/done` — everything actioned today, newest first, and the only route back
 * from a dismiss. No keyboard shortcuts are bound here; all keyboard handling
 * goes through `hooks/useHotkeys.ts`, never a local `keydown` listener.
 */
export function DonePage() {
  const [data, setData] = useState<DoneResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [undoState, setUndoState] = useState<Record<number, RowUndo>>({});
  const [undone, setUndone] = useState<UndoneNotice | null>(null);

  const load = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    try {
      setData(await fetchDone());
      setError(null);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      if (!quiet) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const handleUndo = useCallback(
    async (item: QueueItem) => {
      const verb = item.status === 'snoozed' ? 'Un-snoozed' : 'Undone';
      setUndoState((prev) => ({ ...prev, [item.id]: { phase: 'sending' } }));
      try {
        // Never gated on the browser's clock; the window is the server's call.
        await undoQueueItem(item.id);
        setUndone({ verb, contact: item.lead.contact_name });
        setUndoState((prev) => {
          const next = { ...prev };
          delete next[item.id];
          return next;
        });
        // Refetch rather than splice: `summary.queue_cleared` and the counts
        // are server-computed and must not be inferred from array lengths.
        await load(true);
      } catch (caught) {
        // The row stays on /done either way; only the control goes away.
        const phase = isUndoWindowExpired(caught) ? 'expired' : 'failed';
        setUndoState((prev) => ({
          ...prev,
          [item.id]:
            phase === 'expired'
              ? { phase }
              : { phase, message: errorMessage(caught) },
        }));
      }
    },
    [load],
  );

  return (
    <>
      <PageHeader
        current="/done"
        title="Done Today"
        subtitle="Everything you actioned today. Undo puts an item back in your inbox."
      >
        {data && <SummaryStrip data={data} />}
      </PageHeader>

      <main className="container medium done-page">
        {loading && !data && (
          <div className="done-loading">
            <span className="loading-spinner" aria-hidden="true" />
            <span>Loading today…</span>
          </div>
        )}

        {error && (
          <div className="done-notice" role="alert">
            <span>{error}</span>
            <button type="button" className="secondary" onClick={() => void load()}>
              Try again
            </button>
          </div>
        )}

        {undone && (
          <div className="done-banner" role="status">
            <span className="done-banner__text">
              <strong>{undone.verb}</strong> — {undone.contact} is back in your inbox,
              at the position they were in.
            </span>
            <Link className="done-banner__link" to="/inbox">
              Open inbox
            </Link>
            <button
              type="button"
              className="secondary done-banner__dismiss"
              onClick={() => setUndone(null)}
            >
              Dismiss
            </button>
          </div>
        )}

        {/* Both empty states are selected by the server, never inferred from
            list length. */}
        {data && data.summary.total === 0 && (
          <NothingDoneYet date={data.date} timeZone={data.timezone} />
        )}

        {data && data.summary.queue_cleared && <QueueCleared data={data} />}

        {data && data.items.length > 0 && (
          <DoneList
            items={data.items}
            timeZone={data.timezone}
            onUndo={(item) => void handleUndo(item)}
            undoState={undoState}
          />
        )}
      </main>
    </>
  );
}
