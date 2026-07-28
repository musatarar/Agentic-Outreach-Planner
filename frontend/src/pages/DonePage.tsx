import { useCallback, useEffect, useState } from 'react';
import { errorMessage } from '../api/client';
import { fetchDone } from '../api/endpoints';
import type { DoneResponse } from '../api/types';
import { DoneList } from '../components/done/DoneList';
import { formatDayLabel } from '../components/done/format';
import { PageHeader } from '../components/PageHeader';
import '../components/done/done.css';

function SummaryStrip({ data }: { data: DoneResponse }) {
  const { summary } = data;
  return (
    <div className="done-summary">
      {/* CONTRACT §9.5: the day comes from the server, computed in
          TRIAGE_TIMEZONE. Nothing on this page asks the browser what day it
          is — a reviewer in UTC-7 clearing the queue at 6pm local would
          otherwise be told they had done nothing. */}
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

/**
 * `/done` — everything actioned today, newest first.
 *
 * This screen exists so that pressing `A` on the inbox is cheap: a visible,
 * reversible record is what lets someone move fast. It is also the only route
 * back from a dismiss, which is otherwise permanent by design.
 */
export function DonePage() {
  const [data, setData] = useState<DoneResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setData(await fetchDone());
      setError(null);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

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

        {data && data.items.length > 0 && (
          <DoneList items={data.items} timeZone={data.timezone} />
        )}

        {/* Replaced wholesale by the two real empty states in mus-41-c. */}
        {data && data.summary.total === 0 && (
          <p className="done-notice">Nothing done yet today.</p>
        )}
      </main>
    </>
  );
}
